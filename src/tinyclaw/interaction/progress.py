"""Structured user-visible progress with deterministic coalescing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, utc_now


class ProgressEventType(str, Enum):
    PHASE_STARTED = "phase_started"
    STEP_PROGRESS = "step_progress"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    WAITING_USER = "waiting_user"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class ProgressEvent:
    task_id: str
    type: ProgressEventType
    phase: str
    message: str
    occurred_at: datetime = field(default_factory=utc_now)
    completed_units: int | None = None
    total_units: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.phase, "phase")
        require_text(self.message, "message")
        object.__setattr__(self, "occurred_at", parse_datetime(self.occurred_at, default_now=True))
        if self.completed_units is not None and self.completed_units < 0:
            raise ValueError("completed_units cannot be negative")
        if self.total_units is not None and self.total_units <= 0:
            raise ValueError("total_units must be positive")
        if (
            self.completed_units is not None
            and self.total_units is not None
            and self.completed_units > self.total_units
        ):
            raise ValueError("completed_units cannot exceed total_units")

    @property
    def fraction(self) -> float | None:
        if self.completed_units is None or self.total_units is None:
            return None
        return self.completed_units / self.total_units


@dataclass
class _ProgressCursor:
    phase: str
    emitted_at: datetime
    fraction: float | None


class ProgressCoalescer:
    _ALWAYS_EMIT = frozenset(
        {
            ProgressEventType.PHASE_STARTED,
            ProgressEventType.WAITING_USER,
            ProgressEventType.COMPLETED,
            ProgressEventType.FAILED,
        }
    )

    def __init__(
        self,
        *,
        minimum_interval: timedelta = timedelta(seconds=5),
        minimum_fraction_delta: float = 0.1,
    ) -> None:
        if minimum_interval.total_seconds() < 0:
            raise ValueError("minimum_interval cannot be negative")
        if not 0 <= minimum_fraction_delta <= 1:
            raise ValueError("minimum_fraction_delta must be between 0 and 1")
        self.minimum_interval = minimum_interval
        self.minimum_fraction_delta = minimum_fraction_delta
        self._cursors: dict[str, _ProgressCursor] = {}

    def should_emit(self, event: ProgressEvent) -> bool:
        cursor = self._cursors.get(event.task_id)
        phase_changed = cursor is not None and cursor.phase != event.phase
        first = cursor is None
        elapsed = (
            event.occurred_at - cursor.emitted_at
            if cursor is not None
            else self.minimum_interval
        )
        fraction_changed = (
            event.fraction is not None
            and cursor is not None
            and cursor.fraction is not None
            and event.fraction - cursor.fraction >= self.minimum_fraction_delta
        )
        emit = (
            first
            or phase_changed
            or event.type in self._ALWAYS_EMIT
            or elapsed >= self.minimum_interval
            or fraction_changed
        )
        if emit:
            self._cursors[event.task_id] = _ProgressCursor(
                phase=event.phase,
                emitted_at=event.occurred_at,
                fraction=event.fraction,
            )
        return emit

    def clear(self, task_id: str) -> None:
        self._cursors.pop(task_id, None)
