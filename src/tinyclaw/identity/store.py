"""SQLite persistence for global identities, links, sessions, and audit events."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, to_primitive, utc_now
from tinyclaw.contracts.identity import (
    ChannelIdentityLink,
    GlobalIdentity,
    IdentityStatus,
    SessionDescriptor,
    SessionScope,
)


class IdentityStoreError(RuntimeError):
    pass


class IdentityNotFoundError(IdentityStoreError):
    pass


class IdentityLinkConflictError(IdentityStoreError):
    pass


@dataclass(frozen=True, kw_only=True)
class IdentityAuditEvent:
    global_user_id: str
    sequence: int
    event_type: str
    actor: str
    payload: Mapping[str, Any]
    event_id: str
    occurred_at: str


class SQLiteIdentityStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
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
                CREATE TABLE IF NOT EXISTS global_identities (
                    global_user_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS identity_links (
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    platform_user_id TEXT NOT NULL,
                    global_user_id TEXT NOT NULL
                        REFERENCES global_identities(global_user_id),
                    linked_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (channel, account_id, platform_user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_links_global
                    ON identity_links(global_user_id, linked_at);

                CREATE TABLE IF NOT EXISTS identity_audit_events (
                    event_id TEXT PRIMARY KEY,
                    global_user_id TEXT NOT NULL
                        REFERENCES global_identities(global_user_id),
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(global_user_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS session_descriptors (
                    session_id TEXT PRIMARY KEY,
                    route_key TEXT NOT NULL UNIQUE,
                    scope_type TEXT NOT NULL,
                    scope_version INTEGER NOT NULL CHECK (scope_version >= 1),
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    thread_id TEXT,
                    global_user_id TEXT,
                    active_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    schema_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_resolution_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES session_descriptors(session_id),
                    occurred_at TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    global_user_id TEXT,
                    decision_reason TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_identity(self, global_user_id: str) -> GlobalIdentity:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM global_identities WHERE global_user_id = ?",
                (global_user_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError(global_user_id)
            links = self._connection.execute(
                """
                SELECT * FROM identity_links
                WHERE global_user_id = ?
                ORDER BY linked_at, channel, account_id, platform_user_id
                """,
                (global_user_id,),
            ).fetchall()
        return self._identity_from_rows(row, links)

    def find_by_link(
        self,
        *,
        channel: str,
        account_id: str,
        platform_user_id: str,
    ) -> GlobalIdentity | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT global_user_id FROM identity_links
                WHERE channel = ? AND account_id = ? AND platform_user_id = ?
                """,
                (channel, account_id, platform_user_id),
            ).fetchone()
        return self.get_identity(row["global_user_id"]) if row else None

    def create_with_link(
        self,
        identity: GlobalIdentity,
        link: ChannelIdentityLink,
        *,
        actor: str,
        reason: str,
    ) -> tuple[GlobalIdentity, bool]:
        now = utc_now().isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT global_user_id FROM identity_links
                    WHERE channel = ? AND account_id = ? AND platform_user_id = ?
                    """,
                    (link.channel, link.account_id, link.platform_user_id),
                ).fetchone()
                if existing:
                    self._connection.rollback()
                    return self.get_identity(existing["global_user_id"]), False
                self._connection.execute(
                    """
                    INSERT INTO global_identities (
                        global_user_id, status, revision, created_at, updated_at,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.global_user_id,
                        identity.status.value,
                        identity.revision,
                        identity.created_at.isoformat(),
                        now,
                        identity.schema_version,
                    ),
                )
                self._insert_link(link, identity.global_user_id)
                self._append_audit_locked(
                    identity.global_user_id,
                    event_type="identity_created",
                    actor=actor,
                    payload={
                        "reason": reason,
                        "link": to_primitive(link),
                    },
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_identity(identity.global_user_id), True

    def link(
        self,
        global_user_id: str,
        link: ChannelIdentityLink,
        *,
        actor: str,
        reason: str,
    ) -> GlobalIdentity:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_identity_locked(global_user_id)
                existing = self._connection.execute(
                    """
                    SELECT global_user_id FROM identity_links
                    WHERE channel = ? AND account_id = ? AND platform_user_id = ?
                    """,
                    (link.channel, link.account_id, link.platform_user_id),
                ).fetchone()
                if existing:
                    if existing["global_user_id"] == global_user_id:
                        self._connection.rollback()
                        return self.get_identity(global_user_id)
                    raise IdentityLinkConflictError(
                        "platform identity is already linked to another global user"
                    )
                self._insert_link(link, global_user_id)
                self._bump_revision_locked(global_user_id)
                self._append_audit_locked(
                    global_user_id,
                    event_type="identity_linked",
                    actor=actor,
                    payload={"reason": reason, "link": to_primitive(link)},
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_identity(global_user_id)

    def unlink(
        self,
        *,
        channel: str,
        account_id: str,
        platform_user_id: str,
        actor: str,
        reason: str,
    ) -> GlobalIdentity:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT global_user_id FROM identity_links
                    WHERE channel = ? AND account_id = ? AND platform_user_id = ?
                    """,
                    (channel, account_id, platform_user_id),
                ).fetchone()
                if row is None:
                    raise IdentityNotFoundError("identity link not found")
                global_user_id = row["global_user_id"]
                self._connection.execute(
                    """
                    DELETE FROM identity_links
                    WHERE channel = ? AND account_id = ? AND platform_user_id = ?
                    """,
                    (channel, account_id, platform_user_id),
                )
                self._bump_revision_locked(global_user_id)
                self._append_audit_locked(
                    global_user_id,
                    event_type="identity_unlinked",
                    actor=actor,
                    payload={
                        "reason": reason,
                        "channel": channel,
                        "account_id": account_id,
                        "platform_user_id": platform_user_id,
                    },
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_identity(global_user_id)

    def merge(
        self,
        source_global_user_id: str,
        target_global_user_id: str,
        *,
        actor: str,
        reason: str,
    ) -> GlobalIdentity:
        if source_global_user_id == target_global_user_id:
            raise ValueError("source and target identities must differ")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_identity_locked(source_global_user_id)
                self._require_active_identity_locked(target_global_user_id)
                self._connection.execute(
                    """
                    UPDATE identity_links SET global_user_id = ?
                    WHERE global_user_id = ?
                    """,
                    (target_global_user_id, source_global_user_id),
                )
                now = utc_now().isoformat()
                self._connection.execute(
                    """
                    UPDATE global_identities
                    SET status = ?, revision = revision + 1, updated_at = ?
                    WHERE global_user_id = ?
                    """,
                    (IdentityStatus.MERGED.value, now, source_global_user_id),
                )
                self._bump_revision_locked(target_global_user_id)
                self._append_audit_locked(
                    source_global_user_id,
                    event_type="identity_merged",
                    actor=actor,
                    payload={
                        "reason": reason,
                        "target_global_user_id": target_global_user_id,
                    },
                )
                self._append_audit_locked(
                    target_global_user_id,
                    event_type="identity_merge_received",
                    actor=actor,
                    payload={
                        "reason": reason,
                        "source_global_user_id": source_global_user_id,
                    },
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_identity(target_global_user_id)

    def list_audit_events(self, global_user_id: str) -> list[IdentityAuditEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM identity_audit_events
                WHERE global_user_id = ? ORDER BY sequence
                """,
                (global_user_id,),
            ).fetchall()
        return [
            IdentityAuditEvent(
                event_id=row["event_id"],
                global_user_id=row["global_user_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                actor=row["actor"],
                occurred_at=row["occurred_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def resolve_session(self, descriptor: SessionDescriptor) -> tuple[SessionDescriptor, bool]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM session_descriptors WHERE route_key = ?",
                    (descriptor.route_key,),
                ).fetchone()
                created = row is None
                if created:
                    self._connection.execute(
                        """
                        INSERT INTO session_descriptors (
                            session_id, route_key, scope_type, scope_version,
                            channel, account_id, peer_id, thread_id,
                            global_user_id, active_task_id, created_at, updated_at,
                            decision_reason, schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            descriptor.session_id,
                            descriptor.route_key,
                            descriptor.scope_type.value,
                            descriptor.scope_version,
                            descriptor.channel,
                            descriptor.account_id,
                            descriptor.peer_id,
                            descriptor.thread_id,
                            descriptor.global_user_id,
                            descriptor.active_task_id,
                            descriptor.created_at.isoformat(),
                            descriptor.updated_at.isoformat(),
                            descriptor.decision_reason,
                            descriptor.schema_version,
                        ),
                    )
                    stored = descriptor
                else:
                    stored = self._session_from_row(row)
                self._connection.execute(
                    """
                    INSERT INTO session_resolution_events (
                        event_id, session_id, occurred_at, channel, account_id,
                        peer_id, global_user_id, decision_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        stored.session_id,
                        utc_now().isoformat(),
                        descriptor.channel,
                        descriptor.account_id,
                        descriptor.peer_id,
                        descriptor.global_user_id,
                        descriptor.decision_reason,
                    ),
                )
                self._connection.commit()
                return stored, created
            except Exception:
                self._connection.rollback()
                raise

    def get_session(self, session_id: str) -> SessionDescriptor:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM session_descriptors WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise IdentityNotFoundError(f"session {session_id!r} not found")
        return self._session_from_row(row)

    def _insert_link(self, link: ChannelIdentityLink, global_user_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO identity_links (
                channel, account_id, platform_user_id, global_user_id,
                linked_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                link.channel,
                link.account_id,
                link.platform_user_id,
                global_user_id,
                link.linked_at.isoformat(),
                json.dumps(to_primitive(link.metadata), ensure_ascii=False, sort_keys=True),
            ),
        )

    def _require_active_identity_locked(self, global_user_id: str) -> None:
        row = self._connection.execute(
            "SELECT status FROM global_identities WHERE global_user_id = ?",
            (global_user_id,),
        ).fetchone()
        if row is None:
            raise IdentityNotFoundError(global_user_id)
        if row["status"] != IdentityStatus.ACTIVE.value:
            raise IdentityStoreError(f"identity {global_user_id!r} is not active: {row['status']}")

    def _bump_revision_locked(self, global_user_id: str) -> None:
        self._connection.execute(
            """
            UPDATE global_identities
            SET revision = revision + 1, updated_at = ?
            WHERE global_user_id = ?
            """,
            (utc_now().isoformat(), global_user_id),
        )

    def _append_audit_locked(
        self,
        global_user_id: str,
        *,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
            FROM identity_audit_events WHERE global_user_id = ?
            """,
            (global_user_id,),
        ).fetchone()
        self._connection.execute(
            """
            INSERT INTO identity_audit_events (
                event_id, global_user_id, sequence, event_type, actor,
                occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                global_user_id,
                int(row["next_sequence"]),
                event_type,
                actor,
                utc_now().isoformat(),
                json.dumps(to_primitive(payload), ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def _identity_from_rows(
        row: sqlite3.Row,
        link_rows: list[sqlite3.Row],
    ) -> GlobalIdentity:
        return GlobalIdentity(
            global_user_id=row["global_user_id"],
            status=IdentityStatus(row["status"]),
            revision=row["revision"],
            created_at=parse_datetime(row["created_at"]),
            schema_version=row["schema_version"],
            channel_links=tuple(
                ChannelIdentityLink(
                    channel=link["channel"],
                    account_id=link["account_id"],
                    platform_user_id=link["platform_user_id"],
                    linked_at=parse_datetime(link["linked_at"]),
                    metadata=json.loads(link["metadata_json"]),
                )
                for link in link_rows
            ),
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionDescriptor:
        return SessionDescriptor(
            session_id=row["session_id"],
            route_key=row["route_key"],
            scope_type=SessionScope(row["scope_type"]),
            scope_version=row["scope_version"],
            channel=row["channel"],
            account_id=row["account_id"],
            peer_id=row["peer_id"],
            thread_id=row["thread_id"],
            global_user_id=row["global_user_id"],
            active_task_id=row["active_task_id"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
            decision_reason=row["decision_reason"],
            schema_version=row["schema_version"],
        )
