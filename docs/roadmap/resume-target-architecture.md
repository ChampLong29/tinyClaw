# TinyClaw 目标交互网关架构设计

> 对应需求：resume-target-requirements.md
> 设计核心：Gateway 管理交互协议、状态与可靠性；Agent Runtime 管理推理与工具；Channel Adapter 管理平台差异。

## 1. 设计原则

1. Gateway 不依赖具体 Agent 实现。
2. Channel 私有 payload 不进入核心领域模型。
3. Task、Delivery 和 Trace 状态全部持久化。
4. 同会话严格顺序建立在持久化 Sequence 和 Lane 上，不只依赖内存队列。
5. Exactly Once 依赖平台幂等能力；不支持时明确 At-Least-Once。
6. 用户可见进度不暴露思维链。
7. 所有失败和人工纠正都可转为 Replay 资产。

> 实施进展：普通渠道、CLI 与 WebSocket 请求已先持久化为 Task，再进入由 Session Key
> 派生的独立执行 Lane；同会话串行、不同会话可并行。生产入口已支持显式
> `/task status/cancel/pause/modify/resume`、启动中断恢复，以及 SQLite Delivery 的
> Sequence、Lease、幂等键和平台回执。持久化 IdentityResolver 默认按账号/渠道/会话
> 隔离，并支持显式 Link、Unlink、Merge 及版本化 Session Policy。生产工具路径已接入
> 持久化 Clarification/Confirmation：高风险动作在有效签名确认前不会分派，授权仅能被
> 同一 Task/Session/Identity 对精确参数消费一次。Capability Renderer 已按真实 Sender
> 能力选择 Card/Markdown/Text、进度 update/milestone 及附件/按钮降级，并将原生渲染元数据
> 随 Durable Delivery 传到 Channel 边界。Delivery Worker 已提供 claim 后、send 后 settle 前、
> settle 后三阶段确定性崩溃注入，覆盖 Lease 恢复、FIFO 和 ACK 丢失窗口；另提供默认不
> 发送真实消息的 `python -m tinyclaw.delivery.acceptance` 部署演练命令与 JSON 报告。
> 外部平台沙箱的真实网络、Auth 过期和 ACK 丢失演练仍需专用测试账号人工执行，步骤见
> `docs/roadmap/delivery-sandbox-acceptance.md`。

## 2. 总体架构

    Channel Adapters
      Feishu / WorkWeChat / DingTalk / Telegram / CLI
                │ InboundEnvelope
                ▼
    Ingress Gateway
      ├── Dedupe
      ├── Identity Resolver
      ├── Session Resolver
      └── Binding Router
                │
                ▼
    Interaction Orchestrator
      ├── Task Store
      ├── State Machine
      ├── Control Command Handler
      ├── Clarification/Confirmation
      ├── Progress Policy
      └── Checkpoint/Recovery
                │ Runtime Port
                ▼
    Agent Runtime
      ├── Prompt Assembly
      ├── Model Client
      ├── Tool Dispatcher
      └── Tool Recovery Policy
                │ OutboundIntent / Trace Event
                ▼
    Presentation Layer
      ├── Channel Capability Registry
      ├── Renderer
      └── Progress Coalescer
                │
                ▼
    Durable Delivery
      ├── Delivery Store
      ├── Persistent Lane Scheduler
      ├── Lease Worker
      ├── Retry/Idempotency
      └── Dead Letter
                │
                ▼
    Channel Sender
                │ ACK/message_id
                ▼
    Trace and Evaluation
      ├── Interaction Trace
      ├── Feedback Collector
      ├── Bad Case Classifier
      ├── Replay Store
      └── Regression Runner

横切模块：

- Config/Policy Registry。
- Artifact Store。
- Metrics/Health。
- Privacy Redactor。
- Schema Migration。

## 3. 推荐目录

