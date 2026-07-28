"""Production compatibility facade for the transactional delivery store."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tinyclaw.contracts import (
    ContentBlock,
    ContentBlockType,
    OutboundIntent,
    OutboundTarget,
    SemanticType,
)
from tinyclaw.contracts.delivery import DeliveryRecord, DeliveryState, DeliveryTarget
from tinyclaw.delivery.queue import DeliveryQueue, QueuedDelivery
from tinyclaw.delivery.store import (
    DeliveryNotFoundError,
    DeliveryStoreError,
    SQLiteDeliveryStore,
)
from tinyclaw.delivery.worker import DeliveryReceipt, LeaseDeliveryWorker
from tinyclaw.observability import TraceRecorder
from tinyclaw.presentation import OutboundRenderer

DeliverFunction = Callable[[str, str, str, dict[str, Any] | None], DeliveryReceipt]


@dataclass(frozen=True, kw_only=True)
class LegacyMigrationReport:
    pending_imported: int = 0
    dead_letters_imported: int = 0
    already_imported: int = 0
    failed_imports: int = 0

    @property
    def total_seen(self) -> int:
        return (
            self.pending_imported
            + self.dead_letters_imported
            + self.already_imported
            + self.failed_imports
        )


class DurableDeliveryQueue:
    """Keep the legacy enqueue call shape while persisting to SQLite."""

    def __init__(
        self,
        store: SQLiteDeliveryStore,
        *,
        renderer: OutboundRenderer | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.store = store
        self.renderer = renderer or OutboundRenderer()
        self.trace_recorder = trace_recorder

    def enqueue(
        self,
        channel: str,
        to: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        metadata = dict(meta or {})
        account_id = str(metadata.get("account_id") or channel)
        lane_key = str(metadata.get("lane_key") or f"{channel}:{account_id}:{to}")
        session_id = str(metadata.get("session_id") or lane_key)
        intent_id = str(metadata.get("intent_id") or uuid.uuid4().hex)
        semantic_value = str(metadata.get("semantic_type") or SemanticType.RESULT.value)
        try:
            semantic_type = SemanticType(semantic_value)
        except ValueError:
            semantic_type = SemanticType.RESULT
        intent = OutboundIntent(
            intent_id=intent_id,
            session_id=session_id,
            semantic_type=semantic_type,
            target=OutboundTarget(
                channel=channel,
                account_id=account_id,
                peer_id=to,
            ),
            content_blocks=(ContentBlock(type=ContentBlockType.TEXT, text=text),),
            presentation_hints=metadata,
            dedupe_key=metadata.get("dedupe_key"),
        )
        delivery_ids = self.enqueue_intent(intent, meta=metadata)
        return delivery_ids[0]

    def enqueue_intent(
        self,
        intent: OutboundIntent,
        *,
        meta: dict[str, Any] | None = None,
    ) -> list[str]:
        metadata = dict(meta or {})
        metadata.setdefault("intent_id", intent.intent_id)
        metadata.setdefault("semantic_type", intent.semantic_type.value)
        lane_key = str(
            metadata.get("lane_key")
            or (f"{intent.target.channel}:{intent.target.account_id}:{intent.target.peer_id}")
        )
        rendered = self.renderer.render(intent)
        if not rendered:
            raise ValueError("outbound intent produced no renderable messages")
        self._trace(
            event_type="outbound_intent_rendered",
            session_id=intent.session_id,
            task_id=intent.task_id,
            payload={
                "intent_id": intent.intent_id,
                "semantic_type": intent.semantic_type.value,
                "channel": intent.target.channel,
                "chunk_count": len(rendered),
                "snapshot_hash": rendered[0].semantic_snapshot.snapshot_hash,
            },
        )

        delivery_ids: list[str] = []
        base_idempotency_key = str(
            metadata.get("idempotency_key") or intent.dedupe_key or f"intent:{intent.intent_id}"
        )
        for message in rendered:
            delivery_ids.append(
                self._enqueue_rendered(
                    intent=intent,
                    message=message,
                    metadata=metadata,
                    lane_key=lane_key,
                    base_idempotency_key=base_idempotency_key,
                )
            )
        return delivery_ids

    def _enqueue_rendered(
        self,
        *,
        intent: OutboundIntent,
        message: Any,
        metadata: dict[str, Any],
        lane_key: str,
        base_idempotency_key: str,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        record = self.store.enqueue(
            delivery_id=delivery_id,
            intent_id=intent.intent_id,
            session_id=intent.session_id,
            lane_key=lane_key,
            target=DeliveryTarget(
                channel=intent.target.channel,
                account_id=intent.target.account_id,
                peer_id=intent.target.peer_id,
                thread_id=intent.target.thread_id,
            ),
            payload={
                "text": message.text,
                "format": message.format,
                "meta": {
                    **metadata,
                    "chunk_index": message.chunk_index,
                    "chunk_count": message.chunk_count,
                },
                "semantic_snapshot": {
                    "semantic_type": message.semantic_snapshot.semantic_type,
                    "content_blocks": message.semantic_snapshot.content_blocks,
                    "snapshot_hash": message.semantic_snapshot.snapshot_hash,
                },
                "render_metadata": dict(message.metadata),
            },
            idempotency_key=(f"{base_idempotency_key}:{message.chunk_index}"),
        )
        deduplicated = record.delivery_id != delivery_id
        self._trace(
            event_type=("delivery_enqueue_deduplicated" if deduplicated else "delivery_enqueued"),
            session_id=intent.session_id,
            task_id=intent.task_id,
            payload={
                "delivery_id": record.delivery_id,
                "intent_id": record.intent_id,
                "lane_key": record.lane_key,
                "sequence": record.sequence,
                "idempotency_key": record.idempotency_key,
                "state": record.state.value,
                "deduplicated": deduplicated,
            },
        )
        return record.delivery_id

    def _trace(
        self,
        *,
        event_type: str,
        session_id: str,
        task_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        if self.trace_recorder is None:
            return
        try:
            self.trace_recorder.record(
                event_type=event_type,
                producer="delivery",
                producer_version="delivery-v1",
                session_id=session_id,
                task_id=task_id,
                payload=payload,
            )
        except Exception:
            pass


class LegacyDeliveryMigrator:
    """Idempotently copy file-WAL records into SQLite without deleting sources."""

    def __init__(
        self,
        *,
        legacy_queue_dir: Path,
        store: SQLiteDeliveryStore,
    ) -> None:
        self.legacy_queue = DeliveryQueue(legacy_queue_dir)
        self.store = store

    def migrate(self) -> LegacyMigrationReport:
        pending_imported = 0
        dead_letters_imported = 0
        already_imported = 0
        failed_imports = 0
        entries = [
            *((item, False) for item in self.legacy_queue.load_pending()),
            *((item, True) for item in self.legacy_queue.load_failed()),
        ]
        entries.sort(key=lambda item: (item[0].enqueued_at, item[0].id))
        for entry, dead_letter in entries:
            try:
                existed = self._already_imported(entry)
                record = self._import_entry(entry)
                if dead_letter and record.state == DeliveryState.PENDING:
                    record = self.store.import_dead_letter(
                        record.delivery_id,
                        error={
                            "type": "LegacyDeliveryFailure",
                            "message": entry.last_error or "legacy dead letter",
                            "retry_count": entry.retry_count,
                        },
                    )
            except (DeliveryStoreError, OverflowError, TypeError, ValueError):
                failed_imports += 1
                continue
            if existed:
                already_imported += 1
            elif record.state == DeliveryState.DEAD_LETTER:
                dead_letters_imported += 1
            else:
                pending_imported += 1
        return LegacyMigrationReport(
            pending_imported=pending_imported,
            dead_letters_imported=dead_letters_imported,
            already_imported=already_imported,
            failed_imports=failed_imports,
        )

    def _already_imported(self, entry: QueuedDelivery) -> bool:
        try:
            self.store.get(self._delivery_id(entry))
        except DeliveryNotFoundError:
            return False
        return True

    def _import_entry(self, entry: QueuedDelivery) -> DeliveryRecord:
        metadata = dict(entry.meta)
        account_id = str(metadata.get("account_id") or entry.channel)
        lane_key = str(metadata.get("lane_key") or f"{entry.channel}:{account_id}:{entry.to}")
        timestamp = max(float(entry.enqueued_at), 0.0)
        return self.store.enqueue(
            delivery_id=self._delivery_id(entry),
            intent_id=str(metadata.get("intent_id") or f"legacy:{entry.id}"),
            session_id=str(metadata.get("session_id") or lane_key),
            lane_key=lane_key,
            target=DeliveryTarget(
                channel=entry.channel,
                account_id=account_id,
                peer_id=entry.to,
            ),
            payload={"text": entry.text, "meta": metadata},
            idempotency_key=f"legacy:{entry.id}",
            created_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        )

    @staticmethod
    def _delivery_id(entry: QueuedDelivery) -> str:
        return f"legacy-{entry.id}"


class _CallbackSender:
    def __init__(self, deliver_fn: DeliverFunction) -> None:
        self.deliver_fn = deliver_fn

    def send(self, record: DeliveryRecord) -> DeliveryReceipt:
        metadata = record.payload.get("meta")
        meta = dict(metadata) if isinstance(metadata, dict) else {}
        return self.deliver_fn(
            record.target.channel,
            record.target.peer_id,
            str(record.payload.get("text", "")),
            meta,
        )


class DurableDeliveryRunner:
    """Background runner backed by leases and transactional FIFO lanes."""

    def __init__(
        self,
        queue: DurableDeliveryQueue,
        deliver_fn: DeliverFunction,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.worker = LeaseDeliveryWorker(
            store=queue.store,
            sender=_CallbackSender(deliver_fn),
            worker_id=worker_id or f"delivery-{uuid.uuid4().hex[:12]}",
            trace_recorder=queue.trace_recorder,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._background_loop,
            daemon=True,
            name="durable-delivery-runner",
        )
        self._thread.start()

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.worker.run_once()
            except Exception:
                pass
            self._stop_event.wait(timeout=1.0)

    def stop(self) -> bool:
        """Request shutdown and report whether the worker fully stopped."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        return self._thread is None or not self._thread.is_alive()

    def get_stats(self) -> dict[str, int]:
        records = self.queue.store.list_records()
        counts = {state: 0 for state in DeliveryState}
        for record in records:
            counts[record.state] += 1
        return {
            "pending": counts[DeliveryState.PENDING] + counts[DeliveryState.RETRY_WAIT],
            "in_flight": counts[DeliveryState.IN_FLIGHT],
            "failed": counts[DeliveryState.DEAD_LETTER],
            "total_attempted": self.worker.total_attempted,
            "delivered": counts[DeliveryState.ACKED] + counts[DeliveryState.ACCEPTED_UNCONFIRMED],
            "acked": counts[DeliveryState.ACKED],
            "accepted_unconfirmed": counts[DeliveryState.ACCEPTED_UNCONFIRMED],
        }
