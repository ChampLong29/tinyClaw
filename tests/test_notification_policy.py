from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tinyclaw.contracts import DeliveryPriority
from tinyclaw.notification import (
    NotificationDecisionKind,
    NotificationGateway,
    NotificationPolicyConfig,
    NotificationReason,
    NotificationRequest,
    SQLiteNotificationPolicy,
)
from tinyclaw.observability import TraceRecorder


def make_request(**overrides) -> NotificationRequest:
    values = {
        "request_id": "request-1",
        "session_id": "session-1",
        "topic": "reminder",
        "channel": "feishu",
        "account_id": "bot-1",
        "peer_id": "peer-1",
        "text": "remember",
        "dedupe_key": "reminder-1",
    }
    values.update(overrides)
    return NotificationRequest(**values)


def test_subscription_and_expiry_precede_other_policy_checks(tmp_path: Path):
    policy = SQLiteNotificationPolicy(tmp_path / "notifications.db")
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    policy.set_subscription(
        session_id="session-1",
        topic="reminder",
        enabled=False,
        now=now,
    )
    try:
        decision = policy.evaluate(
            make_request(expires_at=now - timedelta(seconds=1)),
            now=now,
        )

        assert decision.allowed is False
        assert decision.reason == NotificationReason.UNSUBSCRIBED
        assert decision.kind == NotificationDecisionKind.SUPPRESSED
    finally:
        policy.close()


def test_dedupe_survives_restart_after_enqueue_commit(tmp_path: Path):
    db_path = tmp_path / "notifications.db"
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    first_policy = SQLiteNotificationPolicy(db_path)
    first = first_policy.evaluate(make_request(), now=now)
    first_policy.commit(first)
    first_policy.close()

    reopened = SQLiteNotificationPolicy(db_path)
    try:
        duplicate = reopened.evaluate(
            make_request(request_id="request-2"),
            now=now + timedelta(minutes=1),
        )

        assert duplicate.allowed is False
        assert duplicate.reason == NotificationReason.DUPLICATE
    finally:
        reopened.close()


def test_urgent_override_is_explicit_for_quiet_hours_and_rate_limits(
    tmp_path: Path,
):
    policy = SQLiteNotificationPolicy(
        tmp_path / "notifications.db",
        config=NotificationPolicyConfig(
            per_hour_limit=1,
            per_day_limit=1,
            quiet_start_hour=0,
            quiet_end_hour=23,
            timezone_name="UTC",
            urgent_overrides_quiet_hours=True,
            urgent_overrides_rate_limits=True,
        ),
    )
    now = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    try:
        normal = policy.evaluate(
            make_request(dedupe_key="normal"),
            now=now,
        )
        assert normal.reason == NotificationReason.QUIET_HOURS

        urgent = policy.evaluate(
            make_request(
                request_id="request-urgent",
                dedupe_key="urgent",
                priority=DeliveryPriority.URGENT,
            ),
            now=now,
        )
        assert urgent.allowed is True
        policy.commit(urgent)

        second_urgent = policy.evaluate(
            make_request(
                request_id="request-urgent-2",
                dedupe_key="urgent-2",
                priority=DeliveryPriority.URGENT,
            ),
            now=now,
        )
        assert second_urgent.allowed is True
    finally:
        policy.close()


class RecordingQueue:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.entries = []

    def enqueue(self, channel, to, text, meta=None):
        if self.fail:
            raise RuntimeError("disk full")
        self.entries.append((channel, to, text, meta))
        return "delivery-1"


def test_gateway_never_enqueues_suppressed_notification(tmp_path: Path):
    policy = SQLiteNotificationPolicy(tmp_path / "notifications.db")
    trace = TraceRecorder(tmp_path / "observability")
    policy.set_subscription(
        session_id="session-1",
        topic="reminder",
        enabled=False,
    )
    queue = RecordingQueue()
    try:
        decision = NotificationGateway(
            policy=policy,
            queue=queue,
            trace_recorder=trace,
        ).notify(make_request())

        assert decision.allowed is False
        assert queue.entries == []
        events = trace.read_events(session_id="session-1")
        assert events[0].event_type == "notification_suppressed"
        assert events[0].payload["reason"] == "unsubscribed"
    finally:
        policy.close()


def test_enqueue_failure_releases_dedupe_reservation(tmp_path: Path):
    policy = SQLiteNotificationPolicy(tmp_path / "notifications.db")
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            NotificationGateway(
                policy=policy,
                queue=RecordingQueue(fail=True),
            ).notify(make_request())

        retry = policy.evaluate(
            make_request(request_id="request-retry"),
        )
        assert retry.allowed is True
        failed = policy.list_decisions()[0]
        assert failed.kind == NotificationDecisionKind.ENQUEUE_FAILED
    finally:
        policy.close()


def test_stale_reservation_is_recovered_after_crash(tmp_path: Path):
    policy = SQLiteNotificationPolicy(
        tmp_path / "notifications.db",
        config=NotificationPolicyConfig(reservation_timeout_seconds=10),
    )
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    try:
        abandoned = policy.evaluate(make_request(), now=now)
        assert abandoned.kind == NotificationDecisionKind.RESERVED

        recovered = policy.evaluate(
            make_request(request_id="request-after-crash"),
            now=now + timedelta(seconds=11),
        )

        assert recovered.allowed is True
        assert policy.list_decisions()[0].kind == (NotificationDecisionKind.ENQUEUE_FAILED)
    finally:
        policy.close()


def test_digest_request_is_deferred_and_persisted(tmp_path: Path):
    policy = SQLiteNotificationPolicy(tmp_path / "notifications.db")
    try:
        decision = policy.evaluate(make_request(digest=True))

        assert decision.allowed is False
        assert decision.kind == NotificationDecisionKind.DEFERRED
        assert decision.reason == NotificationReason.DIGEST
    finally:
        policy.close()
