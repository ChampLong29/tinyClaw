"""Command queue: central dispatcher routing to named lanes."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from tinyclaw.concurrency.lane_queue import LaneQueue


class CommandQueue:
    """Central dispatcher routing callables to named LaneQueues.

    Lanes are created lazily on first use. reset_all() increments all
    generation counters for restart recovery.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, LaneQueue] = {}
        self._lock = threading.Lock()

    def get_or_create_lane(self, name: str, max_concurrency: int = 1) -> LaneQueue:
        """Get or create a named lane."""
        with self._lock:
            if name not in self._lanes:
                self._lanes[name] = LaneQueue(name, max_concurrency)
            return self._lanes[name]

    def enqueue(self, lane_name: str, fn: Callable[[], Any]) -> Future:
        """Route a callable to the named lane. Returns Future."""
        lane = self.get_or_create_lane(lane_name)
        return lane.enqueue(fn)

    def reset_all(self) -> dict[str, int]:
        """Increment generation on all lanes. Returns {name: new_generation}."""
        result: dict[str, int] = {}
        with self._lock:
            for name, lane in self._lanes.items():
                lane.generation = lane.generation + 1
                result[name] = lane.generation
        return result

    def wait_for_all(self, timeout: float = 10.0) -> bool:
        """Wait for all lanes to become idle."""
        deadline = time.monotonic() + timeout
        with self._lock:
            lanes = list(self._lanes.values())
        for lane in lanes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not lane.wait_for_idle(timeout=remaining):
                return False
        return True

    def stats(self) -> dict[str, dict[str, Any]]:
        """Return stats for all lanes."""
        with self._lock:
            return {name: lane.stats() for name, lane in self._lanes.items()}

    def lane_names(self) -> list[str]:
        with self._lock:
            return list(self._lanes.keys())
