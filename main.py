#!/usr/bin/env python3
"""tinyClaw 单入口运行时。

一个命令启动所有核心能力：
- CLI REPL
- WebSocket Gateway
- Heartbeat + Cron
- Delivery
- Channel 接入（Telegram、Feishu、Work WeChat）
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextvars
import json
import queue
import subprocess
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
from tinyclaw.channel.dingtalk import DingTalkChannel, DingTalkLongConnectionChannel
from tinyclaw.channel.feishu import FeishuChannel
from tinyclaw.channel.telegram import TelegramChannel
from tinyclaw.channel.wecom_cli import WeComCliChannel
from tinyclaw.channel.workwechat import WorkWeChatChannel, WorkWeChatLongConnectionChannel
from tinyclaw.concurrency import LANE_CRON, LANE_HEARTBEAT, CommandQueue
from tinyclaw.contracts.interaction import TaskInstance, TaskState
from tinyclaw.delivery import (
    DeliveryReceipt,
    DurableDeliveryQueue,
    DurableDeliveryRunner,
    LegacyDeliveryMigrator,
    SQLiteDeliveryStore,
)
from tinyclaw.gateway import AgentConfig, AgentManager, Binding, BindingTable, GatewayServer
from tinyclaw.gateway.routing import resolve_route
from tinyclaw.gateway.server import get_event_loop
from tinyclaw.identity import IdentitySessionResolver, SessionPolicy, SQLiteIdentityStore
from tinyclaw.intelligence import (
    BootstrapLoader,
    MemoryStore,
    SkillsManager,
    build_static_prefix,
    build_system_prompt,
)
from tinyclaw.intelligence.reminder import ReminderStore
from tinyclaw.interaction import (
    ActiveTaskExistsError,
    ClarificationRequiredSignal,
    ClarificationService,
    ConfirmationService,
    ConfirmationTokenSigner,
    ProductionInteractionService,
    SQLiteRequestStore,
    SQLiteTaskStore,
    TaskExecutionOutcome,
    TaskStateMachine,
    ToolExecutionContext,
    ToolExecutionGate,
    parse_clarification_command,
    parse_confirmation_command,
    parse_task_command,
    session_lane_name,
)
from tinyclaw.interaction.confirmation import load_or_create_confirmation_secret
from tinyclaw.interaction.control import ControlAction, ControlPrincipal
from tinyclaw.interaction.request_store import ClarificationRequest, ConfirmationRequest
from tinyclaw.notification import (
    NotificationGateway,
    NotificationPolicyConfig,
    NotificationReason,
    NotificationRequest,
    SQLiteNotificationPolicy,
)
from tinyclaw.observability import (
    ArtifactStore,
    FeedbackRecord,
    FeedbackSource,
    SQLiteFeedbackStore,
    TraceRecorder,
)
from tinyclaw.presentation import CapabilityRegistry, ChannelCapability, OutboundRenderer
from tinyclaw.resilience import AuthProfile, ProfileManager, ResilienceRunner
from tinyclaw.runtime.tool_executor import ToolRecoveryExecutor
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

TURN_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "turn_context",
    default={"channel": "", "peer_id": "", "account_id": ""},
)


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


def _set_reminder(
    store: ReminderStore,
    content: str,
    due_time: str | None,
    minutes_from_now: int | None,
    channel: str = "",
    peer_id: str = "",
    account_id: str = "",
) -> str:
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
            return store.write_reminder(
                content, due, channel=channel, peer_id=peer_id, account_id=account_id
            )
        return store.write_reminder(
            content, channel=channel, peer_id=peer_id, account_id=account_id
        )

    return store.write_reminder(
        content, due, channel=channel, peer_id=peer_id, account_id=account_id
    )


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


def _unwrap_wecom_cli_payload(raw_text: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    content = data.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if (
            isinstance(first, dict)
            and first.get("type") == "text"
            and isinstance(first.get("text"), str)
        ):
            try:
                nested = json.loads(first["text"])
                if isinstance(nested, dict):
                    return nested
            except json.JSONDecodeError:
                pass
    return data


def _wecom_cli_send_message(cli_bin: str, chat_type: int, chatid: str, content: str) -> str:
    cmd = [
        cli_bin,
        "msg",
        "send_message",
        json.dumps(
            {
                "chat_type": chat_type,
                "chatid": chatid,
                "msgtype": "text",
                "text": {"content": content},
            },
            ensure_ascii=False,
        ),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        output = (result.stdout or "").strip() or (result.stderr or "").strip()
        payload = _unwrap_wecom_cli_payload(output)
        if result.returncode != 0:
            return json.dumps(
                {"ok": False, "error": output, "returncode": result.returncode}, ensure_ascii=False
            )
        return json.dumps(
            {"ok": int(payload.get("errcode", -1)) == 0, "result": payload}, ensure_ascii=False
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


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
    print_info("  /feedback <up|down|correction> [说明] -- 记录反馈")
    print_info("  /task status [task_id]              -- 查看任务状态")
    print_info("  /task cancel|pause|resume [task_id] -- 控制当前或指定任务")
    print_info("  /task modify <task_id> <新目标>      -- 修改暂停中的任务")
    print_info("  /confirm approve|deny <id> <token> -- 显式确认或拒绝高风险操作")
    print_info("  /clarify <id> field=value          -- 回答任务澄清问题")
    print_info("  /identity current                    -- 查看当前身份与会话边界")
    print_info("  /identity link <global_id> <channel> <account> <user> -- 显式绑定")
    print_info("  /identity unlink <channel> <account> <user>           -- 解除绑定")
    print_info("  /identity merge <source_global_id> <target_global_id>  -- 合并身份")
    print_info("  提示: wecom-cli 发送格式为 JSON，如 chat_type/chatid/msgtype/text")
    print_info("  quit/exit -- 退出程序")


def _format_task(task: TaskInstance) -> str:
    detail = ""
    if task.failure:
        detail = f" | 失败: {task.failure.message}"
    return (
        f"任务 {task.task_id} | 状态: {task.state.value} | revision: {task.revision}"
        f" | 目标: {task.user_goal}{detail}"
    )


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


def _build_workwechat_intro() -> str:
    return (
        "你好，我是 tinyClaw 助手。\n"
        "我现在可以在企业微信中为你提供对话、记忆和提醒服务。\n\n"
        "你可以直接用自然语言描述任务，我会尽量给出可执行结果。"
    )


def _build_dingtalk_intro() -> str:
    return (
        "你好，我是 tinyClaw 助手。\n"
        "我现在可以在钉钉中为你提供对话、记忆和提醒服务。\n\n"
        "你可以直接用自然语言描述任务，我会尽量给出可执行结果。"
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

    # Cache static prompt parts once at startup — avoids file I/O and
    # string formatting on every turn, and enables Anthropic prompt caching
    # when the static prefix is passed as a cached content block.
    _cached_bootstrap = bootstrap.load_all("full")
    _cached_skills_block = skills_mgr.format_prompt_block()
    _static_prefix = build_static_prefix(
        mode="full",
        bootstrap=_cached_bootstrap,
        skills_block=_cached_skills_block,
    )

    dispatcher = ToolDispatcher()
    tool_gate: ToolExecutionGate | None = None
    tool_recovery_executor = ToolRecoveryExecutor()
    dispatcher.register_builtin(workdir=workspace)
    dispatcher.register(
        {
            "name": "memory_write",
            "description": "保存重要事实到长期记忆。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的事实或偏好。"}
                },
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
            channel=TURN_CONTEXT.get().get("channel", ""),
            peer_id=TURN_CONTEXT.get().get("peer_id", ""),
            account_id=TURN_CONTEXT.get().get("account_id", ""),
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

    def request_clarification(question: str, required_fields: list[str], **_: Any) -> str:
        raise ClarificationRequiredSignal(
            question=question,
            required_fields=tuple(required_fields),
        )

    dispatcher.register(
        {
            "name": "request_clarification",
            "description": "当执行任务缺少必须信息时，暂停任务并向用户提出结构化澄清问题。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "需要用户回答的问题。"},
                    "required_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "答复中必须提供的字段名。",
                    },
                },
                "required": ["question", "required_fields"],
            },
        },
        request_clarification,
    )
    if cfg.get("wecom_cli_tool_enabled", cfg.get("wecom_cli_enabled", False)):
        dispatcher.register(
            {
                "name": "wecom_cli_send_message",
                "description": "通过 wecom-cli 向企业微信会话发送文本消息。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "chat_type": {
                            "type": "integer",
                            "description": "会话类型，1=单聊，2=群聊",
                            "enum": [1, 2],
                        },
                        "chatid": {
                            "type": "string",
                            "description": "会话ID。单聊为userid，群聊为群ID",
                        },
                        "content": {"type": "string", "description": "发送文本内容，最大2048字节"},
                    },
                    "required": ["chat_type", "chatid", "content"],
                },
            },
            lambda chat_type=1, chatid="", content="", **_: _wecom_cli_send_message(
                cfg.get("wecom_cli_bin", "wecom-cli"),
                int(chat_type),
                str(chatid),
                str(content),
            ),
        )

    cmd_queue = CommandQueue()
    cmd_queue.get_or_create_lane(LANE_CRON, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_HEARTBEAT, max_concurrency=1)

    bindings = BindingTable()
    bindings.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))
    mgr = AgentManager(workspace / ".agents")
    mgr.register(
        AgentConfig(
            id="main",
            name="小 Luna",
            dm_scope=cfg["session_scope"],
            model=model_id,
        )
    )

    profile_manager = ProfileManager(
        [
            AuthProfile(name="primary", provider="anthropic", api_key=api_key),
        ]
    )
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
    session_map_lock = threading.RLock()

    def save_session_map() -> None:
        with session_map_lock:
            session_map_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = session_map_path.with_name(f".{session_map_path.name}.tmp")
            temporary.write_text(
                json.dumps(session_key_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(session_map_path)

    def get_session_id(session_key: str) -> str:
        with session_map_lock:
            sid = session_key_map.get(session_key)
            if sid:
                return sid
            sid = session_store.create_session(label=session_key)
            session_key_map[session_key] = sid
            save_session_map()
            return sid

    # In-memory cache of latest messages[] per session.
    # Avoids re-reading the full JSONL from disk on every turn.
    # Key: session_id, Value: latest messages[] list.
    session_cache: dict[str, list[dict]] = {}

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

    def run_turn(
        user_text: str,
        session_key: str,
        channel: str,
        agent_id: str = "main",
        peer_id: str = "",
        account_id: str = "",
        task_id: str = "",
        global_user_id: str = "",
    ) -> str:
        session_id = get_session_id(session_key)

        # Use in-memory cache on warm path; only hit disk on cold start
        if session_id in session_cache:
            history = session_cache[session_id]
        else:
            history = session_store.load_session(session_id)
            session_cache[session_id] = history

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
                date = (
                    source.split("/")[-1].replace(".jsonl", "").replace("_", "-") if source else ""
                )
                lines.append(f"- [{date}] {snippet}")
            mem_ctx = "\n".join(lines)
        else:
            mem_ctx = ""

        # Build system prompt: static prefix (cached once) + dynamic suffix
        system_prompt = build_system_prompt(
            mode="full",
            memory_context=mem_ctx,
            agent_id=agent_id,
            channel=channel,
            model=model_id,
            static_prefix=_static_prefix,
        )
        # Wrap as content blocks so Anthropic can cache the static prefix
        system_blocks = [
            {"type": "text", "text": _static_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_prompt[len(_static_prefix) :]},
        ]

        messages = list(history)
        old_len = len(messages)
        messages.append({"role": "user", "content": user_text})

        token = TURN_CONTEXT.set({"channel": channel, "peer_id": peer_id, "account_id": account_id})
        try:
            tool_handler = dispatcher.dispatch
            if tool_gate is not None and task_id and global_user_id:
                execution_context = ToolExecutionContext(
                    task_id=task_id,
                    session_id=session_key,
                    global_user_id=global_user_id,
                )

                def dispatch_with_gate(name: str, tool_input: dict[str, Any]) -> str:
                    tool_gate.authorize(name, tool_input, execution_context)

                    def emit_tool_event(event: dict[str, Any]) -> None:
                        record_trace(
                            str(event["event_type"]),
                            session_id=session_key,
                            task_id=task_id,
                            payload=dict(event),
                        )

                    return tool_recovery_executor.execute(
                        name,
                        tool_input,
                        dispatcher.dispatch_strict,
                        scope={
                            "task_id": task_id,
                            "session_id": session_key,
                            "global_user_id": global_user_id,
                        },
                        event_sink=emit_tool_event,
                    )

                tool_handler = dispatch_with_gate
            response, updated = resilience.run(
                system=system_blocks,
                messages=messages,
                tools=dispatcher.tools,
                tool_handler=tool_handler,
            )
        except Exception:
            # API call failed — the messages list may be in a dirty state.
            # Evict from cache so the next turn reloads from disk.
            session_cache.pop(session_id, None)
            raise
        finally:
            TURN_CONTEXT.reset(token)
        append_session_delta(session_id, old_len, updated)

        # Update cache with the latest messages (including this turn)
        session_cache[session_id] = updated

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
            bindings.add(
                Binding(
                    agent_id="main",
                    tier=4,
                    match_key="channel",
                    match_value="telegram",
                    priority=90,
                )
            )
        except Exception as exc:
            print_warn(f"Telegram 启动失败: {exc}")

    wecom_cli_channel: WeComCliChannel | None = None
    if server_mode and cfg.get("wecom_cli_poll_enabled", cfg.get("wecom_cli_enabled", False)):
        wc_account = ChannelAccount(
            channel="wecomcli",
            account_id="wecom-cli-default",
            config={
                "cli_bin": cfg.get("wecom_cli_bin", "wecom-cli"),
                "lookback_seconds": int(cfg.get("wecom_cli_lookback_seconds", 300) or 300),
                "overlap_seconds": int(cfg.get("wecom_cli_overlap_seconds", 5) or 5),
                "debug": cfg.get("wecom_cli_debug", False),
            },
        )
        try:
            wecom_cli_channel = WeComCliChannel(wc_account)
            ch_mgr.register(wecom_cli_channel)
            bindings.add(
                Binding(
                    agent_id="main",
                    tier=4,
                    match_key="channel",
                    match_value="wecomcli",
                    priority=92,
                )
            )
            print_info("WeCom CLI 轮询触发模式已启用")
        except Exception as exc:
            print_warn(f"WeCom CLI 通道启动失败: {exc}")

    workwechat_channel: WorkWeChatChannel | None = None
    workwechat_long: WorkWeChatLongConnectionChannel | None = None
    workwechat_sender: Any = None
    workwechat_mode = (
        str(cfg.get("workwechat_mode", "off")).strip().lower() if server_mode else "off"
    )
    if workwechat_mode not in ("long", "webhook", "off"):
        workwechat_mode = "off"

    ww_bot_id = cfg.get("workwechat_bot_id", "")
    ww_bot_secret = cfg.get("workwechat_bot_secret", "")
    ww_corp_id = cfg.get("workwechat_corp_id", "")
    ww_corp_secret = cfg.get("workwechat_corp_secret", "")
    ww_agent_id = int(cfg.get("workwechat_agent_id", 0) or 0)

    if workwechat_mode == "long" and ww_bot_id and ww_bot_secret:
        ww_account = ChannelAccount(
            channel="workwechat",
            account_id="workwechat-default",
            config={
                "bot_id": ww_bot_id,
                "secret": ww_bot_secret,
                "ws_url": cfg.get("workwechat_ws_url", "wss://openws.work.weixin.qq.com"),
                "ping_interval_sec": int(cfg.get("workwechat_ping_interval_sec", 30) or 30),
            },
        )
        try:
            workwechat_long = WorkWeChatLongConnectionChannel(ww_account)
            workwechat_long.start()
            ch_mgr.register_async(workwechat_long)
            workwechat_sender = workwechat_long
            bindings.add(
                Binding(
                    agent_id="main",
                    tier=4,
                    match_key="channel",
                    match_value="workwechat",
                    priority=95,
                )
            )
            print_info("Work WeChat 长连接模式已启用")
        except Exception as exc:
            print_warn(f"Work WeChat 长连接启动失败: {exc}")
    elif workwechat_mode == "webhook" and ww_corp_id and ww_corp_secret:
        ww_account = ChannelAccount(
            channel="workwechat",
            account_id="workwechat-default",
            config={
                "corp_id": ww_corp_id,
                "corp_secret": ww_corp_secret,
                "agent_id": ww_agent_id,
                "webhook_token": cfg.get("workwechat_webhook_token", ""),
            },
        )
        try:
            workwechat_channel = WorkWeChatChannel(ww_account)
            ch_mgr.register(workwechat_channel)
            workwechat_sender = workwechat_channel
            bindings.add(
                Binding(
                    agent_id="main",
                    tier=4,
                    match_key="channel",
                    match_value="workwechat",
                    priority=95,
                )
            )
            print_info("Work WeChat Webhook 模式已启用")
        except Exception as exc:
            print_warn(f"Work WeChat Webhook 启动失败: {exc}")

    dingtalk_channel: DingTalkChannel | None = None
    dingtalk_long: DingTalkLongConnectionChannel | None = None
    dingtalk_sender: Any = None
    dingtalk_mode = str(cfg.get("dingtalk_mode", "off")).strip().lower() if server_mode else "off"
    if dingtalk_mode not in ("long", "webhook", "off"):
        dingtalk_mode = "off"
    if dingtalk_mode == "long":
        dd_client_id = cfg.get("dingtalk_client_id", "")
        dd_client_secret = cfg.get("dingtalk_client_secret", "")
        if dd_client_id and dd_client_secret:
            dd_account = ChannelAccount(
                channel="dingtalk",
                account_id="dingtalk-default",
                config={
                    "client_id": dd_client_id,
                    "client_secret": dd_client_secret,
                    "access_token": cfg.get("dingtalk_access_token", ""),
                    "secret": cfg.get("dingtalk_secret", ""),
                    "webhook_url": cfg.get("dingtalk_webhook_url", ""),
                    "api_base": cfg.get("dingtalk_api_base", "https://oapi.dingtalk.com"),
                },
            )
            try:
                dingtalk_long = DingTalkLongConnectionChannel(dd_account)
                dingtalk_long.start()
                ch_mgr.register_async(dingtalk_long)
                dingtalk_sender = dingtalk_long
                bindings.add(
                    Binding(
                        agent_id="main",
                        tier=4,
                        match_key="channel",
                        match_value="dingtalk",
                        priority=94,
                    )
                )
                print_info("DingTalk 长连接模式已启用")
            except Exception as exc:
                print_warn(f"DingTalk 长连接启动失败: {exc}")
    elif dingtalk_mode == "webhook":
        dd_access_token = cfg.get("dingtalk_access_token", "")
        dd_webhook_url = cfg.get("dingtalk_webhook_url", "")
        if dd_access_token or dd_webhook_url:
            dd_account = ChannelAccount(
                channel="dingtalk",
                account_id="dingtalk-default",
                config={
                    "access_token": dd_access_token,
                    "secret": cfg.get("dingtalk_secret", ""),
                    "webhook_url": dd_webhook_url,
                    "api_base": cfg.get("dingtalk_api_base", "https://oapi.dingtalk.com"),
                    "webhook_token": cfg.get("dingtalk_webhook_token", ""),
                },
            )
            try:
                dingtalk_channel = DingTalkChannel(dd_account)
                ch_mgr.register(dingtalk_channel)
                dingtalk_sender = dingtalk_channel
                bindings.add(
                    Binding(
                        agent_id="main",
                        tier=4,
                        match_key="channel",
                        match_value="dingtalk",
                        priority=94,
                    )
                )
                print_info("DingTalk Webhook 模式已启用")
            except Exception as exc:
                print_warn(f"DingTalk 通道启动失败: {exc}")

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

        bindings.add(
            Binding(
                agent_id="main", tier=4, match_key="channel", match_value="feishu", priority=100
            )
        )

    enabled_channels: list[str] = ["cli"]
    if telegram_channel:
        enabled_channels.append("telegram")
    if wecom_cli_channel:
        enabled_channels.append("wecomcli")
    if feishu_sender:
        enabled_channels.append("feishu")
    if workwechat_sender:
        enabled_channels.append("workwechat")
    if dingtalk_sender:
        enabled_channels.append("dingtalk")

    registered_tools = dispatcher.list_tools()

    def deliver_via_channel(
        sender: Any,
        channel: str,
        to: str,
        text: str,
        **kwargs: Any,
    ) -> DeliveryReceipt:
        result = sender.send_with_receipt(to, text, **kwargs)
        if not result.accepted:
            raise RuntimeError(f"{channel} send failed")
        return DeliveryReceipt(
            platform_message_id=result.platform_message_id,
            confirmed=result.confirmed,
        )

    def deliver_fn(
        channel: str,
        to: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> DeliveryReceipt:
        meta = meta or {}
        render_options = {
            "format": meta.get("format", "text"),
            "render_metadata": dict(meta.get("render_metadata") or {}),
            "delivery_id": meta.get("delivery_id", ""),
            "idempotency_key": meta.get("idempotency_key", ""),
            "delivery_semantics": meta.get("delivery_semantics", "at_least_once"),
        }
        if channel in ("console", "cli"):
            print_assistant(text)
            return DeliveryReceipt()
        if channel == "telegram" and telegram_channel:
            return deliver_via_channel(telegram_channel, channel, to, text, **render_options)
        if channel == "wecomcli" and wecom_cli_channel:
            return deliver_via_channel(wecom_cli_channel, channel, to, text, **render_options)
        if channel == "feishu" and feishu_sender:
            return deliver_via_channel(feishu_sender, channel, to, text, **render_options)
        if channel == "workwechat" and workwechat_sender:
            return deliver_via_channel(
                workwechat_sender,
                channel,
                to,
                text,
                reply_req_id=meta.get("reply_req_id", ""),
                **render_options,
            )
        if channel == "dingtalk" and dingtalk_sender:
            return deliver_via_channel(dingtalk_sender, channel, to, text, **render_options)
        raise RuntimeError(f"unknown or unavailable delivery channel: {channel}")

    delivery_store = SQLiteDeliveryStore(workspace / "delivery.db")
    migration = LegacyDeliveryMigrator(
        legacy_queue_dir=workspace / "delivery-queue",
        store=delivery_store,
    ).migrate()
    if migration.pending_imported or migration.dead_letters_imported:
        print_info(
            "已迁移旧投递记录: "
            f"{migration.pending_imported} pending, "
            f"{migration.dead_letters_imported} dead-letter"
        )
    if migration.failed_imports:
        print_warn(f"有 {migration.failed_imports} 条旧投递记录迁移失败，源文件已保留")
    trace_recorder = TraceRecorder(workspace / "observability")
    feedback_store = SQLiteFeedbackStore(workspace / "feedback.db")
    identity_store = SQLiteIdentityStore(workspace / "identity.db")
    identity_session_resolver = IdentitySessionResolver(identity_store)
    session_policy = SessionPolicy.from_values(
        cfg["session_scope"],
        cfg["session_scope_version"],
    )
    runtime_capabilities: dict[str, ChannelCapability] = {}
    if workwechat_sender:
        runtime_capabilities["workwechat"] = ChannelCapability(
            text_limit=4000,
            markdown=workwechat_mode == "long",
            delivery_receipt=workwechat_mode == "webhook",
        )
    delivery_queue = DurableDeliveryQueue(
        delivery_store,
        renderer=OutboundRenderer(CapabilityRegistry(runtime_capabilities)),
        trace_recorder=trace_recorder,
    )
    notification_policy = SQLiteNotificationPolicy(
        workspace / "notifications.db",
        config=NotificationPolicyConfig(timezone_name="Asia/Shanghai"),
    )
    notification_gateway = NotificationGateway(
        policy=notification_policy,
        queue=delivery_queue,
        trace_recorder=trace_recorder,
    )
    task_store = SQLiteTaskStore(workspace / "interaction.db")
    task_state_machine = TaskStateMachine(task_store, trace_recorder=trace_recorder)
    request_store = SQLiteRequestStore(workspace / "interaction.db")
    clarification_service = ClarificationService(request_store, task_state_machine)
    confirmation_service = ConfirmationService(
        request_store,
        task_state_machine,
        ConfirmationTokenSigner(
            load_or_create_confirmation_secret(
                workspace,
                cfg.get("confirmation_token_secret") or None,
            )
        ),
    )
    tool_gate = ToolExecutionGate(confirmation_service)
    task_targets: dict[str, dict[str, str]] = {}
    task_targets_lock = threading.RLock()

    def emit_task_progress(event) -> None:
        with task_targets_lock:
            target = dict(task_targets.get(event.task_id, {}))
        if not target or event.type.value == "completed":
            return
        try:
            delivery_queue.enqueue(
                target["channel"],
                target["peer_id"],
                f"[任务 {event.task_id[:8]}] {event.message}",
                meta={
                    "session_id": target["session_id"],
                    "task_id": event.task_id,
                    "semantic_type": "progress",
                    "reply_req_id": target.get("reply_req_id", ""),
                },
            )
        except Exception:
            pass

    interaction_service = ProductionInteractionService(
        state_machine=task_state_machine,
        artifact_store=ArtifactStore(workspace / "interaction-artifacts"),
        progress_sink=emit_task_progress,
        clarification_service=clarification_service,
        confirmation_service=confirmation_service,
    )
    recovered_tasks = interaction_service.recover_interrupted()
    if recovered_tasks:
        print_warn(f"检测到 {len(recovered_tasks)} 个中断任务，已标记为 recovery_required")

    def notify_text(
        channel: str,
        peer_id: str,
        text: str,
        *,
        topic: str,
        dedupe_key: str | None = None,
    ):
        return notification_gateway.notify(
            NotificationRequest(
                session_id=f"{channel}:{peer_id}",
                topic=topic,
                channel=channel,
                account_id=channel,
                peer_id=peer_id,
                text=text,
                dedupe_key=dedupe_key,
            )
        )

    def record_trace(
        event_type: str,
        *,
        session_id: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> None:
        try:
            trace_recorder.record(
                event_type=event_type,
                producer="gateway",
                producer_version="gateway-v1",
                session_id=session_id,
                task_id=task_id,
                payload=payload,
            )
        except Exception:
            pass

    def resolve_identity_context(
        *,
        agent_id: str,
        channel: str,
        account_id: str,
        peer_id: str,
        platform_user_id: str,
        thread_id: str | None = None,
    ):
        resolution = identity_session_resolver.resolve(
            agent_id=agent_id,
            policy=session_policy,
            channel=channel,
            account_id=account_id or "default",
            peer_id=peer_id,
            platform_user_id=platform_user_id or peer_id,
            thread_id=thread_id,
        )
        record_trace(
            "identity_session_resolved",
            session_id=resolution.session.session_id,
            payload={
                "agent_id": agent_id,
                "channel": channel,
                "account_id": account_id or "default",
                "peer_id": peer_id,
                "global_user_id": resolution.identity.global_user_id,
                "scope_type": resolution.session.scope_type.value,
                "scope_version": resolution.session.scope_version,
                "route_key": resolution.session.route_key,
                "decision_reason": resolution.session.decision_reason,
                "identity_created": resolution.identity_created,
                "session_created": resolution.session_created,
            },
        )
        return resolution

    def register_task_target(
        task_id: str,
        *,
        session_id: str,
        channel: str,
        peer_id: str,
        reply_req_id: str = "",
    ) -> None:
        with task_targets_lock:
            task_targets[task_id] = {
                "session_id": session_id,
                "channel": channel,
                "peer_id": peer_id,
                "reply_req_id": reply_req_id,
            }

    def enqueue_task_text(
        text: str,
        *,
        task_id: str,
        session_id: str,
        channel: str,
        peer_id: str,
        semantic_type: str,
        reply_req_id: str = "",
    ) -> None:
        meta = {
            "session_id": session_id,
            "task_id": task_id,
            "semantic_type": semantic_type,
        }
        if reply_req_id:
            meta["reply_req_id"] = reply_req_id
        delivery_queue.enqueue(channel, peer_id, text, meta=meta)

    def format_clarification_prompt(request: ClarificationRequest) -> str:
        fields = ", ".join(request.required_fields)
        return (
            f"[任务 {request.task_id[:8]}] 需要补充信息：{request.question}\n"
            f"必填字段：{fields}\n"
            f"请显式回复：/clarify {request.request_id} field=value"
        )

    def format_confirmation_prompt(request: ConfirmationRequest, token: str) -> str:
        return (
            f"[任务 {request.task_id[:8]}] 高风险操作待确认：{request.action_summary}\n"
            f"批准一次：/confirm approve {request.request_id} {token}\n"
            f"拒绝并终止任务：/confirm deny {request.request_id} {token}"
        )

    def emit_clarification_request(request_id: str) -> None:
        request = request_store.get(request_id)
        if not isinstance(request, ClarificationRequest):
            return
        with task_targets_lock:
            target = dict(task_targets.get(request.task_id, {}))
        if target:
            enqueue_task_text(
                format_clarification_prompt(request),
                task_id=request.task_id,
                session_id=request.session_id,
                channel=target["channel"],
                peer_id=target["peer_id"],
                semantic_type="question",
                reply_req_id=target.get("reply_req_id", ""),
            )

    def emit_confirmation_request(request_id: str, token: str) -> None:
        request = request_store.get(request_id)
        if not isinstance(request, ConfirmationRequest):
            return
        with task_targets_lock:
            target = dict(task_targets.get(request.task_id, {}))
        if target:
            enqueue_task_text(
                format_confirmation_prompt(request, token),
                task_id=request.task_id,
                session_id=request.session_id,
                channel=target["channel"],
                peer_id=target["peer_id"],
                semantic_type="confirmation",
                reply_req_id=target.get("reply_req_id", ""),
            )

    interaction_service.clarification_sink = emit_clarification_request
    interaction_service.confirmation_sink = emit_confirmation_request

    def run_persisted_task(
        task_id: str,
        *,
        session_id: str,
        global_user_id: str,
        channel: str,
        agent_id: str,
        peer_id: str,
        account_id: str,
        resume: bool = False,
        resume_input: str | None = None,
    ) -> TaskExecutionOutcome:
        return interaction_service.run(
            task_id,
            global_user_id=global_user_id,
            executor=lambda context: run_turn(
                (
                    context.user_goal
                    if resume_input is None
                    else f"{context.user_goal}\n\n用户补充信息：{resume_input}"
                ),
                session_id,
                channel,
                agent_id,
                peer_id=peer_id,
                account_id=account_id,
                task_id=context.task_id,
                global_user_id=global_user_id,
            ),
            resume=resume,
        )

    def schedule_task_resume(
        task: TaskInstance,
        *,
        session_id: str,
        global_user_id: str,
        channel: str,
        account_id: str,
        peer_id: str,
        agent_id: str,
        reply_req_id: str = "",
        resume_input: str | None = None,
    ) -> None:
        register_task_target(
            task.task_id,
            session_id=session_id,
            channel=channel,
            peer_id=peer_id,
            reply_req_id=reply_req_id,
        )
        target = {
            "session_id": session_id,
            "channel": channel,
            "peer_id": peer_id,
            "reply_req_id": reply_req_id,
        }
        future = cmd_queue.enqueue(
            session_lane_name(session_id),
            lambda: run_persisted_task(
                task.task_id,
                session_id=session_id,
                global_user_id=global_user_id,
                channel=channel,
                agent_id=agent_id,
                peer_id=peer_id,
                account_id=account_id,
                resume=True,
                resume_input=resume_input,
            ),
        )
        future.add_done_callback(
            lambda completed: deliver_task_outcome(completed, fallback_target=target)
        )

    def deliver_task_outcome(future, *, fallback_target: dict[str, str]) -> None:
        try:
            outcome = future.result()
        except Exception as exc:
            print_warn(f"任务执行失败: {exc}")
            return
        with task_targets_lock:
            target = dict(task_targets.get(outcome.task.task_id, fallback_target))
            if outcome.task.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.EXPIRED,
            }:
                task_targets.pop(outcome.task.task_id, None)
        if outcome.task.state == TaskState.COMPLETED and outcome.text:
            text = outcome.text
            semantic_type = "result"
        elif outcome.task.state == TaskState.FAILED:
            message = outcome.task.failure.message if outcome.task.failure else "未知错误"
            text = f"[任务 {outcome.task.task_id[:8]}] 执行失败: {message}"
            semantic_type = "error"
        elif outcome.task.state == TaskState.CANCELLED:
            text = f"[任务 {outcome.task.task_id[:8]}] 已取消"
            semantic_type = "progress"
        else:
            return
        enqueue_task_text(
            text,
            task_id=outcome.task.task_id,
            session_id=target["session_id"],
            channel=target["channel"],
            peer_id=target["peer_id"],
            semantic_type=semantic_type,
            reply_req_id=target.get("reply_req_id", ""),
        )

    def dispatch_task_command(
        text: str,
        *,
        session_id: str,
        channel: str,
        account_id: str,
        peer_id: str,
        agent_id: str,
        global_user_id: str,
        reply_req_id: str = "",
    ) -> bool:
        parsed = parse_task_command(text)
        if parsed is None:
            return False
        principal = ControlPrincipal(
            session_id=session_id,
            global_user_id=global_user_id,
        )
        if parsed.action == ControlAction.RESUME:
            task = interaction_service.resolve_task(parsed, principal)
            register_task_target(
                task.task_id,
                session_id=session_id,
                channel=channel,
                peer_id=peer_id,
                reply_req_id=reply_req_id,
            )
            target = {
                "session_id": session_id,
                "channel": channel,
                "peer_id": peer_id,
                "reply_req_id": reply_req_id,
            }

            def resume_task() -> TaskExecutionOutcome:
                interaction_service.control(parsed, principal)
                return run_persisted_task(
                    task.task_id,
                    session_id=session_id,
                    global_user_id=principal.global_user_id,
                    channel=channel,
                    agent_id=agent_id,
                    peer_id=peer_id,
                    account_id=account_id,
                    resume=True,
                )

            future = cmd_queue.enqueue(
                session_lane_name(session_id),
                resume_task,
            )
            future.add_done_callback(
                lambda completed: deliver_task_outcome(completed, fallback_target=target)
            )
            response = f"任务 {task.task_id} 的恢复请求已进入会话队列"
        else:
            task = interaction_service.control(parsed, principal)
            response = _format_task(task)
        enqueue_task_text(
            response,
            task_id=task.task_id,
            session_id=session_id,
            channel=channel,
            peer_id=peer_id,
            semantic_type="progress",
            reply_req_id=reply_req_id,
        )
        return True

    def dispatch_request_command(
        text: str,
        *,
        session_id: str,
        channel: str,
        account_id: str,
        peer_id: str,
        agent_id: str,
        global_user_id: str,
        reply_req_id: str = "",
    ) -> bool:
        confirmation_command = parse_confirmation_command(text)
        if confirmation_command is not None:
            request = request_store.get(confirmation_command.request_id)
            if not isinstance(request, ConfirmationRequest):
                raise TypeError("request is not a confirmation")
            resolved = confirmation_service.decide(
                request.request_id,
                session_id=session_id,
                global_user_id=global_user_id,
                action_token=confirmation_command.token,
                decision=confirmation_command.decision,
                actor=global_user_id,
            )
            task = task_store.get_task(request.task_id)
            if resolved.state.value == "approved":
                schedule_task_resume(
                    task,
                    session_id=session_id,
                    global_user_id=global_user_id,
                    channel=channel,
                    account_id=account_id,
                    peer_id=peer_id,
                    agent_id=agent_id,
                    reply_req_id=reply_req_id,
                )
                response = f"确认已通过；任务 {task.task_id} 已进入恢复队列"
            else:
                response = f"操作已拒绝；任务 {task.task_id} 已终止"
            enqueue_task_text(
                response,
                task_id=task.task_id,
                session_id=session_id,
                channel=channel,
                peer_id=peer_id,
                semantic_type="progress",
                reply_req_id=reply_req_id,
            )
            return True

        clarification_command = parse_clarification_command(text)
        if clarification_command is None:
            return False
        request = request_store.get(clarification_command.request_id)
        if not isinstance(request, ClarificationRequest):
            raise TypeError("request is not a clarification")
        clarification_service.answer(
            request.request_id,
            session_id=session_id,
            global_user_id=global_user_id,
            answer=clarification_command.answer,
            actor=global_user_id,
        )
        task = task_store.get_task(request.task_id)
        schedule_task_resume(
            task,
            session_id=session_id,
            global_user_id=global_user_id,
            channel=channel,
            account_id=account_id,
            peer_id=peer_id,
            agent_id=agent_id,
            reply_req_id=reply_req_id,
            resume_input=json.dumps(
                clarification_command.answer,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        enqueue_task_text(
            f"澄清答复已记录；任务 {task.task_id} 已进入恢复队列",
            task_id=task.task_id,
            session_id=session_id,
            channel=channel,
            peer_id=peer_id,
            semantic_type="progress",
            reply_req_id=reply_req_id,
        )
        return True

    delivery_runner = DurableDeliveryRunner(delivery_queue, deliver_fn)
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
        reminder_interval = float(cfg.get("reminder_check_interval", 60) or 60)
        while not stop_event.is_set():
            stop_event.wait(timeout=reminder_interval)
            if stop_event.is_set():
                return
            try:
                due = reminder_store.get_due_reminders()
                for r in due:
                    reminder_text = f"[提醒] {r['content']}"
                    r_channel = str(r.get("channel", "")).strip()
                    r_peer = str(r.get("peer_id", "")).strip()
                    if server_mode and r_channel and r_peer:
                        target_channel, target_peer = r_channel, r_peer
                    elif (
                        server_mode
                        and feishu_sender
                        and (feishu_fixed_reminder_to or last_active_feishu_peer)
                    ):
                        target_channel = "feishu"
                        target_peer = feishu_fixed_reminder_to or last_active_feishu_peer
                    else:
                        target_channel, target_peer = "cli", "cli-user"
                    decision = notify_text(
                        target_channel,
                        target_peer,
                        reminder_text,
                        topic="reminder",
                        dedupe_key=f"reminder:{r.get('ts', '')}",
                    )
                    if decision.allowed or decision.reason in {
                        NotificationReason.UNSUBSCRIBED,
                        NotificationReason.EXPIRED,
                        NotificationReason.DUPLICATE,
                    }:
                        reminder_store.mark_reminded(r.get("ts", ""))
            except Exception:
                pass

    reminder_thread = threading.Thread(
        target=reminder_check_loop,
        daemon=True,
        name="reminder-check",
    )
    reminder_thread.start()

    def handle_inbound_message(msg: Any) -> None:
        nonlocal last_active_feishu_peer
        raw = msg.raw if isinstance(msg.raw, dict) else {}
        reply_req_id = str(raw.get("req_id", "") or "").strip()
        if msg.channel == "workwechat" and msg.text == "__workwechat_session_started__":
            if workwechat_sender and msg.peer_id:
                delivery_queue.enqueue(
                    "workwechat",
                    msg.peer_id,
                    _build_workwechat_intro(),
                    meta={"reply_req_id": reply_req_id} if reply_req_id else None,
                )
            return

        if msg.channel == "dingtalk" and msg.text == "__dingtalk_session_started__":
            if dingtalk_sender and msg.peer_id:
                delivery_queue.enqueue("dingtalk", msg.peer_id, _build_dingtalk_intro())
            return

        if msg.channel == "feishu":
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

        aid, legacy_sk = resolve_route(
            bindings,
            mgr,
            msg.channel,
            msg.peer_id,
            account_id=msg.account_id,
        )
        identity_context = resolve_identity_context(
            agent_id=aid,
            channel=msg.channel,
            account_id=msg.account_id,
            peer_id=msg.peer_id,
            platform_user_id=msg.sender_id or msg.peer_id,
        )
        sk = identity_context.session.session_id
        global_user_id = identity_context.identity.global_user_id
        record_trace(
            "inbound_routed",
            session_id=sk,
            payload={
                "channel": msg.channel,
                "account_id": msg.account_id,
                "peer_id": msg.peer_id,
                "agent_id": aid,
                "legacy_session_key": legacy_sk,
                "global_user_id": global_user_id,
                "session_scope": identity_context.session.scope_type.value,
                "session_scope_version": identity_context.session.scope_version,
                "text": msg.text,
                "raw": raw,
            },
        )
        try:
            if dispatch_request_command(
                msg.text,
                session_id=sk,
                channel=msg.channel,
                account_id=msg.account_id,
                peer_id=msg.peer_id,
                agent_id=aid,
                global_user_id=global_user_id,
                reply_req_id=reply_req_id,
            ):
                return
            if dispatch_task_command(
                msg.text,
                session_id=sk,
                channel=msg.channel,
                account_id=msg.account_id,
                peer_id=msg.peer_id,
                agent_id=aid,
                global_user_id=global_user_id,
                reply_req_id=reply_req_id,
            ):
                return
            task = interaction_service.submit(
                session_id=sk,
                global_user_id=global_user_id,
                user_goal=msg.text,
                runtime_ref=f"{aid}:{model_id}",
                trace_id=raw.get("event_id") or None,
            )
            register_task_target(
                task.task_id,
                session_id=sk,
                channel=msg.channel,
                peer_id=msg.peer_id,
                reply_req_id=reply_req_id,
            )
            target = {
                "session_id": sk,
                "channel": msg.channel,
                "peer_id": msg.peer_id,
                "reply_req_id": reply_req_id,
            }
            future = cmd_queue.enqueue(
                session_lane_name(sk),
                lambda: run_persisted_task(
                    task.task_id,
                    session_id=sk,
                    global_user_id=global_user_id,
                    channel=msg.channel,
                    agent_id=aid,
                    peer_id=msg.peer_id,
                    account_id=msg.account_id,
                ),
            )
            future.add_done_callback(
                lambda completed: deliver_task_outcome(completed, fallback_target=target)
            )
            if msg.channel == "feishu":
                save_feishu_state()
        except ActiveTaskExistsError:
            active = interaction_service.latest_task(sk)
            active_id = active.task_id if active else "unknown"
            enqueue_task_text(
                f"当前会话已有活动任务 {active_id}，可使用 /task status 或 /task cancel。",
                task_id=active_id,
                session_id=sk,
                channel=msg.channel,
                peer_id=msg.peer_id,
                semantic_type="progress",
                reply_req_id=reply_req_id,
            )
        except Exception as exc:
            record_trace(
                "inbound_rejected",
                session_id=sk,
                payload={
                    "channel": msg.channel,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
            )
            enqueue_task_text(
                f"任务请求未受理: {exc}",
                task_id="control",
                session_id=sk,
                channel=msg.channel,
                peer_id=msg.peer_id,
                semantic_type="error",
                reply_req_id=reply_req_id,
            )

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

    def wecom_cli_poll_loop() -> None:
        if not wecom_cli_channel:
            return
        poll_interval = float(cfg.get("wecom_cli_poll_interval", 3) or 3)
        health_log_interval = float(cfg.get("wecom_cli_health_log_interval", 30) or 30)
        last_health_log = 0.0
        while not stop_event.is_set():
            try:
                msgs = wecom_cli_channel.poll()
                for m in msgs:
                    inbound_queue.put(m)
                now_ts = time.time()
                if now_ts - last_health_log >= health_log_interval:
                    hs = wecom_cli_channel.get_health()
                    print_info(
                        "[wecomcli] 健康 "
                        f"poll={hs.get('poll_count', 0)} chats={hs.get('chat_count', 0)} "
                        f"raw_msgs={hs.get('raw_message_count', 0)} inbound={hs.get('inbound_count', 0)} "
                        f"send_ok={hs.get('send_ok', 0)} send_fail={hs.get('send_fail', 0)}"
                    )
                    if hs.get("last_error"):
                        print_warn(f"[wecomcli] 最近错误: {hs.get('last_error')}")
                    last_health_log = now_ts
            except Exception as exc:
                print_warn(f"WeCom CLI 轮询失败: {exc}")
            stop_event.wait(timeout=poll_interval)

    if telegram_channel:
        threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-poll").start()
    if wecom_cli_channel:
        threading.Thread(target=wecom_cli_poll_loop, daemon=True, name="wecomcli-poll").start()

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
            session = identity_store.get_session(sk)
            if not session.global_user_id:
                raise RuntimeError("resolved WebSocket session has no global identity")
            global_user_id = session.global_user_id
            resume = False
            resume_input = None
            confirmation_command = parse_confirmation_command(text)
            clarification_command = parse_clarification_command(text)
            if confirmation_command is not None:
                request = request_store.get(confirmation_command.request_id)
                if not isinstance(request, ConfirmationRequest):
                    raise TypeError("request is not a confirmation")
                resolved = confirmation_service.decide(
                    request.request_id,
                    session_id=sk,
                    global_user_id=global_user_id,
                    action_token=confirmation_command.token,
                    decision=confirmation_command.decision,
                    actor=global_user_id,
                )
                task = task_store.get_task(request.task_id)
                if resolved.state.value != "approved":
                    return f"操作已拒绝；任务 {task.task_id} 已终止"
                resume = True
            elif clarification_command is not None:
                request = request_store.get(clarification_command.request_id)
                if not isinstance(request, ClarificationRequest):
                    raise TypeError("request is not a clarification")
                clarification_service.answer(
                    request.request_id,
                    session_id=sk,
                    global_user_id=global_user_id,
                    answer=clarification_command.answer,
                    actor=global_user_id,
                )
                task = task_store.get_task(request.task_id)
                resume = True
                resume_input = json.dumps(
                    clarification_command.answer,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            else:
                task = interaction_service.submit(
                    session_id=sk,
                    global_user_id=global_user_id,
                    user_goal=text,
                    runtime_ref=f"{agent_id}:{model_id}",
                )
            future = cmd_queue.enqueue(
                session_lane_name(sk),
                lambda: run_persisted_task(
                    task.task_id,
                    session_id=sk,
                    global_user_id=global_user_id,
                    channel="websocket",
                    agent_id=agent_id,
                    peer_id=session.peer_id,
                    account_id="gateway",
                    resume=resume,
                    resume_input=resume_input,
                ),
            )
            outcome = await asyncio.wrap_future(future)
            if outcome.task.state == TaskState.COMPLETED:
                return outcome.text or ""
            if outcome.task.failure:
                raise RuntimeError(outcome.task.failure.message)
            if outcome.task.pending_request_ref:
                pending = request_store.get(outcome.task.pending_request_ref)
                if isinstance(pending, ConfirmationRequest):
                    return format_confirmation_prompt(
                        pending,
                        confirmation_service.signer.issue(pending),
                    )
                if isinstance(pending, ClarificationRequest):
                    return format_clarification_prompt(pending)
            raise RuntimeError(f"task ended in state {outcome.task.state.value}")

        def resolve_gateway_session(
            agent_id: str,
            channel: str,
            account_id: str,
            peer_id: str,
            platform_user_id: str,
        ) -> str:
            return resolve_identity_context(
                agent_id=agent_id,
                channel=channel,
                account_id=account_id,
                peer_id=peer_id,
                platform_user_id=platform_user_id,
            ).session.session_id

        gateway = GatewayServer(
            mgr,
            bindings,
            run_agent_fn=run_agent_ws,
            session_resolver_fn=resolve_gateway_session,
            host=gateway_host,
            port=gateway_port,
        )
        try:
            asyncio.run_coroutine_threadsafe(gateway.start(), loop).result(timeout=15)
            pump_future = asyncio.run_coroutine_threadsafe(async_channel_pump(), loop)
        except Exception as exc:
            print_warn(f"Gateway 启动失败（将继续运行渠道服务）: {exc}")
            gateway = None

    webhook_server: ThreadingHTTPServer | None = None
    webhook_thread: threading.Thread | None = None
    workwechat_webhook_server: ThreadingHTTPServer | None = None
    workwechat_webhook_thread: threading.Thread | None = None
    dingtalk_webhook_server: ThreadingHTTPServer | None = None
    dingtalk_webhook_thread: threading.Thread | None = None

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
                    data = json.dumps(
                        {"challenge": payload["challenge"]}, ensure_ascii=False
                    ).encode("utf-8")
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
        webhook_thread = threading.Thread(
            target=webhook_server.serve_forever, daemon=True, name="feishu-webhook"
        )
        webhook_thread.start()
        print_info(f"Feishu Webhook 已启动: http://{webhook_host}:{webhook_port}{webhook_path}")

    if server_mode and workwechat_channel and workwechat_mode == "webhook":
        ww_webhook_host = cfg.get("workwechat_webhook_host", "0.0.0.0")
        ww_webhook_port = int(cfg.get("workwechat_webhook_port", 8767))
        ww_webhook_path = str(cfg.get("workwechat_webhook_path", "/workwechat/events"))

        class WorkWeChatWebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path != ww_webhook_path:
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

                token = self.headers.get("X-Webhook-Token", "") or payload.get("token", "")
                inbound = workwechat_channel.parse_event(payload, token=token)
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

        workwechat_webhook_server = ThreadingHTTPServer(
            (ww_webhook_host, ww_webhook_port),
            WorkWeChatWebhookHandler,
        )
        workwechat_webhook_thread = threading.Thread(
            target=workwechat_webhook_server.serve_forever,
            daemon=True,
            name="workwechat-webhook",
        )
        workwechat_webhook_thread.start()
        print_info(
            f"Work WeChat Webhook 已启动: http://{ww_webhook_host}:{ww_webhook_port}{ww_webhook_path}"
        )

    if server_mode and dingtalk_channel and dingtalk_mode == "webhook":
        dd_webhook_host = cfg.get("dingtalk_webhook_host", "0.0.0.0")
        dd_webhook_port = int(cfg.get("dingtalk_webhook_port", 8768))
        dd_webhook_path = str(cfg.get("dingtalk_webhook_path", "/dingtalk/events"))

        class DingTalkWebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path != dd_webhook_path:
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

                token = self.headers.get("X-Webhook-Token", "") or payload.get("token", "")
                inbound = dingtalk_channel.parse_event(payload, token=token)
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

        dingtalk_webhook_server = ThreadingHTTPServer(
            (dd_webhook_host, dd_webhook_port),
            DingTalkWebhookHandler,
        )
        dingtalk_webhook_thread = threading.Thread(
            target=dingtalk_webhook_server.serve_forever,
            daemon=True,
            name="dingtalk-webhook",
        )
        dingtalk_webhook_thread.start()
        print_info(
            f"DingTalk Webhook 已启动: http://{dd_webhook_host}:{dd_webhook_port}{dd_webhook_path}"
        )

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
    print_info(f"  已启用通道: {', '.join(enabled_channels)}")
    print_info(f"  工具数量: {len(registered_tools)}")
    if server_mode:
        print_info(f"  Gateway: ws://{gateway_host}:{gateway_port}")
        print_info(f"  飞书模式: {feishu_mode}")
        print_info(f"  企业微信模式: {workwechat_mode}")
        print_info(f"  钉钉模式: {dingtalk_mode}")
        print_info("  运行中：多渠道服务 + Gateway + 后台任务")
    else:
        print_info(
            "  命令: /help, /status, /cron, /reminder, /memory, /queue, /lanes, /task, /confirm, /clarify, /identity, /feedback, /trigger"
        )
        print_info("  说明: 外部渠道轮询仅在 server 模式运行")
    print_info("=" * 60)
    print()

    try:
        if server_mode:
            while True:
                for msg in heartbeat.drain_output():
                    notify_text(
                        "cli",
                        "cli-user",
                        f"[心跳] {msg}",
                        topic="heartbeat",
                    )
                for msg in cron.drain_output():
                    notify_text(
                        "cli",
                        "cli-user",
                        f"[定时任务] {msg}",
                        topic="cron",
                    )
                time.sleep(1)
        else:
            while True:
                for msg in heartbeat.drain_output():
                    notify_text(
                        "cli",
                        "cli-user",
                        f"[心跳] {msg}",
                        topic="heartbeat",
                    )
                for msg in cron.drain_output():
                    notify_text(
                        "cli",
                        "cli-user",
                        f"[定时任务] {msg}",
                        topic="cron",
                    )

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
                        print_info(f"  当前模型: {model_id}")
                        print_info(f"  已启用通道: {', '.join(enabled_channels)}")
                        print_info(f"  已注册工具: {', '.join(registered_tools)}")
                        print_info(f"  心跳启用: {'是' if hb.get('enabled') else '否'}")
                        print_info(f"  心跳状态: {running}")
                        print_info(f"  上次运行: {hb.get('last_run', '从未')}")
                        print_info(f"  下次运行: {hb.get('next_in', 'n/a')}秒后")
                        ds = delivery_runner.get_stats()
                        print_info(f"  待投递: {ds.get('pending', 0)}")
                        print_info(f"  投递失败: {ds.get('failed', 0)}")
                        print_info(f"  已投递: {ds.get('delivered', 0)}")
                        print_info(f"  定时任务: {len(cron.list_jobs())} 个")
                        if wecom_cli_channel:
                            hs = wecom_cli_channel.get_health()
                            print_info(
                                "  WeCom轮询: "
                                f"poll={hs.get('poll_count', 0)}, chats={hs.get('chat_count', 0)}, "
                                f"raw_msgs={hs.get('raw_message_count', 0)}, inbound={hs.get('inbound_count', 0)}, "
                                f"send_ok={hs.get('send_ok', 0)}, send_fail={hs.get('send_fail', 0)}"
                            )
                            if hs.get("last_error"):
                                print_warn(f"  WeCom最近错误: {hs.get('last_error')}")
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
                            lane_name = {
                                "main": "主队列",
                                "cron": "定时任务",
                                "heartbeat": "心跳",
                            }.get(name, name)
                            print_info(
                                f"  {lane_name}: 队列深度={st.get('queue_depth', 0)}, "
                                f"活跃={st.get('active', 0)}, 最大并发={st.get('max_concurrency', 0)}"
                            )
                    elif cmd == "/feedback":
                        if len(parts) < 2:
                            print_warn("用法: /feedback <up|down|correction> [说明]")
                            continue
                        feedback_parts = parts[1].split(maxsplit=1)
                        feedback_kind = feedback_parts[0].lower()
                        feedback_text = feedback_parts[1] if len(feedback_parts) > 1 else None
                        feedback_sources = {
                            "up": (FeedbackSource.UPVOTE, 1),
                            "down": (FeedbackSource.DOWNVOTE, -1),
                            "correction": (FeedbackSource.CORRECTION, -1),
                        }
                        selected = feedback_sources.get(feedback_kind)
                        if selected is None:
                            print_warn("反馈类型仅支持 up、down、correction")
                            continue
                        source, rating = selected
                        cli_identity = resolve_identity_context(
                            agent_id="main",
                            channel="cli",
                            account_id="cli-local",
                            peer_id="cli-user",
                            platform_user_id="cli-user",
                        )
                        feedback = FeedbackRecord(
                            session_id=cli_identity.session.session_id,
                            source=source,
                            rating=rating,
                            text=feedback_text,
                        )
                        bad_case = feedback_store.add(feedback)
                        record_trace(
                            "user_feedback",
                            session_id=feedback.session_id,
                            payload={
                                "feedback_id": feedback.feedback_id,
                                "source": feedback.source.value,
                                "rating": feedback.rating,
                                "bad_case_category": (
                                    bad_case.category.value if bad_case else None
                                ),
                            },
                        )
                        suffix = f"，Bad Case: {bad_case.category.value}" if bad_case else ""
                        print_info(f"反馈已记录: {feedback.feedback_id}{suffix}")
                    elif cmd in ("/confirm", "/clarify"):
                        try:
                            cli_identity = resolve_identity_context(
                                agent_id="main",
                                channel="cli",
                                account_id="cli-local",
                                peer_id="cli-user",
                                platform_user_id="cli-user",
                            )
                            dispatch_request_command(
                                user_input,
                                session_id=cli_identity.session.session_id,
                                channel="cli",
                                account_id="cli-local",
                                peer_id="cli-user",
                                agent_id="main",
                                global_user_id=cli_identity.identity.global_user_id,
                            )
                        except Exception as exc:
                            print_warn(f"交互请求处理失败: {exc}")
                    elif cmd == "/task":
                        try:
                            cli_identity = resolve_identity_context(
                                agent_id="main",
                                channel="cli",
                                account_id="cli-local",
                                peer_id="cli-user",
                                platform_user_id="cli-user",
                            )
                            dispatch_task_command(
                                user_input,
                                session_id=cli_identity.session.session_id,
                                channel="cli",
                                account_id="cli-local",
                                peer_id="cli-user",
                                agent_id="main",
                                global_user_id=cli_identity.identity.global_user_id,
                            )
                        except Exception as exc:
                            print_warn(f"任务控制失败: {exc}")
                    elif cmd == "/identity":
                        identity_parts = user_input.split()
                        action = identity_parts[1].lower() if len(identity_parts) > 1 else "current"
                        try:
                            if action == "current":
                                cli_identity = resolve_identity_context(
                                    agent_id="main",
                                    channel="cli",
                                    account_id="cli-local",
                                    peer_id="cli-user",
                                    platform_user_id="cli-user",
                                )
                                print_info(f"Global User: {cli_identity.identity.global_user_id}")
                                print_info(
                                    "Session: "
                                    f"{cli_identity.session.session_id} "
                                    f"({cli_identity.session.scope_type.value} "
                                    f"v{cli_identity.session.scope_version})"
                                )
                                for link in cli_identity.identity.channel_links:
                                    print_info(
                                        "  Link: "
                                        f"{link.channel}/{link.account_id}/{link.platform_user_id}"
                                    )
                            elif action == "link" and len(identity_parts) == 6:
                                identity_session_resolver.identity.link_identity(
                                    identity_parts[2],
                                    channel=identity_parts[3],
                                    account_id=identity_parts[4],
                                    platform_user_id=identity_parts[5],
                                    actor="cli-admin",
                                    reason="explicit_cli_link",
                                )
                                print_info("身份绑定已创建；后续解析将使用目标 Global User")
                            elif action == "unlink" and len(identity_parts) == 5:
                                identity_store.unlink(
                                    channel=identity_parts[2],
                                    account_id=identity_parts[3],
                                    platform_user_id=identity_parts[4],
                                    actor="cli-admin",
                                    reason="explicit_cli_unlink",
                                )
                                print_info("身份绑定已解除并写入审计记录")
                            elif action == "merge" and len(identity_parts) == 4:
                                identity_session_resolver.identity.merge_identities(
                                    identity_parts[2],
                                    identity_parts[3],
                                    actor="cli-admin",
                                    reason="explicit_cli_merge",
                                )
                                print_info("身份已合并；历史会话保持不变，后续解析使用目标身份")
                            else:
                                print_warn(
                                    "用法: /identity current | "
                                    "/identity link <global_id> <channel> <account> <user> | "
                                    "/identity unlink <channel> <account> <user> | "
                                    "/identity merge <source_global_id> <target_global_id>"
                                )
                        except Exception as exc:
                            print_warn(f"身份操作失败: {exc}")
                    elif cmd == "/trigger":
                        print_info(f"  {heartbeat.trigger()}")
                    else:
                        print_warn(f"未知命令: {cmd}")
                    continue

                cli_identity = resolve_identity_context(
                    agent_id="main",
                    channel="cli",
                    account_id="cli-local",
                    peer_id="cli-user",
                    platform_user_id="cli-user",
                )
                sk = cli_identity.session.session_id
                global_user_id = cli_identity.identity.global_user_id
                record_trace(
                    "inbound_routed",
                    session_id=sk,
                    payload={
                        "channel": "cli",
                        "account_id": "cli-local",
                        "peer_id": "cli-user",
                        "agent_id": "main",
                        "global_user_id": global_user_id,
                        "session_scope": cli_identity.session.scope_type.value,
                        "session_scope_version": cli_identity.session.scope_version,
                        "text": user_input,
                    },
                )
                try:
                    task = interaction_service.submit(
                        session_id=sk,
                        global_user_id=global_user_id,
                        user_goal=user_input,
                        runtime_ref=f"main:{model_id}",
                    )
                    register_task_target(
                        task.task_id,
                        session_id=sk,
                        channel="cli",
                        peer_id="cli-user",
                    )
                    target = {
                        "session_id": sk,
                        "channel": "cli",
                        "peer_id": "cli-user",
                        "reply_req_id": "",
                    }
                    future = cmd_queue.enqueue(
                        session_lane_name(sk),
                        lambda: run_persisted_task(
                            task.task_id,
                            session_id=sk,
                            global_user_id=global_user_id,
                            channel="cli",
                            agent_id="main",
                            peer_id="cli-user",
                            account_id="cli-local",
                        ),
                    )
                    future.result(timeout=120)
                    deliver_task_outcome(future, fallback_target=target)
                except concurrent.futures.TimeoutError:
                    future.add_done_callback(
                        lambda completed: deliver_task_outcome(
                            completed,
                            fallback_target=target,
                        )
                    )
                    print_warn(f"请求仍在运行，任务 ID: {task.task_id}")
                except ActiveTaskExistsError:
                    active = interaction_service.latest_task(sk)
                    print_warn(f"当前会话已有活动任务: {active.task_id if active else 'unknown'}")
                except Exception as exc:
                    print_warn(f"错误: {exc}")

    except KeyboardInterrupt:
        print(f"\n{DIM}停止服务.{RESET}")

    finally:
        stop_event.set()
        cron_stop.set()
        heartbeat.stop()
        if reminder_thread.is_alive():
            reminder_thread.join(timeout=2.0)
        ch_mgr.close_all()
        cmd_queue.wait_for_all(timeout=3.0)
        if delivery_runner.stop():
            delivery_store.close()
        notification_policy.close()
        feedback_store.close()
        request_store.close()
        task_store.close()
        identity_store.close()

        if webhook_server:
            webhook_server.shutdown()
            webhook_server.server_close()
            if webhook_thread and webhook_thread.is_alive():
                webhook_thread.join(timeout=2.0)

        if workwechat_webhook_server:
            workwechat_webhook_server.shutdown()
            workwechat_webhook_server.server_close()
            if workwechat_webhook_thread and workwechat_webhook_thread.is_alive():
                workwechat_webhook_thread.join(timeout=2.0)

        if dingtalk_webhook_server:
            dingtalk_webhook_server.shutdown()
            dingtalk_webhook_server.server_close()
            if dingtalk_webhook_thread and dingtalk_webhook_thread.is_alive():
                dingtalk_webhook_thread.join(timeout=2.0)

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
        help="运行模式: cli (纯命令行) | server (多渠道 + 网关服务)",
    )
    parser.add_argument("--cli", action="store_true", help="等价于 --mode cli")
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

    if args.cli:
        args.mode = "cli"

    workspace = _resolve_workspace(args.workspace)
    run_app(workspace, cfg, gateway_host=args.host, gateway_port=args.port, run_mode=args.mode)


if __name__ == "__main__":
    main()
