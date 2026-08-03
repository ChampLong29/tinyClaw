# tinyClaw

tinyClaw 是一个本地优先、可恢复的多渠道 AI Agent Gateway。生产代码位于
`src/tinyclaw/`，提供身份与会话边界、持久化任务交互、可靠有序投递、主动通知治理、
Trace/Replay，以及 CLI、WebSocket、Telegram、飞书、企业微信和钉钉接入。

仓库同时保留 `sessions/zh/` 教学系列。教学代码用于逐章学习，生产能力和运行说明以本
README、`main.py`、`src/tinyclaw/` 与 `docs/roadmap/` 为准。

## 当前状态

截至 2026-08-03：

- 需求验收清单：11/11。
- 自动化测试：109 项。
- 离线可靠投递演练：4/4 场景通过。
- 代码级目标态已完成；尚未执行各真实平台的专用沙箱网络、Auth 过期和 ACK 丢失演练。
- 历史静态检查债务尚未清零：生产目录有 20 条 Ruff lint 诊断，完整格式检查涉及
  26 个文件；教学系列不计入该数字。

详细验收证据见
[需求与验收清单](docs/roadmap/resume-target-requirements.md)；真实平台演练步骤见
[Durable Delivery 部署验收](docs/roadmap/delivery-sandbox-acceptance.md)。

## 核心能力

- 多渠道 Gateway：CLI、WebSocket、Telegram、飞书/Lark、企业微信、钉钉和 WeCom CLI。
- Identity/Session：账号、渠道、用户隔离，显式 Link/Unlink/Merge，版本化 Session Policy。
- Interaction State：任务持久化、revision、进度、取消、暂停、修改、恢复和中断恢复。
- 安全交互：结构化澄清，高风险工具签名确认，授权与精确参数绑定且仅消费一次。
- Tool Recovery：错误分类、只读/幂等安全重试、权限确认、部分副作用转人工处理。
- Durable Delivery：SQLite、Lane Sequence、Lease、FIFO、ACK、Retry Wait、Dead Letter。
- 渠道化呈现：Capability Registry、内容感知分片、卡片/附件降级、Semantic Snapshot。
- 主动通知：Heartbeat、Cron、Reminder 统一经过订阅、静默、频控、去重和 Digest 策略。
- 可观测与评测：append-only Trace、Artifact、反馈/Bad Case、Replay Case 和回归报告。

## 快速开始

### 1. 安装

要求 Python 3.10+，推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --extra dev
```

可选依赖：

```bash
uv sync --extra telegram
```

钉钉 Stream 长连接当前需要单独安装 `dingtalk-stream`；Webhook 模式不需要该依赖。

### 2. 配置

PowerShell：

```powershell
Copy-Item .env.example .env
```

Linux/macOS：

```bash
cp .env.example .env
```

至少配置：

```dotenv
ANTHROPIC_API_KEY=your-api-key
MODEL_ID=your-model-id
```

`ANTHROPIC_BASE_URL` 仅适用于实现 Anthropic Messages API 线协议的兼容端点。使用 Anthropic
官方端点时保持未设置；不要把 OpenAI Chat Completions 端点直接填入。

### 3. 运行

CLI REPL：

```bash
uv run tinyclaw --mode cli
```

多渠道与 WebSocket Gateway：

```bash
uv run tinyclaw --mode server --host localhost --port 8765
```

也可以使用：

```bash
uv run python main.py --mode cli
uv run python -m tinyclaw --mode server
```

支持的入口参数：

| 参数 | 含义 | 默认值 |
|---|---|---|
| `--mode cli\|server` | CLI REPL 或多渠道服务 | `server` |
| `--cli` | 等价于 `--mode cli` | 关闭 |
| `--workspace PATH` | 运行数据与 Agent 工作区 | 仓库 `workspace/` |
| `--env PATH` | 指定环境变量文件 | 当前目录 `.env` |
| `--host HOST` | WebSocket Gateway 监听地址 | `localhost` |
| `--port PORT` | WebSocket Gateway 端口 | `8765` |

## CLI 交互与控制

CLI 模式支持自然语言任务和以下控制命令：

| 命令 | 用途 |
|---|---|
| `/status` | 模型、渠道、工具、心跳、投递和 Cron 状态 |
| `/queue` / `/lanes` | Durable Delivery 与执行 Lane 状态 |
| `/task status [id]` | 查看当前或指定任务 |
| `/task cancel\|pause\|resume [id]` | 控制任务 |
| `/task modify <id> <新目标>` | 修改暂停任务并保留 revision |
| `/confirm approve\|deny <id> <token>` | 显式批准一次或拒绝高风险操作 |
| `/clarify <id> field=value` | 回答结构化澄清问题 |
| `/identity current` | 查看当前 Global Identity 与 Session Scope |
| `/identity link\|unlink\|merge ...` | 显式管理身份关联并写审计记录 |
| `/feedback up\|down\|correction [说明]` | 记录反馈并生成 Bad Case |
| `/cron` / `/reminder` / `/memory` | 查看定时任务、提醒和记忆 |
| `/trigger` | 立即触发 Heartbeat |

同一 Session 的任务进入同一执行 Lane，严格串行；不同 Session 可并行。进程重启时，原
`running`/`waiting` 任务会被标记为需要恢复，而不会被静默当作成功。

## WebSocket JSON-RPC

Server 模式默认监听 `ws://localhost:8765`，支持：

