from datetime import timedelta
from pathlib import Path

import pytest

from tinyclaw.contracts import DeliveryState
from tinyclaw.contracts._common import utc_now
from tinyclaw.delivery import (
    DeliveryFaultPoint,
    DeliveryReceipt,
    DurableDeliveryQueue,
    InjectedDeliveryCrash,
    LeaseDeliveryWorker,
    SQLiteDeliveryStore,
)
from tinyclaw.observability import TraceRecorder
from tinyclaw.presentation import (
    CapabilityRegistry,
    ChannelCapability,
    OutboundRenderer,
)


class IdempotentSender:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.accepted: dict[str, str] = {}

    def send(self, record):
        self.calls.append(record.idempotency_key)
        platform_id = self.accepted.setdefault(
            record.idempotency_key,
            f"platform:{len(self.accepted) + 1}",
        )
        return DeliveryReceipt(platform_message_id=platform_id)


class AtLeastOnceSender:
    def __init__(self) -> None:
        self.accepted: list[str] = []

    def send(self, record):
        self.accepted.append(record.idempotency_key)
        return DeliveryReceipt(platform_message_id=f"platform-attempt:{len(self.accepted)}")


def enqueue_pair(queue: DurableDeliveryQueue, channel: str = "test") -> tuple[str, str]:
    common = {
        "account_id": "bot-1",
        "lane_key": "session-1",
        "session_id": "session-1",
    }
    first = queue.enqueue(
        channel,
        "user-1",
        "first",
        {**common, "intent_id": "intent-1", "idempotency_key": "key-1"},
    )
    second = queue.enqueue(
        channel,
        "user-1",
        "second",
        {**common, "intent_id": "intent-2", "idempotency_key": "key-2"},
    )
    return first, second


def crash_once_at(point_to_crash: DeliveryFaultPoint):
    crashed = False

    def inject(point, _record, _context):
        nonlocal crashed
        if point == point_to_crash and not crashed:
            crashed = True
            raise InjectedDeliveryCrash(point.value)

    return inject


def test_crash_after_claim_recovers_head_before_later_record(tmp_path: Path):
    db_path = tmp_path / "delivery.db"
    now = utc_now()
    store = SQLiteDeliveryStore(db_path)
    queue = DurableDeliveryQueue(store)
    first_id, second_id = enqueue_pair(queue)
    sender = IdempotentSender()
    worker = LeaseDeliveryWorker(
        store=store,
        sender=sender,
        worker_id="before-crash",
        lease_duration=timedelta(seconds=5),
        fault_injector=crash_once_at(DeliveryFaultPoint.AFTER_CLAIM),
    )

    with pytest.raises(InjectedDeliveryCrash):
        worker.run_once(now=now)
    assert sender.calls == []
    assert store.get(first_id).state == DeliveryState.IN_FLIGHT
    assert store.get(second_id).state == DeliveryState.PENDING
    store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        recovered = LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
        )
        first = recovered.run_once(now=now + timedelta(seconds=6))
        assert [record.delivery_id for record in first] == [first_id]
        second = recovered.run_once(now=now + timedelta(seconds=6))
        assert [record.delivery_id for record in second] == [second_id]
        assert sender.calls == ["key-1:0", "key-2:0"]
    finally:
        reopened.close()


def test_idempotent_sender_deduplicates_crash_after_send_before_ack(tmp_path: Path):
    db_path = tmp_path / "delivery.db"
    now = utc_now()
    trace = TraceRecorder(tmp_path / "observability")
    store = SQLiteDeliveryStore(db_path)
    renderer = OutboundRenderer(
        CapabilityRegistry({"idempotent": ChannelCapability(outbound_idempotency=True)})
    )
    queue = DurableDeliveryQueue(store, renderer=renderer, trace_recorder=trace)
    first_id, _second_id = enqueue_pair(queue, channel="idempotent")
    sender = IdempotentSender()
    worker = LeaseDeliveryWorker(
        store=store,
        sender=sender,
        worker_id="before-crash",
        lease_duration=timedelta(seconds=5),
        trace_recorder=trace,
        fault_injector=crash_once_at(DeliveryFaultPoint.AFTER_SEND_BEFORE_SETTLE),
    )

    with pytest.raises(InjectedDeliveryCrash):
        worker.run_once(now=now)
    crashed = store.get(first_id)
    assert crashed.state == DeliveryState.IN_FLIGHT
    assert crashed.payload["meta"]["delivery_semantics"] == "idempotent_retry"
    assert len(sender.accepted) == 1
    store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        result = LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
            trace_recorder=trace,
        ).run_once(now=now + timedelta(seconds=6))
        assert result[0].state == DeliveryState.ACKED
        assert sender.calls == ["key-1:0", "key-1:0"]
        assert len(sender.accepted) == 1

        attempted = [
            event
            for event in trace.read_events(session_id="session-1")
            if event.event_type == "delivery_attempted"
        ]
        assert len(attempted) == 2
        assert all(event.payload["delivery_semantics"] == "idempotent_retry" for event in attempted)
    finally:
        reopened.close()


def test_non_idempotent_sender_is_explicitly_at_least_once_after_ack_loss(
    tmp_path: Path,
):
    db_path = tmp_path / "delivery.db"
    now = utc_now()
    store = SQLiteDeliveryStore(db_path)
    queue = DurableDeliveryQueue(store)
    first_id, _second_id = enqueue_pair(queue, channel="dingtalk")
    sender = AtLeastOnceSender()
    worker = LeaseDeliveryWorker(
        store=store,
        sender=sender,
        worker_id="before-crash",
        lease_duration=timedelta(seconds=5),
        fault_injector=crash_once_at(DeliveryFaultPoint.AFTER_SEND_BEFORE_SETTLE),
    )

    with pytest.raises(InjectedDeliveryCrash):
        worker.run_once(now=now)
    assert store.get(first_id).payload["meta"]["delivery_semantics"] == ("at_least_once")
    store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
        ).run_once(now=now + timedelta(seconds=6))
        assert sender.accepted == ["key-1:0", "key-1:0"]
    finally:
        reopened.close()


def test_crash_after_settle_does_not_resend_acked_head(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    queue = DurableDeliveryQueue(store)
    first_id, second_id = enqueue_pair(queue)
    sender = IdempotentSender()
    worker = LeaseDeliveryWorker(
        store=store,
        sender=sender,
        worker_id="worker-1",
        fault_injector=crash_once_at(DeliveryFaultPoint.AFTER_SETTLE),
    )
    try:
        with pytest.raises(InjectedDeliveryCrash):
            worker.run_once()
        assert store.get(first_id).state == DeliveryState.ACKED

        result = LeaseDeliveryWorker(
            store=store,
            sender=sender,
            worker_id="worker-2",
        ).run_once()
        assert [record.delivery_id for record in result] == [second_id]
        assert sender.calls == ["key-1:0", "key-2:0"]
    finally:
        store.close()
