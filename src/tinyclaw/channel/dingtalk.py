"""DingTalk (钉钉) channel adapter.

- Inbound: webhook callback parse -> InboundMessage
- Outbound: custom robot webhook send (supports signature)
- Long connection: DingTalk Stream mode (optional dependency)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import threading
import time
from typing import Any
from urllib.parse import quote_plus, urlencode, urlparse

from tinyclaw.channel.base import AsyncChannel, Channel, ChannelAccount, InboundMessage

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import dingtalk_stream

    HAS_DINGTALK_STREAM = True
except Exception:
    HAS_DINGTALK_STREAM = False


def _parse_dingtalk_inbound(payload: dict[str, Any], account_id: str) -> InboundMessage | None:
    """Parse DingTalk webhook/stream payload into InboundMessage."""
    if "challenge" in payload:
        return None

    text = ""
    text_node = payload.get("text")
    if isinstance(text_node, dict):
        text = str(text_node.get("content", "")).strip()
    elif isinstance(text_node, str):
        text = text_node.strip()

    if not text:
        content_node = payload.get("content")
        if isinstance(content_node, dict):
            text = str(content_node.get("text", "")).strip()
        elif isinstance(content_node, str):
            text = content_node.strip()

    if not text:
        msg = payload.get("msg")
        if isinstance(msg, dict):
            text_obj = msg.get("text")
            if isinstance(text_obj, dict):
                text = str(text_obj.get("content", "")).strip()

    if not text:
        return None

    sender_id = str(
        payload.get("senderStaffId")
        or payload.get("senderId")
        or payload.get("staffId")
        or payload.get("userid")
        or payload.get("sender_user_id")
        or ""
    ).strip()
    if not sender_id:
        sender_node = payload.get("sender")
        if isinstance(sender_node, dict):
            sender_id = str(sender_node.get("staffId") or sender_node.get("userid") or "").strip()
    if not sender_id:
        return None

    conv_type = str(payload.get("conversationType", payload.get("conversation_type", ""))).strip().lower()
    is_group = conv_type in ("2", "group")
    conv_id = str(payload.get("conversationId", payload.get("conversation_id", ""))).strip()
    if not conv_id:
        conv_node = payload.get("conversation")
        if isinstance(conv_node, dict):
            conv_id = str(conv_node.get("conversationId", "")).strip()

    peer_id = conv_id if conv_id else f"user:{sender_id}"
    return InboundMessage(
        text=text,
        sender_id=sender_id,
        channel="dingtalk",
        account_id=account_id,
        peer_id=peer_id,
        is_group=is_group,
        raw=payload,
    )


class DingTalkChannel(Channel):
    """DingTalk bot channel via webhook callbacks and robot webhook send."""

    name = "dingtalk"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("DingTalkChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.access_token = account.config.get("access_token", "")
        self.secret = account.config.get("secret", "")
        self.webhook_token = account.config.get("webhook_token", "")
        self.api_base = account.config.get("api_base", "https://oapi.dingtalk.com")
        self.webhook_url = account.config.get("webhook_url", "")
        self._http = httpx.Client(timeout=15.0)

    def _build_webhook_url(self) -> str:
        if self.webhook_url:
            base_url = self.webhook_url
        else:
            base_url = f"{self.api_base.rstrip('/')}/robot/send"
            if self.access_token:
                base_url += f"?{urlencode({'access_token': self.access_token})}"

        if not self.secret:
            return base_url

        timestamp = str(int(time.time() * 1000))
        sign_str = f"{timestamp}\n{self.secret}".encode("utf-8")
        digest = hmac.new(self.secret.encode("utf-8"), sign_str, digestmod=hashlib.sha256).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))

        separator = "&" if urlparse(base_url).query else "?"
        return f"{base_url}{separator}timestamp={timestamp}&sign={sign}"

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        url = self._build_webhook_url()
        if not url:
            return False
        try:
            resp = self._http.post(
                url,
                json={
                    "msgtype": "text",
                    "text": {"content": text},
                },
            )
            data = resp.json()
            return int(data.get("errcode", -1)) == 0
        except Exception:
            return False

    def parse_event(self, payload: dict[str, Any], token: str = "") -> InboundMessage | None:
        """Parse DingTalk webhook payload into InboundMessage.

        Works with common callback shapes and a normalized bridge shape.
        """
        if self.webhook_token and token != self.webhook_token:
            return None

        return _parse_dingtalk_inbound(payload, self.account_id)

    def receive(self) -> InboundMessage | None:
        return None

    def close(self) -> None:
        self._http.close()


class _DingTalkStreamHandler:
    """Callback adapter for dingtalk_stream SDK."""

    def __init__(self, owner: "DingTalkLongConnectionChannel") -> None:
        self._owner = owner

    def process(self, callback) -> Any:
        payload: dict[str, Any] = {}
        raw_data = getattr(callback, "data", None)
        if isinstance(raw_data, dict):
            payload = raw_data
        elif isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {}

        inbound = _parse_dingtalk_inbound(payload, self._owner.account_id)
        if inbound is not None:
            self._owner._msg_queue.put(inbound)

        ack_cls = getattr(dingtalk_stream, "AckMessage", None)
        if ack_cls is not None and hasattr(ack_cls, "STATUS_OK"):
            return ack_cls.STATUS_OK, "OK"
        return 200, "OK"


class DingTalkLongConnectionChannel(AsyncChannel):
    """DingTalk Stream-mode long connection channel.

    Requires optional dependency:
        pip install dingtalk-stream
    """

    name = "dingtalk"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_DINGTALK_STREAM:
            raise RuntimeError("DingTalkLongConnectionChannel requires dingtalk-stream")
        self.account_id = account.account_id
        self.client_id = account.config.get("client_id", "")
        self.client_secret = account.config.get("client_secret", "")
        if not self.client_id or not self.client_secret:
            raise RuntimeError("DingTalk long mode requires client_id and client_secret")

        # Reuse webhook sender for outbound messages.
        self._sender = DingTalkChannel(
            ChannelAccount(
                channel="dingtalk",
                account_id=self.account_id,
                config={
                    "access_token": account.config.get("access_token", ""),
                    "secret": account.config.get("secret", ""),
                    "webhook_url": account.config.get("webhook_url", ""),
                    "api_base": account.config.get("api_base", "https://oapi.dingtalk.com"),
                },
            )
        )

        self._msg_queue: queue.Queue = queue.Queue()
        self._running = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._client = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="dingtalk-stream")
        self._thread.start()

    def _run(self) -> None:
        try:
            credential = dingtalk_stream.Credential(self.client_id, self.client_secret)
            self._client = dingtalk_stream.DingTalkStreamClient(credential)

            topic = "chatbot.message"
            chatbot_mod = getattr(dingtalk_stream, "chatbot", None)
            if chatbot_mod is not None:
                msg_cls = getattr(chatbot_mod, "ChatbotMessage", None)
                if msg_cls is not None and hasattr(msg_cls, "TOPIC"):
                    topic = msg_cls.TOPIC

            self._client.register_callback_handler(topic, _DingTalkStreamHandler(self))
            self._client.start_forever()
        except Exception:
            # Keep silent to avoid crashing the host process; startup path reports failures.
            self._running = False

    async def receive_all(self):
        import asyncio

        while self._running and not self._closed:
            try:
                msg = await asyncio.to_thread(self._msg_queue.get, True, 0.5)
                yield msg
            except queue.Empty:
                continue

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        return self._sender.send(to, text, **kwargs)

    def close(self) -> None:
        self._running = False
        self._closed = True
        try:
            if self._client is not None and hasattr(self._client, "stop"):
                self._client.stop()
        except Exception:
            pass
        self._sender.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
