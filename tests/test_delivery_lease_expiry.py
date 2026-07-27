from datetime import timedelta
from pathlib import Path

import pytest

from tinyclaw.contracts import DeliveryTarget
from tinyclaw.contracts._common import utc_now
from tinyclaw.delivery.store import DeliveryLeaseConflictError, SQLiteDeliveryStore


def test_worker_cannot_ack_after_its_lease_expires(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    now = utc_now()
    record = store.enqueue(
        intent_id="intent-1",
        session_id="session-1",
        lane_key="session-1",
        target=DeliveryTarget(
            channel="feishu",
            account_id="bot-1",
            peer_id="user-1",
        ),
        payload={"text": "hello"},
        idempotency_key="key-1",
    )
    store.claim_ready(
        worker_id="worker-1",
        lease_duration=timedelta(seconds=5),
        now=now,
    )
    try:
        with pytest.raises(DeliveryLeaseConflictError):
            store.acknowledge(
                record.delivery_id,
                worker_id="worker-1",
                platform_message_id="platform-1",
                acked_at=now + timedelta(seconds=6),
            )
    finally:
        store.close()