建议在 src/tinyclaw 下新增或收敛：

    contracts/
      envelope.py
      identity.py
      interaction.py
      delivery.py
      trace.py
      versions.py

    identity/
      resolver.py
      store.py
      session_policy.py

    interaction/
      orchestrator.py
      state_machine.py
      task_store.py
      control.py
      clarification.py
      confirmation.py
      progress.py
      recovery.py

    runtime/
      port.py
      local_agent_adapter.py
      tool_recovery.py

    presentation/
      capability.py
      renderer.py
      progress_coalescer.py
      blocks.py

    delivery/
      store.py
      scheduler.py
      worker.py
      retry.py
      idempotency.py
      dead_letter.py
      migration.py

    notification/
      policy.py
      subscription.py
      dedupe.py
      digest.py

    observability/
      trace_recorder.py
      artifacts.py
      feedback.py
      bad_case.py
      metrics.py

    replay/
      schema.py
      recorder.py
      runner.py
      evaluators.py
      report.py

现有 channel、gateway、session、intelligence、scheduler、resilience 和 concurrency 逐步适配这些 Port，不要求一次性删除教学章节。

## 4. 核心契约

### 4.1 InboundEnvelope

字段：

- schema_version
- event_id
- channel、account_id、peer_id、thread_id
- platform_message_id
- sender
- event_type
- content_blocks
- attachments
- reply_to
- received_at
- dedupe_key
- raw_artifact_ref

Adapter 必须生成稳定 dedupe_key。原始 payload 进入 Artifact Store，核心只保留引用。

### 4.2 Identity 与 Session

GlobalIdentity：

- global_user_id
- channel_links
- status
- created_at
- revisions

SessionDescriptor：

- session_id
- scope_type
- scope_version
- route_key
- global_user_id
- channel/account/peer/thread refs
- active_task_id
- created_at、updated_at

SessionResolver 的结果必须包含决策原因，写入 Trace。

### 4.3 TaskInstance

- task_id
- session_id
- revision
- state
- user_goal
- runtime_ref
- checkpoint_ref
- pending_request_ref
- created_at、updated_at
- cancellation_token
- result_ref
- failure

状态转换使用 Compare-And-Set revision，避免并发消息覆盖。

### 4.4 InteractionEvent

统一事件：

- task_created
- state_changed
- progress
- clarification_opened/answered
- confirmation_opened/decided
- control_received/applied
- model_started/completed
- tool_started/completed/failed
- recovery_started/completed
- outbound_created
- delivery_attempted/acked/failed
- feedback_received

每个事件有 event_id、seq、task/session refs、actor、time 和 payload。

### 4.5 OutboundIntent

- intent_id
- session_id、task_id
- semantic_type：progress、question、confirmation、result、error、notification
- content_blocks
- priority
- presentation_hints
- dedupe_key
- expiry
- trace_context

Renderer 将 Intent 转成一个或多个 DeliveryRecord。

### 4.6 DeliveryRecord

- delivery_id
- intent_id
- lane_key
- sequence
- channel target
- payload
- idempotency_key
- state
- lease_owner、lease_until
- retry_count、next_retry_at
- platform_message_id
- last_error
- created_at、acked_at、accepted_at

状态：

    PENDING → IN_FLIGHT → ACKED
       │          ├── ACCEPTED_UNCONFIRMED
       │          ├── RETRY_WAIT → PENDING
       │          └── DEAD_LETTER
       └── CANCELLED/EXPIRED

## 5. Interaction State Machine

主要转换：

    QUEUED → RUNNING
    RUNNING → WAITING_USER
    RUNNING → WAITING_CONFIRMATION
    RUNNING → WAITING_TOOL
    RUNNING → PAUSED
    RUNNING → COMPLETED
    RUNNING → FAILED
    RUNNING → CANCELLED

    WAITING_USER → RUNNING/CANCELLED/EXPIRED
    WAITING_CONFIRMATION → RUNNING/CANCELLED/EXPIRED
    WAITING_TOOL → RUNNING/FAILED/CANCELLED
    PAUSED → RUNNING/CANCELLED
    FAILED → RUNNING 仅通过显式 Retry/Resume

StateMachine 是唯一写 Task state 的入口。所有转换事务内追加 InteractionEvent。

## 6. 入站处理

流程：

