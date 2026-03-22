"""Channel abstraction for tinyClaw.

All platform adapters produce a standardized InboundMessage.
Agent loop only sees InboundMessage -- platform differences are encapsulated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """Standardized inbound message from any channel."""
    text: str
    sender_id: str
    channel: str = ""
    account_id: str = ""
    peer_id: str = ""
    is_group: bool = False
    media: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ChannelAccount:
    """Configuration for a bot account on a channel."""
    channel: str
    account_id: str
    token: str = ""
    config: dict = field(default_factory=dict)


class Channel(ABC):
    """Abstract base class for all channel adapters."""

    name: str = "unknown"

    @abstractmethod
    def receive(self) -> InboundMessage | None:
        """Receive the next message. Returns None if no message available."""
        ...

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        """Send a text message to a recipient."""
        ...

    def close(self) -> None:
        """Clean up resources. Override in subclasses."""
        pass


class ChannelManager:
    """Registry for channel instances."""

    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.accounts: list[ChannelAccount] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    def list_channels(self) -> list[str]:
        return list(self.channels.keys())

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def close_all(self) -> None:
        for ch in self.channels.values():
            ch.close()
