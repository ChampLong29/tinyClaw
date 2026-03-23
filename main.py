#!/usr/bin/env python3
"""tinyClaw CLI 入口。

将所有模块组装成生产级 AI Agent Gateway。

用法:
    python main.py --help
    python main.py --mode full --workspace ./workspace
    python main.py --mode cli
    python main.py --mode gateway --port 8765

模块:
  - Agent 循环 + 工具分发 (agent/)
  - 会话存储 + 上下文保护 (session/)
  - 渠道适配器 (channel/)
  - 网关路由 + WebSocket 服务器 (gateway/)
  - 智能系统: soul, memory, skills, prompt builder (intelligence/)
  - 调度器: heartbeat + cron (scheduler/)
  - 投递: WAL 队列 + runner (delivery/)
  - 容错: 3层重试 + Auth 轮换 (resilience/)
  - 并发: 命名 Lane (concurrency/)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import threading
import time
from pathlib import Path

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from tinyclaw import config, client
from tinyclaw.agent import AgentLoop
from tinyclaw.agent.tools import ToolDispatcher
from tinyclaw.channel import cli as cli_channel
from tinyclaw.channel.base import ChannelManager
from tinyclaw.concurrency import CommandQueue, LANE_MAIN, LANE_CRON, LANE_HEARTBEAT
from tinyclaw.delivery import DeliveryQueue, DeliveryRunner, chunk_message
from tinyclaw.gateway import AgentManager, AgentConfig, Binding, BindingTable, GatewayServer
from tinyclaw.gateway.routing import build_session_key
from tinyclaw.intelligence import (
    BootstrapLoader, SoulSystem, MemoryStore, SkillsManager, build_system_prompt,
)
from tinyclaw.intelligence.reminder import ReminderStore
from tinyclaw.scheduler import HeartbeatRunner, CronService
from tinyclaw.utils.ansi import (
    CYAN, GREEN, YELLOW, DIM, RESET, BOLD, print_assistant, print_info, print_warn,
)
from tinyclaw.resilience import AuthProfile, ProfileManager


def _resolve_workspace(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    return Path(__file__).parent / "workspace"


def _create_client_factory(api_key: str, base_url: str | None):
    def factory():
        return client.create_client(api_key, base_url)
    return factory


def _parse_reminder_time(content: str) -> tuple[str, str | None, int | None]:
    """解析时间表达式，如「一分钟后」或「明天上午10点」。"""
    import re
    from datetime import datetime, timezone, timedelta

    # 匹配「一分钟后」「5分钟后」「半小时后」等模式
    m = re.search(r"(\d+|半小?[时分秒]?)后", content)
    minutes = None
    if m:
        unit = m.group(1)
        if unit in ("秒", "秒后"):
            minutes = 1 / 60
        elif unit in ("分", "分钟后", "分后"):
            minutes = int(re.search(r"\d+", content).group(0)) if re.search(r"\d+", content) else 1
        elif unit in ("小?时", "小时后", "小时后"):
            minutes = 60 * (int(re.search(r"\d+", content).group(0)) if re.search(r"\d+", content) else 1)
        elif "半小" in unit:
            minutes = 30
        if minutes is not None:
            # 从内容中移除时间表达式
            cleaned = re.sub(r"\d+分?钟?后?", "", content).strip()
            due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            return cleaned, due.isoformat(), None

    return content, None, None


def _set_reminder(store, content: str, due_time: str | None, minutes_from_now: int | None) -> str:
    """设置提醒。"""
    from datetime import datetime, timezone, timedelta

    due = None
    if due_time:
        try:
            due = datetime.fromisoformat(due_time.replace("Z", "+00:00"))
        except ValueError:
            return f"时间格式错误，请使用 ISO 格式如 2024-01-15T10:00:00"
    elif minutes_from_now:
        due = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    else:
        # 尝试从内容中解析时间
        content, due_str, _ = _parse_reminder_time(content)
        if due_str:
            due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            return store.write_reminder(content, due)
        return store.write_reminder(content)

    return store.write_reminder(content, due)


def _list_reminders(store) -> str:
    """列出所有提醒。"""
    due = store.get_due_reminders()
    if not due:
        return "没有待处理的提醒"
    lines = ["待处理提醒："]
    for r in due:
        lines.append(f"- {r['content']} (到期: {r.get('due', '未知')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# REPL 命令
# ---------------------------------------------------------------------------

def print_help() -> None:
    print_info("tinyClaw 命令:")
    print_info("  /help     -- 显示帮助")
    print_info("  /status   -- 显示系统状态")
    print_info("  /memory   -- 显示记忆统计")
    print_info("  /reminder -- 显示提醒列表")
    print_info("  /queue    -- 显示投递队列状态")
    print_info("  /lanes    -- 显示并发 Lane 状态")
    print_info("  /trigger  -- 立即触发心跳")
    print_info("  /cron     -- 列出定时任务")
    print_info("  quit/exit -- 退出程序")


# ---------------------------------------------------------------------------
# Full 模式: 集成所有功能
# ---------------------------------------------------------------------------

def run_full_mode(
    workspace: Path,
    api_key: str,
    model_id: str,
    base_url: str | None,
    heartbeat_interval: float,
    hb_start: int,
    hb_end: int,
) -> None:
    client_factory = _create_client_factory(api_key, base_url)
    bootstrap = BootstrapLoader(workspace)
    soul = SoulSystem(workspace)
    memory = MemoryStore(workspace)
    skills_mgr = SkillsManager(workspace)
    skills_mgr.discover()

    bindings = BindingTable()
    mgr = AgentManager(workspace / ".agents")
    mgr.register(AgentConfig(id="main", name="小 Luna", dm_scope="per-peer", model=model_id))

    cmd_queue = CommandQueue()
    cmd_queue.get_or_create_lane(LANE_MAIN, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_CRON, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_HEARTBEAT, max_concurrency=1)

    queue = DeliveryQueue(workspace / "delivery-queue")
    reminder_store = ReminderStore(workspace)
    dispatcher = ToolDispatcher()
    dispatcher.register_builtin(workdir=workspace)
    dispatcher.register({
        "name": "memory_write",
        "description": "保存重要事实到长期记忆。",
        "input_schema": {"type": "object", "properties": {
            "content": {"type": "string", "description": "要记住的事实或偏好。"}},
            "required": ["content"]},
    }, lambda content="", **_: memory.write_memory(content))
    dispatcher.register({
        "name": "memory_search",
        "description": "搜索长期记忆中的相关信息。",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词。"}},
            "required": ["query"]},
    }, lambda query="", **_: memory.hybrid_search(query))
    dispatcher.register({
        "name": "reminder_write",
        "description": "设置提醒。用于用户请求提醒时调用。",
        "input_schema": {"type": "object", "properties": {
            "content": {"type": "string", "description": "提醒内容（要做什么）。"},
            "due_time": {"type": "string", "description": "到期时间，ISO 格式（如 2024-01-15T10:00:00）。"},
            "minutes_from_now": {"type": "integer", "description": "从现在起几分钟（作为 due_time 的替代）。"}},
            "required": ["content"]},
    }, lambda content="", due_time=None, minutes_from_now=None, **_: (
        _set_reminder(reminder_store, content, due_time, minutes_from_now)
    ))
    dispatcher.register({
        "name": "reminder_list",
        "description": "列出所有待处理的提醒。",
        "input_schema": {"type": "object", "properties": {}},
    }, lambda **_: _list_reminders(reminder_store))

    def deliver_fn(ch: str, to: str, text: str) -> None:
        # CLI 模式不需要打印投递日志，响应已直接显示
        pass

    delivery_runner = DeliveryRunner(queue, deliver_fn)
    delivery_runner.start()

    heartbeat_lock = threading.Lock()
    heartbeat = HeartbeatRunner(
        workspace=workspace,
        lane_lock=heartbeat_lock,
        interval=heartbeat_interval,
        active_hours=(hb_start, hb_end),
        client_factory=client_factory,
        model=model_id,
    )
    heartbeat.start()

    cron_file = workspace / "CRON.json"
    cron = CronService(
        cron_file=cron_file,
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

    # 提醒检查线程：每 60 秒检查一次到期提醒
    def reminder_check_loop() -> None:
        """定期检查到期提醒并输出。"""
        last_checked: list = []
        while True:
            time.sleep(60)
            try:
                due = reminder_store.get_due_reminders()
                new_due = [r for r in due if r not in last_checked]
                if new_due:
                    for r in new_due:
                        print(f"\n{GREEN}{BOLD}[提醒] {RESET}{r['content']}")
                    last_checked = due
                else:
                    last_checked = due
            except Exception:
                pass

    threading.Thread(target=reminder_check_loop, daemon=True, name="reminder-check").start()

    messages: dict[str, list[dict]] = {}

    def make_user_turn(user_text: str, session_key: str, sys_prompt: str):
        def _turn() -> str:
            if session_key not in messages:
                messages[session_key] = []
            msgs = messages[session_key]
            loop = AgentLoop(
                client=client_factory(),
                model=model_id,
                system_prompt=sys_prompt,
                dispatcher=dispatcher,
            )
            try:
                reply, _ = loop.run_turn(msgs, user_text)
                chunks = chunk_message(reply, "console")
                for chunk in chunks:
                    queue.enqueue("console", "user", chunk)
                return reply
            except Exception as exc:
                return f"[错误: {exc}]"
        return _turn

    print_info("=" * 60)
    print_info(f"  tinyClaw  |  全功能模式  |  工作区: {workspace}")
    print_info(f"  模型: {model_id}")
    print_info(f"  心跳: 每 {heartbeat_interval} 秒，活跃时段 {hb_start}:00-{hb_end}:00")
    print_info(f"  定时任务: {len(cron.jobs)} 个")
    print_info("  命令: /help, /status, /memory, /reminder, /queue, /lanes, /trigger, /cron")
    print_info("=" * 60)
    print()

    while True:
        for msg in heartbeat.drain_output():
            print(f"{CYAN}[心跳]{RESET} {msg}")
            queue.enqueue("console", "user", f"[心跳] {msg}")
        for msg in cron.drain_output():
            print(f"{CYAN}[定时任务]{RESET} {msg}")
            queue.enqueue("console", "user", f"[定时任务] {msg}")

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
                # 心跳状态
                hb = heartbeat.status()
                running = "运行中" if hb.get("running") else "空闲"
                should = "是" if hb.get("should_run") else "否"
                print_info(f"  启用: {'是' if hb.get('enabled') else '否'}")
                print_info(f"  状态: {running}")
                print_info(f"  即将运行: {should} ({hb.get('reason', '')})")
                print_info(f"  上次运行: {hb.get('last_run', '从未')}")
                print_info(f"  下次运行: {hb.get('next_in', 'n/a')}后")
                # 投递状态
                ds = delivery_runner.get_stats()
                print_info(f"  待投递: {ds.get('pending', 0)}")
                print_info(f"  投递失败: {ds.get('failed', 0)}")
                print_info(f"  已投递: {ds.get('delivered', 0)}")
                # 定时任务
                jobs = cron.list_jobs()
                print_info(f"  定时任务: {len(jobs)} 个")
            elif cmd == "/memory":
                stats = memory.get_stats()
                print_info(f"  常青记忆: {stats.get('evergreen_chars', 0)} 字符")
                print_info(f"  日常记录: {stats.get('daily_entries', 0)} 条")
            elif cmd == "/reminder":
                print_info(f"  {_list_reminders(reminder_store)}")
            elif cmd == "/queue":
                qs = delivery_runner.get_stats()
                print_info(f"  待投递: {qs.get('pending', 0)}")
                print_info(f"  投递中: {qs.get('in_flight', 0)}")
                print_info(f"  失败: {qs.get('failed', 0)}")
                print_info(f"  已完成: {qs.get('delivered', 0)}")
            elif cmd == "/lanes":
                for name, st in cmd_queue.stats().items():
                    lane_name = {"main": "主队列", "cron": "定时任务", "heartbeat": "心跳"}.get(name, name)
                    print_info(f"  {lane_name}: 队列深度={st.get('queue_depth', 0)}, 活跃={st.get('active', 0)}, 最大并发={st.get('max_concurrency', 0)}")
            elif cmd == "/trigger":
                result = heartbeat.trigger()
                print_info(f"  {result}")
            elif cmd == "/cron":
                for j in cron.list_jobs():
                    tag = f"{GREEN}启用{RESET}" if j["enabled"] else f"{YELLOW}停用{RESET}"
                    next_in = j.get('next_in')
                    next_str = f"{next_in}秒后" if next_in is not None else "未计划"
                    last_run = j.get('last_run', '从未')
                    print(f"  [{tag}] {j['name']} | 下次: {next_str} | 上次: {last_run}")
            else:
                print_warn(f"未知命令: {cmd}")
            continue

        session_key = build_session_key("main", channel="console", peer_id="cli-user")
        sys_prompt = build_system_prompt(
            mode="full",
            bootstrap=bootstrap.load_all("full"),
            skills_block=skills_mgr.format_prompt_block(),
            memory_context="",
            agent_id="main",
            channel="console",
            model=model_id,
        )
        future = cmd_queue.enqueue(
            LANE_MAIN,
            make_user_turn(user_input, session_key, sys_prompt),
        )
        try:
            result = future.result(timeout=120)
            if result:
                print_assistant(result)
        except concurrent.futures.TimeoutError:
            print_warn("请求超时。")
        except Exception as exc:
            print_warn(f"错误: {exc}")
        continue

    heartbeat.stop()
    cron_stop.set()
    cmd_queue.wait_for_all(timeout=3.0)
    delivery_runner.stop()


# ---------------------------------------------------------------------------
# CLI 模式: 简单 REPL
# ---------------------------------------------------------------------------

def run_cli_mode(
    workspace: Path,
    api_key: str,
    model_id: str,
    base_url: str | None,
) -> None:
    cli = cli_channel.CLIChannel()
    bootstrap = BootstrapLoader(workspace)
    soul = SoulSystem(workspace)
    memory = MemoryStore(workspace)
    skills_mgr = SkillsManager(workspace)
    skills_mgr.discover()

    dispatcher = ToolDispatcher()
    dispatcher.register_builtin(workdir=workspace)
    dispatcher.register({
        "name": "memory_write",
        "description": "保存重要事实到长期记忆。",
        "input_schema": {"type": "object", "properties": {
            "content": {"type": "string"}}, "required": ["content"]},
    }, lambda content="", **_: memory.write_memory(content))
    dispatcher.register({
        "name": "memory_search",
        "description": "搜索长期记忆。",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    }, lambda query="", **_: "\n".join(
        f"- {r['snippet']}" for r in memory.hybrid_search(query)
    ))

    sys_prompt = build_system_prompt(
        mode="full",
        bootstrap=bootstrap.load_all("full"),
        skills_block=skills_mgr.format_prompt_block(),
        agent_id="main",
        channel="cli",
        model=model_id,
    )

    loop = AgentLoop(
        client=client.create_client(api_key, base_url),
        model=model_id,
        system_prompt=sys_prompt,
        dispatcher=dispatcher,
    )
    messages: list[dict] = []

    print_info("=" * 60)
    print_info(f"  tinyClaw  |  CLI 模式  |  工作区: {workspace}")
    print_info(f"  模型: {model_id}  |  输入 /help 查看命令")
    print_info("=" * 60)
    print()

    while True:
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
        if user_input == "/help":
            print_help()
            continue
        if user_input == "/memory":
            stats = memory.get_stats()
            print_info(f"  {stats}")
            continue

        try:
            reply, messages = loop.run_turn(messages, user_input)
            if reply:
                print_assistant(reply)
        except Exception as exc:
            print_warn(f"[错误: {exc}]")


# ---------------------------------------------------------------------------
# Gateway 模式: WebSocket 服务器
# ---------------------------------------------------------------------------

async def _run_agent(mgr, agent_id: str, sk: str, text: str) -> str:
    """Gateway 模式的 Agent runner。"""
    from tinyclaw.agent import AgentLoop
    from tinyclaw.agent.tools import ToolDispatcher
    from tinyclaw.intelligence import BootstrapLoader, build_system_prompt, SkillsManager
    import tinyclaw.client as _c

    cfg = mgr.get_agent(agent_id)
    if not cfg:
        return "[未知 Agent]"
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    base_url = os.getenv("ANTHROPIC_BASE_URL") or None
    ws = _c.create_client(api_key, base_url)
    workspace = Path.cwd() / "workspace"
    bootstrap = BootstrapLoader(workspace).load_all("full")
    skills = SkillsManager(workspace)
    skills.discover()
    sys_prompt = build_system_prompt(
        mode="full", bootstrap=bootstrap,
        skills_block=skills.format_prompt_block(),
        agent_id=agent_id, channel="websocket",
    )
    dispatcher = ToolDispatcher()
    dispatcher.register_builtin(workdir=workspace)
    loop = AgentLoop(ws, cfg.effective_model, sys_prompt, dispatcher)
    msgs = mgr.get_session(sk)
    try:
        reply, _ = loop.run_turn(msgs, text)
        return reply
    except Exception as exc:
        return f"[错误: {exc}]"


async def _gateway_main(port: int, host: str) -> None:
    from tinyclaw.gateway import AgentManager, AgentConfig, BindingTable, GatewayServer
    import asyncio

    mgr = AgentManager(Path.cwd() / "workspace" / ".agents")
    mgr.register(AgentConfig(id="main", name="小 Luna", model="claude-sonnet-4-20250514"))
    bindings = BindingTable()
    bindings.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))

    server = GatewayServer(
        mgr, bindings,
        run_agent_fn=_run_agent,
        host=host, port=port,
    )
    await server.start()
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="tinyClaw AI Agent Gateway")
    parser.add_argument("--mode", choices=["full", "cli", "gateway"], default="full",
                        help="运行模式: full (全部功能), cli (仅 REPL), gateway (WebSocket)")
    parser.add_argument("--workspace", default=None, help="工作区目录")
    parser.add_argument("--env", default=None, help=".env 文件路径")
    parser.add_argument("--port", type=int, default=8765, help="Gateway 端口 (gateway 模式)")
    parser.add_argument("--host", default="localhost", help="Gateway 主机 (gateway 模式)")
    args = parser.parse_args()

    env_path = Path(args.env) if args.env else Path.cwd() / ".env"
    cfg = config.load_config(env_path)

    if not cfg["anthropic_api_key"]:
        print(f"{YELLOW}错误: ANTHROPIC_API_KEY 未设置。{RESET}")
        print(f"{DIM}请将 .env.example 复制为 .env 并填入你的 API Key。{RESET}")
        sys.exit(1)

    workspace = _resolve_workspace(args.workspace)

    if args.mode == "gateway":
        import asyncio
        asyncio.run(_gateway_main(args.port, args.host))
    elif args.mode == "cli":
        run_cli_mode(
            workspace, cfg["anthropic_api_key"],
            cfg["model_id"], cfg["anthropic_base_url"],
        )
    else:
        run_full_mode(
            workspace, cfg["anthropic_api_key"],
            cfg["model_id"], cfg["anthropic_base_url"],
            cfg["heartbeat_interval"],
            cfg["heartbeat_active_start"],
            cfg["heartbeat_active_end"],
        )


if __name__ == "__main__":
    main()
