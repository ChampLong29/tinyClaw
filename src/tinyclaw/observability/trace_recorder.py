"""Append-only partitioned interaction trace recorder."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts import TraceContext, TraceEvent
from tinyclaw.contracts._common import parse_datetime, to_primitive, utc_now
from tinyclaw.observability.artifacts import ArtifactStore
from tinyclaw.observability.redaction import Redactor

ANNOTATION_SCHEMA = "trace_annotation.v1"
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, kw_only=True)
class TraceAnnotation:
    trace_event_id: str
    revision: int
    labels: tuple[str, ...] = ()
    note: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    annotated_at: datetime = field(default_factory=utc_now)
    schema_version: str = ANNOTATION_SCHEMA

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("annotation revision must be positive")
        object.__setattr__(
            self,
            "annotated_at",
            parse_datetime(self.annotated_at, default_now=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


class TraceRecorder:
    def __init__(
        self,
        root: Path | str,
        *,
        artifact_threshold_bytes: int = 16_384,
        redactor: Redactor | None = None,
    ) -> None:
        if artifact_threshold_bytes < 1:
            raise ValueError("artifact_threshold_bytes must be positive")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifact_threshold_bytes = artifact_threshold_bytes
        self.redactor = redactor or Redactor()
        self.artifacts = ArtifactStore(
            self.root / "artifacts",
            redactor=self.redactor,
        )
        self._lock = threading.RLock()
        self._next_sequences: dict[tuple[str, str], int] = {}
        self._annotation_revisions: dict[tuple[Path, str], int] = {}

    def record(
        self,
        *,
        event_type: str,
        producer: str,
        payload: Mapping[str, Any] | None = None,
        session_id: str,
        task_id: str | None = None,
        trace_context: TraceContext | None = None,
        interaction_event_id: str | None = None,
        artifact_refs: tuple[str, ...] = (),
        producer_version: str = "unknown",
        occurred_at: datetime | str | None = None,
    ) -> TraceEvent:
        partition_task = task_id or "_session"
        partition = (session_id, partition_task)
        path = self._events_path(session_id, task_id)
        with self._lock:
            sequence = self._next_sequences.get(partition)
            if sequence is None:
                sequence = self._load_next_sequence(path)
            prepared_payload, generated_refs = self._prepare_payload(payload or {})
            context = trace_context or TraceContext(
                trace_id=uuid.uuid4().hex,
                span_id=uuid.uuid4().hex[:16],
            )
            event = TraceEvent(
                trace_context=context,
                event_type=event_type,
                producer=producer,
                sequence=sequence,
                payload=prepared_payload,
                session_id=session_id,
                task_id=task_id,
                interaction_event_id=interaction_event_id,
                artifact_refs=tuple((*artifact_refs, *generated_refs)),
                occurred_at=parse_datetime(occurred_at, default_now=True),
                producer_version=producer_version,
            )
            self._append_json_line(path, event.to_dict())
            self._next_sequences[partition] = sequence + 1
            return event

    def annotate(
        self,
        *,
        session_id: str,
        task_id: str | None,
        trace_event_id: str,
        labels: tuple[str, ...] = (),
        note: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceAnnotation:
        path = self._partition_dir(session_id, task_id) / "annotations.jsonl"
        key = (path, trace_event_id)
        with self._lock:
            revision = self._annotation_revisions.get(key)
            if revision is None:
                revision = self._load_annotation_revision(path, trace_event_id)
            annotation = TraceAnnotation(
                trace_event_id=trace_event_id,
                revision=revision + 1,
                labels=labels,
                note=self.redactor.redact(note),
                metadata=self.redactor.redact(metadata or {}),
            )
            self._append_json_line(path, annotation.to_dict())
            self._annotation_revisions[key] = annotation.revision
            return annotation

    def read_events(
        self,
        *,
        session_id: str,
        task_id: str | None = None,
    ) -> list[TraceEvent]:
        path = self._events_path(session_id, task_id)
        if not path.exists():
            return []
        events: list[TraceEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            context = raw["trace_context"]
            events.append(
                TraceEvent(
                    trace_event_id=raw["trace_event_id"],
                    trace_context=TraceContext(
                        trace_id=context["trace_id"],
                        span_id=context["span_id"],
                        parent_span_id=context.get("parent_span_id"),
                    ),
                    event_type=raw["event_type"],
                    producer=raw["producer"],
                    sequence=int(raw["sequence"]),
                    payload=raw.get("payload") or {},
                    session_id=raw.get("session_id"),
                    task_id=raw.get("task_id"),
                    interaction_event_id=raw.get("interaction_event_id"),
                    artifact_refs=tuple(raw.get("artifact_refs") or ()),
                    occurred_at=raw["occurred_at"],
                    producer_version=raw.get("producer_version") or "unknown",
                    annotation_revision=int(raw.get("annotation_revision") or 0),
                    schema_version=raw["schema_version"],
                )
            )
        return events

    def write_timeline(
        self,
        *,
        session_id: str,
        task_id: str | None = None,
    ) -> Path:
        events = self.read_events(session_id=session_id, task_id=task_id)
        path = self._partition_dir(session_id, task_id) / "timeline.md"
        lines = ["# Interaction Timeline", ""]
        for event in events:
            lines.append(
                f"- `{event.sequence:04d}` {event.occurred_at.isoformat()} "
                f"**{event.event_type}** · {event.producer}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _prepare_payload(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        redacted = self.redactor.redact(payload)
        encoded = json.dumps(
            redacted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= self.artifact_threshold_bytes:
            return redacted, ()
        artifact = self.artifacts.put_bytes(
            encoded,
            mime_type="application/json",
        )
        return (
            {
                "artifact_ref": artifact.artifact_ref,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            },
            (artifact.artifact_ref,),
        )

    def _events_path(self, session_id: str, task_id: str | None) -> Path:
        return self._partition_dir(session_id, task_id) / "events.jsonl"

    def _partition_dir(self, session_id: str, task_id: str | None) -> Path:
        session = self._safe_component(session_id)
        task = self._safe_component(task_id or "_session")
        path = self.root / "traces" / session / task
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe_component(value: str) -> str:
        sanitized = _SAFE_COMPONENT_RE.sub("_", value).strip("._")
        if not sanitized:
            raise ValueError("trace partition cannot be empty")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        return f"{sanitized[:100]}-{digest}"

    @staticmethod
    def _load_next_sequence(path: Path) -> int:
        if not path.exists():
            return 0
        last_sequence = -1
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last_sequence = max(last_sequence, int(json.loads(line)["sequence"]))
        return last_sequence + 1

    @staticmethod
    def _load_annotation_revision(path: Path, trace_event_id: str) -> int:
        if not path.exists():
            return 0
        revision = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw["trace_event_id"] == trace_event_id:
                revision = max(revision, int(raw["revision"]))
        return revision

    @staticmethod
    def _append_json_line(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
