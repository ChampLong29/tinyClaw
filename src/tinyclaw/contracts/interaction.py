"""Persistent task and interaction event contracts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now
from tinyclaw.contracts.versions import INTERACTION_EVENT_V1, TASK_INSTANCE_V1


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_TOOL = "waiting_tool"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    RECOVERY_REQUIRED = "recovery_required"


class InteractionEventType(str, Enum):
    TASK_CREATED = "task_created"
    STATE_CHANGED = "state_changed"
    PROGRESS = "progress"
    CLARIFICATION_OPENED = "clarification_opened"
    CLARIFICATION_ANSWERED = "clarification_answered"
    CONFIRMATION_OPENED = "confirmation_opened"
    CONFIRMATION_DECIDED = "confirmation_decided"
    CONTROL_RECEIVED = "control_received"
    CONTROL_APPLIED = "control_applied"
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    OUTBOUND_CREATED = "outbound_created"
    DELIVERY_ATTEMPTED = "delivery_attempted"
    DELIVERY_ACKED = "delivery_acked"
    DELIVERY_FAILED = "delivery_failed"
    FEEDBACK_RECEIVED = "feedback_received"


@dataclass(frozen=True, kw_only=True)
class CancellationToken:
    requested_at: datetime | None = None
    requested_by: str | None = None
    reason: str | None = None
    pending: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_at", parse_datetime(self.requested_at))


@dataclass(frozen=True, kw_only=True)
class FailureInfo:
    code: str
    message: str
    retryable: bool = False
    details_ref: str | None = None

    def __post_init__(self) -> None:
        require_text(self.code, "failure.code")
        require_text(self.message, "failure.message")


@dataclass(frozen=True, kw_only=True)
class TaskInstance:
    task_id: str
    session_id: str
    state: TaskState
    user_goal: str
    revision: int = 1
    runtime_ref: str | None = None
    checkpoint_ref: str | None = None
    pending_request_ref: str | None = None
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    result_ref: str | None = None
    failure: FailureInfo | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = TASK_INSTANCE_V1

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.session_id, "session_id")
        require_text(self.user_goal, "user_goal")
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        if self.schema_version != TASK_INSTANCE_V1:
            raise ValueError(f"unsupported task schema: {self.schema_version}")
        if self.state == TaskState.COMPLETED and not self.result_ref:
            raise ValueError("completed task requires result_ref")
        if self.state == TaskState.FAILED and self.failure is None:
            raise ValueError("failed task requires failure")
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, kw_only=True)
class InteractionEvent:
    session_id: str
    event_type: InteractionEventType
    actor: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    schema_version: str = INTERACTION_EVENT_V1

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.session_id, "session_id"),
            (self.actor, "actor"),
        ):
            require_text(value, name)
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if self.schema_version != INTERACTION_EVENT_V1:
            raise ValueError(f"unsupported interaction event schema: {self.schema_version}")
        object.__setattr__(self, "occurred_at", parse_datetime(self.occurred_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)
