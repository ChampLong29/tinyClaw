"""Lease-based worker for ordered durable deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from tinyclaw.contracts._common import parse_datetime, require_text
from tinyclaw.contracts.delivery import DeliveryRecord
from tinyclaw.delivery.retry import DeliveryRetryPolicy
from tinyclaw.delivery.store import SQLiteDeliveryStore


@dataclass(frozen=True, kw_only=True)
class DeliveryReceipt:
    platform_message_id: str

    def __post_init__(self) -> None:
        require_text(self.platform_message_id, "platform_message_id")


class DeliverySender(Protocol):
    def send(self, record: DeliveryRecord) -> DeliveryReceipt:
        ...


class LeaseDeliveryWorker:
    def __init__(
        self,
        *,
        store: SQLiteDeliveryStore,
        sender: DeliverySender,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_policy: DeliveryRetryPolicy | None = None,
    ) -> None:
        self.store = store
        self.sender = sender
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_policy = retry_policy or DeliveryRetryPolicy()
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
            self.total_attempted += 1
            try:
                receipt = self.sender.send(record)
                updated = self.store.acknowledge(
                    record.delivery_id,
                    worker_id=self.worker_id,
                    platform_message_id=receipt.platform_message_id,
                    acked_at=current,
                )
                self.total_acked += 1
            except Exception as exc:
                delay = self.retry_policy.delay(record.attempt_count)
                updated = self.store.record_failure(
                    record.delivery_id,
                    worker_id=self.worker_id,
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    next_retry_at=current + delay,
                    max_attempts=self.retry_policy.max_attempts,
                )
                self.total_failed += 1
            results.append(updated)
        return results
