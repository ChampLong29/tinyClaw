"""Channel module: platform adapters for tinyClaw."""

from tinyclaw.channel.base import (
    AsyncChannel, Channel, InboundMessage, ChannelAccount, ChannelManager,
)
from tinyclaw.channel.feishu import FeishuChannel, FeishuLongConnectionChannel
from tinyclaw.channel.telegram import TelegramChannel
from tinyclaw.channel.wecom_cli import WeComCliChannel
from tinyclaw.channel.workwechat import WorkWeChatChannel, WorkWeChatLongConnectionChannel

__all__ = [
    "AsyncChannel", "Channel", "InboundMessage", "ChannelAccount", "ChannelManager",
    "FeishuChannel", "FeishuLongConnectionChannel", "TelegramChannel",
    "WeComCliChannel",
    "WorkWeChatChannel", "WorkWeChatLongConnectionChannel",
]
