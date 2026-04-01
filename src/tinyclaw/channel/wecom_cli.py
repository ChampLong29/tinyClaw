"""WeCom CLI channel adapter.

This adapter uses the external `wecom-cli` command as a sidecar bridge:
- poll chats/messages with `wecom-cli msg get_msg_chat_list/get_message`
- send text with `wecom-cli msg send_message`
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import timedelta
from typing import Any

from tinyclaw.channel.base import Channel, ChannelAccount, InboundMessage
from tinyclaw.utils.timezone import now_beijing


def _unwrap_cli_payload(raw_text: str) -> dict[str, Any]:
    """Normalize wecom-cli output to a plain JSON dict."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    # wecom-cli often wraps tool outputs as {content:[{type:"text",text:"{...}"}],isError:false}
    content = data.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text" and isinstance(first.get("text"), str):
            try:
                nested_text = first["text"].strip()
                nested = json.loads(nested_text)
                if isinstance(nested, dict):
                    return nested
            except json.JSONDecodeError:
                pass
    return data


class WeComCliChannel(Channel):
    """Use wecom-cli as a polling channel for inbound trigger + outbound send."""

    name = "wecomcli"
    MAX_TEXT_LEN = 2048

    def __init__(self, account: ChannelAccount) -> None:
        self.account_id = account.account_id
        self._cli_bin = account.config.get("cli_bin", "wecom-cli")
        self._lookback_seconds = int(account.config.get("lookback_seconds", 300) or 300)
        self._overlap_seconds = int(account.config.get("overlap_seconds", 5) or 5)
        self._debug = bool(account.config.get("debug", False))

        self._last_window_end = None
        self._seen_msg_ids: set[str] = set()
        self._stats: dict[str, Any] = {
            "poll_count": 0,
            "chat_count": 0,
            "raw_message_count": 0,
            "inbound_count": 0,
            "send_ok": 0,
            "send_fail": 0,
            "last_poll_begin": "",
            "last_poll_end": "",
            "last_poll_at": 0.0,
            "last_inbound_at": 0.0,
            "last_error": "",
        }

    def _run_msg(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        cmd = [self._cli_bin, "msg", method, json.dumps(args, ensure_ascii=False)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            output = (result.stdout or "").strip() or (result.stderr or "").strip()
            if result.returncode != 0:
                if self._debug:
                    print(f"[wecomcli] cmd failed method={method} rc={result.returncode} out={output}")
                self._stats["last_error"] = f"cmd:{method} rc={result.returncode}"
                return {}
            payload = _unwrap_cli_payload(output)
            if self._debug:
                print(f"[wecomcli] method={method} payload_keys={list(payload.keys())}")
            return payload
        except Exception as exc:
            if self._debug:
                print(f"[wecomcli] cmd exception method={method} err={exc}")
            self._stats["last_error"] = f"cmd:{method} exc={exc}"
            return {}

    def _format_time(self, dt) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _extract_chats(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("chats", "chat_list", "list", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _extract_messages(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("messages", "msg_list", "message_list", "list", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _parse_text(self, msg: dict[str, Any]) -> str:
        text_node = msg.get("text")
        if isinstance(text_node, dict) and isinstance(text_node.get("content"), str):
            return text_node["content"].strip()
        if isinstance(text_node, str):
            return text_node.strip()
        if isinstance(msg.get("content"), str):
            return msg.get("content", "").strip()
        return ""

    def _extract_sender_id(self, msg: dict[str, Any]) -> str:
        # Common official shape: messages[].userid
        top_userid = msg.get("userid")
        if isinstance(top_userid, str) and top_userid.strip():
            return top_userid.strip()

        sender = msg.get("from")
        if isinstance(sender, dict):
            for key in ("userid", "user_id", "id"):
                value = sender.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _infer_chat_type(self, chat: dict[str, Any], chat_id: str) -> int:
        raw_chat_type = chat.get("chat_type", chat.get("chattype"))
        if str(raw_chat_type) in ("2", "group"):
            return 2
        if str(raw_chat_type) in ("1", "single", "user"):
            return 1

        # get_msg_chat_list may not provide chat_type. Infer from explicit hints.
        chat_name = str(chat.get("chat_name", "")).strip()
        if "群" in chat_name:
            return 2

        # Unknown type: default single-chat first. poll() will fallback to try both.
        return 1

    def poll(self) -> list[InboundMessage]:
        now = now_beijing()
        if self._last_window_end is None:
            begin = now - timedelta(seconds=self._lookback_seconds)
        else:
            begin = self._last_window_end - timedelta(seconds=self._overlap_seconds)
        end = now
        self._stats["poll_count"] += 1
        self._stats["last_poll_begin"] = self._format_time(begin)
        self._stats["last_poll_end"] = self._format_time(end)
        self._stats["last_poll_at"] = time.time()

        chats: list[dict[str, Any]] = []
        chat_cursor: str | None = None
        while True:
            chat_args: dict[str, Any] = {
                "begin_time": self._format_time(begin),
                "end_time": self._format_time(end),
            }
            if chat_cursor:
                chat_args["cursor"] = chat_cursor

            chat_payload = self._run_msg("get_msg_chat_list", chat_args)
            batch = self._extract_chats(chat_payload)
            chats.extend(batch)

            next_cursor = chat_payload.get("next_cursor")
            if isinstance(next_cursor, str) and next_cursor.strip():
                chat_cursor = next_cursor.strip()
                continue

            if bool(chat_payload.get("has_more", False)):
                break
            break

        self._stats["chat_count"] = len(chats)
        if self._debug:
            print(
                "[wecomcli] poll window "
                f"begin={self._format_time(begin)} end={self._format_time(end)} chats={len(chats)}"
            )

        inbound_list: list[InboundMessage] = []
        for chat in chats:
            chat_id = str(chat.get("chatid", chat.get("chat_id", chat.get("id", "")))).strip()
            if not chat_id:
                continue

            first_guess = self._infer_chat_type(chat, chat_id)
            chat_types_to_try = [first_guess] if first_guess == 2 else [1, 2]

            for chat_type in chat_types_to_try:
                cursor = None
                collected_for_type = 0
                while True:
                    args: dict[str, Any] = {
                        "chat_type": chat_type,
                        "chatid": chat_id,
                        "begin_time": self._format_time(begin),
                        "end_time": self._format_time(end),
                    }
                    if cursor:
                        args["cursor"] = cursor

                    payload = self._run_msg("get_message", args)
                    messages = self._extract_messages(payload)
                    self._stats["raw_message_count"] += len(messages)
                    if self._debug:
                        print(f"[wecomcli] poll chat={chat_id} type={chat_type} messages={len(messages)}")
                    for msg in messages:
                        msg_id = str(msg.get("msgid", msg.get("message_id", msg.get("id", ""))))
                        if msg_id and msg_id in self._seen_msg_ids:
                            continue

                        text = self._parse_text(msg)
                        if not text:
                            continue

                        sender_id = self._extract_sender_id(msg)
                        if not sender_id:
                            sender_id = chat_id if chat_type == 1 else "group-member"

                        peer_id = f"chat:{chat_id}" if chat_type == 2 else f"user:{chat_id}"
                        inbound_list.append(
                            InboundMessage(
                                text=text,
                                sender_id=sender_id,
                                channel="wecomcli",
                                account_id=self.account_id,
                                peer_id=peer_id,
                                is_group=chat_type == 2,
                                raw={"chat": chat, "message": msg},
                            )
                        )
                        collected_for_type += 1
                        if msg_id:
                            self._seen_msg_ids.add(msg_id)

                    if len(self._seen_msg_ids) > 20000:
                        self._seen_msg_ids.clear()

                    next_cursor = payload.get("next_cursor")
                    if isinstance(next_cursor, str) and next_cursor.strip():
                        cursor = next_cursor.strip()
                        continue
                    break

                # If single-chat guess already produced messages, avoid duplicate attempts.
                if collected_for_type > 0:
                    break

        self._last_window_end = end
        self._stats["inbound_count"] += len(inbound_list)
        if inbound_list:
            self._stats["last_inbound_at"] = time.time()
        if self._debug:
            print(f"[wecomcli] poll inbound_count={len(inbound_list)}")
        return inbound_list

    def receive(self) -> InboundMessage | None:
        msgs = self.poll()
        return msgs[0] if msgs else None

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        chat_type = 2 if to.startswith("chat:") else 1
        chat_id = to.split(":", 1)[1] if ":" in to else to

        for chunk in self._chunk(text):
            payload = self._run_msg(
                "send_message",
                {
                    "chat_type": chat_type,
                    "chatid": chat_id,
                    "msgtype": "text",
                    "text": {"content": chunk},
                },
            )
            if int(payload.get("errcode", -1)) != 0:
                if self._debug:
                    print(f"[wecomcli] send failed payload={payload}")
                self._stats["send_fail"] += 1
                self._stats["last_error"] = f"send errcode={payload.get('errcode', -1)}"
                return False
        self._stats["send_ok"] += 1
        return True

    def _chunk(self, text: str) -> list[str]:
        if len(text) <= self.MAX_TEXT_LEN:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= self.MAX_TEXT_LEN:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, self.MAX_TEXT_LEN)
            if cut <= 0:
                cut = self.MAX_TEXT_LEN
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    def get_health(self) -> dict[str, Any]:
        return dict(self._stats)
