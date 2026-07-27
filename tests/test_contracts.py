from datetime import datetime, timedelta, timezone

import pytest

from tinyclaw.channel.base import InboundMessage
from tinyclaw.contracts import (
    ContentBlock,
    ContentBlockType,
    DeliveryRecord,
    DeliveryState,
    DeliveryTarget,
    InboundEnvelope,
    SenderIdentity,
    SessionScope,
    TaskInstance,
    TaskState,
    build_route_key,
)


def test_legacy_inbound_adapter_is_stable_and_does_not_leak_raw_payload():
    message = InboundMessage(
        text="hello",
        sender_id="user-1",
        channel="feishu",
        account_id="bot-1",
        peer_id="chat-1",
        raw={
            "header": {"event_id": "event-1", "event_type": "message"},
            "event": {"message": {"message_id": "message-1"}},
            "secret": "must-not-leak",
        },
    )

    first = message.to_envelope(raw_artifact_ref="artifact:sha256:abc")
    second = message.to_envelope(raw_artifact_ref="artifact:sha256:abc")

    assert first.event_id == "event-1"
    assert first.platform_message_id == "message-1"
    assert first.dedupe_key == second.dedupe_key
    assert first.text == "hello"
    assert first.raw_artifact_ref == "artifact:sha256:abc"
    serialized = first.to_dict()
    assert "raw" not in serialized
    assert "secret" not in str(serialized)
    assert InboundEnvelope.from_dict(serialized).to_dict() == serialized


def test_session_route_key_changes_when_scope_policy_version_changes():
    common = {
        "agent_id": "main",
        "scope_type": SessionScope.PER_ACCOUNT_CHANNEL_PEER,
        "channel": "feishu",
        "account_id": "bot-a",
        "peer_id": "user-a",
    }

    assert build_route_key(scope_version=1, **common) != build_route_key(
        scope_version=2, **common
    )


def test_linked_global_user_scope_requires_explicit_identity():
    with pytest.raises(ValueError, match="global_user_id"):
        build_route_key(
            agent_id="main",
            scope_type=SessionScope.LINKED_GLOBAL_USER,
            scope_version=1,
            channel="feishu",
            account_id="bot-a",
            peer_id="user-a",
        )


def test_completed_task_requires_result_reference():
    with pytest.raises(ValueError, match="result_ref"):
        TaskInstance(
            task_id="task-1",
            session_id="session-1",
            state=TaskState.COMPLETED,
            user_goal="ship it",
        )


def test_in_flight_delivery_requires_a_persistent_lease():
    base = {
        "delivery_id": "delivery-1",
        "intent_id": "intent-1",
        "session_id": "session-1",
        "lane_key": "session:session-1",
        "sequence": 0,
        "target": DeliveryTarget(
            channel="feishu", account_id="bot-a", peer_id="user-a"
        ),
        "payload": {"text": "done"},
        "idempotency_key": "delivery-1",
        "state": DeliveryState.IN_FLIGHT,
    }

    with pytest.raises(ValueError, match="lease"):
        DeliveryRecord(**base)

    record = DeliveryRecord(
        **base,
        lease_owner="worker-1",
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert record.to_dict()["state"] == "in_flight"


def test_inbound_contract_rejects_an_empty_body():
    with pytest.raises(ValueError, match="content_blocks or attachments"):
        InboundEnvelope(
            event_id="event-1",
            channel="cli",
            account_id="local",
            peer_id="user",
            sender=SenderIdentity(platform_user_id="user"),
            dedupe_key="dedupe-1",
            content_blocks=(),
        )


def test_text_content_block_requires_text():
    with pytest.raises(ValueError, match="requires text"):
        ContentBlock(type=ContentBlockType.TEXT)