1. Adapter 解析为 InboundEnvelope。
2. IngressDedupe 通过 dedupe_key 拒绝重复。
3. IdentityResolver 解析 GlobalUser。
4. BindingRouter 选择 Agent/Runtime。
5. SessionResolver 根据 Scope Policy 选择 Session。
6. ControlParser 判断是否为 cancel/resume/modify/status。
7. InteractionOrchestrator 创建或更新 Task。
8. 记录 route 和 state 事件。
9. 调用 Runtime Port。

普通消息不能隐式批准待确认动作；确认必须引用 request_id 或按钮 action token。

## 7. Agent Runtime Port

接口语义：

    start(task_context)
    resume(task_context, checkpoint)
    apply_user_input(task_id, input)
    request_cancel(task_id)
    snapshot(task_id)
    get_status(task_id)

Runtime 输出 Event Stream，而不是直接调用 Channel：

- ProgressEvent。
- ClarificationRequest。
- ConfirmationRequest。
- ToolOperation。
- Result/Error。
- Checkpoint。

现有 Agent loop 通过 LocalAgentRuntimeAdapter 接入。未来其他 Agent 只实现同一 Port。

## 8. Progress 与控制

### 8.1 Progress Coalescer

内部进度频率可能很高，外发策略：

- 状态首次变化立即发送。
- 相同 phase 的高频更新合并。
- 超过 minimum_interval 才更新。
- 渠道支持 message update 时编辑原进度消息。
- 不支持时只发关键节点。
- 最终结果始终发送。

### 8.2 Cancel

CancellationToken 持久化 request time。Runtime 和工具在安全点检查。不可中断工具返回 cancellation_pending；完成后不再进入下一步。

### 8.3 Modify

Modify 创建新 Task revision：

- 保存用户 patch。
- 标记受影响的未完成步骤。
- 已确认可复用 Artifact 保留。
- 必要时回到 clarification。
- 原 revision 不删除。

## 9. Clarification 与 Confirmation

RequestStore 保存 open request，并生成签名 action token。只有：

- 同一 Session/Identity。
- request 仍 open。
- token/request_id 有效。
- 未过期。

才接受回答。

Confirmation 记录 action digest；实际执行前重新计算 digest，参数变化则原确认失效。

当前生产实现同时持久化规范化 action 与 scope。`/confirm approve|deny` 必须携带
request_id 和签名 token；approve-once 在工具执行前原子转换为 consumed，进程重启后仍可
验证，deny 直接取消任务。`/clarify` 只接受结构化 JSON 或 `field=value`，答复完成后通过
原 Session Lane 恢复任务。普通聊天不会隐式回答或批准请求。

## 10. 工具恢复架构

ToolOperation：

- operation_id：语义动作。
- attempt_id：每次尝试。
- idempotency_key。
- side_effect_level。
- retry_policy。
- compensation/check_status capability。

恢复决策：

    classify(error)
      transient → backoff retry
      auth → rotate profile then retry
      invalid args → return to agent
      permission/risk → confirmation
      partial side effect → check status or compensate
      user action → waiting_user
      fatal → failed

禁止对有副作用且未知执行结果的操作盲目重试。

当前生产 Dispatcher 使用 strict 模式把字符串错误恢复为结构化失败，再由
ToolErrorClassifier/ToolRecoveryPolicy 决策。只读/幂等操作可按上限退避重试；每次尝试
共享 operation_id、生成新 attempt_id 并写入 Trace。invalid_arguments 返回 Agent 修正，
permission 进入 Confirmation，partial_side_effect/user_action_required 进入 waiting_user，
fatal 直接失败且不会触发模型凭据轮换。认证轮换通过显式 callback 接入；没有安全凭据或
操作副作用不明确时退回 waiting_user。

## 11. 持久化 FIFO Delivery

### 11.1 入队事务

在同一数据库事务中：

1. 获取 lane 当前 next_sequence。
2. 写 DeliveryRecord PENDING。
3. next_sequence + 1。
4. 提交。

推荐 SQLite 作为默认本地 Store，保留 FileDeliveryStore 作为迁移兼容层。SQLite 提供事务、唯一约束和恢复扫描，优于每消息独立 JSON 文件。

唯一约束：

- delivery_id。
- lane_key + sequence。
- idempotency_key（可按渠道配置）。

