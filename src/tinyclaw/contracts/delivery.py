"""Durable, ordered delivery record contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now
from tinyclaw.contracts.versions import DELIVERY_RECORD_V1


class DeliveryState(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    ACKED = "acked"
    ACCEPTED_UNCONFIRMED = "accepted_unconfirmed"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, kw_only=True)
class DeliveryTarget:
    channel: str
    account_id: str
    peer_id: str
    thread_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.peer_id, "peer_id"),
        ):
            require_text(value, name)


@dataclass(frozen=True, kw_only=True)
class DeliveryRecord:
    delivery_id: str
    intent_id: str
    session_id: str
    lane_key: str
    sequence: int
    target: DeliveryTarget
    payload: Mapping[str, Any]
    idempotency_key: str
    state: DeliveryState = DeliveryState.PENDING
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempt_count: int = 0
    retry_count: int = 0
    next_retry_at: datetime | None = None
    platform_message_id: str | None = None
    last_error: Mapping[str, Any] | None = None
    created_at: datetime = field(default_factory=utc_now)
    acked_at: datetime | None = None
    accepted_at: datetime | None = None
    schema_version: str = DELIVERY_RECORD_V1

    def __post_init__(self) -> None:
        for value, name in (
            (self.delivery_id, "delivery_id"),
            (self.intent_id, "intent_id"),
            (self.session_id, "session_id"),
            (self.lane_key, "lane_key"),
            (self.idempotency_key, "idempotency_key"),
        ):
            require_text(value, name)
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")
        if self.retry_count < 0:
            raise ValueError("retry_count cannot be negative")
        if self.schema_version != DELIVERY_RECORD_V1:
            raise ValueError(f"unsupported delivery schema: {self.schema_version}")
        if self.state == DeliveryState.IN_FLIGHT and not (self.lease_owner and self.lease_until):
            raise ValueError("in_flight delivery requires lease_owner and lease_until")
        if self.state == DeliveryState.ACKED and self.acked_at is None:
            raise ValueError("acked delivery requires acked_at")
        if self.state == DeliveryState.ACCEPTED_UNCONFIRMED and self.accepted_at is None:
            raise ValueError("accepted_unconfirmed delivery requires accepted_at")
        object.__setattr__(self, "lease_until", parse_datetime(self.lease_until))
        object.__setattr__(self, "next_retry_at", parse_datetime(self.next_retry_at))
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))
        object.__setattr__(self, "acked_at", parse_datetime(self.acked_at))
        object.__setattr__(self, "accepted_at", parse_datetime(self.accepted_at))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)