- `send`
- `bindings.set` / `bindings.list`
- `agents.list`
- `sessions.list`
- `status`

发送示例：

```json
{
  "jsonrpc": "2.0",
  "method": "send",
  "params": {
    "text": "生成一份项目状态摘要",
    "channel": "websocket",
    "account_id": "gateway",
    "peer_id": "demo-client",
    "platform_user_id": "demo-user"
  },
  "id": 1
}
```

响应包含 `agent_id`、持久化 `session_key` 和最终 `reply`。请求同样经过 Identity Resolve、
Task State、Session Lane 和 Tool Confirmation，不绕过生产交互链路。

## 生产架构

```text
Inbound Adapter / WebSocket
        ↓
Binding Route → Global Identity → Versioned Session Policy
        ↓
Persistent Task + Interaction State
        ↓
Per-Session Execution Lane → Agent / Tool Recovery / Confirmation
        ↓
Outbound Intent → Capability Renderer → Semantic Snapshot
        ↓
SQLite Delivery Lane → Lease Worker → Channel Sender → ACK
        ↓
Trace / Artifact / Feedback → Replay Case → Regression Report
```

关键边界：

- `InboundMessage` 是兼容 DTO，渠道边界可转换为版本化 `InboundEnvelope`。
- Session 对话正文仍使用 append-only JSONL；身份、任务、确认、通知和投递使用 SQLite。
- 发送适配器返回平台消息 ID 时记录 `acked`；仅确认“请求已接受”时记录
  `accepted_unconfirmed`。
- 支持客户端幂等键的平台使用 `idempotent_retry`；其他平台明确为
  `at_least_once`，不宣称无法证明的 Exactly Once。
- 旧 `workspace/delivery-queue/` 文件只作为迁移源保留；生产投递主存储是
  `workspace/delivery.db`。

## 工作区与持久化数据

默认工作区为 `workspace/`：

| 路径 | 内容 |
|---|---|
| `.sessions/agents/main/sessions/` | append-only 会话 JSONL |
| `identity.db` | Global Identity、渠道关联、Session Scope 审计 |
| `interaction.db` | Task、状态事件、澄清与确认请求 |
| `interaction-artifacts/` | 任务结果和恢复 Artifact |
| `delivery.db` | Delivery Lane、Sequence、Lease、ACK、Dead Letter |
| `notifications.db` | 订阅、抑制原因、频控和去重预留 |
| `feedback.db` | 用户反馈、Bad Case 和人工 revision |
| `observability/` | Trace 分区、Annotation 和大型 Artifact |
| `CRON.json` | Cron 配置 |
| `SOUL.md` / `IDENTITY.md` / `TOOLS.md` | Agent Prompt 与工具说明 |
| `memory/` / `skills/` | 长期记忆与技能 |

切换 `SESSION_SCOPE` 时应同步递增 `SESSION_SCOPE_VERSION`，避免历史上下文被静默串联。
可选值：

- `per-peer`
- `per-channel-peer`
- `per-account-channel-peer`（默认，隔离最严格）
- `linked-global-user`（仅显式关联后跨渠道共享）

`CONFIRMATION_TOKEN_SECRET` 可显式提供；未配置时会在工作区生成并复用稳定密钥。

## 渠道配置

完整变量与默认值以 [.env.example](.env.example) 和
[`load_config`](src/tinyclaw/config.py) 为准。

