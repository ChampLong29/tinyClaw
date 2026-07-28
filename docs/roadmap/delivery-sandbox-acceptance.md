# Durable Delivery 部署验收与平台沙箱演练

本说明把投递验收分成默认安全的离线演练和需要专用测试账号的真实平台演练。离线命令
不会读取渠道密钥，也不会发送真实消息。

## 1. 离线部署验收

执行：

```powershell
uv run python -m tinyclaw.delivery.acceptance --output delivery-drill-report.json
```

需要保留 SQLite 现场用于排查时，增加：

```powershell
--workspace C:\tmp\tinyclaw-delivery-drill
```

报告 schema 为 `delivery_drill_report.v1`。所有场景通过时进程退出码为 0；任一场景失败时
退出码为 1，便于接入 CI 或部署前检查。

| 场景 | 注入点 | 验收目标 |
|---|---|---|
| `lease_recovery_fifo` | Claim 后崩溃 | Lease 到期后先恢复队首，同 Lane 不乱序 |
| `idempotent_ack_loss` | 发送成功、落 ACK 前崩溃 | 重试复用 idempotency key，平台只接受一次 |
| `at_least_once_ack_loss` | 非幂等发送成功、落 ACK 前崩溃 | 重试风险显式记录为 At-Least-Once |
| `retry_wait_preserves_fifo` | 队首首次网络超时 | 队首退避期间后续消息不能越过 |

## 2. 真实平台沙箱演练

真实演练必须使用专用机器人、专用测试会话和无业务影响的接收账号，并在执行前明确记录：

- 平台、账号与 Sender 模式。
- 测试目标 peer/thread。
- 是否支持 client message ID 或其他幂等键。
- 预期投递语义：`idempotent_retry` 或 `at_least_once`。
- 测试开始/结束时间和负责人。

逐平台执行以下用例：

1. 正常发送，确认平台消息 ID 能写入 Delivery ACK。
2. 临时断网或代理返回 5xx，确认进入 Retry Wait，恢复后成功发送。
3. Auth 失效，确认错误可观察且不会无限快速重试；恢复凭据后人工重放。
4. 在 Sender 返回成功后、Store settle 前终止 Worker，等待 Lease 到期后恢复。
5. 核对同 Lane 顺序、重复消息数、Trace 中的 delivery semantics 和最终状态。

禁止在生产群聊、真实客户会话或无法撤回影响的目标上执行故障演练。对于非幂等平台，
第 4 项可能产生重复消息，必须先取得测试目标所有者同意。

## 3. 验收记录

每次真实平台演练至少保留：

- 离线 JSON 报告。
- 平台消息 ID、delivery ID、idempotency key 和 Trace 引用。
- 故障时间线与恢复时间。
- 重复、乱序、丢失数量。
- 未通过项、负责人和复测日期。
