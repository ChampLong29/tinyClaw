"""Work WeChat (企业微信) channel adapters.

- WorkWeChatChannel: webhook bridge mode (normalized JSON inbound + app API send)
- WorkWeChatLongConnectionChannel: official AI Bot long-connection mode
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from typing import Any

from tinyclaw.channel.base import (
    AsyncChannel,
    Channel,
    ChannelAccount,
    ChannelSendResult,
    InboundMessage,
)

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from websockets import connect as ws_connect

    HAS_WEBSOCKETS = True
except Exception:
    HAS_WEBSOCKETS = False


class WorkWeChatChannel(Channel):
    """Work WeChat channel with official send + normalized webhook parse."""

    name = "workwechat"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("WorkWeChatChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.corp_id = account.config.get("corp_id", "")
        self.corp_secret = account.config.get("corp_secret", "")
        self.agent_id = int(account.config.get("agent_id", 0) or 0)
        self.webhook_token = account.config.get("webhook_token", "")
        self.api_base = account.config.get("api_base", "https://qyapi.weixin.qq.com/cgi-bin")

        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._http = httpx.Client(timeout=15.0)

    def _refresh_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        try:
            resp = self._http.get(
                f"{self.api_base}/gettoken",
                params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            )
            data = resp.json()
            if int(data.get("errcode", -1)) != 0:
                return ""
            self._access_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 7200) or 7200)
            self._token_expires_at = time.time() + expires_in - 300
            return self._access_token
        except Exception:
            return ""

    def _parse_target(self, to: str) -> tuple[str, str]:
        """Return target type and id.

        Supported forms:
        - "chat:<chatid>" -> appchat send
        - "user:<userid>" -> message send
        - "<id>" -> message send (touser)
        """
        if to.startswith("chat:"):
            return "chat", to.split(":", 1)[1]
        if to.startswith("user:"):
            return "user", to.split(":", 1)[1]
        return "user", to

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        return self.send_with_receipt(to, text, **kwargs).accepted

    def send_with_receipt(self, to: str, text: str, **kwargs: Any) -> ChannelSendResult:
        token = self._refresh_token()
        if not token:
            return ChannelSendResult(accepted=False)

        target_type, target_id = self._parse_target(to)
        try:
            if target_type == "chat":
                resp = self._http.post(
                    f"{self.api_base}/appchat/send",
                    params={"access_token": token},
                    json={
                        "chatid": target_id,
                        "msgtype": "text",
                        "text": {"content": text},
                        "safe": 0,
                    },
                )
            else:
                resp = self._http.post(
                    f"{self.api_base}/message/send",
                    params={"access_token": token},
                    json={
                        "touser": target_id,
                        "msgtype": "text",
                        "agentid": self.agent_id,
                        "text": {"content": text},
                        "safe": 0,
                    },
                )

            data = resp.json()
            if int(data.get("errcode", -1)) != 0:
                return ChannelSendResult(accepted=False)
            message_id = str(data.get("msgid", "") or data.get("msg_id", "")).strip()
            if message_id:
                return ChannelSendResult(
                    accepted=True,
                    platform_message_id=message_id,
                    confirmed=True,
                )
            return ChannelSendResult(accepted=True)
        except Exception:
            return ChannelSendResult(accepted=False)

    def parse_event(self, payload: dict[str, Any], token: str = "") -> InboundMessage | None:
        """Parse normalized webhook payload into InboundMessage.

        Expected payload (bridge normalized):
        {
          "text": "hello",
          "sender_id": "zhangsan",
          "peer_id": "user:zhangsan" | "chat:xxxx",
          "is_group": false,
          "raw": {...}
        }
        """
        if self.webhook_token and token != self.webhook_token:
            return None

        text = str(payload.get("text", "")).strip()
        sender_id = str(payload.get("sender_id", "")).strip()
        peer_id = str(payload.get("peer_id", "")).strip()
        is_group = bool(payload.get("is_group", False))

        if not text or not sender_id:
            return None
        if not peer_id:
            peer_id = f"user:{sender_id}"

        raw = payload.get("raw")
        if not isinstance(raw, dict):
            raw = payload

        return InboundMessage(
            text=text,
            sender_id=sender_id,
            channel="workwechat",
            account_id=self.account_id,
            peer_id=peer_id,
            is_group=is_group,
            raw=raw,
        )

    def receive(self) -> InboundMessage | None:
        return None

    def close(self) -> None:
        self._http.close()


class WorkWeChatLongConnectionChannel(AsyncChannel):
    """Official Work WeChat AI bot long-connection channel.

    Implements aibot_subscribe + callbacks over WebSocket, and sends outbound
    messages via aibot_send_msg.
    """

    name = "workwechat"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_WEBSOCKETS:
            raise RuntimeError("WorkWeChatLongConnectionChannel requires websockets")
        self.account_id = account.account_id
        self.bot_id = account.config.get("bot_id", "")
        self.secret = account.config.get("secret", "")
        self.ws_url = account.config.get("ws_url", "wss://openws.work.weixin.qq.com")
        self.ping_interval_sec = int(account.config.get("ping_interval_sec", 30) or 30)

        self._running = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._msg_queue: queue.Queue = queue.Queue()
        self._send_queue: queue.Queue = queue.Queue()
        self._last_req_id_by_peer: dict[str, str] = {}
        self._seen_msg_ids: set[str] = set()

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="workwechat-ws")
        self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run_forever())

    async def _run_forever(self) -> None:
        while self._running and not self._closed:
            try:
                async with ws_connect(self.ws_url) as ws:
                    if not await self._subscribe(ws):
                        await asyncio.sleep(2)
                        continue
                    await self._event_loop(ws)
            except Exception:
                await asyncio.sleep(2)

    async def _subscribe(self, ws) -> bool:
        req_id = self._new_req_id()
        await ws.send(
            json.dumps(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": req_id},
                    "body": {"bot_id": self.bot_id, "secret": self.secret},
                },
                ensure_ascii=False,
            )
        )
        try:
            resp_text = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(resp_text)
            ok = int(resp.get("errcode", -1)) == 0
            if ok:
                print("[workwechat] subscribe success")
            else:
                print(f"[workwechat] subscribe failed: {resp}")
            return ok
        except Exception:
            return False

    async def _event_loop(self, ws) -> None:
        last_ping = time.time()
        while self._running and not self._closed:
            now = time.time()
            if now - last_ping >= self.ping_interval_sec:
                await ws.send(
                    json.dumps(
                        {"cmd": "ping", "headers": {"req_id": self._new_req_id()}},
                        ensure_ascii=False,
                    )
                )
                last_ping = now

            await self._drain_send_queue(ws)

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                continue

            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            inbound = self._parse_long_event(payload)
            if inbound is not None:
                self._msg_queue.put(inbound)

    async def _drain_send_queue(self, ws) -> None:
        while True:
            try:
                item = self._send_queue.get_nowait()
            except queue.Empty:
                return
            try:
                await ws.send(json.dumps(item, ensure_ascii=False))
            except Exception:
                return

    def _pick_str(self, *values: Any) -> str:
        for value in values:
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    return stripped
        return ""

    def _coerce_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload.get("body")
        if isinstance(body, dict):
            return body

        data = payload.get("data")
        if isinstance(data, dict):
            nested_body = data.get("body")
            if isinstance(nested_body, dict):
                return nested_body
            return data

        return {}

    def _parse_long_event(self, payload: dict[str, Any]) -> InboundMessage | None:
        cmd = payload.get("cmd", "")
        headers = payload.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        body = self._coerce_body(payload)
        req_id = self._pick_str(headers.get("req_id"), payload.get("req_id"))

        if not cmd:
            cmd = self._pick_str(payload.get("event"), payload.get("type"), body.get("cmd"))

        if cmd in ("aibot_respond_msg", "aibot_send_msg", "ping", "aibot_subscribe"):
            errcode = int(payload.get("errcode", 0) or 0)
            if errcode != 0:
                print(f"[workwechat] command failed cmd={cmd} req_id={req_id} payload={payload}")
            return None

        if cmd == "aibot_msg_callback":
            msg_id = self._pick_str(
                body.get("msgid"), body.get("msg_id"), payload.get("msgid"), payload.get("msg_id")
            )
            if msg_id:
                if msg_id in self._seen_msg_ids:
                    return None
                self._seen_msg_ids.add(msg_id)
                if len(self._seen_msg_ids) > 10000:
                    self._seen_msg_ids.clear()

            msg_type = self._pick_str(
                body.get("msgtype"), body.get("msg_type"), payload.get("msgtype")
            )
            if msg_type != "text":
                return None
            text_node = body.get("text")
            text = ""
            if isinstance(text_node, dict):
                text = self._pick_str(text_node.get("content"), text_node.get("text"))
            elif isinstance(text_node, str):
                text = text_node.strip()
            if not text:
                text = self._pick_str(body.get("content"), payload.get("content"))

            from_node = body.get("from")
            if not isinstance(from_node, dict):
                from_node = {}
            user_id = self._pick_str(
                from_node.get("userid"),
                from_node.get("user_id"),
                body.get("userid"),
                body.get("user_id"),
            )

            chat_type = self._pick_str(body.get("chattype"), body.get("chat_type"), "single")
            chat_id = self._pick_str(body.get("chatid"), body.get("chat_id"))
            if not text or not user_id:
                return None
            peer_id = f"chat:{chat_id}" if chat_type == "group" and chat_id else f"user:{user_id}"
            if req_id:
                self._last_req_id_by_peer[peer_id] = req_id
            print(f"[workwechat] inbound text peer={peer_id} req_id={req_id}")
            return InboundMessage(
                text=text,
                sender_id=user_id,
                channel="workwechat",
                account_id=self.account_id,
                peer_id=peer_id,
                is_group=chat_type == "group",
                raw={"headers": headers, "payload": payload, "req_id": req_id, "msg_id": msg_id},
            )

        if cmd == "aibot_event_callback":
            event = body.get("event", {})
            if not isinstance(event, dict):
                event = {}
            event_type = self._pick_str(
                event.get("eventtype"),
                event.get("event_type"),
                body.get("eventtype"),
                body.get("event_type"),
            )
            if event_type != "enter_chat":
                return None
            from_node = body.get("from")
            if not isinstance(from_node, dict):
                from_node = {}
            user_id = self._pick_str(
                from_node.get("userid"),
                from_node.get("user_id"),
                body.get("userid"),
                body.get("user_id"),
            )
            chat_type = self._pick_str(body.get("chattype"), body.get("chat_type"), "single")
            chat_id = self._pick_str(body.get("chatid"), body.get("chat_id"))
            if not user_id and not chat_id:
                return None
            peer_id = f"chat:{chat_id}" if chat_type == "group" and chat_id else f"user:{user_id}"
            if req_id:
                self._last_req_id_by_peer[peer_id] = req_id
            print(f"[workwechat] inbound event type={event_type} peer={peer_id} req_id={req_id}")
            return InboundMessage(
                text="__workwechat_session_started__",
                sender_id=user_id,
                channel="workwechat",
                account_id=self.account_id,
                peer_id=peer_id,
                is_group=chat_type == "group",
                raw={"headers": headers, "payload": payload, "req_id": req_id},
            )

        if cmd:
            print(f"[workwechat] ignored cmd={cmd}")
        return None

    def _new_req_id(self) -> str:
        return uuid.uuid4().hex

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        if not self._running or self._closed:
            return False

        chat_type = 2 if to.startswith("chat:") else 1
        chat_id = to.split(":", 1)[1] if ":" in to else to
        req_id = str(kwargs.get("reply_req_id", "") or "").strip()

        # Prefer respond command for callback-driven replies; fallback to proactive send.
        if req_id:
            packet = {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "markdown",
                    "markdown": {"content": text},
                },
            }
        else:
            packet = {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": self._new_req_id()},
                "body": {
                    "chatid": chat_id,
                    "chat_type": chat_type,
                    "msgtype": "markdown",
                    "markdown": {"content": text},
                },
            }
        try:
            self._send_queue.put_nowait(packet)
            return True
        except Exception:
            return False

    async def receive_all(self):
        while self._running and not self._closed:
            try:
                msg = await asyncio.to_thread(self._msg_queue.get, True, 0.5)
                yield msg
            except queue.Empty:
                continue

    def close(self) -> None:
        self._running = False
        self._closed = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
