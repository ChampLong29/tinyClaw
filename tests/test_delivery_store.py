from datetime import timedelta
from pathlib import Path

import pytest

from tinyclaw.contracts import DeliveryState, DeliveryTarget
from tinyclaw.contracts._common import utc_now
from tinyclaw.delivery.store import (
    DeliveryIdempotencyConflictError,
    DeliveryLeaseConflictError,
    SQLiteDeliveryStore,
)


@pytest.fixture
def store(tmp_path: Path):
    delivery_store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    try:
        yield delivery_store
    finally:
        delivery_store.close()


def enqueue(
    store: SQLiteDeliveryStore,
    *,
    lane: str = "session:1",
    key: str = "key-1",
    text: str = "hello",
):
    return store.enqueue(
        intent_id=f"intent:{key}",
        session_id=lane,
        lane_key=lane,
        target=DeliveryTarget(
            channel="feishu",
            account_id="bot-1",
            peer_id="user-1",
        ),
        payload={"text": text},
        idempotency_key=key,
    )


def test_enqueue_allocates_monotonic_sequence_and_is_idempotent(store):
    first = enqueue(store, key="key-1")
    duplicate = enqueue(store, key="key-1")
    second = enqueue(store, key="key-2")

    assert first.delivery_id == duplicate.delivery_id
    assert [first.sequence, second.sequence] == [0, 1]
    assert store.lane_next_sequence("session:1") == 2

    with pytest.raises(DeliveryIdempotencyConflictError):
        enqueue(store, key="key-1", text="different")


def test_retrying_head_blocks_later_records_but_not_other_lanes(store):
    now = utc_now()
    head = enqueue(store, lane="session:1", key="key-1")
    later = enqueue(store, lane="session:1", key="key-2")
    other = enqueue(store, lane="session:2", key="key-3")

    claimed = store.claim_ready(
        worker_id="worker-1",
        lease_duration=timedelta(seconds=30),
        now=now,
    )
    assert {record.delivery_id for record in claimed} == {
        head.delivery_id,
        other.delivery_id,
    }
    store.record_failure(
        head.delivery_id,
        worker_id="worker-1",
        error={"message": "temporary"},
        next_retry_at=now + timedelta(minutes=1),
        max_attempts=5,
    )
    store.acknowledge(
        other.delivery_id,
        worker_id="worker-1",
        platform_message_id="platform-other",
        acked_at=now,
    )

    assert store.claim_ready(
        worker_id="worker-1",
        lease_duration=timedelta(seconds=30),
        now=now + timedelta(seconds=30),
    ) == []
    retried = store.claim_ready(
        worker_id="worker-1",
        lease_duration=timedelta(seconds=30),
        now=now + timedelta(minutes=1),
    )
    assert [record.delivery_id for record in retried] == [head.delivery_id]
    assert store.get(later.delivery_id).state == DeliveryState.PENDING


def test_expired_lease_can_be_reclaimed_and_stale_worker_cannot_ack(store):
    now = utc_now()
    record = enqueue(store)
    first_claim = store.claim_ready(
        worker_id="worker-1",
        lease_duration=timedelta(seconds=10),
        now=now,
    )
    assert first_claim[0].attempt_count == 1
    assert store.claim_ready(
        worker_id="worker-2",
        lease_duration=timedelta(seconds=10),
        now=now + timedelta(seconds=5),
    ) == []

    reclaimed = store.claim_ready(
        worker_id="worker-2",
        lease_duration=timedelta(seconds=10),
        now=now + timedelta(seconds=11),
    )
    assert reclaimed[0].delivery_id == record.delivery_id
    assert reclaimed[0].attempt_count == 2

    with pytest.raises(DeliveryLeaseConflictError):
        store.acknowledge(
            record.delivery_id,
            worker_id="worker-1",
            platform_message_id="stale-ack",
        )
    acked = store.acknowledge(
        record.delivery_id,
        worker_id="worker-2",
        platform_message_id="platform-1",
    )
    assert acked.state == DeliveryState.ACKED


def test_in_flight_record_survives_restart_and_reclaims_same_idempotency_key(tmp_path: Path):
    db_path = tmp_path / "delivery.db"
    now = utc_now()
    first_store = SQLiteDeliveryStore(db_path)
    record = enqueue(first_store)
    first_store.claim_ready(
        worker_id="worker-before-crash",
        lease_duration=timedelta(seconds=5),
        now=now,
    )
    first_store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        recovered = reopened.claim_ready(
            worker_id="worker-after-crash",
            lease_duration=timedelta(seconds=5),
            now=now + timedelta(seconds=6),
        )
        assert recovered[0].delivery_id == record.delivery_id
        assert recovered[0].idempotency_key == record.idempotency_key
        assert recovered[0].attempt_count == 2
    finally:
        reopened.close()
