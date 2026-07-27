"""Identity links and explicitly versioned session boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now
from tinyclaw.contracts.versions import GLOBAL_IDENTITY_V1, SESSION_DESCRIPTOR_V1


class IdentityStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    MERGED = "merged"


class SessionScope(str, Enum):
    PER_PEER = "per-peer"
    PER_CHANNEL_PEER = "per-channel-peer"
    PER_ACCOUNT_CHANNEL_PEER = "per-account-channel-peer"
    LINKED_GLOBAL_USER = "linked-global-user"


@dataclass(frozen=True, kw_only=True)
class ChannelIdentityLink:
    channel: str
    account_id: str
    platform_user_id: str
    linked_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.platform_user_id, "platform_user_id"),
        ):
            require_text(value, name)
        object.__setattr__(self, "linked_at", parse_datetime(self.linked_at, default_now=True))


@dataclass(frozen=True, kw_only=True)
class GlobalIdentity:
    global_user_id: str
    channel_links: tuple[ChannelIdentityLink, ...] = ()
    status: IdentityStatus = IdentityStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    revision: int = 1
    schema_version: str = GLOBAL_IDENTITY_V1

    def __post_init__(self) -> None:
        require_text(self.global_user_id, "global_user_id")
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        if self.schema_version != GLOBAL_IDENTITY_V1:
            raise ValueError(f"unsupported identity schema: {self.schema_version}")
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, kw_only=True)
class SessionDescriptor:
    session_id: str
    scope_type: SessionScope
    scope_version: int
    route_key: str
    channel: str
    account_id: str
    peer_id: str
    thread_id: str | None = None
    global_user_id: str | None = None
    active_task_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    decision_reason: str = ""
    schema_version: str = SESSION_DESCRIPTOR_V1

    def __post_init__(self) -> None:
        for value, name in (
            (self.session_id, "session_id"),
            (self.route_key, "route_key"),
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.peer_id, "peer_id"),
        ):
            require_text(value, name)
        if self.scope_version < 1:
            raise ValueError("scope_version must be at least 1")
        if self.scope_type == SessionScope.LINKED_GLOBAL_USER and not self.global_user_id:
            raise ValueError("linked-global-user scope requires global_user_id")
        if self.schema_version != SESSION_DESCRIPTOR_V1:
            raise ValueError(f"unsupported session schema: {self.schema_version}")
        object.__setattr__(self, "created_at", parse_datetime(self.created_at, default_now=True))
        object.__setattr__(self, "updated_at", parse_datetime(self.updated_at, default_now=True))

    @property
    def lane_key(self) -> str:
        return f"session:{self.session_id}"

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


def build_route_key(
    *,
    agent_id: str,
    scope_type: SessionScope,
    scope_version: int,
    channel: str,
    account_id: str,
    peer_id: str,
    global_user_id: str | None = None,
) -> str:
    """Build a non-secret, stable route key with an explicit policy version."""
    if scope_version < 1:
        raise ValueError("scope_version must be at least 1")
    if scope_type == SessionScope.PER_PEER:
        boundary = peer_id
    elif scope_type == SessionScope.PER_CHANNEL_PEER:
        boundary = f"{channel}:{peer_id}"
    elif scope_type == SessionScope.PER_ACCOUNT_CHANNEL_PEER:
        boundary = f"{account_id}:{channel}:{peer_id}"
    else:
        if not global_user_id:
            raise ValueError("linked-global-user scope requires global_user_id")
        boundary = global_user_id
    digest = hashlib.sha256(boundary.encode("utf-8")).hexdigest()[:24]
    return f"agent:{agent_id}:scope:{scope_type.value}:v{scope_version}:{digest}"
