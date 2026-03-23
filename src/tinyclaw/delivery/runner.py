"""Delivery runner: background thread that processes the delivery queue."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from tinyclaw.delivery.queue import DeliveryQueue, MAX_RETRIES, compute_backoff_ms


class DeliveryRunner:
    """Background delivery thread that processes the queue.

    Features:
      - Recovery scan on startup
      - Exponential backoff per-entry
      - Statistics tracking
    """

    def __init__(
        self,
        queue: DeliveryQueue,
        deliver_fn: Callable[[str, str, str], None],  # (channel, to, text) -> None
    ) -> None:
        self.queue = queue
        self.deliver_fn = deliver_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.total_attempted = 0
        self.total_succeeded = 0
        self.total_failed = 0

    def start(self) -> None:
        """Run recovery scan, then start the background thread."""
        self._recovery_scan()
        self._thread = threading.Thread(
            target=self._background_loop,
            daemon=True,
            name="delivery-runner",
        )
        self._thread.start()

    def _recovery_scan(self) -> None:
        """Report pending and failed entries on startup."""
        pending = self.queue.load_pending()
        failed = self.queue.load_failed()
        parts = []
        if pending:
            parts.append(f"{len(pending)} pending")
        if failed:
            parts.append(f"{len(failed)} failed")
        return  # caller prints if needed

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._process_pending()
            except Exception:
                pass
            self._stop_event.wait(timeout=1.0)

    def _process_pending(self) -> None:
        """Process all entries whose next_retry_at has passed."""
        pending = self.queue.load_pending()
        now = time.time()

        for entry in pending:
            if self._stop_event.is_set():
                break
            if entry.next_retry_at > now:
                continue

            self.total_attempted += 1
            try:
                self.deliver_fn(entry.channel, entry.to, entry.text)
                self.queue.ack(entry.id)
                self.total_succeeded += 1
            except Exception as exc:
                error_msg = str(exc)
                self.queue.fail(entry.id, error_msg)
                self.total_failed += 1

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def get_stats(self) -> dict:
        pending = self.queue.load_pending()
        failed = self.queue.load_failed()
        in_flight = max(0, self.total_attempted - len(pending) - self.total_succeeded - self.total_failed)
        return {
            "pending": len(pending),
            "in_flight": in_flight,
            "failed": len(failed),
            "total_attempted": self.total_attempted,
            "delivered": self.total_succeeded,
        }
