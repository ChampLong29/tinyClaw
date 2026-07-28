"""Lease-based worker for ordered durable deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from tinyclaw.contracts._common import parse_datetime, require_text
from tinyclaw.contracts.delivery import DeliveryRecord
from tinyclaw.delivery.retry import DeliveryRetryPolicy
from tinyclaw.delivery.store import SQLiteDeliveryStore
from tinyclaw.observability import TraceRecorder


@dataclass(frozen=True, kw_only=True)
class DeliveryReceipt:
    platform_message_id: str | None = None
    confirmed: bool | None = None

    def __post_init__(self) -> None:
        confirmed = (
            self.platform_message_id is not None if self.confirmed is None else self.confirmed
        )
        object.__setattr__(self, "confirmed", confirmed)
        if confirmed:
            require_text(self.platform_message_id, "platform_message_id")
        elif self.platform_message_id is not None:
            raise ValueError("unconfirmed receipt cannot have platform_message_id")


class DeliverySender(Protocol):
    def send(self, record: DeliveryRecord) -> DeliveryReceipt: ...


class DeliveryFaultPoint(str, Enum):
    AFTER_CLAIM = "after_claim"
    AFTER_SEND_BEFORE_SETTLE = "after_send_before_settle"
    AFTER_SETTLE = "after_settle"


class InjectedDeliveryCrash(BaseException):
    """Test-only crash signal that deliberately bypasses delivery error handling."""


class DeliveryFaultInjector(Protocol):
    def __call__(
        self,
        point: DeliveryFaultPoint,
        record: DeliveryRecord,
        context: dict[str, Any],
    ) -> None: ...


class LeaseDeliveryWorker:
    def __init__(
        self,
        *,
        store: SQLiteDeliveryStore,
        sender: DeliverySender,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_policy: DeliveryRetryPolicy | None = None,
        trace_recorder: TraceRecorder | None = None,
        fault_injector: DeliveryFaultInjector | None = None,
    ) -> None:
        self.store = store
        self.sender = sender
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_policy = retry_policy or DeliveryRetryPolicy()
        self.trace_recorder = trace_recorder
        self.fault_injector = fault_injector
        self.total_attempted = 0
        self.total_acked = 0
        self.total_failed = 0

    def run_once(
        self,
        *,
        limit: int = 100,
        now: datetime | str | None = None,
    ) -> list[DeliveryRecord]:
        current = parse_datetime(now, default_now=True)
        claimed = self.store.claim_ready(
            worker_id=self.worker_id,
            lease_duration=self.lease_duration,
            limit=limit,
            now=current,
        )
        results: list[DeliveryRecord] = []
        for record in claimed:
            self._inject(DeliveryFaultPoint.AFTER_CLAIM, record)
            self.total_attempted += 1
            self._trace("delivery_attempted", record)
            try:
                receipt = self.sender.send(record)
                self._inject(
                    DeliveryFaultPoint.AFTER_SEND_BEFORE_SETTLE,
                    record,
                    receipt=receipt,
                )
                settled_at = current if now is not None else parse_datetime(None, default_now=True)
                if receipt.confirmed:
                    updated = self.store.acknowledge(
                        record.delivery_id,
                        worker_id=self.worker_id,
                        platform_message_id=receipt.platform_message_id or "",
                        acked_at=settled_at,
                    )
                    self.total_acked += 1
                    self._trace("delivery_acked", updated)
                else:
                    updated = self.store.mark_accepted_unconfirmed(
                        record.delivery_id,
                        worker_id=self.worker_id,
                        accepted_at=settled_at,
                    )
                    self._trace("delivery_accepted_unconfirmed", updated)
            except Exception as exc:
                settled_at = current if now is not None else parse_datetime(None, default_now=True)
                delay = self.retry_policy.delay(record.attempt_count)
                updated = self.store.record_failure(
                    record.delivery_id,
                    worker_id=self.worker_id,
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    next_retry_at=settled_at + delay,
                    max_attempts=self.retry_policy.max_attempts,
                )
                self.total_failed += 1
                self._trace(
                    "delivery_failed",
                    updated,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            self._inject(DeliveryFaultPoint.AFTER_SETTLE, updated)
            results.append(updated)
        return results

    def _inject(
        self,
        point: DeliveryFaultPoint,
        record: DeliveryRecord,
        **context: Any,
    ) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point, record, context)

    def _trace(
        self,
        event_type: str,
        record: DeliveryRecord,
        *,
        error: dict[str, str] | None = None,
    ) -> None:
        if self.trace_recorder is None:
            return
        metadata = record.payload.get("meta")
        task_id = str(metadata.get("task_id") or "") or None if isinstance(metadata, dict) else None
        payload = {
            "delivery_id": record.delivery_id,
            "intent_id": record.intent_id,
            "lane_key": record.lane_key,
            "sequence": record.sequence,
            "attempt_count": record.attempt_count,
            "state": record.state.value,
            "idempotency_key": record.idempotency_key,
            "delivery_semantics": (
                metadata.get("delivery_semantics", "at_least_once")
                if isinstance(metadata, dict)
                else "at_least_once"
            ),
        }
        if error is not None:
            payload["error"] = error
        try:
            self.trace_recorder.record(
                event_type=event_type,
                producer="delivery-worker",
                producer_version="delivery-v1",
                session_id=record.session_id,
                task_id=task_id,
                payload=payload,
            )
        except Exception:
            pass
