from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tinyclaw.contracts.identity import IdentityStatus, SessionScope
from tinyclaw.identity import (
    IdentityLinkConflictError,
    IdentitySessionResolver,
    SessionPolicy,
    SQLiteIdentityStore,
)


def _resolver(tmp_path: Path):
    store = SQLiteIdentityStore(tmp_path / "identity.db")
    return store, IdentitySessionResolver(store)


def test_first_seen_identity_is_stable_and_account_isolated(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:
        policy = SessionPolicy(scope_type=SessionScope.PER_ACCOUNT_CHANNEL_PEER)
        first = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="feishu",
            account_id="bot-a",
            peer_id="chat-1",
            platform_user_id="user-1",
        )
        repeated = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="feishu",
            account_id="bot-a",
            peer_id="chat-1",
            platform_user_id="user-1",
        )
        other_account = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="feishu",
            account_id="bot-b",
            peer_id="chat-1",
            platform_user_id="user-1",
        )

        assert first.identity_created is True
        assert repeated.identity_created is False
        assert first.identity.global_user_id == repeated.identity.global_user_id
        assert first.session.session_id == repeated.session.session_id
        assert first.identity.global_user_id != other_account.identity.global_user_id
        assert first.session.session_id != other_account.session.session_id
    finally:
        store.close()


def test_cross_channel_sharing_requires_explicit_link(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:
        policy = SessionPolicy(scope_type=SessionScope.LINKED_GLOBAL_USER)
        feishu = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="feishu",
            account_id="bot-a",
            peer_id="chat-a",
            platform_user_id="open-id-a",
        )
        resolver.identity.link_identity(
            feishu.identity.global_user_id,
            channel="telegram",
            account_id="bot-t",
            platform_user_id="telegram-user",
            actor="admin",
            reason="verified account ownership",
        )
        telegram = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="telegram",
            account_id="bot-t",
            peer_id="chat-t",
            platform_user_id="telegram-user",
        )

        assert telegram.identity.global_user_id == feishu.identity.global_user_id
        assert telegram.session.session_id == feishu.session.session_id
        events = store.list_audit_events(feishu.identity.global_user_id)
        assert [event.event_type for event in events] == [
            "identity_created",
            "identity_linked",
        ]
        assert [event.sequence for event in events] == [0, 1]
    finally:
        store.close()


def test_link_conflict_cannot_silently_reassign_identity(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:
        first = resolver.resolve(
            agent_id="main",
            policy=SessionPolicy(),
            channel="feishu",
            account_id="bot-a",
            peer_id="chat-a",
            platform_user_id="user-a",
        )
        second = resolver.resolve(
            agent_id="main",
            policy=SessionPolicy(),
            channel="telegram",
            account_id="bot-t",
            peer_id="chat-t",
            platform_user_id="user-t",
        )

        with pytest.raises(IdentityLinkConflictError):
            resolver.identity.link_identity(
                first.identity.global_user_id,
                channel="telegram",
                account_id="bot-t",
                platform_user_id="user-t",
                actor="admin",
                reason="attempted reassignment",
            )
        assert (
            store.find_by_link(
                channel="telegram",
                account_id="bot-t",
                platform_user_id="user-t",
            ).global_user_id
            == second.identity.global_user_id
        )
    finally:
        store.close()


def test_scope_version_creates_a_new_session_without_overwriting_history(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:
        common = {
            "agent_id": "main",
            "channel": "workwechat",
            "account_id": "corp-a",
            "peer_id": "user:1",
            "platform_user_id": "user-1",
        }
        version_one = resolver.resolve(
            policy=SessionPolicy(scope_version=1),
            **common,
        )
        version_two = resolver.resolve(
            policy=SessionPolicy(scope_version=2),
            **common,
        )

        assert version_one.identity.global_user_id == version_two.identity.global_user_id
        assert version_one.session.session_id != version_two.session.session_id
        assert ":v1:" in version_one.session.route_key
        assert ":v2:" in version_two.session.route_key
    finally:
        store.close()


def test_explicit_merge_moves_links_but_preserves_old_session_record(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:
        policy = SessionPolicy(scope_type=SessionScope.LINKED_GLOBAL_USER)
        source = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="telegram",
            account_id="bot-t",
            peer_id="chat-t",
            platform_user_id="user-t",
        )
        target = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="feishu",
            account_id="bot-f",
            peer_id="chat-f",
            platform_user_id="user-f",
        )
        old_source_session = source.session.session_id

        resolver.identity.merge_identities(
            source.identity.global_user_id,
            target.identity.global_user_id,
            actor="admin",
            reason="verified same person",
        )
        resolved_again = resolver.resolve(
            agent_id="main",
            policy=policy,
            channel="telegram",
            account_id="bot-t",
            peer_id="chat-t",
            platform_user_id="user-t",
        )

        assert resolved_again.identity.global_user_id == target.identity.global_user_id
        assert resolved_again.session.session_id == target.session.session_id
        assert resolved_again.session.session_id != old_source_session
        assert store.get_identity(source.identity.global_user_id).status == IdentityStatus.MERGED
        assert store.list_audit_events(source.identity.global_user_id)[-1].event_type == (
            "identity_merged"
        )
        assert store.list_audit_events(target.identity.global_user_id)[-1].event_type == (
            "identity_merge_received"
        )
    finally:
        store.close()


def test_identity_and_session_resolution_survive_restart(tmp_path: Path):
    db_path = tmp_path / "identity.db"
    first_store = SQLiteIdentityStore(db_path)
    first_resolver = IdentitySessionResolver(first_store)
    first = first_resolver.resolve(
        agent_id="main",
        policy=SessionPolicy(),
        channel="telegram",
        account_id="bot-a",
        peer_id="chat-a",
        platform_user_id="user-a",
    )
    first_store.close()

    second_store = SQLiteIdentityStore(db_path)
    try:
        second = IdentitySessionResolver(second_store).resolve(
            agent_id="main",
            policy=SessionPolicy(),
            channel="telegram",
            account_id="bot-a",
            peer_id="chat-a",
            platform_user_id="user-a",
        )
        assert second.identity.global_user_id == first.identity.global_user_id
        assert second.session.session_id == first.session.session_id
        assert second.identity_created is False
        assert second.session_created is False
    finally:
        second_store.close()


def test_concurrent_first_seen_resolution_creates_one_identity(tmp_path: Path):
    store, resolver = _resolver(tmp_path)
    try:

        def resolve_once():
            return resolver.resolve(
                agent_id="main",
                policy=SessionPolicy(),
                channel="feishu",
                account_id="bot-a",
                peer_id="chat-a",
                platform_user_id="user-a",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: resolve_once(), range(2)))

        assert sum(result.identity_created for result in results) == 1
        assert results[0].identity.global_user_id == results[1].identity.global_user_id
        assert results[0].session.session_id == results[1].session.session_id
    finally:
        store.close()
