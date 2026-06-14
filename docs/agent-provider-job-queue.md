# AgentProvider + Job Queue Design

## Decision

Build wechat-auto's own `AgentProvider` layer and lightweight job queue. Do not bind the product to one agent app, and do not start with "one group = one agent instance".

Recommended model:

```text
one WeChat message that needs deep processing = one job
one chat/group = one serial context
all deep agents = a limited worker pool
```

This keeps the product light while protecting user experience when several groups trigger complex tasks at the same time.

## Why

The current direct raw-agent path has two product problems:

1. A general-purpose agent runs its own workflow, so even simple replies can become slow or timeout.
2. One long agent run can block or delay other group replies if the monitor waits inline.

The product should treat agent apps as scarce execution resources, like employees taking tickets from a task board:

```text
WeChat listener -> job board -> dispatcher -> available agent worker -> result -> WeChat send
```

This is inspired by GenericAgent's agent team worker / BBS idea, but we should not copy its natural-language claiming flow directly. We need product-owned state, locks, timeouts, and send guarantees.

## Non-goals

- Do not make every group own a dedicated agent instance by default.
- Do not let agent apps decide job state, group ordering, or send target.
- Do not rely on natural-language "claiming" as the source of truth.
- Do not let complex agent execution block DB polling.
- Do not send raw agent logs, tool traces, markdown plans, or intermediate reasoning to WeChat.

## Product Routing

Every incoming trigger should be routed before any deep agent call.

```text
triggered message
  -> local safety precheck
  -> local smalltalk / empty mention fallback
  -> task complexity routing
  -> fast reply or deep job
```

Routing classes:

| Class | Examples | Path | User impact |
| --- | --- | --- | --- |
| `block` | transfer money, passwords, keys, DB paths, delete data, promises/authorization | local refusal / handoff | safe and immediate |
| `fast_reply` | hello, help, simple clarification, normal short KB answer | local rule / fast LLM / local retrieval | fast response |
| `deep_agent` | image analysis, chart analysis, file summary, multi-step diagnosis, open-ended free task | job queue + provider | controlled delay with status |

Free mode should mean "complex tasks may upgrade to deep agents", not "all messages go to a general agent".

## User Experience Contract

The user should never see silence for a matched complex task.

Recommended defaults:

```text
time_to_ack target: < 3s
ack_after_seconds: 5-8s
deep_task_timeout: 90s
per_group_concurrency: 1
global_deep_concurrency: 1 first, then 2 after validation
```

Message behavior:

```text
simple task -> final reply directly
deep task queued/running -> optional "收到，我先处理一下，稍等。"
deep task done -> final summary reply
deep task timeout -> "这个处理时间有点长，我先中止了，可以换个问法再试。"
provider unavailable -> "复杂处理服务暂不可用，我先收到。"
```

## Job State Model

Use a local SQLite table as the source of truth. Suggested database: `wechat-decrypt/temp/agent_jobs.sqlite`.

Suggested table: `agent_jobs`.

| Field | Purpose |
| --- | --- |
| `id` | internal job id |
| `job_key` | stable external key, e.g. target + local_id |
| `group_key` | chat/group isolation key |
| `target_name` | display/debug target |
| `sender` | sender id/name for mention prefix |
| `message_local_id` | source WeChat message id |
| `task_type` | `fast_reply`, `vision_analysis`, `chart_analysis`, `deep_free`, etc. |
| `priority` | normal/high; high only for product-defined cases |
| `status` | lifecycle state |
| `payload_json` | full safe payload for provider |
| `provider` | selected provider name |
| `worker_id` | assigned worker |
| `result_text` | final text eligible for WeChat send |
| `error` | short failure reason |
| `created_at` | enqueue time |
| `started_at` | worker start time |
| `finished_at` | completion time |
| `ack_sent_at` | first feedback sent time |
| `sent_at` | final WeChat send time |
| `send_status` | `pending`, `sent`, `failed`, `skipped` |

Lifecycle:

```text
queued -> running -> done -> sending -> sent
queued -> cancelled
running -> timeout
running -> failed
done -> send_failed
```

Important invariants:

- `job_key` must be unique to prevent duplicate replies.
- Only one `running` job per `group_key` unless explicitly overridden.
- Final send must re-check target/group binding before sending.
- A job result is not trusted until postcheck and target validation pass.

## Dispatcher Rules

The dispatcher owns scheduling. Agent apps only execute assigned jobs.

Rules:

1. Pick queued jobs by priority and creation time.
2. Skip a job if its `group_key` already has a running job.
3. Never exceed global worker capacity for the task lane.
4. Mark job as `running` with an atomic SQLite update.
5. Release or timeout stale running jobs.
6. Send ack if queue/running time exceeds `ack_after_seconds`.
7. Postcheck provider output before final send.

Atomic claim pattern:

```sql
UPDATE agent_jobs
SET status='running', worker_id=?, started_at=?
WHERE id=? AND status='queued'
```

If affected rows is `0`, another worker already claimed it.

## AgentProvider Interface

