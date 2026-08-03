# CLAUDE.md

This file contains repository guidance for coding agents. User-facing setup and architecture documentation
live in `README.md`.

## Project shape

tinyClaw has two intentionally separate parts:

- `src/tinyclaw/` and `main.py`: the production multi-channel Agent Gateway.
- `sessions/zh/`: progressive teaching snapshots. They are historical learning material, not the source of
  truth for production behavior or the default Ruff baseline.

## Common commands

```bash
uv sync --extra dev
cp .env.example .env

uv run tinyclaw --mode cli
uv run tinyclaw --mode server --host localhost --port 8765

uv run pytest -q
uv run ruff check main.py src tests
uv run ruff format --check main.py src tests

uv run python -m tinyclaw.delivery.acceptance
```

The runtime requires `ANTHROPIC_API_KEY`. `ANTHROPIC_BASE_URL` is only valid for endpoints compatible
with the Anthropic Messages API wire protocol.

## Production architecture

```text
Channel/WebSocket Inbound
  → Binding Route
  → Global Identity + Versioned Session Policy
  → Persistent Task / Interaction State
  → Per-Session CommandQueue Lane
  → Agent Runtime + Tool Recovery + Confirmation Gate
  → Outbound Intent + Capability Renderer
  → SQLite Delivery Store + Lease Worker + Channel Receipt
  → Trace / Artifact / Feedback / Replay
```

Key modules:

```text
src/tinyclaw/
├── agent/           Agent loop and tool dispatcher
├── channel/         Telegram, Feishu, Work WeChat, DingTalk, WeCom CLI
├── concurrency/     Named FIFO execution lanes
├── contracts/       Versioned envelope, identity, task, delivery, trace contracts
├── delivery/        SQLite store, durable facade, lease worker, acceptance drill
├── gateway/         Binding route and WebSocket JSON-RPC
├── identity/        Global identity links and session resolution
├── interaction/     Task state, control, clarification, confirmation, production bridge
├── notification/    Subscription, quiet hours, rate limits, dedupe, digest
├── observability/   Trace, artifacts, feedback, Bad Cases
├── presentation/    Channel capability registry and renderer
├── replay/          Replay cases, evaluators, reports
├── resilience/      Model and tool recovery
├── runtime/         Runtime port and classified tool executor
├── scheduler/       Heartbeat and Cron
└── session/         Append-only JSONL conversation history
```

## Important invariants

- Identity/session boundaries include channel and account by default. Never merge histories implicitly.
  Explicit link, unlink, merge, and policy-version changes must remain audited.
- Persist a Task before scheduling execution. Use `session_lane_name(session_id)` so the same session is
  serial while different sessions can run concurrently.
- Task transitions are revisioned and atomic with their interaction event. Reject illegal transitions and
  stale revisions.
- High-risk tool calls require an exact action/scope digest and a signed approve-once token. Consume the
  authorization before dispatch.
- Only retry tools automatically when the operation is read-only or idempotent. Unknown/partial side
  effects must wait for user action.
- All Heartbeat, Cron, and Reminder notifications go through `NotificationGateway`. Suppressed requests
  never enter the delivery queue.
- Production outbound messages go through `DurableDeliveryQueue` and `LeaseDeliveryWorker`. The legacy
  file queue is migration input only.
- Preserve FIFO by allowing only the earliest unfinished sequence in a lane to be claimed.
- `acked` requires a durable platform message ID. Adapter acceptance without an ID is
  `accepted_unconfirmed` and remains At-Least-Once.
- Reuse the same idempotency key across retries. Never claim Exactly Once for platforms that cannot
  enforce it.
- Renderer capabilities must describe actual Sender behavior. Persist the semantic snapshot and render
  metadata with every delivery chunk.
- Trace recording must not break the task, notification, or delivery main path. Large values belong in
  Artifact storage.

## Runtime data

The default `workspace/` contains:

- `.sessions/agents/main/sessions/`: JSONL conversation history.
- `identity.db`: identities, links, session policy audit.
- `interaction.db`: tasks, state events, clarification and confirmation requests.
- `delivery.db`: ordered delivery records, leases, ACKs and dead letters.
- `notifications.db`: notification decisions, reservations and dedupe.
- `feedback.db`: feedback and Bad Cases.
- `observability/` and `interaction-artifacts/`: traces and artifacts.

Do not commit runtime databases, generated reports, secrets, or channel credentials.

## Validation expectations

For production changes, run focused tests first, then:

```bash
uv run pytest -q
uv run ruff check main.py src tests
uv run ruff format --check main.py src tests
```

Known baseline as of 2026-08-03: tests pass, while the full production Ruff run still reports 20
fixable lint diagnostics (`I001` 12, `F401` 7, `F841` 1) and 26 files with formatting drift. Do not
weaken Ruff configuration or claim the baseline is green. Keep touched Python files clean and reduce the
baseline intentionally in a separate mechanical change.

For delivery changes, also run:

```bash
uv run python -m tinyclaw.delivery.acceptance
```

The current automated acceptance target is documented in
`docs/roadmap/resume-target-requirements.md`. Real external-platform fault drills are manual and must use
dedicated sandbox accounts and targets.
