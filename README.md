# tinyClaw

**从零构建生产级 AI Agent Gateway**

> 10 个递进章节，每章一个可运行的 Python 文件。
> 简体中文代码 + 文档并排放置。

---

## 两个部分

本仓库包含两个并行结构：

### 1. `sessions/zh/` - 学习路径

10 个递进式教学文件，每章增加一个核心概念：

```sh
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env: 设置 ANTHROPIC_API_KEY

python sessions/zh/s01_agent_loop.py       # Agent 循环
python sessions/zh/s02_tool_use.py        # + 工具调用
python sessions/zh/s03_sessions.py          # + 会话持久化
python sessions/zh/s04_channels.py         # + 渠道适配
python sessions/zh/s05_gateway_routing.py  # + 网关路由
python sessions/zh/s06_intelligence.py      # + 智能系统
python sessions/zh/s07_heartbeat_cron.py   # + 心跳 & Cron
python sessions/zh/s08_delivery.py         # + 可靠投递
python sessions/zh/s09_resilience.py        # + 容错弹性
python sessions/zh/s10_concurrency.py       # + 并发控制
```

### 2. `src/tinyclaw/` - 生产项目

完成学习路径后，可使用 `src/tinyclaw/` 下的生产级代码：

```
src/tinyclaw/
├── config.py           # .env 配置加载
├── client.py           # Anthropic client 工厂
├── utils/              # 工具函数 (ANSI 颜色等)
├── agent/              # Agent 循环 + 工具分发
├── session/            # 会话持久化 + 上下文保护
├── channel/            # 渠道适配器 (CLI / Telegram / Feishu)
├── gateway/            # 5 层路由 + WebSocket 网关
├── intelligence/       # Soul / Memory / Skills / 8 层 Prompt
├── scheduler/          # 心跳 + Cron 调度
├── delivery/           # WAL 投递队列
├── resilience/         # 3 层重试 + Auth 轮换
└── concurrency/        # 命名 Lane 并发控制
```

---

## 快速开始 (生产项目)

```sh
# 安装依赖
pip install -e .
# 或
pip install -r requirements.txt

# 配置
cp .env.example .env
# 编辑 .env:
#   ANTHROPIC_API_KEY=sk-ant-xxxxx
#   MODEL_ID=claude-sonnet-4-20250514

# 查看帮助
python main.py --help

# 三种运行模式:
python main.py --mode cli       # 简单 REPL 对话
python main.py --mode full      # 全功能模式 (心跳 + cron + 投递 + 并发)
python main.py --mode gateway   # WebSocket 网关 (默认端口 8765)
```

### Full 模式特性

- 命名 Lane (main / cron / heartbeat)，FIFO 队列
- WAL 写前日志投递队列，指数退避重试
- 3 层重试：Auth 轮换 → 上下文压缩 → 工具调用循环
- 心跳主动检查 (仅在活跃时段运行)
- Cron 调度器 (at / every / cron 表达式)
- 混合记忆搜索 (TF-IDF + 模拟向量)
- 8 层 System Prompt 动态组装
- Skills 技能发现

### Gateway 模式

```sh
# 启动 WebSocket 网关
python main.py --mode gateway --port 8765

# JSON-RPC 2.0 接口:
# ws://localhost:8765

# 发送消息:
{"jsonrpc": "2.0", "method": "send", "params": {"text": "你好!"}, "id": 1}

# 列出 agents:
{"jsonrpc": "2.0", "method": "agents.list", "params": {}, "id": 2}

# 查看路由 bindings:
{"jsonrpc": "2.0", "method": "bindings.list", "params": {}, "id": 3}
```

---

## 架构 (生产项目)

```
tinyClaw
├── agent/         while True + stop_reason (Agent 循环)
│                  schema + handler (工具分发)
├── session/       JSONL 持久化 (Session Store)
│                  3 阶段上下文溢出保护 (Context Guard)
├── channel/       InboundMessage 抽象
│                  Telegram 长轮询 / Feishu Webhook
├── gateway/       5 层 Binding 表 (路由)
│                  WebSocket + JSON-RPC 2.0
├── intelligence/  soul / memory / skills / 8 层 prompt
├── scheduler/     Heartbeat (主动检查)
│                  Cron (at/every/cron 表达式)
├── delivery/      WAL 队列 + 后台投递
├── resilience/    Auth Profile 轮换
│                  3 层重试洋葱
└── concurrency/  命名 FIFO Lane (generation 追踪)
```

---

## 章节依赖关系

```
s01 --> s02 --> s03 --> s04 --> s05
                 |               |
                 v               v
                s06 ----------> s07 --> s08
                 |               |
                 v               v
                s09 ----------> s10
```

| 章节 | 核心概念 | 行数 |
|------|----------|------|
| s01 | Agent 循环 = while + stop_reason | ~175 |
| s02 | 工具 = schema dict + handler map | ~445 |
| s03 | JSONL 持久化，超限摘要压缩 | ~890 |
| s04 | 所有平台都产生相同的 InboundMessage | ~780 |
| s05 | Binding 表映射 (channel, peer) 到 agent | ~625 |
| s06 | System prompt = 磁盘文件，切换即换人格 | ~750 |
| s07 | 定时线程 + 队列 | ~660 |
| s08 | 写磁盘优先，崩溃不丢消息 | ~870 |
| s09 | 3 层重试洋葱 + Auth 轮换 | ~1130 |
| s10 | 命名 Lane + FIFO 队列 | ~900 |

---

## 配置 (.env)

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

## 工作区文件

`workspace/` 目录下的文件：

| 文件 | 用途 |
|------|------|
| `SOUL.md` | Agent 个性 / 灵魂定义 |
| `IDENTITY.md` | Agent 身份描述 |
| `TOOLS.md` | 工具使用指南 |
| `MEMORY.md` | 长期记忆 (常青内容) |
| `USER.md` | 用户信息 |
| `HEARTBEAT.md` | 心跳检查指令 |
| `BOOTSTRAP.md` | 启动时加载的提示 |
| `AGENTS.md` | 多 Agent 配置 |
| `CRON.json` | Cron 任务配置 |
| `skills/` | Skill 技能目录 |

---

## 项目结构

```
tinyClaw/
├── README.md              # 本文件
├── .env.example           # 配置模板
├── requirements.txt       # Python 依赖
├── pyproject.toml         # 包配置
├── main.py                # 生产项目 CLI 入口
├── src/tinyclaw/          # 生产级代码 (按功能模块划分)
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
├── sessions/zh/          # 学习路径 (保留)
│   ├── s01_agent_loop.py
│   ├── s02_tool_use.py
│   └── ... (10 个 .py + 10 个 .md)
└── workspace/             # Agent 工作区
    ├── SOUL.md
    ├── TOOLS.md
    ├── skills/
    └── ...
```

---

## 环境要求

- Python 3.10+
- Anthropic API Key (或通过 `ANTHROPIC_BASE_URL` 配置兼容端点)

## 依赖

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
