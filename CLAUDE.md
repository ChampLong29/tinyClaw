# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**tinyClaw** is an educational project that teaches how to build a production-grade AI Agent Gateway from scratch through 10 progressive, runnable Python files. Each section adds exactly one new concept while keeping all prior code intact.

The project has two parallel structures:
- `sessions/zh/` - Learning path (10 progressive teaching files, Chinese)
- `src/tinyclaw/` - Production project (modular code by feature)

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with ANTHROPIC_API_KEY and MODEL_ID

# Run learning sections
python sessions/zh/s01_agent_loop.py
python sessions/zh/s02_tool_use.py
# ... through s10_concurrency.py

# Run production project
python main.py --mode cli       # Simple REPL
python main.py --mode full      # Full features (heartbeat + cron + delivery + concurrency)
python main.py --mode gateway   # WebSocket gateway (port 8765)
```

## Architecture

The project builds an AI Agent Gateway layer by layer:

```
s01: Agent Loop      - while True + stop_reason (the foundation)
s02: Tool Use       - dispatch table for model-called tools
s03: Sessions       - JSONL persistence, context overflow handling
s04: Channels       - Telegram + Feishu adapters
s05: Gateway        - 5-tier routing, session isolation
s06: Intelligence   - soul, memory, skills, 8-layer prompt assembly
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
├── config.py           # .env configuration loading
├── client.py           # Anthropic client factory
├── utils/              # ANSI colors, helpers
├── agent/              # Agent loop + tool dispatcher
├── session/            # JSONL store + context guard
├── channel/            # CLI / Telegram / Feishu adapters
├── gateway/            # 5-tier routing + WebSocket server
├── intelligence/       # soul / memory / skills / prompt builder
├── scheduler/          # heartbeat + cron
├── delivery/           # WAL queue + runner
├── resilience/         # 3-layer retry + auth rotation
└── concurrency/       # named FIFO lanes
```

## Key Patterns

- **Agent Loop**: `messages[]` accumulates history, `stop_reason` controls flow (`end_turn` vs `tool_use`)
- **Tool Dispatch**: schema dict + handler map; model picks name, code looks it up
- **Session Storage**: JSONL append-only, replay on read, summarize for overflow
- **Channel Abstraction**: All platforms produce standardized `InboundMessage`
- **Prompt Assembly**: 8-layer stack (soul, identity, tools, etc.) merged from disk files
- **Named Lanes**: Concurrency isolation via FIFO queues per (channel, peer) pair
