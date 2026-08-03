# TinyClaw 目标交互网关需求分析

> 状态：已实现并通过自动化验收（2026-08-03）
> 目标：在多渠道 Gateway 上提供可恢复任务交互、可靠有序投递、渠道化呈现、主动通知治理和 Trace/Replay。

说明：这里的“验收”指下方 11 项功能验收和 109 项测试；生产目录的历史 Ruff
lint/format 基线仍待单独机械清理，不计作功能未完成项。

## 1. 背景与成功标准

当前项目已有 Channel Adapter、InboundMessage/InboundEnvelope、Binding 路由、持久化
Global Identity、版本化 Session Policy、JSONL 会话、Prompt 分层、持久化 Task State、
会话级执行 Lane、SQLite Delivery、失败恢复、NotificationPolicy，以及 Trace/Replay。
普通渠道、CLI 与 WebSocket 请求按 Session Key 串行、跨 Session 并行；平台返回消息 ID
时记录 ACK，只接受请求但没有消息 ID 时显式记录 `accepted_unconfirmed`。

以下是本轮改造前的基线问题，现均已有对应实现和测试证据：

- Agent 执行缺少统一 Interaction State。
- 用户中断、取消、修改、继续、澄清与确认没有稳定协议。
- 待投递文件能够在重启后重新扫描，但尚无持久化 `in_flight`、Lease、幂等键和会话序号。
- 当前全局 `main` Lane 能够串行执行用户请求，但不能实现“同会话严格有序、不同会话并行”。
- 发送函数成功不等于平台 ACK；发送成功、删除本地文件前崩溃时存在重复窗口。
- 不同渠道的卡片、文件和进度能力缺少统一 Capability 模型。
- 主动通知缺少去重、频率、静默时段和订阅治理。
- Trace 未统一关联路由、任务状态、工具、投递和用户反馈。
- 失败会话尚不能固化为 Replay/回归用例。

最终目标流程：

    Inbound Event
    → Identity and Session Resolve
    → Interaction State Machine
    → Agent/Tool Execution
    → Progress/Confirm/Interrupt
    → Channel Renderer
    → Ordered Durable Delivery
    → Feedback Trace
    → Replay and Regression

## 2. 范围

### 2.1 必须完成

- 统一 Channel Envelope、Identity 和 Session Boundary。
- Interaction State 与 Task Instance 持久化。
- 进度反馈、中断、取消、修改、继续。
- 澄清与高风险操作确认。
- 工具失败分类和恢复策略。
- 持久化会话 Lane、序号和队首阻塞。
- 幂等键、平台回执和重复检测。
- Channel Capability 与结果渲染。
- Heartbeat/Cron/Reminder 治理。
- 统一 Trace、反馈记录、Replay 和回归评测。

### 2.2 非目标

- 重做 ClawCodeAgent 的完整编码 Runtime。
- 以复杂多 Agent 协作为第一优先级。
- 无评测支撑的“更自然聊天”Prompt 调优。
- 一次性实现所有平台的全部富媒体能力。
- 宣称底层平台不支持时仍能保证严格 Exactly Once。

## 3. 角色与用例

### 3.1 角色

- 最终用户：跨飞书、企微、钉钉与 Agent 交互。
- Agent 开发者：通过 Runtime Port 接入模型、工具和业务 Agent。
- 渠道开发者：实现 Adapter 与 Capability。
- 运维/评测者：检查 Trace、失败队列、Replay 和回归结果。

### 3.2 核心用例

1. 同一用户按策略跨渠道共享或隔离会话。
2. 长任务持续显示进度，用户可取消、修改和继续。
3. 缺少参数或高风险工具调用时等待用户确认。
4. 工具失败后自动重试、切换方案或请求用户介入。
5. 进程崩溃后恢复未完成任务和待发送消息。
6. 同一会话消息严格按序，不同会话并行。
7. 同一结果按渠道渲染为文本、卡片或文件。
8. 主动提醒遵循订阅、静默时段、频率和去重策略。
9. 将负反馈、取消、重试和失败交互转为 Replay Case。

