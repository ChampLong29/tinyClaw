"""Transactional SQLite delivery store with persistent FIFO lanes."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive
from tinyclaw.contracts.delivery import DeliveryRecord, DeliveryState, DeliveryTarget


class DeliveryStoreError(RuntimeError):
    pass


class DeliveryNotFoundError(DeliveryStoreError):
    pass


class DeliveryIdempotencyConflictError(DeliveryStoreError):
    pass


class DeliveryLeaseConflictError(DeliveryStoreError):
    pass


_UNFINISHED_STATES = (
    DeliveryState.PENDING.value,
    DeliveryState.IN_FLIGHT.value,
    DeliveryState.RETRY_WAIT.value,
)


class SQLiteDeliveryStore:
    """Durable delivery records and monotonic per-lane sequences."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or (Path.cwd() / "workspace" / "delivery.db"))
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
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delivery_lanes (
                    lane_key TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL CHECK (next_sequence >= 0)
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    lane_key TEXT NOT NULL REFERENCES delivery_lanes(lane_key),
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    target_channel TEXT NOT NULL,
                    target_account_id TEXT NOT NULL,
                    target_peer_id TEXT NOT NULL,
                    target_thread_id TEXT,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                    next_retry_at TEXT,
                    platform_message_id TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    acked_at TEXT,
                    accepted_at TEXT,
                    schema_version TEXT NOT NULL,
                    UNIQUE(lane_key, sequence),
                    UNIQUE(target_channel, target_account_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_deliveries_lane_head
                    ON deliveries(lane_key, sequence, state);
                CREATE INDEX IF NOT EXISTS idx_deliveries_retry
                    ON deliveries(state, next_retry_at);
                """
            )
            columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(deliveries)")
            }
            if "accepted_at" not in columns:
                self._connection.execute("ALTER TABLE deliveries ADD COLUMN accepted_at TEXT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteDeliveryStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def enqueue(
        self,
        *,
        intent_id: str,
        session_id: str,
        lane_key: str,
        target: DeliveryTarget,
        payload: Mapping[str, Any],
        idempotency_key: str,
        delivery_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> DeliveryRecord:
        delivery_id = delivery_id or uuid.uuid4().hex
        for value, field_name in (
            (delivery_id, "delivery_id"),
            (intent_id, "intent_id"),
            (session_id, "session_id"),
            (lane_key, "lane_key"),
            (idempotency_key, "idempotency_key"),
        ):
            require_text(value, field_name)
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        created = parse_datetime(created_at, default_now=True)
        payload_json = self._canonical_json(payload)
        with self._lock:
            self._begin()
            try:
                existing = self._connection.execute(
                    """
                    SELECT * FROM deliveries
                    WHERE target_channel = ? AND target_account_id = ?
                        AND idempotency_key = ?
                    """,
                    (target.channel, target.account_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    record = self._row_to_record(existing)
                    self._assert_same_enqueue(
                        record,
                        intent_id=intent_id,
                        session_id=session_id,
                        lane_key=lane_key,
                        target=target,
                        payload_json=payload_json,
                    )
                    self._connection.commit()
                    return record

                duplicate_id = self._connection.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if duplicate_id is not None:
                    raise DeliveryIdempotencyConflictError(
                        f"delivery_id {delivery_id!r} already exists"
                    )

                self._connection.execute(
                    """
                    INSERT INTO delivery_lanes(lane_key, next_sequence)
                    VALUES (?, 0) ON CONFLICT(lane_key) DO NOTHING
                    """,
                    (lane_key,),
                )
                lane = self._connection.execute(
                    "SELECT next_sequence FROM delivery_lanes WHERE lane_key = ?",
                    (lane_key,),
                ).fetchone()
                sequence = int(lane["next_sequence"])
                self._connection.execute(
                    """
                    INSERT INTO deliveries (
                        delivery_id, intent_id, session_id, lane_key, sequence,
                        target_channel, target_account_id, target_peer_id,
                        target_thread_id, payload_json, idempotency_key, state,
                        attempt_count, retry_count, created_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        delivery_id,
                        intent_id,
                        session_id,
                        lane_key,
                        sequence,
                        target.channel,
                        target.account_id,
                        target.peer_id,
                        target.thread_id,
                        payload_json,
                        idempotency_key,
                        DeliveryState.PENDING.value,
                        created.isoformat(),
                        "delivery_record.v1",
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE delivery_lanes SET next_sequence = next_sequence + 1
                    WHERE lane_key = ?
                    """,
                    (lane_key,),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get(delivery_id)

    def claim_ready(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        limit: int = 100,
        now: datetime | str | None = None,
    ) -> list[DeliveryRecord]:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        if limit <= 0:
            return []
        current = parse_datetime(now, default_now=True)
        lease_until = current + lease_duration
        placeholders = ", ".join("?" for _ in _UNFINISHED_STATES)
        with self._lock:
            self._begin()
            try:
                rows = self._connection.execute(
                    f"""
                    SELECT d.* FROM deliveries d
                    WHERE d.state IN ({placeholders})
                      AND d.sequence = (
                          SELECT MIN(h.sequence) FROM deliveries h
                          WHERE h.lane_key = d.lane_key
                            AND h.state IN ({placeholders})
                      )
                      AND (
                          d.state = ?
                          OR (d.state = ? AND d.next_retry_at <= ?)
                          OR (d.state = ? AND d.lease_until <= ?)
                      )
                    ORDER BY d.created_at, d.lane_key
                    LIMIT ?
                    """,
                    (
                        *_UNFINISHED_STATES,
                        *_UNFINISHED_STATES,
                        DeliveryState.PENDING.value,
                        DeliveryState.RETRY_WAIT.value,
                        current.isoformat(),
                        DeliveryState.IN_FLIGHT.value,
                        current.isoformat(),
                        limit,
                    ),
                ).fetchall()
                claimed_ids: list[str] = []
                for row in rows:
                    cursor = self._connection.execute(
                        """
                        UPDATE deliveries
                        SET state = ?, lease_owner = ?, lease_until = ?,
                            attempt_count = attempt_count + 1
                        WHERE delivery_id = ?
                          AND (
                              state = ?
                              OR (state = ? AND next_retry_at <= ?)
                              OR (state = ? AND lease_until <= ?)
                          )
                        """,
                        (
                            DeliveryState.IN_FLIGHT.value,
                            worker_id,
                            lease_until.isoformat(),
                            row["delivery_id"],
                            DeliveryState.PENDING.value,
                            DeliveryState.RETRY_WAIT.value,
                            current.isoformat(),
                            DeliveryState.IN_FLIGHT.value,
                            current.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        claimed_ids.append(row["delivery_id"])
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return [self.get(delivery_id) for delivery_id in claimed_ids]

    def acknowledge(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        platform_message_id: str,
        acked_at: datetime | str | None = None,
    ) -> DeliveryRecord:
        require_text(worker_id, "worker_id")
        require_text(platform_message_id, "platform_message_id")
        current = parse_datetime(acked_at, default_now=True)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE deliveries
                SET state = ?, platform_message_id = ?, acked_at = ?,
                    lease_owner = NULL, lease_until = NULL, next_retry_at = NULL
                WHERE delivery_id = ? AND state = ? AND lease_owner = ?
                    AND lease_until > ?
                """,
                (
                    DeliveryState.ACKED.value,
                    platform_message_id,
                    current.isoformat(),
                    delivery_id,
                    DeliveryState.IN_FLIGHT.value,
                    worker_id,
                    current.isoformat(),
                ),
            )
        if cursor.rowcount != 1:
            existing = self.get(delivery_id)
            if (
                existing.state == DeliveryState.ACKED
                and existing.platform_message_id == platform_message_id
            ):
                return existing
            raise DeliveryLeaseConflictError(
                f"worker {worker_id!r} does not own delivery {delivery_id!r}"
            )
        return self.get(delivery_id)

    def mark_accepted_unconfirmed(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        accepted_at: datetime | str | None = None,
    ) -> DeliveryRecord:
        require_text(worker_id, "worker_id")
        current = parse_datetime(accepted_at, default_now=True)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE deliveries
                SET state = ?, accepted_at = ?,
                    lease_owner = NULL, lease_until = NULL, next_retry_at = NULL
                WHERE delivery_id = ? AND state = ? AND lease_owner = ?
                    AND lease_until > ?
                """,
                (
                    DeliveryState.ACCEPTED_UNCONFIRMED.value,
                    current.isoformat(),
                    delivery_id,
                    DeliveryState.IN_FLIGHT.value,
                    worker_id,
                    current.isoformat(),
                ),
            )
        if cursor.rowcount != 1:
            existing = self.get(delivery_id)
            if existing.state == DeliveryState.ACCEPTED_UNCONFIRMED:
                return existing
            raise DeliveryLeaseConflictError(
                f"worker {worker_id!r} does not own delivery {delivery_id!r}"
            )
        return self.get(delivery_id)

    def import_dead_letter(
        self,
        delivery_id: str,
        *,
        error: Mapping[str, Any],
    ) -> DeliveryRecord:
        """Mark a newly imported pending record as a legacy dead letter."""
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE deliveries
                SET state = ?, last_error_json = ?
                WHERE delivery_id = ? AND state = ?
                """,
                (
                    DeliveryState.DEAD_LETTER.value,
                    self._canonical_json(error),
                    delivery_id,
                    DeliveryState.PENDING.value,
                ),
            )
        if cursor.rowcount == 1:
            return self.get(delivery_id)
        existing = self.get(delivery_id)
        if existing.state == DeliveryState.DEAD_LETTER:
            return existing
        raise DeliveryStoreError(f"delivery {delivery_id!r} is not pending")

    def record_failure(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        next_retry_at: datetime | str,
        max_attempts: int,
    ) -> DeliveryRecord:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        retry_at = parse_datetime(next_retry_at)
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    raise DeliveryNotFoundError(delivery_id)
                if row["state"] != DeliveryState.IN_FLIGHT.value or row["lease_owner"] != worker_id:
                    raise DeliveryLeaseConflictError(
                        f"worker {worker_id!r} does not own delivery {delivery_id!r}"
                    )
                state = (
                    DeliveryState.DEAD_LETTER
                    if row["attempt_count"] >= max_attempts
                    else DeliveryState.RETRY_WAIT
                )
                self._connection.execute(
                    """
                    UPDATE deliveries
                    SET state = ?, retry_count = retry_count + 1,
                        next_retry_at = ?, last_error_json = ?,
                        lease_owner = NULL, lease_until = NULL
                    WHERE delivery_id = ?
                    """,
                    (
                        state.value,
                        None if state == DeliveryState.DEAD_LETTER else retry_at.isoformat(),
                        self._canonical_json(error),
                        delivery_id,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get(delivery_id)

    def retry_dead_letter(self, delivery_id: str) -> DeliveryRecord:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE deliveries
                SET state = ?, retry_count = 0, attempt_count = 0,
                    next_retry_at = NULL, last_error_json = NULL,
                    lease_owner = NULL, lease_until = NULL
                WHERE delivery_id = ? AND state = ?
                """,
                (
                    DeliveryState.PENDING.value,
                    delivery_id,
                    DeliveryState.DEAD_LETTER.value,
                ),
            )
        if cursor.rowcount != 1:
            raise DeliveryStoreError(f"delivery {delivery_id!r} is not dead-lettered")
        return self.get(delivery_id)

    def get(self, delivery_id: str) -> DeliveryRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise DeliveryNotFoundError(delivery_id)
        return self._row_to_record(row)

    def list_records(
        self,
        *,
        lane_key: str | None = None,
        state: DeliveryState | None = None,
    ) -> list[DeliveryRecord]:
        clauses: list[str] = []
        values: list[str] = []
        if lane_key is not None:
            clauses.append("lane_key = ?")
            values.append(lane_key)
        if state is not None:
            clauses.append("state = ?")
            values.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM deliveries{where} ORDER BY lane_key, sequence",  # noqa: S608
                values,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def lane_next_sequence(self, lane_key: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT next_sequence FROM delivery_lanes WHERE lane_key = ?",
                (lane_key,),
            ).fetchone()
        return int(row["next_sequence"]) if row else 0

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> str:
        return json.dumps(
            to_primitive(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _assert_same_enqueue(
        record: DeliveryRecord,
        *,
        intent_id: str,
        session_id: str,
        lane_key: str,
        target: DeliveryTarget,
        payload_json: str,
    ) -> None:
        same = (
            record.intent_id == intent_id
            and record.session_id == session_id
            and record.lane_key == lane_key
            and record.target == target
            and SQLiteDeliveryStore._canonical_json(record.payload) == payload_json
        )
        if not same:
            raise DeliveryIdempotencyConflictError(
                f"idempotency key {record.idempotency_key!r} was reused with different content"
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            delivery_id=row["delivery_id"],
            intent_id=row["intent_id"],
            session_id=row["session_id"],
            lane_key=row["lane_key"],
            sequence=row["sequence"],
            target=DeliveryTarget(
                channel=row["target_channel"],
                account_id=row["target_account_id"],
                peer_id=row["target_peer_id"],
                thread_id=row["target_thread_id"],
            ),
            payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            state=DeliveryState(row["state"]),
            lease_owner=row["lease_owner"],
            lease_until=parse_datetime(row["lease_until"]),
            attempt_count=row["attempt_count"],
            retry_count=row["retry_count"],
            next_retry_at=parse_datetime(row["next_retry_at"]),
            platform_message_id=row["platform_message_id"],
            last_error=json.loads(row["last_error_json"]) if row["last_error_json"] else None,
            created_at=parse_datetime(row["created_at"], default_now=True),
            acked_at=parse_datetime(row["acked_at"]),
            accepted_at=parse_datetime(row["accepted_at"]),
            schema_version=row["schema_version"],
        )
