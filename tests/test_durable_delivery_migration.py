from pathlib import Path

from tinyclaw.contracts import DeliveryState
from tinyclaw.delivery.durable import (
    DurableDeliveryQueue,
    LegacyDeliveryMigrator,
)
from tinyclaw.delivery.queue import DeliveryQueue
from tinyclaw.delivery.store import SQLiteDeliveryStore
from tinyclaw.delivery.worker import DeliveryReceipt, LeaseDeliveryWorker
from tinyclaw.observability import TraceRecorder
from tinyclaw.presentation import (
    CapabilityRegistry,
    ChannelCapability,
    OutboundRenderer,
)


class UnconfirmedSender:
    def send(self, _record):
        return DeliveryReceipt()


def test_durable_queue_renders_chunks_with_stable_semantic_snapshot(
    tmp_path: Path,
):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    renderer = OutboundRenderer(CapabilityRegistry({"dingtalk": ChannelCapability(text_limit=80)}))
    queue = DurableDeliveryQueue(store, renderer=renderer)
    text = "```python\n" + "\n".join(f"print({index})" for index in range(20)) + "\n```"
    try:
        queue.enqueue("dingtalk", "peer-1", text)
        records = store.list_records()

        assert len(records) > 1
        assert [record.sequence for record in records] == list(range(len(records)))
        assert all(record.payload["text"].count("```") % 2 == 0 for record in records)
        snapshot_hashes = {
            record.payload["semantic_snapshot"]["snapshot_hash"] for record in records
        }
        assert len(snapshot_hashes) == 1
    finally:
        store.close()


def test_rendered_enqueue_retry_returns_existing_delivery_id(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    queue = DurableDeliveryQueue(store)
    metadata = {
        "intent_id": "notification:daily-summary",
        "idempotency_key": "notification:daily-summary",
    }
    try:
        first = queue.enqueue("feishu", "peer-1", "summary", metadata)
        retried = queue.enqueue("feishu", "peer-1", "summary", metadata)

        assert retried == first
        assert len(store.list_records()) == 1
    finally:
        store.close()


def test_unconfirmed_adapter_acceptance_is_not_platform_ack(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    queue = DurableDeliveryQueue(store)
    delivery_id = queue.enqueue("dingtalk", "peer-1", "hello")
    try:
        result = LeaseDeliveryWorker(
            store=store,
            sender=UnconfirmedSender(),
            worker_id="worker-1",
        ).run_once()[0]

        assert result.state == DeliveryState.ACCEPTED_UNCONFIRMED
        assert result.accepted_at is not None
        assert result.acked_at is None
        assert result.platform_message_id is None
        assert store.get(delivery_id).state == DeliveryState.ACCEPTED_UNCONFIRMED
    finally:
        store.close()


def test_delivery_lifecycle_is_recorded_in_shared_trace(tmp_path: Path):
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    trace = TraceRecorder(tmp_path / "observability")
    queue = DurableDeliveryQueue(store, trace_recorder=trace)
    queue.enqueue("dingtalk", "peer-1", "hello")
    try:
        LeaseDeliveryWorker(
            store=store,
            sender=UnconfirmedSender(),
            worker_id="worker-1",
            trace_recorder=trace,
        ).run_once()

        events = trace.read_events(
            session_id="dingtalk:dingtalk:peer-1",
        )
        assert [event.event_type for event in events] == [
            "outbound_intent_rendered",
            "delivery_enqueued",
            "delivery_attempted",
            "delivery_accepted_unconfirmed",
        ]
        assert [event.sequence for event in events] == [0, 1, 2, 3]
    finally:
        store.close()


def test_legacy_migration_is_ordered_idempotent_and_non_destructive(
    tmp_path: Path,
):
    queue_dir = tmp_path / "delivery-queue"
    legacy = DeliveryQueue(queue_dir)
    first_id = legacy.enqueue("feishu", "peer-1", "first")
    second_id = legacy.enqueue("feishu", "peer-1", "second")
    legacy.move_to_failed(second_id)
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    migrator = LegacyDeliveryMigrator(
        legacy_queue_dir=queue_dir,
        store=store,
    )
    try:
        first = migrator.migrate()
        records = store.list_records(lane_key="feishu:feishu:peer-1")

        assert first.pending_imported == 1
        assert first.dead_letters_imported == 1
        assert [record.payload["text"] for record in records] == [
            "first",
            "second",
        ]
        assert [record.sequence for record in records] == [0, 1]
        assert records[1].state == DeliveryState.DEAD_LETTER
        assert (queue_dir / f"{first_id}.json").exists()
        assert (queue_dir / "failed" / f"{second_id}.json").exists()

        second = migrator.migrate()
        assert second.already_imported == 2
        assert len(store.list_records()) == 2
    finally:
        store.close()


def test_invalid_legacy_entry_is_reported_without_blocking_valid_entries(
    tmp_path: Path,
):
    queue_dir = tmp_path / "delivery-queue"
    legacy = DeliveryQueue(queue_dir)
    legacy.enqueue("", "peer-1", "invalid")
    valid_id = legacy.enqueue("feishu", "peer-1", "valid")
    store = SQLiteDeliveryStore(tmp_path / "delivery.db")
    try:
        report = LegacyDeliveryMigrator(
            legacy_queue_dir=queue_dir,
            store=store,
        ).migrate()

        assert report.failed_imports == 1
        assert report.pending_imported == 1
        assert store.list_records()[0].delivery_id == f"legacy-{valid_id}"
    finally:
        store.close()


def test_store_upgrades_database_created_before_unconfirmed_state(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "delivery.db"
    SQLiteDeliveryStore(db_path).close()
    connection = sqlite3.connect(db_path)
    connection.execute("ALTER TABLE deliveries DROP COLUMN accepted_at")
    connection.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        columns = {
            row["name"] for row in reopened._connection.execute("PRAGMA table_info(deliveries)")
        }
        assert "accepted_at" in columns
    finally:
        reopened.close()