## 4. 功能需求

### FR-01 统一 Channel Envelope

InboundEnvelope 至少包含：

- event_id、channel、account_id、peer_id、thread_id。
- sender identity、message_id、reply_to。
- event_type、content blocks、attachments。
- received_at、channel_metadata。
- dedupe_key。

OutboundIntent 描述语义结果，不直接绑定平台格式：

- target identity/session。
- content blocks。
- delivery priority。
- interaction/task ref。
- idempotency_key。
- presentation hints。

Adapter 负责平台协议转换，核心 Gateway 不读取平台私有 payload。

### FR-02 身份与会话边界

IdentityResolver 将平台身份映射为 GlobalUser，可配置：

- per-peer
- per-channel-peer
- per-account-channel-peer
- linked-global-user

Session Key 必须包含显式 Scope Version。账号解绑、身份合并和策略变更保留审计记录，不能静默串联历史上下文。

验收：

- 同平台多账号不串线。
- 跨渠道共享只在显式绑定后发生。
- Session Policy 改变不会覆盖旧 Session。

### FR-03 Task Instance 与 Interaction State

每个长任务创建 task_instance.v1，状态：

- queued
- running
- waiting_user
- waiting_confirmation
- waiting_tool
- paused
- completed
- failed
- cancelled

要求：

- 状态转换持久化并带 revision。
- 每次转换记录原因、actor、时间和关联 Trace。
- 非法转换被拒绝。
- 重启后 running/waiting 状态可恢复或进入 recovery_required。
- 一个 Session 可有多个历史任务，但同一交互默认只有一个 active task。

### FR-04 进度反馈

Agent/Tool Runtime 通过 Progress Port 上报：

- phase_started
- step_progress
- tool_started
- tool_completed
- waiting_user
- retrying
- completed
- failed

Gateway 根据渠道能力和节流策略发送：

- 首次状态。
- 关键阶段变化。
- 超过阈值的心跳进度。
- 最终结果。

不得把每个 token 或内部思维链直接发送给用户。

### FR-05 中断、取消、修改与继续

用户指令映射为 Control Command：

- cancel task_id
- pause task_id
- resume task_id
- modify task_id with patch
- status task_id

要求：

- Cancel 使用协作取消信号，工具在安全点停止。
- 不可中断工具标记 cancellation_pending。
- Modify 生成新 task revision，保留已完成步骤和 Artifact。
- Resume 从持久化 Checkpoint 恢复。
- 控制命令具有权限和 Session 校验，不能控制他人任务。

### FR-06 澄清与确认

ClarificationRequest：

- request_id、task_id、question、required_fields。
- expires_at、default_action。
- state：open、answered、expired、cancelled。

ConfirmationRequest：

- action summary。
- risk level。
- tool/arguments 摘要。
- scope、expires_at。
- approve once、deny、modify。

高风险动作没有有效确认不得执行。用户回答必须关联 request_id，避免把普通聊天误当确认。

### FR-07 工具失败恢复

错误分类：

- transient_network
- rate_limited
- auth_expired
- invalid_arguments
- permission_denied
- tool_unavailable
- execution_timeout
- partial_side_effect
- user_action_required
- fatal

策略：

- 可重试错误按 Policy 退避。
- auth_expired 可轮换 Profile。
- invalid_arguments 回到 Agent 修正。
- partial_side_effect 必须先查询/补偿，不能盲重试。
- 高风险或不确定错误进入 waiting_user。
- 每次尝试共享 operation_id，生成独立 attempt_id。

### FR-08 持久化有序投递

DeliveryRecord 增加：

- delivery_id、session_key、lane_key。
- sequence。
- idempotency_key。
- state：pending、in_flight、acked、accepted_unconfirmed、retry_wait、dead_letter。
- channel_message_id、attempts。
- next_retry_at、lease_until。

规则：

