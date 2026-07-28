"""Capability-aware rendering of platform-neutral outbound intents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from tinyclaw.contracts import (
    ContentBlock,
    ContentBlockType,
    OutboundIntent,
)
from tinyclaw.presentation.capability import (
    CapabilityRegistry,
    ChannelCapability,
)

_FENCE_RE = re.compile(r"(^```[^\n]*\n.*?^```\s*$)", re.MULTILINE | re.DOTALL)
_MARKDOWN_DECORATION_RE = re.compile(r"(\*\*|__|~~|(?<!`)`(?!`))")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]+\)")


@dataclass(frozen=True, kw_only=True)
class SemanticSnapshot:
    semantic_type: str
    content_blocks: tuple[Mapping[str, Any], ...]
    snapshot_hash: str

    @classmethod
    def from_intent(cls, intent: OutboundIntent) -> "SemanticSnapshot":
        blocks = tuple(block.to_dict() for block in intent.content_blocks)
        canonical = json.dumps(
            {
                "semantic_type": intent.semantic_type.value,
                "content_blocks": blocks,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            semantic_type=intent.semantic_type.value,
            content_blocks=blocks,
            snapshot_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, kw_only=True)
class RenderedMessage:
    text: str
    format: str
    chunk_index: int
    chunk_count: int
    semantic_snapshot: SemanticSnapshot
    metadata: Mapping[str, Any] = field(default_factory=dict)


class OutboundRenderer:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or CapabilityRegistry()

    def render(self, intent: OutboundIntent) -> list[RenderedMessage]:
        capability = self.registry.get(intent.target.channel)
        snapshot = SemanticSnapshot.from_intent(intent)
        text, output_format, degradation, native_payload = self._render_blocks(
            intent.content_blocks,
            capability,
        )
        delivery_mode = "send"
        if intent.semantic_type.value == "progress":
            delivery_mode = "update" if capability.progress_update else "milestone"
            if not capability.progress_update:
                degradation.append("progress_update_to_milestones")
        chunks = split_content_aware(text, capability.text_limit)
        return [
            RenderedMessage(
                text=chunk,
                format=output_format,
                chunk_index=index,
                chunk_count=len(chunks),
                semantic_snapshot=snapshot,
                metadata={
                    "degradation": tuple(degradation),
                    "semantic_type": intent.semantic_type.value,
                    "delivery_mode": delivery_mode,
                    "native_payload": native_payload if index == 0 else {},
                },
            )
            for index, chunk in enumerate(chunks)
        ]

    def _render_blocks(
        self,
        blocks: tuple[ContentBlock, ...],
        capability: ChannelCapability,
    ) -> tuple[str, str, list[str], Mapping[str, Any]]:
        rendered: list[str] = []
        degradation: list[str] = []
        has_markdown = False
        has_native_card = False
        native_payload: dict[str, list[Mapping[str, Any]]] = {
            "attachments": [],
            "actions": [],
            "cards": [],
        }
        for block in blocks:
            if block.type == ContentBlockType.TEXT:
                rendered.append(block.text or "")
            elif block.type == ContentBlockType.MARKDOWN:
                if capability.markdown:
                    rendered.append(block.text or "")
                    has_markdown = True
                else:
                    rendered.append(markdown_to_plain(block.text or ""))
                    degradation.append("markdown_to_text")
            elif block.type in (ContentBlockType.FILE, ContentBlockType.IMAGE):
                supported = (
                    capability.file if block.type == ContentBlockType.FILE else capability.image
                )
                if supported:
                    name = str(
                        block.metadata.get("filename")
                        or block.metadata.get("alt")
                        or block.type.value
                    )
                    rendered.append(name)
                    native_payload["attachments"].append(
                        {
                            "kind": block.type.value,
                            "artifact_ref": block.artifact_ref,
                            "mime_type": block.mime_type,
                            "metadata": dict(block.metadata),
                        }
                    )
                else:
                    rendered.append(self._render_artifact(block, capability))
                    degradation.append(f"{block.type.value}_to_link")
            elif block.type == ContentBlockType.LINK:
                label = block.text or str(block.metadata.get("label") or "链接")
                url = str(block.metadata.get("url") or block.artifact_ref or "")
                rendered.append(f"[{label}]({url})" if capability.markdown else f"{label}: {url}")
                has_markdown = has_markdown or capability.markdown
            elif block.type == ContentBlockType.ACTIONS:
                rendered.append(self._render_actions(block))
                raw_actions = block.metadata.get("actions")
                if capability.buttons and isinstance(raw_actions, list):
                    native_payload["actions"].extend(
                        dict(action) for action in raw_actions if isinstance(action, Mapping)
                    )
                else:
                    degradation.append("buttons_to_text")
            elif block.type == ContentBlockType.CARD:
                rendered.append(self._render_card(block, markdown=capability.markdown))
                has_markdown = has_markdown or capability.markdown
                if capability.card:
                    native_payload["cards"].append(
                        {"text": block.text, "metadata": dict(block.metadata)}
                    )
                    has_native_card = True
                else:
                    degradation.append(
                        "card_to_markdown" if capability.markdown else "card_to_text"
                    )
        text = "\n\n".join(part for part in rendered if part)
        compact_payload = {key: value for key, value in native_payload.items() if value}
        output_format = "card" if has_native_card else ("markdown" if has_markdown else "text")
        return text, output_format, degradation, compact_payload

    @staticmethod
    def _render_artifact(
        block: ContentBlock,
        capability: ChannelCapability,
    ) -> str:
        name = str(block.metadata.get("filename") or block.metadata.get("alt") or block.type.value)
        reference = block.artifact_ref or ""
        if capability.markdown:
            prefix = "!" if block.type == ContentBlockType.IMAGE else ""
            return f"{prefix}[{name}]({reference})"
        return f"{name}: {reference}"

    @staticmethod
    def _render_actions(block: ContentBlock) -> str:
        raw_actions = block.metadata.get("actions")
        if not isinstance(raw_actions, list):
            return block.text or ""
        labels = [
            str(action.get("label") or action.get("text") or "").strip()
            for action in raw_actions
            if isinstance(action, Mapping)
        ]
        visible = [label for label in labels if label]
        return "可选操作：" + " / ".join(visible) if visible else (block.text or "")

    @staticmethod
    def _render_card(block: ContentBlock, *, markdown: bool) -> str:
        title = str(block.metadata.get("title") or "").strip()
        body = str(block.text or block.metadata.get("body") or "").strip()
        raw_fields = block.metadata.get("fields")
        fields: list[tuple[str, str]] = []
        if isinstance(raw_fields, Mapping):
            fields = [(str(key), str(value)) for key, value in raw_fields.items()]
        elif isinstance(raw_fields, list):
            fields = [
                (
                    str(item.get("label") or item.get("name") or ""),
                    str(item.get("value") or ""),
                )
                for item in raw_fields
                if isinstance(item, Mapping)
            ]
        if markdown:
            parts = [f"**{title}**" if title else "", body]
            parts.extend(f"- **{label}**: {value}" for label, value in fields if label or value)
        else:
            parts = [title, body]
            parts.extend(f"{label}: {value}" for label, value in fields if label or value)
        return "\n".join(part for part in parts if part) or "卡片"


def markdown_to_plain(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1: \2", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return _MARKDOWN_DECORATION_RE.sub("", text)


def split_content_aware(text: str, limit: int) -> list[str]:
    """Split text without leaving an unterminated fenced code block."""
    if not text:
        return []
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    units = [unit for unit in _FENCE_RE.split(text) if unit]
    chunks: list[str] = []
    for unit in units:
        if unit.lstrip().startswith("```"):
            _append_fenced_code(chunks, unit.strip(), limit)
        else:
            _append_plain(chunks, unit.strip(), limit)
    return [chunk for chunk in chunks if chunk]


def _append_plain(chunks: list[str], text: str, limit: int) -> None:
    for paragraph in text.split("\n\n"):
        remaining = paragraph
        while remaining:
            room = limit
            if chunks and not chunks[-1].endswith("```"):
                separator = "\n\n"
                room = limit - len(chunks[-1]) - len(separator)
                if room > 0 and len(remaining) <= room:
                    chunks[-1] += separator + remaining
                    remaining = ""
                    continue
            cut = _safe_cut(remaining, room if room > 0 else limit)
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()


def _append_fenced_code(chunks: list[str], block: str, limit: int) -> None:
    first_newline = block.find("\n")
    opening = block[: first_newline + 1] if first_newline >= 0 else "```\n"
    body = block[len(opening) :]
    if body.endswith("```"):
        body = body[:-3].rstrip()
    overhead = len(opening) + len("\n```")
    body_limit = limit - overhead
    if body_limit <= 0:
        _append_plain(chunks, block, limit)
        return
    while body:
        cut = _safe_cut(body, body_limit)
        piece = body[:cut].rstrip()
        chunks.append(f"{opening}{piece}\n```")
        body = body[cut:].lstrip("\n")


def _safe_cut(text: str, limit: int) -> int:
    if len(text) <= limit:
        return len(text)
    candidates = [
        text.rfind("\n", 0, limit + 1),
        text.rfind(" ", 0, limit + 1),
    ]
    cut = max(candidates)
    cut = cut if cut > 0 else limit
    for match in _MARKDOWN_LINK_RE.finditer(text):
        if match.start() < cut < match.end():
            if match.start() > 0:
                return match.start()
            if match.end() <= limit:
                return match.end()
    return cut
