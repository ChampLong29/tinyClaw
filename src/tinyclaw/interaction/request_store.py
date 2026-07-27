"""Persistent clarification and confirmation request records."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now


class RequestState(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    APPROVED = "approved"
    DENIED = "denied"
    MODIFIED = "modified"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfirmationDecision(str, Enum):
    APPROVE_ONCE = "approve_once"
    DENY = "deny"
    MODIFY = "modify"


@dataclass(frozen=True, kw_only=True)
class ClarificationRequest:
    task_id: str
    session_id: str
    global_user_id: str
    question: str
    required_fields: tuple[str, ...]
    expires_at: datetime
    default_action: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: RequestState = RequestState.OPEN
    answer: Mapping[str, Any] | None = None
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = "clarification_request.v1"

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "request_id"),
            (self.task_id, "task_id"),
            (self.session_id, "session_id"),
            (self.global_user_id, "global_user_id"),
            (self.question, "question"),
            (self.default_action, "default_action"),
        ):
            require_text(value, name)
        if not self.required_fields:
            raise ValueError("required_fields cannot be empty")
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        object.__setattr__(self, "expires_at", parse_datetime(self.expires_at))
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, kw_only=True)
class ConfirmationRequest:
    task_id: str
    session_id: str
    global_user_id: str
    action_summary: str
    action_digest: str
    risk_level: RiskLevel
    scope: Mapping[str, Any]
    expires_at: datetime
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: RequestState = RequestState.OPEN
    decision: ConfirmationDecision | None = None
    modification: Mapping[str, Any] | None = None
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = "confirmation_request.v1"

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "request_id"),
            (self.task_id, "task_id"),
            (self.session_id, "session_id"),
            (self.global_user_id, "global_user_id"),
            (self.action_summary, "action_summary"),
            (self.action_digest, "action_digest"),
        ):
            require_text(value, name)
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        object.__setattr__(self, "expires_at", parse_datetime(self.expires_at))
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


InteractionRequest = ClarificationRequest | ConfirmationRequest


class RequestStoreError(RuntimeError):
    pass


class RequestNotFoundError(RequestStoreError):
    pass


class OpenRequestExistsError(RequestStoreError):
    pass


class RequestRevisionConflictError(RequestStoreError):
    pass


class SQLiteRequestStore:
    """JSON-payload SQLite store with indexed authorization fields."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or (Path.cwd() / "workspace" / "interaction.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS interaction_requests (
                request_id TEXT PRIMARY KEY,
                request_type TEXT NOT NULL,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                global_user_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                expires_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_requests_task
                ON interaction_requests(task_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_request_per_task
                ON interaction_requests(task_id) WHERE state = 'open';
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(self, request: InteractionRequest) -> None:
        request_type = self._request_type(request)
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO interaction_requests (
                        request_id, request_type, task_id, session_id,
                        global_user_id, state, revision, expires_at,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._values(request, request_type),
                )
            except sqlite3.IntegrityError as exc:
                if "task_id" in str(exc):
                    raise OpenRequestExistsError(
                        f"task {request.task_id!r} already has an open request"
                    ) from exc
                raise RequestStoreError(str(exc)) from exc

    def get(self, request_id: str) -> InteractionRequest:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM interaction_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise RequestNotFoundError(request_id)
        return self._from_row(row)

    def update(
        self,
        request: InteractionRequest,
        *,
        expected_revision: int,
    ) -> InteractionRequest:
        if request.revision != expected_revision + 1:
            raise ValueError("updated request revision must equal expected_revision + 1")
        request_type = self._request_type(request)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE interaction_requests
                SET state = ?, revision = ?, expires_at = ?, payload_json = ?,
                    updated_at = ?
                WHERE request_id = ? AND request_type = ? AND revision = ?
                """,
                (
                    request.state.value,
                    request.revision,
                    request.expires_at.isoformat(),
                    json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True),
                    request.updated_at.isoformat(),
                    request.request_id,
                    request_type,
                    expected_revision,
                ),
            )
        if cursor.rowcount != 1:
            raise RequestRevisionConflictError(
                f"request {request.request_id!r} revision changed"
            )
        return request

    def resolve(
        self,
        request: InteractionRequest,
        *,
        state: RequestState,
        expected_revision: int,
        **changes: Any,
    ) -> InteractionRequest:
        updated = replace(
            request,
            state=state,
            revision=expected_revision + 1,
            updated_at=utc_now(),
            **changes,
        )
        return self.update(updated, expected_revision=expected_revision)

    @staticmethod
    def _request_type(request: InteractionRequest) -> str:
        if isinstance(request, ClarificationRequest):
            return "clarification"
        return "confirmation"

    @staticmethod
    def _values(request: InteractionRequest, request_type: str) -> tuple[object, ...]:
        return (
            request.request_id,
            request_type,
            request.task_id,
            request.session_id,
            request.global_user_id,
            request.state.value,
            request.revision,
            request.expires_at.isoformat(),
            json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True),
            request.created_at.isoformat(),
            request.updated_at.isoformat(),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> InteractionRequest:
        data = json.loads(row["payload_json"])
        if row["request_type"] == "clarification":
            return ClarificationRequest(
                request_id=data["request_id"],
                task_id=data["task_id"],
                session_id=data["session_id"],
                global_user_id=data["global_user_id"],
                question=data["question"],
                required_fields=tuple(data["required_fields"]),
                expires_at=data["expires_at"],
                default_action=data["default_action"],
                state=RequestState(data["state"]),
                answer=data.get("answer"),
                revision=data["revision"],
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                schema_version=data["schema_version"],
            )
        return ConfirmationRequest(
            request_id=data["request_id"],
            task_id=data["task_id"],
            session_id=data["session_id"],
            global_user_id=data["global_user_id"],
            action_summary=data["action_summary"],
            action_digest=data["action_digest"],
            risk_level=RiskLevel(data["risk_level"]),
            scope=data["scope"],
            expires_at=data["expires_at"],
            state=RequestState(data["state"]),
            decision=ConfirmationDecision(data["decision"]) if data.get("decision") else None,
            modification=data.get("modification"),
            revision=data["revision"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            schema_version=data["schema_version"],
        )