1. 先 WAL 持久化，再允许发送。
2. 每个 session/lane 使用单调 sequence。
3. max_concurrency=1 的 Lane 只发送队首。
4. 队首 retry_wait 时后续默认不可越过。
5. 发送前获取持久化 Lease，避免多 Worker 重复消费。
6. 平台成功回执后原子写 ACK；仅适配器接受、未返回平台消息 ID 时写 accepted_unconfirmed，不得伪装成 ACK。
7. 重启按 lane_key + sequence 重建。
8. 超过阈值进入 Dead Letter，保留人工重放。

### FR-09 幂等与投递语义

- 平台支持 client_message_id 时使用 delivery_id/idempotency_key。
- 平台返回 message_id 时持久化并用于重复查询。
- 入站按 channel + message_id 去重。
- 出站在“平台成功、ACK 前崩溃”窗口采用状态查询或幂等重试。
- 平台不支持幂等时明确语义为 At-Least-Once，并在 UX 上处理重复风险。

不得无条件宣称 Exactly Once。

### FR-10 Channel Capability 与渲染

Capability 至少包含：

- streaming/update-message。
- markdown。
- card。
- file/image。
- max_text_length。
- threading/reply。
- progress_update。
- interaction_buttons。

Renderer 接收 OutboundIntent，按 Capability 选择：

- 单条文本。
- 分段文本。
- 可更新进度消息。
- 卡片。
- 文件/Artifact 链接。
- 降级文本。

同一语义结果必须有平台无关 Snapshot，便于 Replay。
Snapshot 需要稳定 hash，并随每个分段 DeliveryRecord 一并持久化。

### FR-11 主动通知治理

所有 Heartbeat、Cron 和 Reminder 经 NotificationPolicy：

- 用户/会话订阅开关。
- topic。
- dedupe_key。
- cooldown。
- per-hour/per-day limit。
- quiet hours/timezone。
- priority。
- digest policy。
- expiry。

被抑制通知记录 reason，不进入 DeliveryQueue。高优先级绕过静默时段必须显式配置。
允许通知先预留 dedupe/rate 配额，只有耐久入队成功后才提交；入队失败必须释放预留。

### FR-12 统一 Trace

interaction_trace.v1 关联：

- inbound event。
- identity/session route。
- task 和 state transitions。
- Prompt/Agent/model/tool version。
- model request/response 摘要。
- tool operation/attempt/result。
- clarification/confirmation/control。
- outbound intent、delivery attempt、ACK。
- latency、token、error。
- user feedback。

大内容写 Artifact，Trace 保存 hash/ref。Trace append-only，后处理标签使用 Revision。
Trace/Artifact 写入失败不得阻断 Task、Notification 或 Delivery 主路径。

### FR-13 用户反馈与 Bad Case

反馈来源：

- 显式赞/踩。
- 用户纠正。
- Cancel。
- Retry。
- 人工接管。
- 同一请求短时间重问。
- 工具或投递失败。

Bad Case 分类至少包括：

- wrong_route
- session_leak
- context_loss
- missing_clarification
- unsafe_action
- tool_failure
- recovery_failure
- duplicate_delivery
- out_of_order
- channel_rendering
- notification_noise
- poor_final_answer

分类结果必须保存 confidence、reason 和关联 trace_event_ids，支持人工 Revision。

### FR-14 Replay 与回归评测

ReplayCase 固化：

- 归一化 Inbound。
- Identity/Session Policy。
- Agent/Prompt/Tool versions。
- 外部工具录制结果或 Stub。
- 预期约束和评测器。
- 原始 Trace/Artifact ref。

模式：

- deterministic stub replay。
- live agent replay。
- delivery simulation。

比较不同 Prompt、Model、Agent 或 Gateway 版本的：

- Task completion。
- State transition validity。
- Clarification/confirmation behavior。
- Tool success/recovery。
- Duplicate/out-of-order count。
- User-visible latency。
- Token/attempt cost。

回归报告至少提供逐 evaluator PASS/FAIL、总分、baseline/candidate delta 和
regression 标记，并可输出 JSON 与 Markdown。

## 5. 非功能需求

### NFR-01 可靠性

- 核心状态持久化。
- 状态转换幂等。
- Worker 崩溃后 Lease 到期可恢复。
- Dead Letter 不丢失原始 payload 与错误。

