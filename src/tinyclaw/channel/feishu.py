"""Feishu (Lark) channel adapters.

- FeishuChannel: webhook callbacks (requires external HTTP server)
- FeishuLongConnectionChannel: long-connection via lark-oapi WS client (no ngrok)
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any

from tinyclaw.channel.base import AsyncChannel, Channel, InboundMessage, ChannelAccount

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class FeishuChannel(Channel):
    """Feishu / Lark bot using event callbacks (requires a web server)."""

    name = "feishu"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("FeishuChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.app_id = account.config.get("app_id", "")
        self.app_secret = account.config.get("app_secret", "")
        self._encrypt_key = account.config.get("encrypt_key", "")
        self._bot_open_id = account.config.get("bot_open_id", "")
        is_lark = account.config.get("is_lark", False)
        self.api_base = ("https://open.larksuite.com/open-apis" if is_lark
                         else "https://open.feishu.cn/open-apis")
        self._tenant_token: str = ""
        self._token_expires_at: float = 0.0
        self._http = httpx.Client(timeout=15.0)

    def _refresh_token(self) -> str:
        if self._tenant_token and time.time() < self._token_expires_at:
            return self._tenant_token
        try:
            resp = self._http.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                return ""
            self._tenant_token = data.get("tenant_access_token", "")
            self._token_expires_at = time.time() + data.get("expire", 7200) - 300
            return self._tenant_token
        except Exception:
            return ""

    def _bot_mentioned(self, event: dict) -> bool:
        for m in event.get("message", {}).get("mentions", []):
            mid = m.get("id", {})
            if isinstance(mid, dict) and mid.get("open_id") == self._bot_open_id:
                return True
            if isinstance(mid, str) and mid == self._bot_open_id:
                return True
            if m.get("key") == self._bot_open_id:
                return True
        return False

    def _parse_content(self, message: dict) -> tuple[str, list]:
        msg_type = message.get("msg_type", "text")
        raw = message.get("content", "{}")
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return "", []

        media: list[dict] = []
        if msg_type == "text":
            return content.get("text", ""), media
        if msg_type == "post":
            texts: list[str] = []
            for lc in content.values():
                if not isinstance(lc, dict):
                    continue
                title = lc.get("title", "")
                if title:
                    texts.append(title)
                for para in lc.get("content", []):
                    for node in para:
                        tag = node.get("tag")
                        if tag == "text":
                            texts.append(node.get("text", ""))
                        elif tag == "a":
                            texts.append(node.get("text", "") + " " + node.get("href", ""))
            return "\n".join(texts), media
        if msg_type == "image":
            key = content.get("image_key", "")
            if key:
                media.append({"type": "image", "key": key})
            return "[image]", media
        return "", media

    def parse_event(self, payload: dict, token: str = "") -> InboundMessage | None:
        """Parse a Feishu event callback payload."""
        if self._encrypt_key and token and token != self._encrypt_key:
            return None
        if "challenge" in payload:
            return None

        header = payload.get("header", {})
        event_type = header.get("event_type", "")
        if event_type == "p2.im.chat.access_event.bot_p2p_chat_entered_v1":
            return self.parse_session_event(payload)

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        user_id = sender.get("open_id", sender.get("user_id", ""))
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")
        is_group = chat_type == "group"

        if is_group and self._bot_open_id and not self._bot_mentioned(event):
            return None

        text, media = self._parse_content(message)
        if not text:
            return None

        return InboundMessage(
            text=text, sender_id=user_id, channel="feishu",
            account_id=self.account_id,
            peer_id=user_id if chat_type == "p2p" else chat_id,
            media=media, is_group=is_group, raw=payload,
        )

    def parse_session_event(self, payload: dict) -> InboundMessage | None:
        """Parse bot-p2p-chat-entered event as a synthetic inbound message."""
        header = payload.get("header", {})
        event = payload.get("event", {})
        chat_id = event.get("chat_id", "")
        operator = event.get("operator_id", {})
        sender_id = ""
        if isinstance(operator, dict):
            sender_id = operator.get("open_id", "") or operator.get("user_id", "")
        if not chat_id:
            return None
        return InboundMessage(
            text="__feishu_session_started__",
            sender_id=sender_id,
            channel="feishu",
            account_id=self.account_id,
            peer_id=chat_id,
            is_group=False,
            raw={
                "event_type": "p2.im.chat.access_event.bot_p2p_chat_entered_v1",
                "event_id": header.get("event_id", ""),
                "payload": payload,
            },
        )

    def receive(self) -> InboundMessage | None:
        # Feishu uses webhooks, not polling. See parse_event() for event handling.
        return None

    def send(self, to: str, text: str, **kwargs) -> bool:
        token = self._refresh_token()
        if not token:
            return False
        try:
            resp = self._http.post(
                f"{self.api_base}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": to, "msg_type": "text",
                      "content": json.dumps({"text": text})},
            )
            data = resp.json()
            if data.get("code") != 0:
                return False
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# FeishuLongConnectionChannel -- lark-oapi WebSocket long connection
# ---------------------------------------------------------------------------


class FeishuLongConnectionChannel(AsyncChannel):
    """Feishu long-connection channel via lark-oapi WS client.

    Runs in its own background thread with its own event loop, bridging
    incoming push events into an async queue. No ngrok or public URL needed.

    Usage:
        ch = FeishuLongConnectionChannel(
            account=ChannelAccount(...),
            gw_event_loop=loop,       # gateway's running event loop
            gw_send_fn=send_fn,       # async fn(peer_id, text) called by gateway
        )
        await ch.start()              # start background thread
        async for msg in ch.receive_all():
            # msg is InboundMessage -- gateway routes and processes it
            ...
        ch.close()

    The channel implements AsyncChannel: receive_all() yields InboundMessage,
    and send() uses the HTTP API. The gateway owns the routing logic.
    """

    name = "feishu"

    def __init__(
        self,
        account: ChannelAccount,
        gw_event_loop_getter,  # callable -> asyncio.AbstractEventLoop
        gw_send_fn,           # async (peer_id, text) -> None
    ) -> None:
        self.account_id = account.account_id
        self.app_id = account.config.get("app_id", "")
        self.app_secret = account.config.get("app_secret", "")
        self._bot_open_id = account.config.get("bot_open_id", "")
        is_lark = account.config.get("is_lark", False)
        self.api_base = ("https://open.larksuite.com/open-apis" if is_lark
                         else "https://open.feishu.cn/open-apis")
        self._gw_loop_getter = gw_event_loop_getter
        self._gw_send = gw_send_fn

        self._tenant_token: str = ""
        self._token_expires_at: float = 0.0
        self._http = httpx.Client(timeout=15.0)

        self._thread: threading.Thread | None = None
        self._msg_queue: queue.Queue = queue.Queue()
        self._running = False
        self._closed = False

    def start(self) -> None:
        """Start the WS connection in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="feishu-ws")
        self._thread.start()

    def _run(self) -> None:
        """Background thread: sets up lark-oapi WS client in its own event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # All lark_oapi imports must happen here (before gateway's asyncio.run()
        # starts). Otherwise the module-level get_event_loop() captures the
        # running gateway loop.
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.api.im.v1 import (
            P2ImMessageReceiveV1,
            P2ImChatAccessEventBotP2pChatEnteredV1,
        )

        def on_message_receive(event):
            try:
                msg_data = event.event
                if not msg_data or not hasattr(msg_data, "message") or not msg_data.message:
                    return

                message = msg_data.message
                msg_type = getattr(message, "message_type", "text")
                raw_content = getattr(message, "content", "{}")

                try:
                    content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                except (json.JSONDecodeError, TypeError):
                    content = {}

                if msg_type != "text":
                    return

                text = content.get("text", "").strip()
                if not text:
                    return

                sender = getattr(msg_data, "sender", None)
                user_id = ""
                if sender and hasattr(sender, "sender_id"):
                    sid = sender.sender_id
                    user_id = getattr(sid, "open_id", "") or getattr(sid, "user_id", "")

                chat_type = getattr(message, "chat_type", "p2p")
                peer_id = getattr(message, "chat_id", user_id)
                is_group = chat_type == "group"

                if user_id == self._bot_open_id:
                    return

                # Yield as InboundMessage for ChannelManager to pick up
                self._msg_queue.put(InboundMessage(
                    text=text, sender_id=user_id,
                    channel="feishu", account_id=self.account_id,
                    peer_id=peer_id, is_group=is_group,
                ))

                # Also delegate send to gateway's event loop
                async def _deliver():
                    await self._gw_send(peer_id, text)

                gw_loop = self._gw_loop_getter()
                if gw_loop and gw_loop.is_running():
                    asyncio.run_coroutine_threadsafe(_deliver(), gw_loop)

            except Exception as exc:
                print(f"[feishu] 处理消息异常: {exc}")

        def on_session_started(event):
            """Handle p2p first-chat-entered event and surface to ChannelManager."""
            try:
                event_data = getattr(event, "event", None)
                if not event_data:
                    return
                chat_id = getattr(event_data, "chat_id", "")
                if not chat_id:
                    return

                operator = getattr(event_data, "operator_id", None)
                sender_id = ""
                if operator is not None:
                    sender_id = getattr(operator, "open_id", "") or getattr(operator, "user_id", "")

                header = getattr(event, "header", None)
                event_id = getattr(header, "event_id", "") if header else ""

                self._msg_queue.put(InboundMessage(
                    text="__feishu_session_started__",
                    sender_id=sender_id,
                    channel="feishu",
                    account_id=self.account_id,
                    peer_id=chat_id,
                    is_group=False,
                    raw={
                        "event_type": "p2.im.chat.access_event.bot_p2p_chat_entered_v1",
                        "event_id": event_id,
                    },
                ))
            except Exception as exc:
                print(f"[feishu] 会话创建事件处理异常: {exc}")

        class _Processor:
            def __init__(self, cb, event_type_cls):
                self._cb = cb
                self._type = event_type_cls

            def type(self):
                return self._type

            def do(self, data):
                self._cb(data)
                return None

        handler = EventDispatcherHandler()
        handler._processorMap["p2.im.message.receive_v1"] = _Processor(
            on_message_receive,
            P2ImMessageReceiveV1,
        )
        handler._processorMap["p2.im.chat.access_event.bot_p2p_chat_entered_v1"] = _Processor(
            on_session_started,
            P2ImChatAccessEventBotP2pChatEnteredV1,
        )

        from lark_oapi.ws import Client as FeishuWSClient
        domain = ("https://open.feishu.cn"
                  if self.api_base == "https://open.feishu.cn/open-apis"
                  else "https://open.larksuite.com")
        ws_client = FeishuWSClient(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=handler,
            domain=domain,
            auto_reconnect=True,
        )

        print(f"[gateway] 飞书长连接已启动（无需 ngrok）")
        ws_client.start()

    def _refresh_token(self) -> str:
        if self._tenant_token and time.time() < self._token_expires_at:
            return self._tenant_token
        try:
            resp = self._http.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                return ""
            self._tenant_token = data.get("tenant_access_token", "")
            self._token_expires_at = time.time() + data.get("expire", 7200) - 300
            return self._tenant_token
        except Exception:
            return ""

    def send(self, to: str, text: str, **kwargs) -> bool:
        token = self._refresh_token()
        if not token:
            return False
        try:
            resp = self._http.post(
                f"{self.api_base}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": to, "msg_type": "text",
                      "content": json.dumps({"text": text})},
            )
            data = resp.json()
            if data.get("code") != 0:
                return False
            return True
        except Exception:
            return False

    async def receive_all(self):
        """Async iterator: yields InboundMessage as they arrive."""
        while self._running and not self._closed:
            try:
                # Avoid blocking the gateway event loop thread.
                msg = await asyncio.to_thread(self._msg_queue.get, True, 0.5)
                yield msg
            except queue.Empty:
                continue

    def close(self) -> None:
        self._running = False
        self._closed = True
        self._http.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

