#!/usr/bin/env python3
"""tinyClaw 单入口运行时。

一个命令启动所有核心能力：
- CLI REPL
- WebSocket Gateway
- Heartbeat + Cron
- Delivery
- Channel 接入（Telegram、Feishu 长连接、Feishu Webhook）
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from tinyclaw import client, config
from tinyclaw.agent.tools import ToolDispatcher
from tinyclaw.channel import ChannelAccount, FeishuLongConnectionChannel
from tinyclaw.channel.base import ChannelManager
from tinyclaw.channel.feishu import FeishuChannel
from tinyclaw.channel.telegram import TelegramChannel
from tinyclaw.concurrency import CommandQueue, LANE_CRON, LANE_HEARTBEAT, LANE_MAIN
from tinyclaw.delivery import DeliveryQueue, DeliveryRunner, chunk_message
from tinyclaw.gateway import AgentConfig, AgentManager, Binding, BindingTable, GatewayServer
from tinyclaw.gateway.routing import resolve_route
from tinyclaw.gateway.server import get_event_loop
from tinyclaw.intelligence import BootstrapLoader, MemoryStore, SkillsManager, build_system_prompt
from tinyclaw.intelligence.reminder import ReminderStore
from tinyclaw.resilience import AuthProfile, ProfileManager, ResilienceRunner
from tinyclaw.scheduler import CronService, HeartbeatRunner
from tinyclaw.session import SessionStore
from tinyclaw.utils.ansi import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RESET,
    YELLOW,
    print_assistant,
    print_info,
    print_warn,
)
from tinyclaw.utils.timezone import format_iso_to_beijing


def _resolve_workspace(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    return Path(__file__).parent / "workspace"


def _create_client_factory(api_key: str, base_url: str | None):
    def factory():
        return client.create_client(api_key, base_url)

    return factory


def _parse_reminder_time(content: str) -> tuple[str, str | None, int | None]:
    """解析时间表达式，如「一分钟后」。"""
    import re
    from datetime import datetime, timedelta, timezone

    m = re.search(r"(\d+|半小?[时分秒]?)后", content)
    minutes = None
    if m:
        unit = m.group(1)
        num_match = re.search(r"\d+", content)
        num_value = int(num_match.group(0)) if num_match else 1
        if unit in ("秒", "秒后"):
            minutes = 1 / 60
        elif unit in ("分", "分钟后", "分后"):
            minutes = num_value
        elif unit in ("小?时", "小时后", "小时后"):
            minutes = 60 * num_value
        elif "半小" in unit:
            minutes = 30
        if minutes is not None:
            cleaned = re.sub(r"\d+分?钟?后?", "", content).strip()
            due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            return cleaned, due.isoformat(), None

    return content, None, None


def _set_reminder(store: ReminderStore, content: str, due_time: str | None, minutes_from_now: int | None) -> str:
    from datetime import datetime, timedelta, timezone

    due = None
    if due_time:
        try:
            due = datetime.fromisoformat(due_time.replace("Z", "+00:00"))
        except ValueError:
            return "时间格式错误，请使用 ISO 格式如 2024-01-15T10:00:00"
    elif minutes_from_now:
        due = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    else:
        content, due_str, _ = _parse_reminder_time(content)
        if due_str:
            due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            return store.write_reminder(content, due)
        return store.write_reminder(content)

    return store.write_reminder(content, due)


def _format_reminders(store: ReminderStore) -> str:
    reminders = store.get_all_reminders()
    if not reminders:
        return "没有待处理的提醒"
    lines = ["待处理提醒："]
    for r in reminders:
        due = format_iso_to_beijing(r.get("due", ""), fmt="%Y-%m-%d %H:%M")
        lines.append(f"- {r.get('content', '')} (到期: {due or '无'})")
    return "\n".join(lines)


def _serialize_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    btype = getattr(block, "type", "")
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}),
        }
    if btype == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": getattr(block, "content", ""),
        }
    return {"type": "text", "text": str(block)}


def _serialize_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_serialize_block(b) for b in content]
    return str(content)


def _extract_assistant_text(response: Any) -> str:
    text = ""
    for block in getattr(response, "content", []):
        if hasattr(block, "text"):
            text += block.text
        elif isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")
    return text.strip()


def _recent_user_texts(messages: list[dict], max_count: int = 3) -> list[str]:
    texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        if len(texts) >= max_count:
            break
    texts.reverse()
    return texts


def print_help() -> None:
    print_info("tinyClaw 命令:")
    print_info("  /help     -- 显示帮助")
    print_info("  /status   -- 显示系统状态")
    print_info("  /cron     -- 列出定时任务")
    print_info("  /reminder -- 列出提醒")
    print_info("  /memory   -- 显示记忆统计")
    print_info("  /queue    -- 显示投递队列状态")
    print_info("  /lanes    -- 显示并发 Lane 状态")
    print_info("  /trigger  -- 立即触发心跳")
    print_info("  quit/exit -- 退出程序")


def _build_feishu_intro() -> str:
    return (
        "你好，我是 tinyClaw 助手。\n"
        "我现在可以在飞书中为你提供对话、记忆和提醒服务。\n\n"
        "常见命令：\n"
        "1. 记住：例如“记住我喜欢黑咖啡”\n"
        "2. 检索记忆：例如“我之前说过什么偏好？”\n"
        "3. 设置提醒：例如“30分钟后提醒我开会”\n"
        "4. 查看提醒：例如“列出我的提醒”\n\n"
        "你可以直接用自然语言下达任务。"
    )


def run_app(
    workspace: Path,
    cfg: dict[str, Any],
    gateway_host: str,
    gateway_port: int,
    run_mode: str,
) -> None:
    api_key = cfg["anthropic_api_key"]
    model_id = cfg["model_id"]
    base_url = cfg["anthropic_base_url"]

    client_factory = _create_client_factory(api_key, base_url)
    bootstrap = BootstrapLoader(workspace)
    memory = MemoryStore(workspace)
    skills_mgr = SkillsManager(workspace)
    skills_mgr.discover()
    reminder_store = ReminderStore(workspace)

    dispatcher = ToolDispatcher()
    dispatcher.register_builtin(workdir=workspace)
    dispatcher.register(
        {
            "name": "memory_write",
            "description": "保存重要事实到长期记忆。",
            "input_schema": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "要记住的事实或偏好。"}},
                "required": ["content"],
            },
        },
        lambda content="", **_: memory.write_memory(content),
    )
    dispatcher.register(
        {
            "name": "memory_search",
            "description": "搜索长期记忆中的相关信息。",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词。"}},
                "required": ["query"],
            },
        },
        lambda query="", **_: json.dumps(memory.hybrid_search(query), ensure_ascii=False, indent=2),
    )
    dispatcher.register(
        {
            "name": "reminder_write",
            "description": "设置提醒。用于用户请求提醒时调用。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "提醒内容。"},
                    "due_time": {"type": "string", "description": "ISO 到期时间。"},
                    "minutes_from_now": {"type": "integer", "description": "从现在起多少分钟。"},
                },
                "required": ["content"],
            },
        },
        lambda content="", due_time=None, minutes_from_now=None, **_: _set_reminder(
            reminder_store,
            content,
            due_time,
            minutes_from_now,
        ),
    )
    dispatcher.register(
        {
            "name": "reminder_list",
            "description": "列出所有待处理提醒。",
            "input_schema": {"type": "object", "properties": {}},
        },
        lambda **_: _format_reminders(reminder_store),
    )

    cmd_queue = CommandQueue()
    cmd_queue.get_or_create_lane(LANE_MAIN, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_CRON, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_HEARTBEAT, max_concurrency=1)

    bindings = BindingTable()
    bindings.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))
    mgr = AgentManager(workspace / ".agents")
    mgr.register(AgentConfig(id="main", name="小 Luna", dm_scope="per-peer", model=model_id))

    profile_manager = ProfileManager([
        AuthProfile(name="primary", provider="anthropic", api_key=api_key),
    ])
    resilience = ResilienceRunner(profile_manager=profile_manager, model_id=model_id)

    sessions_dir = workspace / ".sessions" / "agents" / "main" / "sessions"
    session_store = SessionStore(agent_id="main", base_dir=sessions_dir)
    session_map_path = sessions_dir.parent / "session_key_map.json"
    if session_map_path.exists():
        try:
            session_key_map = json.loads(session_map_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            session_key_map = {}
    else:
        session_key_map = {}

    def save_session_map() -> None:
        session_map_path.parent.mkdir(parents=True, exist_ok=True)
        session_map_path.write_text(json.dumps(session_key_map, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_session_id(session_key: str) -> str:
        sid = session_key_map.get(session_key)
        if sid:
            return sid
        sid = session_store.create_session(label=session_key)
        session_key_map[session_key] = sid
        save_session_map()
        return sid

    def append_session_delta(session_id: str, old_len: int, updated_messages: list[dict]) -> None:
        for msg in updated_messages[old_len:]:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            session_store.append_transcript(
                session_id,
                {
                    "type": role,
                    "content": _serialize_content(msg.get("content", "")),
                    "ts": time.time(),
                },
            )

    def run_turn(user_text: str, session_key: str, channel: str, agent_id: str = "main") -> str:
        session_id = get_session_id(session_key)
        history = session_store.load_session(session_id)

        search_text = user_text
        recent = _recent_user_texts(history)
        if recent:
            search_text = " ".join(recent) + " " + user_text
        mem_results = memory.hybrid_search(search_text, top_k=3)
        if mem_results:
            lines = ["## 相关记忆（自动检索）\n"]
            for r in mem_results:
                snippet = r.get("snippet", "")
                source = r.get("chunk", {}).get("path", "")
                date = source.split("/")[-1].replace(".jsonl", "").replace("_", "-") if source else ""
                lines.append(f"- [{date}] {snippet}")
            mem_ctx = "\n".join(lines)
        else:
            mem_ctx = ""

        system_prompt = build_system_prompt(
            mode="full",
            bootstrap=bootstrap.load_all("full"),
            skills_block=skills_mgr.format_prompt_block(),
            memory_context=mem_ctx,
            agent_id=agent_id,
            channel=channel,
            model=model_id,
        )

        messages = list(history)
        old_len = len(messages)
        messages.append({"role": "user", "content": user_text})

        response, updated = resilience.run(
            system=system_prompt,
            messages=messages,
            tools=dispatcher.tools,
            tool_handler=dispatcher.dispatch,
        )
        append_session_delta(session_id, old_len, updated)

        return _extract_assistant_text(response)

    server_mode = run_mode == "server"

    ch_mgr = ChannelManager()
    inbound_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    telegram_channel: TelegramChannel | None = None
    if server_mode and cfg.get("telegram_bot_token"):
        tg_account = ChannelAccount(
            channel="telegram",
            account_id="telegram-default",
            token=cfg["telegram_bot_token"],
            config={"allowed_chats": cfg.get("telegram_allowed_chats", "")},
        )
        try:
            telegram_channel = TelegramChannel(tg_account, state_dir=workspace / ".state")
            ch_mgr.register(telegram_channel)
            bindings.add(Binding(agent_id="main", tier=4, match_key="channel", match_value="telegram", priority=90))
        except Exception as exc:
            print_warn(f"Telegram 启动失败: {exc}")

    feishu_mode = str(cfg.get("feishu_mode", "both")).strip().lower() if server_mode else "off"
    if feishu_mode not in ("long", "webhook", "both", "off"):
        feishu_mode = "both"

    feishu_long: FeishuLongConnectionChannel | None = None
    feishu_webhook: FeishuChannel | None = None
    feishu_sender: Any = None
    feishu_fixed_reminder_to = cfg.get("feishu_reminder_to", "")
    feishu_state_path = workspace / ".state" / "feishu" / "known_peers.json"
    if feishu_state_path.exists():
        try:
            _saved = json.loads(feishu_state_path.read_text(encoding="utf-8"))
            feishu_known_peers: set[str] = set(_saved.get("known_peers", []))
            last_active_feishu_peer = _saved.get("last_active_peer", "") or feishu_fixed_reminder_to
            welcomed_event_ids: set[str] = set(_saved.get("welcomed_event_ids", []))
        except (json.JSONDecodeError, OSError):
            feishu_known_peers = set()
            last_active_feishu_peer = feishu_fixed_reminder_to
            welcomed_event_ids = set()
    else:
        feishu_known_peers = set()
        last_active_feishu_peer = feishu_fixed_reminder_to
        welcomed_event_ids = set()

    def save_feishu_state() -> None:
        event_ids = sorted(welcomed_event_ids)
        if len(event_ids) > 1000:
            event_ids = event_ids[-1000:]
        feishu_state_path.parent.mkdir(parents=True, exist_ok=True)
        feishu_state_path.write_text(
            json.dumps(
                {
                    "known_peers": sorted(feishu_known_peers),
                    "last_active_peer": last_active_feishu_peer,
                    "welcomed_event_ids": event_ids,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    fs_app_id = cfg.get("feishu_app_id", "")
    fs_app_secret = cfg.get("feishu_app_secret", "")
    if fs_app_id and fs_app_secret and feishu_mode != "off":
        feishu_account = ChannelAccount(
            account_id="feishu-default",
            channel="feishu",
            config={
                "app_id": fs_app_id,
                "app_secret": fs_app_secret,
                "encrypt_key": cfg.get("feishu_encrypt_key", ""),
                "bot_open_id": cfg.get("feishu_bot_open_id", ""),
                "is_lark": cfg.get("feishu_is_lark", False),
            },
        )

        if feishu_mode in ("long", "both"):
            feishu_long = FeishuLongConnectionChannel(
                account=feishu_account,
                gw_event_loop_getter=get_event_loop,
                gw_send_fn=lambda _peer_id, _text: asyncio.sleep(0),
            )
            feishu_long.start()
            ch_mgr.register_async(feishu_long)
            feishu_sender = feishu_long

        if feishu_mode in ("webhook", "both"):
            try:
                feishu_webhook = FeishuChannel(feishu_account)
                feishu_sender = feishu_webhook
            except Exception as exc:
                print_warn(f"Feishu Webhook 通道初始化失败: {exc}")

        bindings.add(Binding(agent_id="main", tier=4, match_key="channel", match_value="feishu", priority=100))

    def deliver_fn(channel: str, to: str, text: str) -> None:
        if channel in ("console", "cli"):
            print_assistant(text)
            return
        if channel == "telegram" and telegram_channel:
            if not telegram_channel.send(to, text):
                raise RuntimeError("telegram send failed")
            return
        if channel == "feishu" and feishu_sender:
            if not feishu_sender.send(to, text):
                raise RuntimeError("feishu send failed")
            return
        print_warn(f"未知投递通道: {channel}")

    delivery_queue = DeliveryQueue(workspace / "delivery-queue")
    delivery_runner = DeliveryRunner(delivery_queue, deliver_fn)
    delivery_runner.start()

    heartbeat_lock = threading.Lock()
    heartbeat = HeartbeatRunner(
        workspace=workspace,
        lane_lock=heartbeat_lock,
        interval=cfg["heartbeat_interval"],
        active_hours=(cfg["heartbeat_active_start"], cfg["heartbeat_active_end"]),
        client_factory=client_factory,
        model=model_id,
    )
    heartbeat.start()

    cron = CronService(
        cron_file=workspace / "CRON.json",
        client_factory=client_factory,
        model=model_id,
    )

    cron_stop = threading.Event()

    def cron_loop() -> None:
        while not cron_stop.is_set():
            try:
                cron.tick()
            except Exception:
                pass
            cron_stop.wait(timeout=1.0)

    threading.Thread(target=cron_loop, daemon=True, name="cron-tick").start()

    def reminder_check_loop() -> None:
        nonlocal last_active_feishu_peer
        while not stop_event.is_set():
            stop_event.wait(timeout=60)
            if stop_event.is_set():
                return
            try:
                due = reminder_store.get_due_reminders()
                for r in due:
                    reminder_text = f"[提醒] {r['content']}"
                    target_peer = feishu_fixed_reminder_to or last_active_feishu_peer
                    if server_mode and feishu_sender and target_peer:
                        delivery_queue.enqueue("feishu", target_peer, reminder_text)
                    else:
                        delivery_queue.enqueue("cli", "cli-user", reminder_text)
                    reminder_store.mark_reminded(r.get("ts", ""))
            except Exception:
                pass

    threading.Thread(target=reminder_check_loop, daemon=True, name="reminder-check").start()

    def handle_inbound_message(msg: Any) -> None:
        nonlocal last_active_feishu_peer
        if msg.channel == "feishu":
            raw = msg.raw if isinstance(msg.raw, dict) else {}
            event_type = raw.get("event_type", "")
            event_id = raw.get("event_id", "")

            if event_type == "p2.im.chat.access_event.bot_p2p_chat_entered_v1":
                if event_id and event_id in welcomed_event_ids:
                    return
                if event_id:
                    welcomed_event_ids.add(event_id)
                if msg.peer_id and msg.peer_id not in feishu_known_peers:
                    feishu_known_peers.add(msg.peer_id)
                if msg.peer_id:
                    last_active_feishu_peer = msg.peer_id
                save_feishu_state()
                if feishu_sender and msg.peer_id:
                    delivery_queue.enqueue("feishu", msg.peer_id, _build_feishu_intro())
                return

            if msg.peer_id:
                last_active_feishu_peer = msg.peer_id
            if msg.peer_id and msg.peer_id not in feishu_known_peers:
                feishu_known_peers.add(msg.peer_id)
                save_feishu_state()
                if feishu_sender:
                    delivery_queue.enqueue("feishu", msg.peer_id, _build_feishu_intro())

        aid, sk = resolve_route(bindings, mgr, msg.channel, msg.peer_id, account_id=msg.account_id)
        future = cmd_queue.enqueue(LANE_MAIN, lambda: run_turn(msg.text, sk, msg.channel, aid))
        try:
            reply = future.result(timeout=120)
            if reply:
                for chunk in chunk_message(reply, msg.channel):
                    delivery_queue.enqueue(msg.channel, msg.peer_id, chunk)
            if msg.channel == "feishu":
                save_feishu_state()
        except concurrent.futures.TimeoutError:
            print_warn("渠道消息处理超时")
        except Exception as exc:
            print_warn(f"渠道消息处理失败: {exc}")

    def telegram_poll_loop() -> None:
        if not telegram_channel:
            return
        while not stop_event.is_set():
            try:
                msgs = telegram_channel.poll()
                for m in msgs:
                    inbound_queue.put(m)
            except Exception:
                pass
            stop_event.wait(timeout=0.5)

    if telegram_channel:
        threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-poll").start()

    loop = None
    gateway = None
    pump_future = None

    if server_mode:
        loop = get_event_loop()

        async def async_channel_pump() -> None:
            while not stop_event.is_set():
                if not ch_mgr.async_channels:
                    await asyncio.sleep(0.2)
                    continue
                msg = await ch_mgr.receive_next(timeout=0.5)
                if msg is not None:
                    inbound_queue.put(msg)

        async def run_agent_ws(_mgr: AgentManager, agent_id: str, sk: str, text: str) -> str:
            return await asyncio.to_thread(run_turn, text, sk, "websocket", agent_id)

        gateway = GatewayServer(
            mgr,
            bindings,
            run_agent_fn=run_agent_ws,
            host=gateway_host,
            port=gateway_port,
        )
        asyncio.run_coroutine_threadsafe(gateway.start(), loop).result(timeout=15)

        pump_future = asyncio.run_coroutine_threadsafe(async_channel_pump(), loop)

    webhook_server: ThreadingHTTPServer | None = None
    webhook_thread: threading.Thread | None = None

    if server_mode and feishu_webhook and feishu_mode in ("webhook", "both"):
        webhook_host = cfg.get("feishu_webhook_host", "0.0.0.0")
        webhook_port = int(cfg.get("feishu_webhook_port", 8766))
        webhook_path = str(cfg.get("feishu_webhook_path", "/feishu/events"))

        class FeishuWebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path != webhook_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                if "challenge" in payload:
                    data = json.dumps({"challenge": payload["challenge"]}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                token = payload.get("token", "")
                inbound = feishu_webhook.parse_event(payload, token=token)
                if inbound is not None:
                    inbound_queue.put(inbound)
                data = b'{"code":0}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):  # noqa: A003
                return

        webhook_server = ThreadingHTTPServer((webhook_host, webhook_port), FeishuWebhookHandler)
        webhook_thread = threading.Thread(target=webhook_server.serve_forever, daemon=True, name="feishu-webhook")
        webhook_thread.start()
        print_info(f"Feishu Webhook 已启动: http://{webhook_host}:{webhook_port}{webhook_path}")

    def inbound_worker() -> None:
        while not stop_event.is_set():
            try:
                msg = inbound_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            handle_inbound_message(msg)

    if server_mode:
        threading.Thread(target=inbound_worker, daemon=True, name="inbound-worker").start()

    print_info("=" * 60)
    print_info(f"  tinyClaw  |  模式: {run_mode}  |  工作区: {workspace}")
    print_info(f"  模型: {model_id}")
    if server_mode:
        print_info(f"  Gateway: ws://{gateway_host}:{gateway_port}")
        print_info(f"  飞书模式: {feishu_mode}")
        print_info("  运行中：飞书/渠道服务 + Gateway + 后台任务")
    else:
        print_info("  命令: /help, /status, /cron, /reminder, /memory, /queue, /lanes, /trigger")
    print_info("=" * 60)
    print()

    try:
        if server_mode:
            while True:
                for msg in heartbeat.drain_output():
                    print_info(f"[心跳] {msg}")
                for msg in cron.drain_output():
                    print_info(f"[定时任务] {msg}")
                time.sleep(1)
        else:
            while True:
                for msg in heartbeat.drain_output():
                    delivery_queue.enqueue("cli", "cli-user", f"[心跳] {msg}")
                for msg in cron.drain_output():
                    delivery_queue.enqueue("cli", "cli-user", f"[定时任务] {msg}")

                try:
                    user_input = input(f"{CYAN}{BOLD}你 > {RESET}").strip()
                except (KeyboardInterrupt, EOFError):
                    print(f"\n{DIM}再见.{RESET}")
                    break

                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit"):
                    print(f"{DIM}再见.{RESET}")
                    break

                if user_input.startswith("/"):
                    parts = user_input.split(maxsplit=1)
                    cmd = parts[0].lower()
                    if cmd == "/help":
                        print_help()
                    elif cmd == "/status":
                        hb = heartbeat.status()
                        running = "运行中" if hb.get("running") else "空闲"
                        print_info(f"  心跳启用: {'是' if hb.get('enabled') else '否'}")
                        print_info(f"  心跳状态: {running}")
                        print_info(f"  上次运行: {hb.get('last_run', '从未')}")
                        print_info(f"  下次运行: {hb.get('next_in', 'n/a')}秒后")
                        ds = delivery_runner.get_stats()
                        print_info(f"  待投递: {ds.get('pending', 0)}")
                        print_info(f"  投递失败: {ds.get('failed', 0)}")
                        print_info(f"  已投递: {ds.get('delivered', 0)}")
                        print_info(f"  定时任务: {len(cron.list_jobs())} 个")
                    elif cmd == "/cron":
                        jobs = cron.list_jobs()
                        if not jobs:
                            print_info("  没有定时任务")
                        for j in jobs:
                            tag = f"{GREEN}启用{RESET}" if j["enabled"] else f"{YELLOW}停用{RESET}"
                            next_in = j.get("next_in")
                            next_str = f"{next_in}秒后" if next_in is not None else "未计划"
                            print(f"  [{tag}] {j['name']} | 下次: {next_str}")
                    elif cmd == "/reminder":
                        reminders = reminder_store.get_all_reminders()
                        if not reminders:
                            print_info("  没有提醒")
                        for r in reminders:
                            due = format_iso_to_beijing(r.get("due", ""), fmt="%Y-%m-%d %H:%M")
                            print(f"  - {r.get('content', '')} | 到期: {due or '无'}")
                    elif cmd == "/memory":
                        stats = memory.get_stats()
                        print_info(f"  常青记忆: {stats.get('evergreen_chars', 0)} 字符")
                        print_info(f"  日常记录: {stats.get('daily_entries', 0)} 条")
                        print_info("  记忆文件: workspace/memory/")
                    elif cmd == "/queue":
                        qs = delivery_runner.get_stats()
                        print_info(f"  待投递: {qs.get('pending', 0)}")
                        print_info(f"  投递中: {qs.get('in_flight', 0)}")
                        print_info(f"  失败: {qs.get('failed', 0)}")
                        print_info(f"  已完成: {qs.get('delivered', 0)}")
                    elif cmd == "/lanes":
                        for name, st in cmd_queue.stats().items():
                            lane_name = {"main": "主队列", "cron": "定时任务", "heartbeat": "心跳"}.get(name, name)
                            print_info(
                                f"  {lane_name}: 队列深度={st.get('queue_depth', 0)}, "
                                f"活跃={st.get('active', 0)}, 最大并发={st.get('max_concurrency', 0)}"
                            )
                    elif cmd == "/trigger":
                        print_info(f"  {heartbeat.trigger()}")
                    else:
                        print_warn(f"未知命令: {cmd}")
                    continue

                sk = "agent:main:direct:cli-user"
                future = cmd_queue.enqueue(LANE_MAIN, lambda: run_turn(user_input, sk, "cli", "main"))
                try:
                    result = future.result(timeout=120)
                    if result:
                        for chunk in chunk_message(result, "cli"):
                            delivery_queue.enqueue("cli", "cli-user", chunk)
                except concurrent.futures.TimeoutError:
                    print_warn("请求超时。")
                except Exception as exc:
                    print_warn(f"错误: {exc}")

    except KeyboardInterrupt:
        print(f"\n{DIM}停止服务.{RESET}")

    finally:
        stop_event.set()
        cron_stop.set()
        heartbeat.stop()
        ch_mgr.close_all()
        cmd_queue.wait_for_all(timeout=3.0)
        delivery_runner.stop()

        if webhook_server:
            webhook_server.shutdown()
            webhook_server.server_close()
            if webhook_thread and webhook_thread.is_alive():
                webhook_thread.join(timeout=2.0)

        if pump_future:
            pump_future.cancel()
        if gateway and loop:
            try:
                asyncio.run_coroutine_threadsafe(gateway.stop(), loop).result(timeout=5)
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="tinyClaw AI Agent Gateway")
    parser.add_argument(
        "--mode",
        choices=["cli", "server"],
        default="server",
        help="运行模式: cli (纯命令行) | server (飞书/网关服务)",
    )
    parser.add_argument("--workspace", default=None, help="工作区目录")
    parser.add_argument("--env", default=None, help=".env 文件路径")
    parser.add_argument("--port", type=int, default=8765, help="Gateway WebSocket 端口")
    parser.add_argument("--host", default="localhost", help="Gateway 主机")
    args = parser.parse_args()

    env_path = Path(args.env) if args.env else Path.cwd() / ".env"
    cfg = config.load_config(env_path)

    if not cfg["anthropic_api_key"]:
        print(f"{YELLOW}错误: ANTHROPIC_API_KEY 未设置。{RESET}")
        print(f"{DIM}请将 .env.example 复制为 .env 并填入你的 API Key。{RESET}")
        sys.exit(1)

    workspace = _resolve_workspace(args.workspace)
    run_app(workspace, cfg, gateway_host=args.host, gateway_port=args.port, run_mode=args.mode)


if __name__ == "__main__":
    main()
