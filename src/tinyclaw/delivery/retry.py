"""Retry timing policy for durable delivery workers."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class DeliveryRetryPolicy:
    delays_seconds: tuple[float, ...] = (5.0, 25.0, 120.0, 600.0)
    jitter_ratio: float = 0.2
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not self.delays_seconds or any(delay < 0 for delay in self.delays_seconds):
            raise ValueError("delays_seconds must contain non-negative values")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def delay(self, attempt_count: int) -> timedelta:
        index = min(max(attempt_count - 1, 0), len(self.delays_seconds) - 1)
        base = self.delays_seconds[index]
        jitter = base * self.jitter_ratio
        seconds = max(0.0, base + random.uniform(-jitter, jitter))
        return timedelta(seconds=seconds)
