#!/usr/bin/env python3
"""tinyClaw CLI entry point.

Assembles all modules into a production-ready agent gateway.

Usage:
    python main.py --help
    python main.py --mode full --workspace ./workspace
    python main.py --mode cli
    python main.py --mode gateway --port 8765

Modules assembled:
  - Agent loop + tool dispatcher (agent/)
  - Session store + context guard (session/)
  - Channel adapters (channel/)
  - Gateway routing + WebSocket server (gateway/)
  - Intelligence: soul, memory, skills, prompt builder (intelligence/)
  - Scheduler: heartbeat + cron (scheduler/)
  - Delivery: WAL queue + runner (delivery/)
  - Resilience: 3-layer retry + auth rotation (resilience/)
  - Concurrency: named lanes (concurrency/)
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


# ---------------------------------------------------------------------------
# REPL commands
# ---------------------------------------------------------------------------

def print_help() -> None:
    print_info("tinyClaw CLI commands:")
    print_info("  /help     -- show this help")
    print_info("  /status   -- show system status")
    print_info("  /memory   -- show memory stats")
    print_info("  /queue    -- show delivery queue stats")
    print_info("  /lanes    -- show concurrency lane stats")
    print_info("  /trigger  -- trigger heartbeat now")
    print_info("  /cron     -- list cron jobs")
    print_info("  quit/exit -- exit")


# ---------------------------------------------------------------------------
# Full mode: all features assembled
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
    mgr.register(AgentConfig(id="main", name="Luna", dm_scope="per-peer", model=model_id))

    cmd_queue = CommandQueue()
    cmd_queue.get_or_create_lane(LANE_MAIN, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_CRON, max_concurrency=1)
    cmd_queue.get_or_create_lane(LANE_HEARTBEAT, max_concurrency=1)

    queue = DeliveryQueue(workspace / "delivery-queue")
    dispatcher = ToolDispatcher()
    dispatcher.register_builtin(workdir=workspace)
    dispatcher.register({
        "name": "memory_write",
        "description": "Save important facts to long-term memory.",
        "input_schema": {"type": "object", "properties": {
            "content": {"type": "string", "description": "The fact or preference to remember."}},
            "required": ["content"]},
    }, lambda content="", **_: memory.write_memory(content))
    dispatcher.register({
        "name": "memory_search",
        "description": "Search long-term memory for relevant information.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query."}},
            "required": ["query"]},
    }, lambda query="", **_: memory.hybrid_search(query))

    def deliver_fn(ch: str, to: str, text: str) -> None:
        print_info(f"  [delivery] {ch} -> {to}: {text[:60]}...")

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
                return f"[error: {exc}]"
        return _turn

    print_info("=" * 60)
    print_info(f"  tinyClaw  |  full mode  |  workspace: {workspace}")
    print_info(f"  Model: {model_id}")
    print_info(f"  Heartbeat: {heartbeat_interval}s, {hb_start}:00-{hb_end}:00")
    print_info(f"  Cron jobs: {len(cron.jobs)}")
    print_info("  Commands: /help, /status, /memory, /queue, /lanes, /trigger, /cron")
    print_info("=" * 60)
    print()

    while True:
        for msg in heartbeat.drain_output():
            print(f"{CYAN}[heartbeat]{RESET} {msg}")
            queue.enqueue("console", "user", f"[Heartbeat] {msg}")
        for msg in cron.drain_output():
            print(f"{CYAN}[cron]{RESET} {msg}")
            queue.enqueue("console", "user", f"[Cron] {msg}")

        try:
            user_input = input(f"{CYAN}{BOLD}You > {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye.{RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}Goodbye.{RESET}")
            break

        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            if cmd == "/help":
                print_help()
            elif cmd == "/status":
                print_info(f"  Heartbeat: {heartbeat.status()}")
                print_info(f"  Delivery: {delivery_runner.get_stats()}")
                print_info(f"  Cron: {len(cron.list_jobs())} jobs")
            elif cmd == "/memory":
                stats = memory.get_stats()
                print_info(f"  Memory stats: {stats}")
            elif cmd == "/queue":
                stats = delivery_runner.get_stats()
                print_info(f"  Queue: {stats}")
            elif cmd == "/lanes":
                for name, st in cmd_queue.stats().items():
                    print_info(f"  {name}: {st}")
            elif cmd == "/trigger":
                result = heartbeat.trigger()
                print_info(f"  {result}")
            elif cmd == "/cron":
                for j in cron.list_jobs():
                    tag = f"{GREEN}ON{RESET}" if j["enabled"] else f"{YELLOW}OFF{RESET}"
                    print(f"  [{tag}] {j['name']} (next: {j.get('next_in', 'n/a')}s)")
            else:
                print_warn(f"Unknown: {cmd}")
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
            print_warn("Request timed out.")
        except Exception as exc:
            print_warn(f"Error: {exc}")
        continue

    heartbeat.stop()
    cron_stop.set()
    cmd_queue.wait_for_all(timeout=3.0)
    delivery_runner.stop()


# ---------------------------------------------------------------------------
# CLI mode: simple REPL
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
        "description": "Save important facts to long-term memory.",
        "input_schema": {"type": "object", "properties": {
            "content": {"type": "string"}}, "required": ["content"]},
    }, lambda content="", **_: memory.write_memory(content))
    dispatcher.register({
        "name": "memory_search",
        "description": "Search long-term memory.",
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
    print_info(f"  tinyClaw  |  CLI mode  |  workspace: {workspace}")
    print_info(f"  Model: {model_id}  |  Type /help for commands")
    print_info("=" * 60)
    print()

    while True:
        try:
            user_input = input(f"{CYAN}{BOLD}You > {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye.{RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}Goodbye.{RESET}")
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
            print_warn(f"[error: {exc}]")


# ---------------------------------------------------------------------------
# Gateway mode: WebSocket server
# ---------------------------------------------------------------------------

async def _run_agent(mgr, agent_id: str, sk: str, text: str) -> str:
    """Agent runner for gateway mode."""
    from tinyclaw.agent import AgentLoop
    from tinyclaw.agent.tools import ToolDispatcher
    from tinyclaw.intelligence import BootstrapLoader, build_system_prompt, SkillsManager
    import tinyclaw.client as _c

    cfg = mgr.get_agent(agent_id)
    if not cfg:
        return "[unknown agent]"
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
        return f"[error: {exc}]"


async def _gateway_main(port: int, host: str) -> None:
    from tinyclaw.gateway import AgentManager, AgentConfig, BindingTable, GatewayServer
    import asyncio

    mgr = AgentManager(Path.cwd() / "workspace" / ".agents")
    mgr.register(AgentConfig(id="main", name="Luna", model="claude-sonnet-4-20250514"))
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="tinyClaw AI Agent Gateway")
    parser.add_argument("--mode", choices=["full", "cli", "gateway"], default="full",
                        help="Run mode: full (all features), cli (REPL only), gateway (WebSocket)")
    parser.add_argument("--workspace", default=None, help="Workspace directory")
    parser.add_argument("--env", default=None, help=".env file path")
    parser.add_argument("--port", type=int, default=8765, help="Gateway port (gateway mode)")
    parser.add_argument("--host", default="localhost", help="Gateway host (gateway mode)")
    args = parser.parse_args()

    env_path = Path(args.env) if args.env else Path.cwd() / ".env"
    cfg = config.load_config(env_path)

    if not cfg["anthropic_api_key"]:
        print(f"{YELLOW}Error: ANTHROPIC_API_KEY not set.{RESET}")
        print(f"{DIM}Copy .env.example to .env and fill in your key.{RESET}")
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