| 渠道 | 模式 | 关键配置 |
|---|---|---|
| Telegram | polling | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_ALLOWED_CHATS` |
| 飞书/Lark | `long` / `webhook` / `both` / `off` | `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_MODE` |
| 企业微信 | `long` / `webhook` / `off` | `WORKWECHAT_BOT_ID/SECRET` 或 `CORP_ID/SECRET` |
| 钉钉 | `long` / `webhook` / `off` | `DINGTALK_CLIENT_ID/SECRET` 或 webhook 配置 |
| WeCom CLI | Tool 与 Poll 独立开关 | `WECOM_CLI_TOOL_ENABLED`、`WECOM_CLI_POLL_ENABLED` |

企业微信的具体接线、长连接回包和 WeCom CLI 能力见
[企业微信实现解析](docs/wecom-channel-cli-skills-deep-dive.md)。

## 可靠投递验收

默认安全的离线演练不会读取渠道密钥，也不会发送真实消息：

```bash
uv run python -m tinyclaw.delivery.acceptance --output delivery-drill-report.json
```

覆盖场景：

- Claim 后崩溃与 Lease 恢复。
- 发送成功、Store settle 前崩溃。
- 幂等平台重试去重。
- 非幂等平台 At-Least-Once 重复风险。
- 队首 Retry Wait 期间保持 FIFO。

真实平台演练可能产生外部消息或重复消息，必须使用专用测试账号和目标，按
[沙箱演练手册](docs/roadmap/delivery-sandbox-acceptance.md)执行。

## 开发与验证

```bash
uv run pytest -q
uv run ruff check main.py src tests
uv run ruff format --check main.py src tests
```

截至 2026-08-03，前述测试为 109 项全通过；两个全量 Ruff 命令用于暴露和逐步清理历史
基线，当前分别报告 20 条可修复 lint 诊断（`I001` 12、`F401` 7、`F841` 1）和 26 个
待格式化文件。不要把它们描述为已全绿，也不要通过忽略规则掩盖；本次涉及的 Python
文件应单独保持 lint/format 通过。

当前仓库的生产测试覆盖：

- Identity/Session 隔离与并发。
- Interaction State、原子 revision、Cancel/Modify/Resume。
- Clarification/Confirmation 与高风险工具闸门。
- Tool Error 分类与恢复决策。
- SQLite Delivery、Lease、FIFO、ACK、迁移和崩溃窗口。
- Capability Renderer、通知策略、Trace/Feedback/Replay。

教学系列保留历史实现，不作为生产 Ruff 基线；生产修改应至少检查 `main.py`、`src/` 和
`tests/`。

## 项目结构

```text
tinyClaw/
├── main.py
├── pyproject.toml
├── src/tinyclaw/
│   ├── agent/           # Agent Loop 与工具分发
│   ├── channel/         # 渠道 Adapter 与回执边界
│   ├── concurrency/     # 命名执行 Lane
│   ├── contracts/       # Envelope/Task/Delivery/Trace 版本化契约
│   ├── delivery/        # SQLite Store、Renderer 接入、Lease Worker、演练 CLI
│   ├── gateway/         # Binding Route 与 WebSocket JSON-RPC
│   ├── identity/        # Global Identity 与 Session Resolve
│   ├── interaction/     # Task State、Control、Clarification、Confirmation
│   ├── notification/    # 主动通知策略与 Gateway
│   ├── observability/   # Trace、Artifact、Feedback/Bad Case
│   ├── presentation/    # Channel Capability 与 Renderer
│   ├── replay/          # Replay Case、Evaluator 与报告
│   ├── resilience/      # 模型与工具恢复
│   ├── runtime/         # Runtime Port 与 Tool Executor
│   ├── scheduler/       # Heartbeat 与 Cron
│   └── session/         # JSONL Session Store
├── tests/               # 生产自动化测试
├── docs/roadmap/        # 需求、架构、验收与沙箱演练
├── sessions/zh/         # 教学系列（与生产实现解耦）
└── workspace/           # 默认运行工作区
```

## 文档导航

- [目标需求与最终验收](docs/roadmap/resume-target-requirements.md)
- [目标/落地架构](docs/roadmap/resume-target-architecture.md)
- [可靠投递部署验收](docs/roadmap/delivery-sandbox-acceptance.md)
- [企业微信 Channel 与 WeCom CLI](docs/wecom-channel-cli-skills-deep-dive.md)

## 已知边界

- 真实飞书、企业微信、钉钉和 Telegram 平台故障演练尚需专用沙箱凭据与测试目标。
- 非幂等平台在“平台已接受但本地未 settle”窗口只能保证 At-Least-Once。
- 默认 Replay Executor 使用录制 observation；Live Replay 需要注入真实 `ReplayExecutor`。
- 对正在执行且不可中断的模型/工具调用，取消是协作式的，会在下一个安全点生效。
- 生产代码的全量 Ruff lint/format 历史基线尚未清零，功能测试通过不代表静态检查全绿。
- 仓库当前未附独立 `LICENSE` 文件；对外分发前需要明确许可证。