Business code should depend on a provider protocol, not on GenericAgent, Hermes, OpenClaw, or any concrete app.

Minimal interface:

```text
health() -> ProviderHealth
list_workers() -> WorkerStatus[]
run(job) -> AgentResult
cancel(job_id) -> bool
```

First implementation can be synchronous:

```text
dispatcher assigns job -> provider.run(job) blocks within timeout -> returns result
```

Later implementations can become asynchronous:

```text
submit(job) -> external_job_id
poll(external_job_id) -> result/status
cancel(external_job_id)
```

Suggested result shape:

```json
{
  "ok": true,
  "status": "done",
  "reply_text": "@用户 分析结论是……",
  "error": "",
  "latency": 12.3,
  "provider": "genericagent",
  "worker_id": "ga-1"
}
```

Provider result rules:

- `reply_text` must be plain WeChat-sendable text.
- Markdown plans, tool logs, raw JSON, and chain-of-thought-like text are invalid.
- If no valid text is produced, return `failed` or `timeout`, not an empty success.

## Provider Roadmap

### M1: GenericAgentProvider

Use the current GenericAgent bridge as the first provider because it already exists in the environment.

Scope:

- health check against `desktop_bridge.py`.
- run one deep job with fixed WeChat reply prompt/protocol.
- clean output and enforce `postcheck`.
- timeout and failure reporting.
- worker count starts at `1`.

Do not rely on GenericAgent team BBS for product state in M1. The BBS idea is useful, but job state belongs to wechat-auto.

### M2: HermesProvider

Add Hermes after the queue/provider boundary is stable.

Probe requirements:

- health check.
- run text job.
- run image job.
- timeout/cancel behavior.
- structured result or reliable text extraction.
- multiple instance support and isolation behavior.

If Hermes has stronger multi-instance support, it can become the default deep provider later.

### M3: Provider Pool

Add provider worker pool only after metrics show need.

Start with:

```text
deep_worker_count = 1
```

Then test:

```text
deep_worker_count = 2
```

Do not scale by group count. Scale by queue pressure, timeout rate, and worker utilization.

### M4: Lanes

Split by capability when needed:

```text
fast_lane: local rules / local retrieval / fast LLM
deep_llm_lane: image, chart, summary, analysis
desktop_lane: real desktop/browser/clipboard tasks, concurrency = 1
```

### M5: Async Dispatch / Poll / Send

Decision: after multi-agent worker pool is available, move GenericAgent production execution away from synchronous `run()` waiting and into a three-stage asynchronous flow.

Why: local timeouts only stop the WeChat project's waiting loop. They do not stop GenericAgent's actual run, so a timed-out job may still consume external agent resources and may later produce a valid result. The product should therefore treat GenericAgent as an external executor that is submitted to, polled, and reconciled, not as a blocking function call.

Recommended flow:

```text
queued
  -> dispatching
  -> submitted / agent_running
  -> done
  -> sending
  -> sent
```

Failure and manual-control states:

```text
queued/dispatching/submitted/agent_running -> failed
submitted/agent_running -> expired
submitted/agent_running -> cancelled
done/sending -> send_failed
```

Stage ownership:

| Stage | Responsibility | User impact |
| --- | --- | --- |
| dispatcher | claim queued jobs, submit to GenericAgent, store external refs, then release local worker quickly | local queue is not blocked by long agent runs |
| reconciler | poll submitted/running external sessions, extract final reply, update `result_text` | late GenericAgent results become visible and recoverable |
| sender | scan `done + send_status=pending`, atomically send to WeChat, mark sent/send_failed | sending is separated from agent success/failure |

Suggested additional fields:

| Field | Purpose |
| --- | --- |
| `external_provider` | selected external executor, e.g. `genericagent` |
| `external_session_id` | GenericAgent `sessionId` |
| `external_user_msg_id` | prompt message id, used to find later assistant output |
| `external_status` | submitted/running/done/failed/cancelled/unknown |
| `submitted_at` | external submit time |
| `last_polled_at` | last reconcile check time |
| `next_poll_at` | next due poll time, for backoff |
| `agent_deadline_at` | product SLA deadline; after this do not auto-send without policy |
| `reconcile_attempts` | poll attempts count |
| `send_attempts` | send attempts count |
| `result_message_id` | external assistant message id for result de-duplication |
| `dispatch_owner` | worker claiming dispatch |
| `dispatch_locked_until` | stale dispatch lock recovery |

Provider interface extension:

```text
submit(job) -> { accepted, external_session_id, external_user_msg_id, error }
poll(external_session_id, external_user_msg_id) -> { status, reply_text, result_message_id, error, raw }
cancel(external_session_id) -> bool
```

Concurrency rules must still count external in-flight jobs. Releasing the local worker after `submit()` must not mean unlimited GenericAgent prompts. Initial defaults:

```text
global_external_concurrency = 1
per_group_external_concurrency = 1
each job gets its own GenericAgent session
```

Timeout semantics:

