"""Persistent user feedback and explainable bad-case classification."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from tinyclaw.contracts import TraceEvent
from tinyclaw.contracts._common import parse_datetime, to_primitive, utc_now
from tinyclaw.observability.redaction import Redactor

FEEDBACK_SCHEMA = "user_feedback.v1"
BAD_CASE_SCHEMA = "bad_case.v1"


class FeedbackSource(str, Enum):
    UPVOTE = "upvote"
    DOWNVOTE = "downvote"
    CORRECTION = "correction"
    CANCEL = "cancel"
    RETRY = "retry"
    HUMAN_TAKEOVER = "human_takeover"
    REPEATED_REQUEST = "repeated_request"
    TOOL_FAILURE = "tool_failure"
    DELIVERY_FAILURE = "delivery_failure"


class BadCaseCategory(str, Enum):
    WRONG_ROUTE = "wrong_route"
    SESSION_LEAK = "session_leak"
    CONTEXT_LOSS = "context_loss"
    MISSING_CLARIFICATION = "missing_clarification"
    UNSAFE_ACTION = "unsafe_action"
    TOOL_FAILURE = "tool_failure"
    RECOVERY_FAILURE = "recovery_failure"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    OUT_OF_ORDER = "out_of_order"
    CHANNEL_RENDERING = "channel_rendering"
    NOTIFICATION_NOISE = "notification_noise"
    POOR_FINAL_ANSWER = "poor_final_answer"


@dataclass(frozen=True, kw_only=True)
class FeedbackRecord:
    session_id: str
    source: FeedbackSource
    feedback_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str | None = None
    trace_event_id: str | None = None
    rating: int | None = None
    text: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = FEEDBACK_SCHEMA

    def __post_init__(self) -> None:
        if self.rating is not None and self.rating not in (-1, 0, 1):
            raise ValueError("feedback rating must be -1, 0, or 1")
        object.__setattr__(
            self,
            "created_at",
            parse_datetime(self.created_at, default_now=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, kw_only=True)
class BadCaseRecord:
    feedback_id: str
    session_id: str
    category: BadCaseCategory
    confidence: float
    reason: str
    bad_case_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str | None = None
    trace_event_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = BAD_CASE_SCHEMA

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("bad-case confidence must be between 0 and 1")
        object.__setattr__(
            self,
            "created_at",
            parse_datetime(self.created_at, default_now=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, kw_only=True)
class BadCaseRevision:
    bad_case_id: str
    revision: int
    category: BadCaseCategory
    confidence: float
    reason: str
    reviewer: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("bad-case revision must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError("bad-case confidence must be between 0 and 1")
        object.__setattr__(
            self,
            "created_at",
            parse_datetime(self.created_at, default_now=True),
        )


class BadCaseClassifier:
    def classify(
        self,
        feedback: FeedbackRecord,
        *,
        trace_events: Iterable[TraceEvent] = (),
    ) -> BadCaseRecord | None:
        events = tuple(trace_events)
        explicit = feedback.metadata.get("bad_case_category")
        if explicit:
            try:
                category = BadCaseCategory(str(explicit))
            except ValueError:
                category = None
            if category is not None:
                return self._record(
                    feedback,
                    category=category,
                    confidence=1.0,
                    reason="explicit feedback category",
                    events=events,
                )

        event_types = {event.event_type.lower() for event in events}
        if feedback.source == FeedbackSource.TOOL_FAILURE or any(
            "tool" in event_type and "fail" in event_type for event_type in event_types
        ):
            return self._record(
                feedback,
                category=BadCaseCategory.TOOL_FAILURE,
                confidence=0.95,
                reason="tool failure feedback or trace event",
                events=events,
            )
        if feedback.source == FeedbackSource.DELIVERY_FAILURE:
            failure = str(feedback.metadata.get("failure_kind") or "").lower()
            if "duplicate" in failure:
                category = BadCaseCategory.DUPLICATE_DELIVERY
            elif "order" in failure:
                category = BadCaseCategory.OUT_OF_ORDER
            elif "render" in failure:
                category = BadCaseCategory.CHANNEL_RENDERING
            else:
                category = BadCaseCategory.RECOVERY_FAILURE
            return self._record(
                feedback,
                category=category,
                confidence=0.9,
                reason=f"delivery failure: {failure or 'unspecified'}",
                events=events,
            )
        if feedback.source == FeedbackSource.REPEATED_REQUEST:
            return self._record(
                feedback,
                category=BadCaseCategory.CONTEXT_LOSS,
                confidence=0.75,
                reason="same request repeated within feedback window",
                events=events,
            )
        if feedback.source in (
            FeedbackSource.DOWNVOTE,
            FeedbackSource.CORRECTION,
        ):
            return self._record(
                feedback,
                category=BadCaseCategory.POOR_FINAL_ANSWER,
                confidence=0.7,
                reason=f"negative user feedback: {feedback.source.value}",
                events=events,
            )
        return None

    @staticmethod
    def _record(
        feedback: FeedbackRecord,
        *,
        category: BadCaseCategory,
        confidence: float,
        reason: str,
        events: tuple[TraceEvent, ...],
    ) -> BadCaseRecord:
        return BadCaseRecord(
            feedback_id=feedback.feedback_id,
            session_id=feedback.session_id,
            task_id=feedback.task_id,
            category=category,
            confidence=confidence,
            reason=reason,
            trace_event_ids=tuple(event.trace_event_id for event in events),
        )


class SQLiteFeedbackStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_feedback (
                feedback_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT,
                trace_event_id TEXT,
                source TEXT NOT NULL,
                rating INTEGER,
                text TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bad_cases (
                bad_case_id TEXT PRIMARY KEY,
                feedback_id TEXT NOT NULL UNIQUE
                    REFERENCES user_feedback(feedback_id),
                session_id TEXT NOT NULL,
                task_id TEXT,
                category TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                trace_event_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bad_cases_category
                ON bad_cases(category, created_at);

            CREATE TABLE IF NOT EXISTS bad_case_revisions (
                bad_case_id TEXT NOT NULL REFERENCES bad_cases(bad_case_id),
                revision INTEGER NOT NULL,
                category TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(bad_case_id, revision)
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def add(
        self,
        feedback: FeedbackRecord,
        *,
        classifier: BadCaseClassifier | None = None,
        trace_events: Iterable[TraceEvent] = (),
    ) -> BadCaseRecord | None:
        redacted_metadata = self.redactor.redact(feedback.metadata)
        redacted_text = self.redactor.redact(feedback.text)
        bad_case = (classifier or BadCaseClassifier()).classify(
            feedback,
            trace_events=trace_events,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO user_feedback(
                        feedback_id, session_id, task_id, trace_event_id,
                        source, rating, text, metadata_json, created_at,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback.feedback_id,
                        feedback.session_id,
                        feedback.task_id,
                        feedback.trace_event_id,
                        feedback.source.value,
                        feedback.rating,
                        redacted_text,
                        json.dumps(
                            redacted_metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        feedback.created_at.isoformat(),
                        feedback.schema_version,
                    ),
                )
                if bad_case is not None:
                    self._insert_bad_case(bad_case)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return bad_case

    def list_bad_cases(
        self,
        *,
        category: BadCaseCategory | None = None,
    ) -> list[BadCaseRecord]:
        query = "SELECT * FROM bad_cases"
        values: tuple[str, ...] = ()
        if category is not None:
            query += " WHERE category = ?"
            values = (category.value,)
        query += " ORDER BY created_at, bad_case_id"
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return [
            BadCaseRecord(
                bad_case_id=row["bad_case_id"],
                feedback_id=row["feedback_id"],
                session_id=row["session_id"],
                task_id=row["task_id"],
                category=BadCaseCategory(row["category"]),
                confidence=float(row["confidence"]),
                reason=row["reason"],
                trace_event_ids=tuple(json.loads(row["trace_event_ids_json"])),
                created_at=row["created_at"],
                schema_version=row["schema_version"],
            )
            for row in rows
        ]

    def revise_bad_case(
        self,
        bad_case_id: str,
        *,
        category: BadCaseCategory,
        confidence: float,
        reason: str,
        reviewer: str,
    ) -> BadCaseRevision:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                exists = self._connection.execute(
                    "SELECT 1 FROM bad_cases WHERE bad_case_id = ?",
                    (bad_case_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(bad_case_id)
                row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) AS revision
                    FROM bad_case_revisions WHERE bad_case_id = ?
                    """,
                    (bad_case_id,),
                ).fetchone()
                revision = BadCaseRevision(
                    bad_case_id=bad_case_id,
                    revision=int(row["revision"]) + 1,
                    category=category,
                    confidence=confidence,
                    reason=reason,
                    reviewer=reviewer,
                )
                self._connection.execute(
                    """
                    INSERT INTO bad_case_revisions(
                        bad_case_id, revision, category, confidence,
                        reason, reviewer, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision.bad_case_id,
                        revision.revision,
                        revision.category.value,
                        revision.confidence,
                        revision.reason,
                        revision.reviewer,
                        revision.created_at.isoformat(),
                    ),
                )
                self._connection.commit()
                return revision
            except Exception:
                self._connection.rollback()
                raise

    def list_bad_case_revisions(
        self,
        bad_case_id: str,
    ) -> list[BadCaseRevision]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM bad_case_revisions
                WHERE bad_case_id = ? ORDER BY revision
                """,
                (bad_case_id,),
            ).fetchall()
        return [
            BadCaseRevision(
                bad_case_id=row["bad_case_id"],
                revision=int(row["revision"]),
                category=BadCaseCategory(row["category"]),
                confidence=float(row["confidence"]),
                reason=row["reason"],
                reviewer=row["reviewer"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _insert_bad_case(self, bad_case: BadCaseRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO bad_cases(
                bad_case_id, feedback_id, session_id, task_id,
                category, confidence, reason,
                trace_event_ids_json, created_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bad_case.bad_case_id,
                bad_case.feedback_id,
                bad_case.session_id,
                bad_case.task_id,
                bad_case.category.value,
                bad_case.confidence,
                bad_case.reason,
                json.dumps(bad_case.trace_event_ids),
                bad_case.created_at.isoformat(),
                bad_case.schema_version,
            ),
        )
