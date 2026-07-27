"""Append-only interaction trace schema."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now
from tinyclaw.contracts.versions import INTERACTION_TRACE_V1


@dataclass(frozen=True, kw_only=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.trace_id, "trace_id")
        require_text(self.span_id, "span_id")


@dataclass(frozen=True, kw_only=True)
class TraceEvent:
    trace_context: TraceContext
    event_type: str
    producer: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    trace_event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    task_id: str | None = None
    interaction_event_id: str | None = None
    artifact_refs: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=utc_now)
    producer_version: str = "unknown"
    annotation_revision: int = 0
    schema_version: str = INTERACTION_TRACE_V1

    def __post_init__(self) -> None:
        for value, name in (
            (self.trace_event_id, "trace_event_id"),
            (self.event_type, "event_type"),
            (self.producer, "producer"),
        ):
            require_text(value, name)
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if self.annotation_revision < 0:
            raise ValueError("annotation_revision cannot be negative")
        if self.schema_version != INTERACTION_TRACE_V1:
            raise ValueError(f"unsupported trace schema: {self.schema_version}")
        object.__setattr__(self, "occurred_at", parse_datetime(self.occurred_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)
