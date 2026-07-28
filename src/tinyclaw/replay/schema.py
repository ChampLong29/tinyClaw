"""Versioned replay case, observation, and report schemas."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, to_primitive, utc_now

REPLAY_CASE_SCHEMA = "replay_case.v1"
REPLAY_REPORT_SCHEMA = "replay_report.v1"
DEFAULT_EVALUATORS = (
    "route",
    "state",
    "interaction",
    "completion",
    "delivery",
    "rendering",
    "notification",
    "cost",
)


@dataclass(frozen=True, kw_only=True)
class ReplayCase:
    inbound: Mapping[str, Any]
    case_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    identity_policy: Mapping[str, Any] = field(default_factory=dict)
    versions: Mapping[str, str] = field(default_factory=dict)
    tool_recordings: Mapping[str, Any] = field(default_factory=dict)
    channel_capability: Mapping[str, Any] = field(default_factory=dict)
    expected: Mapping[str, Any] = field(default_factory=dict)
    evaluators: tuple[str, ...] = DEFAULT_EVALUATORS
    source_trace_refs: tuple[str, ...] = ()
    recorded_observation: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = REPLAY_CASE_SCHEMA

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("replay case_id cannot be empty")
        if not self.inbound:
            raise ValueError("replay case requires normalized inbound data")
        if self.schema_version != REPLAY_CASE_SCHEMA:
            raise ValueError(f"unsupported replay case schema: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    def save(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path | str) -> "ReplayCase":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            case_id=raw["case_id"],
            inbound=raw["inbound"],
            identity_policy=raw.get("identity_policy") or {},
            versions=raw.get("versions") or {},
            tool_recordings=raw.get("tool_recordings") or {},
            channel_capability=raw.get("channel_capability") or {},
            expected=raw.get("expected") or {},
            evaluators=(tuple(raw["evaluators"]) if "evaluators" in raw else DEFAULT_EVALUATORS),
            source_trace_refs=tuple(raw.get("source_trace_refs") or ()),
            recorded_observation=raw.get("recorded_observation") or {},
            schema_version=raw.get("schema_version") or REPLAY_CASE_SCHEMA,
        )


@dataclass(frozen=True, kw_only=True)
class ReplayObservation:
    route: Mapping[str, Any] = field(default_factory=dict)
    states: tuple[str, ...] = ()
    invalid_transitions: tuple[str, ...] = ()
    clarification_requested: bool = False
    confirmation_requested: bool = False
    task_completed: bool = False
    tool_results: tuple[Mapping[str, Any], ...] = ()
    deliveries: tuple[Mapping[str, Any], ...] = ()
    rendered_messages: tuple[Mapping[str, Any], ...] = ()
    notifications: tuple[Mapping[str, Any], ...] = ()
    metrics: Mapping[str, float | int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReplayObservation":
        return cls(
            route=raw.get("route") or {},
            states=tuple(raw.get("states") or ()),
            invalid_transitions=tuple(raw.get("invalid_transitions") or ()),
            clarification_requested=bool(raw.get("clarification_requested", False)),
            confirmation_requested=bool(raw.get("confirmation_requested", False)),
            task_completed=bool(raw.get("task_completed", False)),
            tool_results=tuple(raw.get("tool_results") or ()),
            deliveries=tuple(raw.get("deliveries") or ()),
            rendered_messages=tuple(raw.get("rendered_messages") or ()),
            notifications=tuple(raw.get("notifications") or ()),
            metrics=raw.get("metrics") or {},
        )


@dataclass(frozen=True, kw_only=True)
class EvaluationOutcome:
    evaluator: str
    passed: bool
    score: float
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ReplayReport:
    case_id: str
    mode: str
    outcomes: tuple[EvaluationOutcome, ...]
    observation: ReplayObservation
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    generated_at: datetime = field(default_factory=utc_now)
    schema_version: str = REPLAY_REPORT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            parse_datetime(self.generated_at, default_now=True),
        )

    @property
    def passed(self) -> bool:
        return all(outcome.passed for outcome in self.outcomes)

    @property
    def score(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(outcome.score for outcome in self.outcomes) / len(self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        value = to_primitive(self)
        value["passed"] = self.passed
        value["score"] = self.score
        return value
