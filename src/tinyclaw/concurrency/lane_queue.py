"""Named lane queue: FIFO with concurrency control."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import Any, Callable


class LaneQueue:
    """A named FIFO queue with max_concurrency parallelism.

    Tasks are callables enqueued via enqueue(). Each task runs in its own
    thread. Results are delivered via concurrent.futures.Future.

    The generation counter supports restart recovery: when generation is
    incremented, stale tasks from the previous generation complete but
    won't re-pump the queue.
    """

    def __init__(self, name: str, max_concurrency: int = 1) -> None:
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self._deque: deque[tuple[Callable, Future, int]] = deque()
        self._condition = threading.Condition()
        self._active_count = 0
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    @generation.setter
    def generation(self, value: int) -> None:
        with self._condition:
            self._generation = value
            self._condition.notify_all()

    def enqueue(
        self, fn: Callable[[], Any], generation: int | None = None,
    ) -> Future:
        """Enqueue a callable. Returns a Future with the result."""
        future: Future = Future()
        with self._condition:
            gen = generation if generation is not None else self._generation
            self._deque.append((fn, future, gen))
            self._pump()
        return future

    def _pump(self) -> None:
        """Start tasks from the deque until active >= max_concurrency."""
        while self._active_count < self.max_concurrency and self._deque:
            fn, future, gen = self._deque.popleft()
            self._active_count += 1
            t = threading.Thread(
                target=self._run_task,
                args=(fn, future, gen),
                daemon=True,
                name=f"lane-{self.name}",
            )
            t.start()

    def _run_task(
        self,
        fn: Callable[[], Any],
        future: Future,
        gen: int,
    ) -> None:
        try:
            result = fn()
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self._task_done(gen)

    def _task_done(self, gen: int) -> None:
        with self._condition:
            self._active_count -= 1
            if gen == self._generation:
                self._pump()
            self._condition.notify_all()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until active_count == 0 and deque is empty. Returns True on success."""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        with self._condition:
            while self._active_count > 0 or len(self._deque) > 0:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._condition.wait(timeout=remaining)
            return True

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "name": self.name,
                "queue_depth": len(self._deque),
                "active": self._active_count,
                "max_concurrency": self.max_concurrency,
                "generation": self._generation,
            }
