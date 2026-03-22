[English](README.md) | [中文](README.zh.md) | [日本語](README.ja.md)

# claw0 / tinyClaw

**From Zero to One: Build an AI Agent Gateway**

> 10 progressive sections -- every section is a single, runnable Python file.
> 3 languages (English, Chinese, Japanese) -- code + docs co-located.
> Production project structure under `src/tinyclaw/` -- ready to use.

---

## Two Parts

This repository has two parallel structures:

### 1. `sessions/` - Learning Path (教学路径)

10 progressive teaching files, each adding exactly one new concept.
Run any section directly:

```sh
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY

python sessions/zh/s01_agent_loop.py    # Agent Loop
python sessions/zh/s02_tool_use.py     # + Tool Use
python sessions/zh/s03_sessions.py      # + Sessions
python sessions/zh/s04_channels.py     # + Channels
python sessions/zh/s05_gateway_routing.py  # + Gateway
python sessions/zh/s06_intelligence.py  # + Intelligence
python sessions/zh/s07_heartbeat_cron.py  # + Heartbeat & Cron
python sessions/zh/s08_delivery.py     # + Delivery
python sessions/zh/s09_resilience.py   # + Resilience
python sessions/zh/s10_concurrency.py  # + Concurrency
```

### 2. `src/tinyclaw/` - Production Project (生产项目)

After completing the learning path, the production-ready project is organized under `src/tinyclaw/`:

```
src/tinyclaw/
├── config.py          # 配置加载 (.env)
├── client.py          # Anthropic client 工厂
├── utils/             # 工具函数
├── agent/             # Agent 循环 + 工具分发
├── session/           # 会话持久化 + 上下文保护
├── channel/           # 渠道适配器 (CLI/Telegram/Feishu)
├── gateway/           # 5层路由 + WebSocket 网关
├── intelligence/      # Soul/Memory/Skills/8层 Prompt
├── scheduler/         # 心跳 + Cron
├── delivery/          # WAL 投递队列
├── resilience/        # 3层重试 + Auth 轮换
└── concurrency/       # 命名 Lane
```

---

## Quick Start (Production Project)

```sh
# 安装
pip install -e .
# 或
pip install -r requirements.txt

# 配置
cp .env.example .env
# 编辑 .env:
#   ANTHROPIC_API_KEY=sk-ant-xxxxx
#   MODEL_ID=claude-sonnet-4-20250514

# 运行
python main.py --help

# 三种模式:
python main.py --mode cli      # 简单 REPL 对话
python main.py --mode full     # 全功能模式 (心跳 + cron + 投递 + 并发)
python main.py --mode gateway   # WebSocket 网关 (默认端口 8765)
```

### Full Mode Features

- Named lanes (main/cron/heartbeat) with FIFO queues
- Write-ahead log delivery queue with exponential backoff
- 3-layer retry: auth rotation → overflow compact → tool-use loop
- Heartbeat proactive checks during active hours
- Cron scheduler (at/every/cron expressions)
- Hybrid memory search (TF-IDF + simulated vector)
- 8-layer system prompt assembly
- Skills discovery from workspace directories

### Gateway Mode

```sh
# 启动 WebSocket 网关
python main.py --mode gateway --port 8765

# JSON-RPC 2.0 接口:
# ws://localhost:8765

# 发送消息:
{"jsonrpc": "2.0", "method": "send", "params": {"text": "Hello!"}, "id": 1}

# 列出 agents:
{"jsonrpc": "2.0", "method": "agents.list", "params": {}, "id": 2}

# 查看路由 bindings:
{"jsonrpc": "2.0", "method": "bindings.list", "params": {}, "id": 3}
```

---

## Architecture (Production)

```
tinyClaw
├── agent/        while True + stop_reason (Agent Loop)
│                 schema + handler (Tool Dispatcher)
├── session/      JSONL persistence (Session Store)
│                 3-stage overflow (Context Guard)
├── channel/      InboundMessage abstraction (base)
│                 Telegram long-polling / Feishu webhooks
├── gateway/      5-tier binding table (routing)
│                 WebSocket + JSON-RPC 2.0 (server)
├── intelligence/ soul / memory / skills / 8-layer prompt
├── scheduler/    Heartbeat (proactive checks)
│                 Cron (at/every/cron expressions)
├── delivery/     WAL queue + background runner
├── resilience/   Auth profile rotation
│                 3-layer retry onion
└── concurrency/  Named FIFO lanes (generation tracking)
```

---

## Section Dependencies (Learning Path)

```
s01 --> s02 --> s03 --> s04 --> s05
                 |               |
                 v               v
                s06 ----------> s07 --> s08
                 |               |
                 v               v
                s09 ----------> s10
```

---

## Configuration

`.env` 配置文件:

```sh
# LLM (必需)
ANTHROPIC_API_KEY=sk-ant-xxxxx
MODEL_ID=claude-sonnet-4-20250514

# 可选: 自定义 API 端点 (OpenRouter 等)
# ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1

# Telegram (可选)
# TELEGRAM_BOT_TOKEN=123456:ABC-DEF
# TELEGRAM_ALLOWED_CHATS=12345,67890

# Feishu/Lark (可选)
# FEISHU_APP_ID=cli_xxxxxxxx
# FEISHU_APP_SECRET=xxxxxxxx
# FEISHU_IS_LARK=true

# 心跳 (可选)
# HEARTBEAT_INTERVAL=1800
# HEARTBEAT_ACTIVE_START=9
# HEARTBEAT_ACTIVE_END=22
```

---

## Workspace Files

工作区文件位于 `workspace/`:

| 文件 | 用途 |
|------|------|
| `SOUL.md` | Agent 个性/灵魂定义 |
| `IDENTITY.md` | Agent 身份描述 |
| `TOOLS.md` | 工具使用指南 |
| `MEMORY.md` | 长期记忆 (常青内容) |
| `HEARTBEAT.md` | 心跳检查指令 |
| `BOOTSTRAP.md` | 启动时加载的提示 |
| `CRON.json` | Cron 任务配置 |
| `skills/` | Skill 技能目录 |

---

## Repository Structure

```
tinyClaw/
├── README.md              # This file
├── .env.example           # Configuration template
├── requirements.txt        # Python dependencies
├── pyproject.toml         # Package configuration
├── main.py                # Production CLI entry point
├── src/tinyclaw/          # Production project (按功能模块划分)
│   ├── __init__.py
│   ├── config.py
│   ├── client.py
│   ├── utils/
│   ├── agent/
│   ├── session/
│   ├── channel/
│   ├── gateway/
│   ├── intelligence/
│   ├── scheduler/
│   ├── delivery/
│   ├── resilience/
│   └── concurrency/
├── sessions/               # Learning path (教学代码, 保留原有结构)
│   ├── en/                # English
│   ├── zh/                # 中文
│   └── ja/                # 日本語
└── workspace/              # Agent 工作区
    ├── SOUL.md
    ├── TOOLS.md
    ├── skills/
    └── ...
```

---

## Prerequisites

- Python 3.10+
- An API key for Anthropic (or compatible provider via `ANTHROPIC_BASE_URL`)

## Dependencies

```
anthropic>=0.39.0
python-dotenv>=1.0.0
websockets>=12.0
croniter>=2.0.0
httpx>=0.27.0
```

---

## License

MIT
