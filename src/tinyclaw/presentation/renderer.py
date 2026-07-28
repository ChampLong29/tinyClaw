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
        text, output_format, degradation = self._render_blocks(
            intent.content_blocks,
            capability,
        )
        chunks = split_content_aware(text, capability.text_limit)
        return [
            RenderedMessage(
                text=chunk,
                format=output_format,
                chunk_index=index,
                chunk_count=len(chunks),
                semantic_snapshot=snapshot,
                metadata={
                    "degradation": degradation,
                    "semantic_type": intent.semantic_type.value,
                },
            )
            for index, chunk in enumerate(chunks)
        ]

    def _render_blocks(
        self,
        blocks: tuple[ContentBlock, ...],
        capability: ChannelCapability,
    ) -> tuple[str, str, tuple[str, ...]]:
        rendered: list[str] = []
        degradation: list[str] = []
        has_markdown = False
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
                rendered.append(self._render_artifact(block, capability))
                supported = (
                    capability.file if block.type == ContentBlockType.FILE else capability.image
                )
                if not supported:
                    degradation.append(f"{block.type.value}_to_link")
            elif block.type == ContentBlockType.LINK:
                label = block.text or str(block.metadata.get("label") or "链接")
                url = str(block.metadata.get("url") or block.artifact_ref or "")
                rendered.append(f"[{label}]({url})" if capability.markdown else f"{label}: {url}")
                has_markdown = has_markdown or capability.markdown
            elif block.type == ContentBlockType.ACTIONS:
                rendered.append(self._render_actions(block))
                if not capability.buttons:
                    degradation.append("buttons_to_text")
        text = "\n\n".join(part for part in rendered if part)
        return text, "markdown" if has_markdown else "text", tuple(degradation)

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
    return cut if cut > 0 else limit