- `timeout` should not mean the agent stopped.
- `expired` should mean the product will not auto-send by default because the result is too late.
- UI should offer `拉取结果`, `拉取并发送`, and `忽略` for expired/timed-out jobs.

Minimal implementation order:

1. Add fields and keep the current synchronous worker for compatibility.
2. Implement `GenericAgentProvider.submit()` and `poll()` while preserving `run()` for smoke tests.
3. Add dispatcher `run-once`: `queued -> dispatching -> submitted`.
4. Add reconciler `run-once`: `submitted/agent_running -> done/failed/expired`.
5. Add sender `run-once`: `done + pending -> sent/send_failed`.
6. Replace the current synchronous GenericAgent worker loop with dispatcher + reconciler + sender loops.

This avoids desktop resource conflicts while allowing non-desktop deep tasks to scale.

## GenericAgent Team Worker Lessons

GenericAgent has a useful BBS/mapreduce pattern:

```text
BBS posts -> workers poll -> worker claims -> worker executes -> worker posts result
```

Useful ideas to borrow:

- task board mental model;
- multiple workers;
- workers wake when new work exists;
- long results can be file-backed;
- master/worker separation.

Things not to copy directly for WeChat product execution:

- 60-second worker polling interval is too slow for chat UX.
- natural-language claiming is not a hard lock.
- BBS posts are not a structured job state machine.
- result posts need parsing and can include reports/logs.
- it does not know WeChat group ordering, message ids, send confirmation, or wrong-group risk.

If we later build `GenericAgentTeamProvider`, use BBS only as the external agent communication channel. Keep `agent_jobs.sqlite` as the product source of truth.

## Safety

Safety should stay local and layered:

```text
precheck before provider
provider prompt constraints
postcheck after provider
send target validation before WeChat send
event logging for every skip/failure
```

Hard local blocks before provider:

- money transfer/payment;
- passwords/tokens/API keys;
- database paths/logs/internal files;
- delete/format/modify critical config;
- authorization, promise, quote, or decision on behalf of the owner;
- prompt injection asking to bypass system or safety rules.

Deep provider must never be the only safety boundary.

## Observability

Record metrics before deciding to add more provider instances.

Core metrics:

| Metric | Target |
| --- | --- |
| `time_to_ack` | < 3s |
| `time_to_final_reply` for normal tasks | < 15s |
| `time_to_final_reply` for deep tasks | < 90s |
| `timeout_rate` | < 5% |
| `send_wrong_group_count` | 0 |
| `same_group_out_of_order_count` | 0 |
| `queue_wait_seconds` | trend down / bounded |
| `worker_utilization` | scale signal |
| `provider_failure_rate` | provider quality signal |

Events to log:

- `job_queued`
- `job_started`
- `job_ack_sent`
- `job_done`
- `job_timeout`
- `job_failed`
- `job_send_ok`
- `job_send_failed`
- `provider_unavailable`

## Minimal Implementation Plan

### Step 1: Data model and queue API

- Add `agent_jobs.py` for SQLite job CRUD.
- Add enqueue, claim, complete, fail, timeout helpers.
- Keep it independent of Streamlit/control API at first.

### Step 2: Complexity router

- Route smalltalk and safety locally.
- Route image/chart/summary/free-analysis requests to `deep_agent` jobs.
- Keep current inline GenericAgent path as fallback during migration.

### Step 3: GenericAgentProvider

- Move current bridge call into provider module.
- Use a strict WeChat reply prompt.
- Enforce timeout and output cleaning.

### Step 4: Worker loop

- One background worker process/thread initially.
- Same-group serial scheduling.
- Final result send through existing sender path.

### Step 5: UI/API visibility

- Show queued/running/done/failed jobs in control UI.
- Show provider health.
- Allow manual cancel/retry.

### Step 6: HermesProvider probe

- Add only after Step 1-5 are stable.
- First support text, then image.

## Default Config Proposal

```json
{
  "agent_provider": {
    "default": "genericagent",
    "fallback": [],
    "deep_worker_count": 1,
    "per_group_concurrency": 1,
    "ack_after_seconds": 6,
    "deep_task_timeout": 90,
    "providers": {
      "genericagent": {
        "enabled": true,
        "bridge_url": "http://127.0.0.1:14168"
      },
      "hermes": {
        "enabled": false
      }
    }
  }
}
```

## Open Questions

- Should deep tasks send an ack immediately, or only after 5-8 seconds?
- Should failed deep tasks auto-retry once, or avoid retry to prevent duplicate replies?
- Should image-only messages become jobs immediately, or wait for a follow-up text trigger?
- What is Hermes' best control API for multi-instance jobs?
- Should provider pool be process-based, HTTP-based, or both?

## Current Recommendation

Implement in this order:

1. `agent_jobs.sqlite` + dispatcher state machine.
2. GenericAgent provider as the first concrete provider.
3. Same-group serial + global deep concurrency of `1`.
4. Ack/timeout UX.
5. Control UI visibility.
6. Hermes provider probe and then multi-worker scale-up.

This gives product-grade UX without prematurely building a heavy multi-agent platform.
