"""Channel presentation capabilities and registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ChannelCapability:
    text_limit: int = 4096
    markdown: bool = False
    streaming: bool = False
    message_update: bool = False
    card: bool = False
    buttons: bool = False
    file: bool = False
    image: bool = False
    thread_reply: bool = False
    delivery_receipt: bool = False

    def __post_init__(self) -> None:
        if self.text_limit <= 0:
            raise ValueError("text_limit must be positive")

    @property
    def progress_update(self) -> bool:
        return self.message_update or self.streaming


DEFAULT_CAPABILITY = ChannelCapability()

CHANNEL_CAPABILITIES: dict[str, ChannelCapability] = {
    "cli": ChannelCapability(text_limit=16_384, markdown=True),
    "console": ChannelCapability(text_limit=16_384, markdown=True),
    "telegram": ChannelCapability(
        text_limit=4096,
        image=True,
        file=True,
        thread_reply=True,
        delivery_receipt=True,
    ),
    "feishu": ChannelCapability(
        text_limit=4000,
        image=True,
        file=True,
        delivery_receipt=True,
    ),
    "workwechat": ChannelCapability(
        text_limit=4000,
        image=True,
        file=True,
        delivery_receipt=True,
    ),
    "wecomcli": ChannelCapability(text_limit=4000, image=True, file=True),
    "dingtalk": ChannelCapability(text_limit=4000),
}


class CapabilityRegistry:
    def __init__(
        self,
        capabilities: dict[str, ChannelCapability] | None = None,
        *,
        default: ChannelCapability = DEFAULT_CAPABILITY,
    ) -> None:
        self._capabilities = dict(CHANNEL_CAPABILITIES)
        if capabilities:
            self._capabilities.update(capabilities)
        self.default = default

    def get(self, channel: str) -> ChannelCapability:
        return self._capabilities.get(channel, self.default)

    def register(self, channel: str, capability: ChannelCapability) -> None:
        if not channel.strip():
            raise ValueError("channel cannot be empty")
        self._capabilities[channel] = capability
