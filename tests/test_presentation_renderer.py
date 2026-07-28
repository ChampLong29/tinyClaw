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
