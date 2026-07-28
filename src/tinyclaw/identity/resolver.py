"""Persistent identity and explicitly versioned session resolution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tinyclaw.contracts._common import require_text
from tinyclaw.contracts.identity import (
    ChannelIdentityLink,
    GlobalIdentity,
    SessionDescriptor,
    SessionScope,
    build_route_key,
)
from tinyclaw.identity.store import SQLiteIdentityStore


@dataclass(frozen=True, kw_only=True)
class SessionPolicy:
    scope_type: SessionScope = SessionScope.PER_ACCOUNT_CHANNEL_PEER
    scope_version: int = 1

    def __post_init__(self) -> None:
        if self.scope_version < 1:
            raise ValueError("scope_version must be at least 1")

    @classmethod
    def from_values(cls, scope_type: str, scope_version: int) -> "SessionPolicy":
        return cls(
            scope_type=SessionScope(scope_type),
            scope_version=scope_version,
        )


@dataclass(frozen=True, kw_only=True)
class IdentitySessionResolution:
    identity: GlobalIdentity
    session: SessionDescriptor
    identity_created: bool
    session_created: bool


class IdentityResolver:
    def __init__(self, store: SQLiteIdentityStore) -> None:
        self.store = store

    def resolve_identity(
        self,
        *,
        channel: str,
        account_id: str,
        platform_user_id: str,
    ) -> tuple[GlobalIdentity, bool]:
        for value, name in (
            (channel, "channel"),
            (account_id, "account_id"),
            (platform_user_id, "platform_user_id"),
        ):
            require_text(value, name)
        existing = self.store.find_by_link(
            channel=channel,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
        if existing:
            return existing, False
        digest = hashlib.sha256(
            f"{channel}\x1f{account_id}\x1f{platform_user_id}".encode("utf-8")
        ).hexdigest()[:24]
        identity, created = self.store.create_with_link(
            GlobalIdentity(global_user_id=f"user:{digest}"),
            ChannelIdentityLink(
                channel=channel,
                account_id=account_id,
                platform_user_id=platform_user_id,
                metadata={"created_by": "automatic-platform-boundary"},
            ),
            actor="identity-resolver",
            reason="first_seen",
        )
        return identity, created

    def link_identity(
        self,
        global_user_id: str,
        *,
        channel: str,
        account_id: str,
        platform_user_id: str,
        actor: str,
        reason: str,
    ) -> GlobalIdentity:
        return self.store.link(
            global_user_id,
            ChannelIdentityLink(
                channel=channel,
                account_id=account_id,
                platform_user_id=platform_user_id,
                metadata={"link_reason": reason},
            ),
            actor=actor,
            reason=reason,
        )

    def merge_identities(
        self,
        source_global_user_id: str,
        target_global_user_id: str,
        *,
        actor: str,
        reason: str,
    ) -> GlobalIdentity:
        return self.store.merge(
            source_global_user_id,
            target_global_user_id,
            actor=actor,
            reason=reason,
        )


class SessionResolver:
    def __init__(self, store: SQLiteIdentityStore) -> None:
        self.store = store

    def resolve(
        self,
        *,
        agent_id: str,
        identity: GlobalIdentity,
        policy: SessionPolicy,
        channel: str,
        account_id: str,
        peer_id: str,
        thread_id: str | None = None,
    ) -> tuple[SessionDescriptor, bool]:
        route_key = build_route_key(
            agent_id=agent_id,
            scope_type=policy.scope_type,
            scope_version=policy.scope_version,
            channel=channel,
            account_id=account_id,
            peer_id=peer_id,
            global_user_id=identity.global_user_id,
        )
        decision_reason = (
            f"explicit_identity_link:{identity.global_user_id}"
            if policy.scope_type == SessionScope.LINKED_GLOBAL_USER
            else f"session_scope:{policy.scope_type.value}:v{policy.scope_version}"
        )
        return self.store.resolve_session(
            SessionDescriptor(
                session_id=route_key,
                route_key=route_key,
                scope_type=policy.scope_type,
                scope_version=policy.scope_version,
                channel=channel,
                account_id=account_id,
                peer_id=peer_id,
                thread_id=thread_id,
                global_user_id=identity.global_user_id,
                decision_reason=decision_reason,
            )
        )


class IdentitySessionResolver:
    def __init__(self, store: SQLiteIdentityStore) -> None:
        self.identity = IdentityResolver(store)
        self.session = SessionResolver(store)

    def resolve(
        self,
        *,
        agent_id: str,
        policy: SessionPolicy,
        channel: str,
        account_id: str,
        peer_id: str,
        platform_user_id: str,
        thread_id: str | None = None,
    ) -> IdentitySessionResolution:
        identity, identity_created = self.identity.resolve_identity(
            channel=channel,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
        session, session_created = self.session.resolve(
            agent_id=agent_id,
            identity=identity,
            policy=policy,
            channel=channel,
            account_id=account_id,
            peer_id=peer_id,
            thread_id=thread_id,
        )
        return IdentitySessionResolution(
            identity=identity,
            session=session,
            identity_created=identity_created,
            session_created=session_created,
        )