### NFR-02 顺序与并发

- 同 lane 默认严格 FIFO。
- 不同 lane 可并行。
- 任何允许越过队首的策略必须显式并留下 Trace。

### NFR-03 安全与隐私

- Trace 和 Session 中密钥脱敏。
- 高风险工具需要确认。
- Identity 与 Control Command 做权限校验。
- 数据导出支持字段级脱敏和保留期限。

### NFR-04 可观测性

最少指标：

- inbound/outbound 数量。
- active/waiting task。
- queue depth、oldest age、dead letter。
- delivery success、retry、duplicate。
- per-lane latency。
- tool error。
- clarification/confirmation rate。
- cancel/recovery success。
- notification suppression。

### NFR-05 可扩展性

新增 Channel 只实现 Adapter、Capability 和 Renderer；新增 Agent 只实现 Runtime Port，不修改 Gateway Core。

## 6. 里程碑

### M0 契约与状态

完成 Envelope、Identity、Task、Interaction State、Trace Schema。

### M1 可控交互

完成 Progress、Control、Clarification、Confirmation、Tool Recovery。

当前已完成生产 Clarification/Confirmation 闭环、高风险工具前置闸门，以及分类驱动的
Tool Recovery 执行器；未知副作用不会盲目重试，每个 operation/attempt/decision 均进入
持久化 Trace。

### M2 可靠投递

完成持久化 Lane、Sequence、Lease、ACK、Idempotency 和 Dead Letter。

### M3 渠道化 UX

完成 Capability Renderer、进度更新、卡片/文件降级和 NotificationPolicy。

当前 Capability Renderer、进度 update/milestone 选择、Card/按钮/附件原生元数据与降级链
已接入 Durable Delivery；能力矩阵只声明实际 Sender 已实现的能力。

### M4 Trace/Replay

完成反馈、Bad Case、Replay Case 与版本回归报告。

## 7. 简历证据映射

| 简历表述 | 必须提供的证据 |
|---|---|
| 渠道接入、身份路由与会话边界 | Channel 契约、Identity/Session 隔离测试 |
| 任务状态与可控交互 | State Machine、Cancel/Modify/Resume、确认测试 |
| 上下文装配与工具失败恢复 | Prompt 版本、错误 taxonomy、恢复测试 |
| 可靠投递与渠道化体验 | Crash recovery、FIFO、Idempotency、Renderer 测试 |
| 反馈 Trace 与回归评测 | Trace v1、Replay Case、版本对比报告 |

## 8. 最终验收

- [x] 多账号、多用户和跨渠道 Session 边界测试通过。
- [x] Interaction State 非法转换被拒绝。
- [x] 长任务可进度反馈、取消、修改和恢复。
- [x] 高风险动作无确认不可执行。
- [x] 工具错误按类别进入正确恢复路径。
- [x] 进程崩溃后 Pending Delivery 可恢复。
- [x] 同会话在重试条件下仍保持顺序。
- [x] 幂等平台不重复发送；非幂等平台明确 At-Least-Once。
- [x] 文本、卡片、文件和降级渲染有契约测试。
- [x] 主动通知遵循订阅、静默、频率和去重。
- [x] 失败 Trace 可转为 Replay Case 并生成回归报告。

验收证据：

- Interaction State：`tests/test_interaction_state.py` 覆盖非法转换拒绝、revision 冲突、
  原子事件持久化和中断任务恢复。
- 长任务控制：`tests/test_production_interaction.py` 覆盖进度结果、协作取消、
  Pause/Modify/Resume 以及恢复时的 Session Lane 顺序。
- 主动通知：`tests/test_notification_policy.py` 覆盖订阅、过期、冷却、小时/日频控、
  静默时段、显式高优先级绕过、去重、Digest、崩溃预留恢复和抑制不入队。
- Trace/Replay：`tests/test_trace_replay_feedback.py` 覆盖失败 Trace 固化为 Replay Case、
  JSON/Markdown 报告、逐 evaluator 结果以及 baseline/candidate 回归比较。
