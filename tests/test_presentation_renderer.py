from tinyclaw.contracts import (
    ContentBlock,
    ContentBlockType,
    OutboundIntent,
    OutboundTarget,
    SemanticType,
)
from tinyclaw.presentation import (
    CapabilityRegistry,
    ChannelCapability,
    OutboundRenderer,
    split_content_aware,
)


def make_intent(
    *,
    channel: str = "plain",
    blocks: tuple[ContentBlock, ...],
) -> OutboundIntent:
    return OutboundIntent(
        intent_id="intent-1",
        session_id="session-1",
        semantic_type=SemanticType.RESULT,
        target=OutboundTarget(
            channel=channel,
            account_id="account-1",
            peer_id="peer-1",
        ),
        content_blocks=blocks,
    )


def test_renderer_uses_capability_and_records_degradation():
    registry = CapabilityRegistry(
        {
            "rich": ChannelCapability(text_limit=100, markdown=True),
            "plain": ChannelCapability(text_limit=100),
        }
    )
    block = ContentBlock(
        type=ContentBlockType.MARKDOWN,
        text="**完成**：[报告](artifact://report)",
    )

    rich = OutboundRenderer(registry).render(make_intent(channel="rich", blocks=(block,)))[0]
    plain = OutboundRenderer(registry).render(make_intent(channel="plain", blocks=(block,)))[0]

    assert rich.format == "markdown"
    assert rich.text == "**完成**：[报告](artifact://report)"
    assert plain.format == "text"
    assert plain.text == "完成：报告: artifact://report"
    assert plain.metadata["degradation"] == ("markdown_to_text",)
    assert rich.semantic_snapshot.snapshot_hash == plain.semantic_snapshot.snapshot_hash


def test_renderer_falls_back_from_artifact_and_buttons_to_text():
    intent = make_intent(
        blocks=(
            ContentBlock(
                type=ContentBlockType.FILE,
                artifact_ref="artifact://run/report.json",
                metadata={"filename": "report.json"},
            ),
            ContentBlock(
                type=ContentBlockType.ACTIONS,
                metadata={
                    "actions": [
                        {"label": "确认"},
                        {"label": "取消"},
                    ]
                },
            ),
        )
    )

    rendered = OutboundRenderer().render(intent)[0]

    assert rendered.text == ("report.json: artifact://run/report.json\n\n可选操作：确认 / 取消")
    assert rendered.metadata["degradation"] == (
        "file_to_link",
        "buttons_to_text",
    )


def test_content_aware_split_reopens_and_closes_code_fences():
    text = (
        "说明\n\n```python\n"
        + "\n".join(f"print({index})" for index in range(20))
        + "\n```\n\n结束"
    )

    chunks = split_content_aware(text, 80)

    assert len(chunks) > 2
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert any(chunk.startswith("```python") for chunk in chunks)


def test_card_actions_and_attachment_use_native_metadata_or_explicit_degradation():
    blocks = (
        ContentBlock(
            type=ContentBlockType.CARD,
            text="部署即将开始",
            metadata={"title": "部署确认", "fields": {"环境": "production"}},
        ),
        ContentBlock(
            type=ContentBlockType.ACTIONS,
            metadata={"actions": [{"label": "确认", "action": "approve"}]},
        ),
        ContentBlock(
            type=ContentBlockType.FILE,
            artifact_ref="artifact://report.json",
            metadata={"filename": "report.json"},
        ),
    )
    registry = CapabilityRegistry(
        {
            "native": ChannelCapability(
                text_limit=1000,
                markdown=True,
                card=True,
                buttons=True,
                file=True,
            )
        }
    )

    native = OutboundRenderer(registry).render(make_intent(channel="native", blocks=blocks))[0]
    plain = OutboundRenderer(registry).render(make_intent(channel="plain", blocks=blocks))[0]

    assert native.format == "card"
    assert native.metadata["degradation"] == ()
    assert native.metadata["native_payload"]["cards"][0]["metadata"]["title"] == "部署确认"
    assert native.metadata["native_payload"]["actions"][0]["action"] == "approve"
    assert native.metadata["native_payload"]["attachments"][0]["artifact_ref"] == (
        "artifact://report.json"
    )
    assert plain.format == "text"
    assert plain.metadata["degradation"] == (
        "card_to_text",
        "buttons_to_text",
        "file_to_link",
    )


def test_progress_selects_update_or_milestone_delivery_mode():
    block = ContentBlock(type=ContentBlockType.TEXT, text="50%")
    registry = CapabilityRegistry({"updatable": ChannelCapability(message_update=True)})
    updatable = OutboundRenderer(registry).render(
        OutboundIntent(
            intent_id="progress-1",
            session_id="session-1",
            semantic_type=SemanticType.PROGRESS,
            target=OutboundTarget(
                channel="updatable",
                account_id="account-1",
                peer_id="peer-1",
            ),
            content_blocks=(block,),
        )
    )[0]
    plain = OutboundRenderer(registry).render(
        OutboundIntent(
            intent_id="progress-2",
            session_id="session-1",
            semantic_type=SemanticType.PROGRESS,
            target=OutboundTarget(
                channel="plain",
                account_id="account-1",
                peer_id="peer-1",
            ),
            content_blocks=(block,),
        )
    )[0]

    assert updatable.metadata["delivery_mode"] == "update"
    assert "progress_update_to_milestones" not in updatable.metadata["degradation"]
    assert plain.metadata["delivery_mode"] == "milestone"
    assert plain.metadata["degradation"] == ("progress_update_to_milestones",)


def test_content_aware_split_does_not_break_markdown_link():
    link = "[完整报告](https://example.test/reports/123)"
    chunks = split_content_aware(f"前言文字 {link} 后续说明", 50)

    assert any(link in chunk for chunk in chunks)
    assert all(("[" not in chunk) or ("](" in chunk and ")" in chunk) for chunk in chunks)


def test_default_capabilities_do_not_claim_unimplemented_native_uploads():
    registry = CapabilityRegistry()
    for channel in ("telegram", "feishu", "workwechat", "wecomcli", "dingtalk"):
        capability = registry.get(channel)
        assert capability.file is False
        assert capability.image is False
        assert capability.card is False
        assert capability.buttons is False
