from datetime import timedelta
from pathlib import Path

from tinyclaw.contracts import DeliveryState, DeliveryTarget
from tinyclaw.contracts._common import utc_now
from tinyclaw.delivery.retry import DeliveryRetryPolicy
from tinyclaw.delivery.store import SQLiteDeliveryStore
from tinyclaw.delivery.worker import DeliveryReceipt, LeaseDeliveryWorker


def enqueue(store: SQLiteDeliveryStore, key: str):
    return store.enqueue(
        intent_id=f"intent:{key}",
        session_id="session:1",
        lane_key="session:1",
        target=DeliveryTarget(
            channel="feishu",
            account_id="bot-1",
            peer_id="user-1",
        ),
        payload={"text": key},
        idempotency_key=key,
    )


class SuccessfulSender:
    def __init__(self):
        self.sent = []

    def send(self, record):
        self.sent.append(record)
        return DeliveryReceipt(platform_message_id=f"platform:{record.delivery_id}")


class FailingSender:
    def send(self, _record):
        raise ConnectionError("offline")


def test_worker_persists_platform_ack(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    sender = SuccessfulSender()
    record = enqueue(store, "key-1")
    try:
        results = LeaseDeliveryWorker(
            store=store,
            sender=sender,
            worker_id="worker-1",
        ).run_once(now=utc_now())

        assert results[0].state == DeliveryState.ACKED
        assert store.get(record.delivery_id).platform_message_id.startswith("platform:")
        assert sender.sent[0].idempotency_key == "key-1"
    finally:
        store.close()


def test_dead_letter_unblocks_next_lane_record(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    now = utc_now()
    first = enqueue(store, "key-1")
    second = enqueue(store, "key-2")
    worker = LeaseDeliveryWorker(
        store=store,
        sender=FailingSender(),
        worker_id="worker-1",
        retry_policy=DeliveryRetryPolicy(
            delays_seconds=(1,),
            jitter_ratio=0,
            max_attempts=2,
        ),
    )
    try:
        first_result = worker.run_once(now=now)[0]
        assert first_result.state == DeliveryState.RETRY_WAIT
        assert worker.run_once(now=now + timedelta(milliseconds=500)) == []

        second_failure = worker.run_once(now=now + timedelta(seconds=1))[0]
        assert second_failure.delivery_id == first.delivery_id
        assert second_failure.state == DeliveryState.DEAD_LETTER

        next_claim = store.claim_ready(
            worker_id="worker-2",
            lease_duration=timedelta(seconds=30),
            now=now + timedelta(seconds=2),
        )
        assert next_claim[0].delivery_id == second.delivery_id
    finally:
        store.close()
