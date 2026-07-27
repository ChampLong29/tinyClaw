"""SQLite persistence for task instances and their append-only event stream."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from tinyclaw.contracts._common import to_primitive
from tinyclaw.contracts.interaction import (
    CancellationToken,
    FailureInfo,
    InteractionEvent,
    InteractionEventType,
    TaskInstance,
    TaskState,
)


class TaskStoreError(RuntimeError):
    """Base error for persistent task operations."""


class TaskNotFoundError(TaskStoreError):
    pass


class TaskRevisionConflictError(TaskStoreError):
    pass


class ActiveTaskExistsError(TaskStoreError):
    pass


_ACTIVE_STATES = tuple(
    state.value
    for state in (
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.WAITING_USER,
        TaskState.WAITING_CONFIRMATION,
        TaskState.WAITING_TOOL,
        TaskState.PAUSED,
        TaskState.RECOVERY_REQUIRED,
    )
)


class SQLiteTaskStore:
    """Transactional task store.

    A state update and its ``state_changed`` event are committed in the same
    SQLite transaction. The task revision is checked with compare-and-set.
    """

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
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        active_states = ", ".join(f"'{state}'" for state in _ACTIVE_STATES)
        with self._lock:
            self._connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    state TEXT NOT NULL,
                    user_goal TEXT NOT NULL,
                    runtime_ref TEXT,
                    checkpoint_ref TEXT,
                    pending_request_ref TEXT,
                    cancellation_token_json TEXT NOT NULL,
                    result_ref TEXT,
                    failure_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interaction_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    trace_id TEXT,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_session
                    ON tasks(session_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_events_session
                    ON interaction_events(session_id, occurred_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_task_per_session
                    ON tasks(session_id)
                    WHERE state IN ({active_states});
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteTaskStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_task(
        self,
        task: TaskInstance,
        *,
        actor: str,
        trace_id: str | None = None,
    ) -> InteractionEvent:
        event = InteractionEvent(
            session_id=task.session_id,
            task_id=task.task_id,
            event_type=InteractionEventType.TASK_CREATED,
            actor=actor,
            sequence=0,
            trace_id=trace_id,
            payload={
                "state": task.state.value,
                "revision": task.revision,
                "user_goal": task.user_goal,
            },
        )
        with self._lock:
            self._begin()
            try:
                self._insert_task(task)
                self._insert_event(event)
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                if "tasks.session_id" in str(exc):
                    raise ActiveTaskExistsError(
                        f"session {task.session_id!r} already has an active task"
                    ) from exc
                raise TaskStoreError(str(exc)) from exc
            except Exception:
                self._connection.rollback()
                raise
        return event

    def compare_and_set(
        self,
        task: TaskInstance,
        *,
        expected_revision: int,
        previous_state: TaskState,
        actor: str,
        reason: str,
        trace_id: str | None = None,
        event_type: InteractionEventType = InteractionEventType.STATE_CHANGED,
        event_payload: dict[str, object] | None = None,
    ) -> InteractionEvent:
        """Persist a state transition and append its event atomically."""
        if task.revision != expected_revision + 1:
            raise ValueError("updated task revision must equal expected_revision + 1")

        with self._lock:
            self._begin()
            try:
                current = self._connection.execute(
                    "SELECT revision, state, session_id FROM tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                if current is None:
                    raise TaskNotFoundError(task.task_id)
                if current["revision"] != expected_revision or current["state"] != previous_state.value:
                    raise TaskRevisionConflictError(
                        f"task {task.task_id!r} expected revision/state "
                        f"{expected_revision}/{previous_state.value}, found "
                        f"{current['revision']}/{current['state']}"
                    )
                if current["session_id"] != task.session_id:
                    raise TaskStoreError("task session_id cannot change")

                cursor = self._connection.execute(
                    """
                    UPDATE tasks
                    SET revision = ?, state = ?, user_goal = ?, runtime_ref = ?,
                        checkpoint_ref = ?, pending_request_ref = ?,
                        cancellation_token_json = ?, result_ref = ?, failure_json = ?,
                        updated_at = ?, schema_version = ?
                    WHERE task_id = ? AND revision = ? AND state = ?
                    """,
                    self._task_update_values(task, expected_revision, previous_state),
                )
                if cursor.rowcount != 1:
                    raise TaskRevisionConflictError(
                        f"task {task.task_id!r} changed during compare-and-set"
                    )

                sequence_row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                    FROM interaction_events WHERE task_id = ?
                    """,
                    (task.task_id,),
                ).fetchone()
                event = InteractionEvent(
                    session_id=task.session_id,
                    task_id=task.task_id,
                    event_type=event_type,
                    actor=actor,
                    sequence=int(sequence_row["next_sequence"]),
                    trace_id=trace_id,
                    payload=event_payload
                    if event_payload is not None
                    else {
                        "from_state": previous_state.value,
                        "to_state": task.state.value,
                        "reason": reason,
                        "revision": task.revision,
                    },
                )
                self._insert_event(event)
                self._connection.commit()
                return event
            except Exception:
                self._connection.rollback()
                raise

    def append_event(
        self,
        task_id: str,
        *,
        event_type: InteractionEventType,
        actor: str,
        payload: dict[str, object],
        trace_id: str | None = None,
    ) -> InteractionEvent:
        """Append a non-state event with a per-task monotonic sequence."""
        with self._lock:
            self._begin()
            try:
                task_row = self._connection.execute(
                    "SELECT session_id FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task_row is None:
                    raise TaskNotFoundError(task_id)
                sequence_row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                    FROM interaction_events WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                event = InteractionEvent(
                    task_id=task_id,
                    session_id=task_row["session_id"],
                    sequence=int(sequence_row["next_sequence"]),
                    event_type=event_type,
                    actor=actor,
                    trace_id=trace_id,
                    payload=payload,
                )
                self._insert_event(event)
                self._connection.commit()
                return event
            except Exception:
                self._connection.rollback()
                raise

    def get_task(self, task_id: str) -> TaskInstance:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._row_to_task(row)

    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        states: Iterable[TaskState] | None = None,
    ) -> list[TaskInstance]:
        clauses: list[str] = []
        values: list[str] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        state_values = [state.value for state in states or ()]
        if state_values:
            placeholders = ", ".join("?" for _ in state_values)
            clauses.append(f"state IN ({placeholders})")
            values.extend(state_values)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at",  # noqa: S608
                values,
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_events(self, task_id: str) -> list[InteractionEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM interaction_events
                WHERE task_id = ? ORDER BY sequence
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _insert_task(self, task: TaskInstance) -> None:
        self._connection.execute(
            """
            INSERT INTO tasks (
                task_id, session_id, revision, state, user_goal, runtime_ref,
                checkpoint_ref, pending_request_ref, cancellation_token_json,
                result_ref, failure_json, created_at, updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._task_insert_values(task),
        )

    def _insert_event(self, event: InteractionEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO interaction_events (
                event_id, task_id, session_id, sequence, event_type, actor,
                trace_id, occurred_at, payload_json, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.task_id,
                event.session_id,
                event.sequence,
                event.event_type.value,
                event.actor,
                event.trace_id,
                event.occurred_at.isoformat(),
                json.dumps(to_primitive(event.payload), ensure_ascii=False, sort_keys=True),
                event.schema_version,
            ),
        )

    @staticmethod
    def _task_insert_values(task: TaskInstance) -> tuple[object, ...]:
        return (
            task.task_id,
            task.session_id,
            task.revision,
            task.state.value,
            task.user_goal,
            task.runtime_ref,
            task.checkpoint_ref,
            task.pending_request_ref,
            json.dumps(to_primitive(task.cancellation_token), ensure_ascii=False),
            task.result_ref,
            json.dumps(to_primitive(task.failure), ensure_ascii=False) if task.failure else None,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            task.schema_version,
        )

    @staticmethod
    def _task_update_values(
        task: TaskInstance,
        expected_revision: int,
        previous_state: TaskState,
    ) -> tuple[object, ...]:
        values = SQLiteTaskStore._task_insert_values(task)
        return (
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            values[8],
            values[9],
            values[10],
            values[12],
            values[13],
            task.task_id,
            expected_revision,
            previous_state.value,
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskInstance:
        token_data = json.loads(row["cancellation_token_json"])
        failure_data = json.loads(row["failure_json"]) if row["failure_json"] else None
        return TaskInstance(
            task_id=row["task_id"],
            session_id=row["session_id"],
            revision=row["revision"],
            state=TaskState(row["state"]),
            user_goal=row["user_goal"],
            runtime_ref=row["runtime_ref"],
            checkpoint_ref=row["checkpoint_ref"],
            pending_request_ref=row["pending_request_ref"],
            cancellation_token=CancellationToken(**token_data),
            result_ref=row["result_ref"],
            failure=FailureInfo(**failure_data) if failure_data else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> InteractionEvent:
        return InteractionEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            event_type=InteractionEventType(row["event_type"]),
            actor=row["actor"],
            trace_id=row["trace_id"],
            occurred_at=row["occurred_at"],
            payload=json.loads(row["payload_json"]),
            schema_version=row["schema_version"],
        )
