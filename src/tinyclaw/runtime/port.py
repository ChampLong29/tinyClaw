"""Agent Runtime Port and its platform-neutral event stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol

from tinyclaw.contracts._common import require_text


class RuntimeEventType(str, Enum):
    PROGRESS = "progress"
    CLARIFICATION = "clarification"
    CONFIRMATION = "confirmation"
    CHECKPOINT = "checkpoint"
    RESULT = "result"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, kw_only=True)
class TaskContext:
    task_id: str
    session_id: str
    user_goal: str
    task_revision: int
    trace_id: str | None = None
    checkpoint_ref: str | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.session_id, "session_id")
        require_text(self.user_goal, "user_goal")
        if self.task_revision < 1:
            raise ValueError("task_revision must be at least 1")


@dataclass(frozen=True, kw_only=True)
class RuntimeEvent:
    type: RuntimeEventType
    payload: Mapping[str, Any] = field(default_factory=dict)


class RuntimePort(Protocol):
    """Runtime implementations emit events and never call a Channel directly."""

    def start(self, context: TaskContext) -> Iterable[RuntimeEvent]:
        ...

    def resume(self, context: TaskContext, checkpoint_ref: str) -> Iterable[RuntimeEvent]:
        ...

    def apply_user_input(self, task_id: str, user_input: Mapping[str, Any]) -> None:
        ...

    def request_cancel(self, task_id: str) -> bool:
        """Return True when cancellation is pending at a non-interruptible step."""
        ...

    def snapshot(self, task_id: str) -> str | None:
        ...

    def get_status(self, task_id: str) -> Mapping[str, Any]:
        ...
