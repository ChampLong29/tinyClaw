"""Capability-aware outbound presentation."""

from tinyclaw.presentation.capability import (
    CHANNEL_CAPABILITIES,
    DEFAULT_CAPABILITY,
    CapabilityRegistry,
    ChannelCapability,
)
from tinyclaw.presentation.renderer import (
    OutboundRenderer,
    RenderedMessage,
    SemanticSnapshot,
    markdown_to_plain,
    split_content_aware,
)

__all__ = [
    "CHANNEL_CAPABILITIES",
    "DEFAULT_CAPABILITY",
    "CapabilityRegistry",
    "ChannelCapability",
    "OutboundRenderer",
    "RenderedMessage",
    "SemanticSnapshot",
    "markdown_to_plain",
    "split_content_aware",
]