### 11.2 调度

Scheduler 只选择每个 lane 最小未终结 sequence：

    SELECT head
    WHERE state in PENDING/RETRY_WAIT
    AND no earlier unfinished record

获取 Lease 后变为 IN_FLIGHT。max_concurrency_per_lane 默认为 1，不同 lane 并行。

队首处于 RETRY_WAIT 时默认 Head-of-Line blocking。若业务允许越过，必须配置 unordered/bypass policy，并在 Trace 标注。

### 11.3 ACK 与崩溃窗口

- Sender 成功返回 platform_message_id。
- 事务内写 ACKED。
- 若适配器只返回“已接受发送”而无平台 message_id，则写 ACCEPTED_UNCONFIRMED；该状态只表示适配器边界成功，语义仍为 At-Least-Once。
- 如果发送成功但写 ACK 前崩溃：
  - 平台支持 idempotency：使用相同 key 重试。
  - 平台支持查询：按 key/message id 查询。
  - 都不支持：At-Least-Once，并启用重复检测/用户提示。

当前 Worker 的故障注入钩子覆盖 `after_claim`、`after_send_before_settle`、`after_settle`。
DeliveryRecord 的同一 idempotency_key 会在 Lease 到期后原样传给新 Worker 和 Sender；声明
`outbound_idempotency` 的 Sender 可据此去重，其他渠道的持久化元数据和 Trace 明确标记
`at_least_once`。崩溃恢复仍只选择每个 Lane 最早未终结 Sequence。

### 11.4 Dead Letter

保留 payload、Trace、attempts 和最后错误。支持 inspect、retry、cancel、export，不自动无限回队。

## 12. Channel Capability 与 Renderer

ChannelCapability：

- text_limit
- markdown
- streaming
- message_update
- card
- buttons
- file
- image
- thread_reply
- delivery_receipt

Renderer Registry 按 semantic_type 和 capability 选择 renderer。

每次渲染必须同时保存平台无关 SemanticSnapshot 与稳定 hash；DeliveryRecord
仅保存渠道化结果，不得覆盖原始语义快照。

降级链示例：

    rich card → markdown → plain text
    progress update → coalesced text milestones
    file → artifact link → summarized text

Chunker 必须理解内容块边界，避免把代码块、链接或确认按钮语义切断。

当前默认 Capability 采用保守真值：未实现原生上传/卡片的 Sender 不声明 file/image/card；
Work WeChat 按 long/webhook 实际模式动态声明 markdown 与 delivery receipt。Renderer 支持
Card、Actions、File/Image 的原生 payload 元数据，并依次降级到 markdown/plain text、
artifact link；Progress 根据 `progress_update` 选择 update 或 milestone。渲染格式、降级原因
和原生 payload 均持久化并传递至 Sender kwargs，SemanticSnapshot/hash 在所有分段保持一致。

## 13. 主动通知

NotificationRequest 先经过 PolicyEngine，再生成 OutboundIntent。

Policy 决策顺序：

1. subscription。
2. expiry。
3. dedupe。
4. quiet hours。
5. rate limit。
6. priority override。
7. digest/coalesce。

每次 suppressed 记录 reason 和 policy version。Heartbeat、Cron、Reminder 不能直接调用 DeliveryQueue。

允许通知先写 RESERVED；只有耐久投递入队成功后才提交 ALLOWED。入队失败写
ENQUEUE_FAILED 并释放 dedupe reservation，避免通知永久丢失。

## 14. Trace 与 Artifact

TraceRecorder 接收领域事件，按 session/task 分区 append-only 存储。建议：

    traces/<session>/<task>/events.jsonl
    traces/<session>/<task>/artifacts/<sha256>
    traces/<session>/<task>/timeline.md

敏感字段经过 Redactor。Trace event 带 schema、producer 和 version。后处理标签另写 Annotation Revision。

Recorder 写入失败必须与业务路径隔离；Task、Notification、Delivery 使用同一
session/task 分区和单调 sequence。大 payload 先脱敏，再写内容寻址 Artifact。

## 15. Replay 架构

ReplayCase：

