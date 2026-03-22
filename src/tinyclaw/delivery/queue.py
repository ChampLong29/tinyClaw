"""Write-ahead log delivery queue with exponential backoff."""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BACKOFF_MS = [5_000, 25_000, 120_000, 600_000]  # [5s, 25s, 2min, 10min]
MAX_RETRIES = 5


def compute_backoff_ms(retry_count: int) -> int:
    """Exponential backoff with +/- 20% jitter."""
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]
    jitter = random.randint(-base // 5, base // 5)
    return max(0, base + jitter)


@dataclass
class QueuedDelivery:
    """A queued delivery entry with retry state."""
    id: str
    channel: str
    to: str
    text: str
    retry_count: int = 0
    last_error: str | None = None
    enqueued_at: float = field(default_factory=time.time)
    next_retry_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "channel": self.channel, "to": self.to, "text": self.text,
            "retry_count": self.retry_count, "last_error": self.last_error,
            "enqueued_at": self.enqueued_at, "next_retry_at": self.next_retry_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "QueuedDelivery":
        return QueuedDelivery(
            id=data["id"], channel=data["channel"], to=data["to"], text=data["text"],
            retry_count=data.get("retry_count", 0),
            last_error=data.get("last_error"),
            enqueued_at=data.get("enqueued_at", 0.0),
            next_retry_at=data.get("next_retry_at", 0.0),
        )


class DeliveryQueue:
    """Write-ahead log delivery queue.

    Key properties:
      - enqueue: Atomically write to disk before returning
      - ack: Delete on success
      - fail: Increment retry, schedule next attempt
      - move_to_failed: Move to failed/ dir after MAX_RETRIES
      - load_pending: Scan queue dir on startup for recovery
      - retry_failed: Move failed entries back to queue
    """

    def __init__(self, queue_dir: Path | None = None) -> None:
        self.queue_dir = queue_dir or (Path.cwd() / "workspace" / "delivery-queue")
        self.failed_dir = self.queue_dir / "failed"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self._lock = __import__("threading").Lock()

    def enqueue(self, channel: str, to: str, text: str) -> str:
        """Create entry and atomically write to disk. Returns delivery_id."""
        delivery_id = uuid.uuid4().hex[:12]
        entry = QueuedDelivery(
            id=delivery_id,
            channel=channel,
            to=to,
            text=text,
            enqueued_at=time.time(),
            next_retry_at=0.0,
        )
        self._write_entry(entry)
        return delivery_id

    def _write_entry(self, entry: QueuedDelivery) -> None:
        """Atomic write via tmp + os.replace()."""
        final_path = self.queue_dir / f"{entry.id}.json"
        tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"
        data = json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(final_path))

    def _read_entry(self, delivery_id: str) -> QueuedDelivery | None:
        path = self.queue_dir / f"{delivery_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return QueuedDelivery.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return None

    def ack(self, delivery_id: str) -> None:
        """Delete queue file on successful delivery."""
        path = self.queue_dir / f"{delivery_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def fail(self, delivery_id: str, error: str) -> None:
        """Increment retry count and schedule next attempt, or move to failed/."""
        entry = self._read_entry(delivery_id)
        if entry is None:
            return
        entry.retry_count += 1
        entry.last_error = error
        if entry.retry_count >= MAX_RETRIES:
            self.move_to_failed(delivery_id)
            return
        backoff_ms = compute_backoff_ms(entry.retry_count)
        entry.next_retry_at = time.time() + backoff_ms / 1000.0
        self._write_entry(entry)

    def move_to_failed(self, delivery_id: str) -> None:
        src = self.queue_dir / f"{delivery_id}.json"
        dst = self.failed_dir / f"{delivery_id}.json"
        try:
            os.replace(str(src), str(dst))
        except FileNotFoundError:
            pass

    def load_pending(self) -> list[QueuedDelivery]:
        """Scan queue dir on startup. Returns entries sorted by enqueued_at."""
        entries: list[QueuedDelivery] = []
        if not self.queue_dir.exists():
            return entries
        for path in self.queue_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entries.append(QueuedDelivery.from_dict(json.load(f)))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        entries.sort(key=lambda e: e.enqueued_at)
        return entries

    def load_failed(self) -> list[QueuedDelivery]:
        entries: list[QueuedDelivery] = []
        if not self.failed_dir.exists():
            return entries
        for path in self.failed_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entries.append(QueuedDelivery.from_dict(json.load(f)))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        entries.sort(key=lambda e: e.enqueued_at)
        return entries

    def retry_failed(self) -> int:
        """Move all failed/ entries back to queue, reset retry count."""
        count = 0
        if not self.failed_dir.exists():
            return count
        for path in self.failed_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                entry = QueuedDelivery.from_dict(json.load(open(path)))
                entry.retry_count = 0
                entry.last_error = None
                entry.next_retry_at = 0.0
                self._write_entry(entry)
                path.unlink()
                count += 1
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        return count
