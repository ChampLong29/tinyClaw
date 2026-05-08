# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**tinyClaw** is an educational project that teaches how to build a production-grade AI Agent Gateway from scratch through 10 progressive, runnable Python files. Each section adds exactly one new concept while keeping all prior code intact.

The project has two parallel structures:
- `sessions/zh/` - Learning path (10 progressive teaching files, Chinese)
- `src/tinyclaw/` - Production project (modular code by feature)

## Common Commands

```bash
# Install dependencies (uv is the only package manager — no requirements.txt)
uv sync

# Optional extras
uv sync --extra dev        # pytest + ruff
uv sync --extra telegram   # python-telegram-bot

# Configure environment
cp .env.example .env
# Edit .env with ANTHROPIC_API_KEY and MODEL_ID

# Run learning sections
python sessions/zh/s01_agent_loop.py
python sessions/zh/s02_tool_use.py
# ... through s10_concurrency.py

# Run production project
python main.py --mode cli       # CLI REPL with /status, /cron, /lanes, etc.
python main.py --mode server    # Multi-channel + gateway + background tasks (heartbeat/cron/delivery)
python main.py --mode server --port 8877   # Custom gateway WebSocket port
```

## Architecture

The project builds an AI Agent Gateway layer by layer:

```
s01: Agent Loop      - while True + stop_reason (the foundation)
s02: Tool Use       - dispatch table for model-called tools
s03: Sessions       - JSONL persistence, context overflow handling
s04: Channels       - Telegram / Feishu / WorkWeChat / DingTalk / WeCom CLI adapters
s05: Gateway        - 5-tier routing, session isolation
s06: Intelligence   - soul, memory, skills, 8-layer prompt assembly, reminders
s07: Heartbeat      - proactive agent + cron scheduler
s08: Delivery       - write-ahead queue with backoff
s09: Resilience     - 3-layer retry, auth profile rotation
s10: Concurrency    - named lanes with FIFO queues
```

Section dependencies:
- s01 → s02 → s03 → s04 → s05
- s03 → s06 → s07 → s08
- s06,s03 → s09 → s10

## Production Project Structure (`src/tinyclaw/`)

```
src/tinyclaw/
├── config.py           # .env configuration loading (all channel env vars)
├── client.py           # Anthropic client factory
├── utils/              # ANSI colors, timezone (Beijing time formatting)
├── agent/              # Agent loop + ToolDispatcher (register_builtin + register)
├── session/            # JSONL store + context guard
├── channel/            # Telegram / Feishu / WorkWeChat / DingTalk / WeComCLI adapters
├── gateway/            # 5-tier BindingTable routing + WebSocket JSON-RPC server
├── intelligence/       # soul / memory / skills / prompt builder / reminder store
├── scheduler/          # heartbeat + cron
├── delivery/           # WAL queue + chunker + runner
├── resilience/         # 3-layer retry (ResilienceRunner) + auth rotation
└── concurrency/        # named FIFO lanes (CommandQueue)
```

## Key Patterns

- **Agent Loop**: `messages[]` accumulates history, `stop_reason` controls flow (`end_turn` vs `tool_use`)
- **Tool Dispatch**: `ToolDispatcher` with `register_builtin()` for built-in tools + `register()` for custom tools; model picks name, dispatcher looks up handler
- **Session Storage**: JSONL append-only, replay on read, summarize for overflow. Session keys are persisted in `session_key_map.json`. Hot path uses in-memory cache (`session_cache` dict) — disk is only read on cold start
- **Channel Abstraction**: All platforms produce standardized `InboundMessage` via `ChannelManager`. Async channels (Feishu long connection, WorkWeChat long connection, DingTalk long connection) use `register_async()`; sync channels (Telegram polling, WeCom CLI polling, webhooks) use `register()`
- **Prompt Assembly**: 8-layer stack built via `build_static_prefix()` (Layers 1-6, cached at startup) + dynamic suffix (memory context, runtime context, channel hints). Static prefix is passed as a `cache_control`-marked content block to enable Anthropic prompt caching
- **Prompt Caching**: `_static_prefix` is built once at startup and never changes across turns. It's wrapped in `{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}` and passed to the API as a content block list, allowing the LLM provider to cache KV states and skip prefill on subsequent turns
- **Named Lanes**: Concurrency isolation via FIFO queues per `(channel, peer)` pair, managed by `CommandQueue` with `LANE_MAIN`, `LANE_CRON`, `LANE_HEARTBEAT`
- **Run Modes**: `--mode cli` (single-user REPL, stdin/stdout) vs `--mode server` (all channels active, background threads for heartbeat/cron/delivery/reminder-check)
- **Reminder System**: `ReminderStore` persists reminders to workspace, `reminder_check_loop` polls and enqueues due reminders to delivery. Model can call `reminder_write`/`reminder_list` tools
- **Timezone**: All user-facing time is formatted to Beijing time (UTC+8) via `utils/timezone.py`

## Run Mode Differences

| Feature | `--mode cli` | `--mode server` |
|---|---|---|
| CLI REPL | Yes | No |
| Telegram polling | No | Yes (if token configured) |
| Feishu long connection / webhook | No | Yes (if configured) |
| WorkWeChat long connection / webhook | No | Yes (if configured) |
| DingTalk long connection / webhook | No | Yes (if configured) |
| WeCom CLI polling | No | Yes (if poll enabled) |
| WebSocket gateway | No | Yes |
| Heartbeat + Cron | No | Yes |
| Delivery queue | Yes (console only) | Yes (all channels) |
| Reminder check loop | Yes (console only) | Yes (all channels) |
| WeCom CLI as callable tool | Yes | Yes |
