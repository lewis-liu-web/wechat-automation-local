# 提案：为 Agent Worker 增加任务处理 Skill

## Why

当前监听回复链路已经具备 agent job queue 基础设施（`agent_jobs` + `agent_provider` + `agent_worker` + `control_api`），但复杂任务和知识库任务的处理稳定性不足。根本原因是：job payload 里只有原始 prompt 和少量元数据，没有系统化的 skill 调用要求，agent 不知道如何处理微信群场景、如何优先使用知识库命中结果、如何拒绝高风险请求。需要新增一个项目内 skill，在 agent provider 构建 prompt 时统一注入，让 agent 明确角色、可用工具、输出格式与安全边界。

## 现状

- `wechat-decrypt/reply_engine.py` 与 `wechat_bot_monitor.py` 是回复决策入口。
- `wechat-decrypt/agent_jobs.py` 提供 SQLite-backed job queue：`enqueue_job()` / `claim_next_job()` / `complete_job()` / `list_sendable()` 等。
- `wechat-decrypt/agent_provider.py` 提供 `GenericAgentProvider`（HTTP bridge）和 `HermesProvider`（CLI）两种 provider；`_build_wechat_deep_prompt(job)` 负责把 job 拼成 agent prompt。
- `wechat-decrypt/agent_worker.py` 轮询 job queue，调用 provider，完成后通过 `wechat_sender.send_reply()` 把结果发回微信群。
- `wechat-decrypt/control_api.py` 暴露 `/agent/jobs`、`/agent/worker/*`、`/agent/dispatcher/*`、`/agent/reconciler/*`、`/agent/async-loop/*` 等端点，支持异步调度与结果回写。
- `wechat-decrypt/task_router.py` 与 `wechat-decrypt/reply_decision.py` 已提供初步的分流与决策层。
- 历史路径 `reply_engine._call_subagent_provider()` 仍直接 spawn `agentmain.py` 同步调用，但生产复杂任务应走 `agent_jobs.enqueue_job()` 异步链路。

## What Changes

- 新增项目内 skill `wechat-decrypt/skills/wechat_task/skill.md`：定义 agent 在微信任务场景下的角色、可用工具、输出格式与安全约束。
- 修改 `agent_provider.py`：
  - 在 `_build_wechat_deep_prompt()` 中读取 job payload 里的 `skill_name` / `skill_prompt` / `knowledge_hits`。
  - 如果 payload 携带 skill，把 skill system prompt 拼到 agent prompt 最前面；把 `knowledge_hits` 以结构化片段形式插入。
- 修改 `reply_engine.py` 与 `wechat_bot_monitor.py`：
  - 对于复杂任务、强知识库命中、或 `task_router` 判定为 `ROUTE_DEEP` 的消息，走 `agent_jobs.enqueue_job()` 异步链路，而不是本地 LLM 或同步 subagent。
  - 在 job payload 中附带 `skill_name`、`skill_prompt` 和 `knowledge_hits`。
- 新增/更新单元测试：覆盖 skill 注入、payload 字段传递、provider prompt 构建。
- 更新文档 `docs/agent-provider-job-queue.md`，说明 skill 注入位置与知识库任务流程。

## Capabilities

### New Capabilities

- `wechat-task-skill`: 项目内 agent skill，定义微信任务处理的角色、工具协议、知识库使用方式与输出格式。
- `skill-prompt-injection`: 在 `agent_provider.py` 的 `_build_wechat_deep_prompt()` 中读取 job payload 的 skill 字段并注入 agent prompt。
- `knowledge-context-in-job`: 让复杂任务/知识库任务通过 `agent_jobs.enqueue_job()` 异步执行，并把 `knowledge_hits` 作为结构化上下文携带。
- `reply-engine-to-job-queue`: 把 `reply_engine` / `wechat_bot_monitor` 中适合走 agent 的任务从本地 LLM 或同步 subagent 切换到 `agent_jobs.enqueue_job()`。
