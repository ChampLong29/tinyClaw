"""Persistent policy engine for proactive notifications."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from tinyclaw.contracts import DeliveryPriority
from tinyclaw.contracts._common import parse_datetime, require_text

POLICY_VERSION = "notification_policy.v1"


class NotificationDecisionKind(str, Enum):
    RESERVED = "reserved"
    ALLOWED = "allowed"
    SUPPRESSED = "suppressed"
    DEFERRED = "deferred"
    ENQUEUE_FAILED = "enqueue_failed"


class NotificationReason(str, Enum):
    ALLOWED = "allowed"
    UNSUBSCRIBED = "unsubscribed"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    QUIET_HOURS = "quiet_hours"
    HOURLY_LIMIT = "hourly_limit"
    DAILY_LIMIT = "daily_limit"
    DIGEST = "digest"
    ENQUEUE_FAILED = "enqueue_failed"


@dataclass(frozen=True, kw_only=True)
class NotificationRequest:
    session_id: str
    topic: str
    channel: str
    account_id: str
    peer_id: str
    text: str
    request_id: str = ""
    dedupe_key: str | None = None
    priority: DeliveryPriority = DeliveryPriority.NORMAL
    expires_at: datetime | None = None
    digest: bool = False

    def __post_init__(self) -> None:
        for value, name in (
            (self.session_id, "session_id"),
            (self.topic, "topic"),
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.peer_id, "peer_id"),
        ):
            require_text(value, name)
        if not self.request_id:
            object.__setattr__(self, "request_id", uuid.uuid4().hex)
        object.__setattr__(self, "expires_at", parse_datetime(self.expires_at))


@dataclass(frozen=True, kw_only=True)
class NotificationPolicyConfig:
    cooldown_seconds: int = 0
    reservation_timeout_seconds: int = 300
    per_hour_limit: int = 20
    per_day_limit: int = 100
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None
    timezone_name: str = "UTC"
    urgent_overrides_quiet_hours: bool = False
    urgent_overrides_rate_limits: bool = False

    def __post_init__(self) -> None:
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        if self.reservation_timeout_seconds < 1:
            raise ValueError("reservation_timeout_seconds must be positive")
        if self.per_hour_limit < 1 or self.per_day_limit < 1:
            raise ValueError("notification limits must be positive")
        for value in (self.quiet_start_hour, self.quiet_end_hour):
            if value is not None and not 0 <= value <= 23:
                raise ValueError("quiet hour must be between 0 and 23")
        if (self.quiet_start_hour is None) != (self.quiet_end_hour is None):
            raise ValueError("quiet_start_hour and quiet_end_hour must be set together")
        ZoneInfo(self.timezone_name)


@dataclass(frozen=True, kw_only=True)
class NotificationDecision:
    decision_id: str
    request_id: str
    allowed: bool
    kind: NotificationDecisionKind
    reason: NotificationReason
    policy_version: str = POLICY_VERSION


class SQLiteNotificationPolicy:
    """Evaluate and persist notification decisions.

    Allowed requests are first reserved. The gateway commits the reservation
    only after durable delivery enqueue succeeds.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        config: NotificationPolicyConfig | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or NotificationPolicyConfig()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS notification_subscriptions (
                session_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(session_id, topic)
            );

            CREATE TABLE IF NOT EXISTS notification_decisions (
                decision_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                dedupe_key TEXT,
                kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL,
                policy_version TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_notification_rate
                ON notification_decisions(session_id, created_at, kind);
            CREATE INDEX IF NOT EXISTS idx_notification_dedupe
                ON notification_decisions(session_id, topic, dedupe_key, kind);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def set_subscription(
        self,
        *,
        session_id: str,
        topic: str,
        enabled: bool,
        now: datetime | str | None = None,
    ) -> None:
        current = parse_datetime(now, default_now=True)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO notification_subscriptions(
                    session_id, topic, enabled, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, topic) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (session_id, topic, int(enabled), current.isoformat()),
            )

    def evaluate(
        self,
        request: NotificationRequest,
        *,
        now: datetime | str | None = None,
    ) -> NotificationDecision:
        current = parse_datetime(now, default_now=True)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._release_stale_reservations(current)
                reason = self._suppression_reason(request, current)
                if reason is None and request.digest:
                    decision = self._record(
                        request,
                        kind=NotificationDecisionKind.DEFERRED,
                        reason=NotificationReason.DIGEST,
                        now=current,
                    )
                elif reason is not None:
                    decision = self._record(
                        request,
                        kind=NotificationDecisionKind.SUPPRESSED,
                        reason=reason,
                        now=current,
                    )
                else:
                    decision = self._record(
                        request,
                        kind=NotificationDecisionKind.RESERVED,
                        reason=NotificationReason.ALLOWED,
                        now=current,
                    )
                self._connection.commit()
                return decision
            except Exception:
                self._connection.rollback()
                raise

    def commit(self, decision: NotificationDecision) -> NotificationDecision:
        return self._finalize(
            decision,
            kind=NotificationDecisionKind.ALLOWED,
            reason=NotificationReason.ALLOWED,
        )

    def fail_enqueue(self, decision: NotificationDecision) -> NotificationDecision:
        return self._finalize(
            decision,
            kind=NotificationDecisionKind.ENQUEUE_FAILED,
            reason=NotificationReason.ENQUEUE_FAILED,
        )

    def list_decisions(self) -> list[NotificationDecision]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM notification_decisions ORDER BY created_at, decision_id"
            ).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def _suppression_reason(
        self,
        request: NotificationRequest,
        now: datetime,
    ) -> NotificationReason | None:
        subscription = self._connection.execute(
            """
            SELECT enabled FROM notification_subscriptions
            WHERE session_id = ? AND topic = ?
            """,
            (request.session_id, request.topic),
        ).fetchone()
        if subscription is not None and not bool(subscription["enabled"]):
            return NotificationReason.UNSUBSCRIBED
        if request.expires_at is not None and request.expires_at <= now:
            return NotificationReason.EXPIRED
        if request.dedupe_key and self._has_duplicate(request):
            return NotificationReason.DUPLICATE
        if self._within_cooldown(request, now):
            return NotificationReason.COOLDOWN

        urgent = request.priority == DeliveryPriority.URGENT
        if self._is_quiet_hour(now) and not (urgent and self.config.urgent_overrides_quiet_hours):
            return NotificationReason.QUIET_HOURS

        hour_count = self._count_since(
            request.session_id,
            now - timedelta(hours=1),
        )
        if hour_count >= self.config.per_hour_limit and not (
            urgent and self.config.urgent_overrides_rate_limits
        ):
            return NotificationReason.HOURLY_LIMIT
        day_count = self._count_since(
            request.session_id,
            now - timedelta(days=1),
        )
        if day_count >= self.config.per_day_limit and not (
            urgent and self.config.urgent_overrides_rate_limits
        ):
            return NotificationReason.DAILY_LIMIT
        return None

    def _release_stale_reservations(self, now: datetime) -> None:
        threshold = now - timedelta(seconds=self.config.reservation_timeout_seconds)
        self._connection.execute(
            """
            UPDATE notification_decisions
            SET kind = ?, reason = ?
            WHERE kind = ? AND created_at <= ?
            """,
            (
                NotificationDecisionKind.ENQUEUE_FAILED.value,
                NotificationReason.ENQUEUE_FAILED.value,
                NotificationDecisionKind.RESERVED.value,
                threshold.isoformat(),
            ),
        )

    def _has_duplicate(self, request: NotificationRequest) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM notification_decisions
            WHERE session_id = ? AND topic = ? AND dedupe_key = ?
              AND kind IN (?, ?)
            LIMIT 1
            """,
            (
                request.session_id,
                request.topic,
                request.dedupe_key,
                NotificationDecisionKind.RESERVED.value,
                NotificationDecisionKind.ALLOWED.value,
            ),
        ).fetchone()
        return row is not None

    def _within_cooldown(
        self,
        request: NotificationRequest,
        now: datetime,
    ) -> bool:
        if self.config.cooldown_seconds == 0:
            return False
        threshold = now - timedelta(seconds=self.config.cooldown_seconds)
        row = self._connection.execute(
            """
            SELECT 1 FROM notification_decisions
            WHERE session_id = ? AND topic = ?
              AND kind IN (?, ?) AND created_at > ?
            LIMIT 1
            """,
            (
                request.session_id,
                request.topic,
                NotificationDecisionKind.RESERVED.value,
                NotificationDecisionKind.ALLOWED.value,
                threshold.isoformat(),
            ),
        ).fetchone()
        return row is not None

    def _is_quiet_hour(self, now: datetime) -> bool:
        start = self.config.quiet_start_hour
        end = self.config.quiet_end_hour
        if start is None or end is None or start == end:
            return False
        local_hour = now.astimezone(ZoneInfo(self.config.timezone_name)).hour
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    def _count_since(self, session_id: str, since: datetime) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count FROM notification_decisions
            WHERE session_id = ? AND created_at > ?
              AND kind IN (?, ?)
            """,
            (
                session_id,
                since.isoformat(),
                NotificationDecisionKind.RESERVED.value,
                NotificationDecisionKind.ALLOWED.value,
            ),
        ).fetchone()
        return int(row["count"])

    def _record(
        self,
        request: NotificationRequest,
        *,
        kind: NotificationDecisionKind,
        reason: NotificationReason,
        now: datetime,
    ) -> NotificationDecision:
        decision = NotificationDecision(
            decision_id=uuid.uuid4().hex,
            request_id=request.request_id,
            allowed=kind == NotificationDecisionKind.RESERVED,
            kind=kind,
            reason=reason,
        )
        self._connection.execute(
            """
            INSERT INTO notification_decisions(
                decision_id, request_id, session_id, topic, dedupe_key,
                kind, reason, priority, created_at, policy_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_id,
                request.request_id,
                request.session_id,
                request.topic,
                request.dedupe_key,
                kind.value,
                reason.value,
                request.priority.value,
                now.isoformat(),
                POLICY_VERSION,
            ),
        )
        return decision

    def _finalize(
        self,
        decision: NotificationDecision,
        *,
        kind: NotificationDecisionKind,
        reason: NotificationReason,
    ) -> NotificationDecision:
        if decision.kind != NotificationDecisionKind.RESERVED:
            raise ValueError("only reserved notification decisions can be finalized")
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE notification_decisions SET kind = ?, reason = ?
                WHERE decision_id = ? AND kind = ?
                """,
                (
                    kind.value,
                    reason.value,
                    decision.decision_id,
                    NotificationDecisionKind.RESERVED.value,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("notification reservation is no longer active")
        return NotificationDecision(
            decision_id=decision.decision_id,
            request_id=decision.request_id,
            allowed=kind == NotificationDecisionKind.ALLOWED,
            kind=kind,
            reason=reason,
        )

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> NotificationDecision:
        kind = NotificationDecisionKind(row["kind"])
        return NotificationDecision(
            decision_id=row["decision_id"],
            request_id=row["request_id"],
            allowed=kind
            in (
                NotificationDecisionKind.RESERVED,
                NotificationDecisionKind.ALLOWED,
            ),
            kind=kind,
            reason=NotificationReason(row["reason"]),
            policy_version=row["policy_version"],
        )