- 输入 Envelope。
- Identity/Session Policy。
- Runtime/Prompt/Model/Tool versions。
- Tool recordings 或 Stub。
- Channel Capability。
- Expected constraints。
- Evaluators。
- Source Trace。

Runner 模式：

- gateway-only：Stub Agent，验证路由、状态、投递。
- agent-stub-tools：真实 Agent、录制工具。
- live：真实 Agent/工具，用于人工实验。

每次运行同时输出机器可读 JSON 与人工可读 Markdown Report；baseline 与
candidate 按 evaluator score 生成 delta 和 regression 标记。

Evaluator：

- route correctness。
- state validity。
- clarification/confirmation。
- task completion。
- delivery order/duplicate。
- rendering validity。
- notification policy。
- latency/token/attempt cost。

## 16. 数据迁移

### Phase A

为现有 JSON Delivery Entry 增加 schema_version、lane_key 和 sequence；只读兼容旧文件。

### Phase B

引入 SQLite Store，启动时迁移旧 Pending/Failed 文件，保留 migration report。

### Phase C

DeliveryRunner 改用 Store Port 和 Lease Worker；旧 Queue 作为导入器。

Session、Reminder 和 Binding 后续可迁移到同一事务 Store，但不作为第一步阻塞项。

## 17. 测试策略

### 单元测试

- State Machine。
- Identity/Session Policy。
- Control 与 Confirmation token。
- Error classification。
- Notification Policy。
- Renderer 降级。
- Sequence、Lease 与唯一约束。

### 契约测试

- 每个 Channel Adapter 的 Envelope 与 Capability。
- Runtime Port。
- Sender ACK/idempotency。
- Trace Schema。

### 集成测试

- Inbound → Task → Progress → Result → Delivery ACK。
- Cancel/Modify/Resume。
- Clarification/Confirmation。
- Retry/Dead Letter。
- Trace → Replay → Report。

### 故障注入

- enqueue 后进程崩溃。
- 发送成功、ACK 前崩溃。
- Worker Lease 超时。
- 队首退避。
- 平台重复回调。
- Auth 过期。
- 工具部分副作用。
- 用户在并发消息中取消任务。

关键验收：

- 重启不丢 Pending。
- 同 lane 在失败重试时不乱序。
- 支持幂等的平台不重复。
- 非幂等平台的语义和风险可观察。
- 不同 lane 不互相阻塞。

## 18. 实施顺序

### P0 契约与 Trace

Envelope、Identity、Task、State、Delivery、Trace Schema；为现有路径加 Adapter。

### P1 Interaction Orchestrator

Task Store、StateMachine、Progress、Control、Clarification、Confirmation。

### P2 Delivery 可靠性

SQLite Store、Sequence、Lease、严格 FIFO、ACK、Idempotency、Dead Letter。

### P3 Channel UX 与通知

Capability、Renderer、Progress Coalescer、NotificationPolicy。

### P4 Replay 与评测

Feedback、Bad Case、Replay Runner、Evaluator、回归报告。

## 19. 架构决策记录

- ADR-01：Gateway 管理交互状态，Agent Runtime 通过 Port 接入。
- ADR-02：持久化 Lane 使用事务 Store，内存 Lane 只作执行器。
- ADR-03：默认同会话 Head-of-Line blocking，保证严格顺序。
- ADR-04：Exactly Once 取决于平台能力，系统不做虚假承诺。
- ADR-05：进度展示为结构化状态，不暴露 Chain-of-Thought。
- ADR-06：Heartbeat/Cron/Reminder 统一经过 NotificationPolicy。
- ADR-07：Trace 是事实流，Bad Case 和人工标签使用 Revision。

## 20. 当前可验证的公开叙事

P0–P4 已完成自动化验收，项目可以有证据地描述为：

- 统一多渠道身份、路由和可配置会话边界。
- 以持久化 Interaction State 支持进度、中断、澄清、确认和恢复。
- 以事务 SQLite、Sequence、Lease、幂等键和 ACK 实现可恢复、有序的投递语义。
- 以 Channel Capability 提供卡片、文件和降级呈现，并治理主动通知。
- 以完整 Trace 将失败与人工纠正转化为 Replay 和版本回归用例。
