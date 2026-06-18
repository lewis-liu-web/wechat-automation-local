## 1. Skill Definition

- [x] 1.1 Create `wechat-decrypt/skills/wechat_task/skill.md` with role, tools, constraints, and output JSON schema
- [x] 1.2 Add helper `wechat-decrypt/skills/__init__.py` or standalone loader so skill text can be read at runtime
- [x] 1.3 Add unit test verifying skill file exists and contains required sections

## 2. Provider Skill Injection

- [x] 2.1 Update `agent_provider._build_wechat_deep_prompt()` to read `skill_name`, `skill_prompt`, and `knowledge_hits` from job payload
- [x] 2.2 Inject `skill_prompt` at the top of the agent prompt when present
- [x] 2.3 Format `knowledge_hits` as a "[知识库片段]" section inside the prompt
- [x] 2.4 Ensure backward compatibility when skill fields are missing
- [x] 2.5 Add unit tests for `_build_wechat_deep_prompt()` with and without skill/knowledge hits

## 3. Knowledge Context in Job Payload

- [x] 3.1 Verify `agent_jobs.enqueue_job()` preserves `knowledge_hits`, `knowledge_bases`, `reply_mode`, and `retrieval_debug` in payload
- [x] 3.2 Add serialization round-trip test for payload fields through SQLite

## 4. Reply Engine → Job Queue

- [x] 4.1 Import `task_router` and `agent_jobs` in `reply_engine.py`
  - Smoke test first: `python -c "import sys; sys.path.insert(0, 'wechat-decrypt'); import task_router, agent_jobs"`
  - `agent_jobs` has no import back-edge to `control_api`; safe to import from `reply_engine`.
- [x] 4.2 Add helper `_should_enqueue_deep_agent()` using routing conditions from design Decision 6
- [x] 4.3 Add helper `_enqueue_deep_agent_reply()` that builds payload with skill/knowledge fields and calls `agent_jobs.enqueue_job()`
- [x] 4.4 Modify `generate_reply()` to return ack `ReplyDecision` when a task is enqueued
  - Use existing `ReplyDecision` dataclass fields: `should_reply`, `reply_text`, `intent`, `risk_level`, `need_human`, `reason`, `wiki_hits`, `retrieval_debug`.
- [x] 4.5 Keep local LLM / sync subagent fallback when enqueue fails
- [x] 4.6 Add unit tests for enqueue decision, ack return, and fallback

## 5. Monitor Integration

- [x] 5.1 Verify `wechat_bot_monitor` treats `intent="deep_agent_queued"` like any other reply and calls `send_reply()` with the ack text
- [x] 5.2 Ensure `last_local_id` advances after sending the ack
- [x] 5.3 Add integration test or dry-run scenario for enqueued flow

## 6. Documentation and Validation

- [x] 6.1 Update `docs/agent-provider-job-queue.md` with skill injection and knowledge task flow
- [x] 6.2 Run `pytest wechat-decrypt/tests/` and ensure all existing tests still pass
- [x] 6.3 Run dry-run command to verify skill prompt appears in provider output
