"""Channel abstraction for tinyClaw.

All platform adapters produce a standardized InboundMessage.
Agent loop only sees InboundMessage -- platform differences are encapsulated.
"""

from __future__ import annotations

import asyncio
import queue
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


class AsyncChannel(ABC):
    """Async variant of Channel for gateway mode.

    Channels that receive messages via push (e.g. Feishu long connection,
    WebSocket) implement receive_all() as an async iterator instead of
    blocking receive().
    """

    name: str = "unknown"

    @abstractmethod
    async def receive_all(self):
        """Async iterator: yields InboundMessage as they arrive."""
        ...

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        ...

    def close(self) -> None:
        pass


class ChannelManager:
    """Registry for channel instances (both sync and async)."""

    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.async_channels: dict[str, AsyncChannel] = {}
        self.accounts: list[ChannelAccount] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    def register_async(self, channel: AsyncChannel) -> None:
        self.async_channels[channel.name] = channel

    def list_channels(self) -> list[str]:
        return list(set(self.channels) | set(self.async_channels))

    def get(self, name: str) -> Channel | AsyncChannel | None:
        return self.channels.get(name) or self.async_channels.get(name)

    def close_all(self) -> None:
        for ch in self.channels.values():
            ch.close()
        for ch in self.async_channels.values():
            ch.close()

    async def receive_next(self, timeout: float = 0.5):
        """Wait for the next message from any async channel."""
        tasks = []
        queues: dict[asyncio.Task, str] = {}

        for name, ch in self.async_channels.items():
            async def source(ch=ch):
                async for msg in ch.receive_all():
                    return msg
                # If channel ends, return None
                return None

            task = asyncio.create_task(source())
            tasks.append(task)
            queues[task] = name

        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                msg = t.result()
                if msg is not None:
                    return msg
        except Exception:
            for t in tasks:
                t.cancel()
        return None

