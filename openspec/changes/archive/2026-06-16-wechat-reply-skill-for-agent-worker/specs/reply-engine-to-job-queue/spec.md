## ADDED Requirements

### Requirement: Reply engine can enqueue deep-agent jobs
- **WHEN** `generate_reply()` detects a task that should be handled by an external agent
- **THEN** it MUST call `agent_jobs.enqueue_job()` instead of `call_llm_provider()`
- **AND** it MUST return a `ReplyDecision` with `should_reply=True`, `reply_text` set to an ack message, and `intent="deep_agent_queued"`

### Requirement: Monitor sends ack for enqueued jobs
- **WHEN** `wechat_bot_monitor` receives a `ReplyDecision` with `intent="deep_agent_queued"` (or equivalent)
- **THEN** it MUST call `send_reply()` with the ack text immediately
- **AND** it MUST advance `last_local_id` so the message is not reprocessed

### Requirement: Deep routing conditions are explicit
- **WHEN** `task_router.route_message()` or `reply_engine` evaluates a message
- **THEN** it MUST route to deep-agent if any of the following holds:
  - the cleaned text contains explicit free-mode/deep-analysis phrases
  - the message type is image/voice/file/video, or contains analysis keywords
  - `reply_decision.decide()` returns `reply_mode="handoff"` or `risk_level="high"`

**Note**: raw/agent_raw targets do not perform local scene wiki retrieval. For those targets, routing is decided by `task_router.route_message()` only; the wiki-hit-count thresholds are reserved for a future standard/balanced-mode deep routing path.

### Requirement: Fast-path messages stay local
- **WHEN** a message does not meet any deep routing condition
- **THEN** `generate_reply()` MUST continue to use the existing local `call_llm_provider()` path

### Requirement: Fallback when job queue is unavailable
- **WHEN** `enqueue_job()` raises an exception or returns an invalid job dict
- **THEN** `generate_reply()` MUST fall back to the local LLM provider or the synchronous subagent path
