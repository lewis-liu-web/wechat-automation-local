## Context

当前 agent 链路已经具备完整基础设施：

- `agent_jobs.py` 提供 SQLite job queue。
- `agent_provider.py` 的 `GenericAgentProvider` / `HermesProvider` 把 job 转成外部 agent prompt。
- `agent_worker.py` / `control_api.py` 负责调度、轮询、结果回写。
- `reply_engine.py` 与 `wechat_bot_monitor.py` 是回复入口，但复杂任务仍可能走本地 LLM 或同步 `agentmain.py`。

问题是：job payload 里没有系统化的 skill 调用要求，agent 对微信群场景、知识库使用、输出格式、安全边界理解不一致。

## Goals / Non-Goals

**Goals:**
- 新增项目内 skill，统一描述微信任务处理规范。
- 在 `agent_provider.py` 构建 prompt 时自动注入 skill 与知识库命中片段。
- 让 `reply_engine` / `wechat_bot_monitor` 把复杂/知识库任务切到 `agent_jobs.enqueue_job()` 异步链路。
- 保证切换过程不破坏现有本地 LLM 与同步 subagent 路径（作为 fallback）。

**Non-Goals:**
- 不改动 UI 知识库文件转换逻辑。
- 不新增外部 LLM provider。
- 不修改 agent provider 与外部 agent 之间的底层协议（仍走 HTTP bridge / Hermes CLI）。

## Decisions

- **Decision 1：skill 以项目内 markdown 文件形式存在**
  - 选择：`wechat-decrypt/skills/wechat_task/skill.md` 作为 skill 文本来源，`_build_wechat_deep_prompt()` 读取并注入。
  - 理由：无需改动外部 agent 或 provider 协议，纯文本即可被 GenericAgent bridge 和 Hermes CLI 理解。
  - 替代方案：远程 skill registry，会引入版本同步问题。

- **Decision 2：注入点放在 `agent_provider.py` 的 `_build_wechat_deep_prompt()`**
  - 选择：在 provider 侧统一注入 skill，而不是在 `reply_engine.py` 里拼好 prompt 再 enqueue。
  - 理由：一个 job 可能由不同 provider（GenericAgent / Hermes）消费，prompt 构建逻辑应集中在 provider；job payload 保持结构化字段即可。
  - 替代方案：每个调用者自己拼 prompt，会重复且难维护。

- **Decision 3：payload 新增 `skill_name`、`skill_prompt`、`knowledge_hits` 字段**
  - 选择：job payload 增加这三个字段，`_build_wechat_deep_prompt()` 读取后注入。
  - 理由：skill 内容与命中片段可序列化进 SQLite，provider 不需要回查文件系统。
  - 替代方案：只传 `skill_name`，provider 去文件系统读 skill 文件；会增加 I/O 与路径耦合。

- **Decision 4：复杂/知识库任务走 `agent_jobs.enqueue_job()`**
  - 选择：`reply_engine` / `wechat_bot_monitor` 根据 `task_router` / `reply_decision` 结果，把需要深度处理的任务 enqueue。
  - 理由：复用现有异步、可观测、可重试基础设施，不需要单独开流程。
  - 替代方案：在 `_call_subagent_provider()` 里同步调用；没有重试、不可观测、阻塞 monitor。

- **Decision 5：保留本地 LLM 与同步 subagent 作为 fallback**
  - 选择：当 agent job 链路不可用时（没有 provider、DB 异常），仍然允许走原有本地 LLM 或同步 subagent 路径。
  - 理由：避免新链路未稳定时直接中断回复能力。

- **Decision 6：调用契约与 ack 流程**
  - (a) `reply_engine.generate_reply()` 保持返回 `ReplyDecision`。若判定为异步 agent 任务，返回 `ReplyDecision(should_reply=True, reply_text="收到，正在处理，稍等。", intent="deep_agent_queued", reason="enqueued_for_skill_agent")`。
  - (b) `wechat_bot_monitor` 收到非空 `reply_text` 后，像普通回复一样调用 `send_reply()` 发送 ack；后续 agent_worker 完成 job 后通过 `wechat_sender.send_reply()` 把最终结果发回群里。
  - (c) 进入异步链路的判定条件（满足任一即 `ROUTE_DEEP`）：
    - 用户显式要求自由模式/深度分析（已有 `_FREE_MODE_PHRASES`）。
    - 消息类型为 image/voice/file/video，或包含分析类关键词（已有逻辑）。
    - 清洗后文本长度超过 80 字且命中至少 2 条 scene wiki 片段。
    - 命中知识库片段数 ≥ 3（强知识库任务）。
    - `reply_decision.decide()` 返回 `reply_mode == "handoff"` 或 `risk_level == "high"`。

## Risks / Trade-offs

- **Risk**: skill prompt 过长导致 agent 上下文窗口紧张。Mitigation: 默认只传 top-5 knowledge hits，skill 文本控制在 2k tokens 以内。
- **Risk**: 不同 provider 对 system prompt 位置理解不同。Mitigation: 在 `_build_wechat_deep_prompt()` 中把 skill 文本放在最前面，与现有 "wechat-deep-reply" 指令合并。
- **Risk**: 切到 job queue 后 monitor 不再同步拿到回复，ack 与最终结果之间需要用户等待。Mitigation: monitor 先发送 ack，worker 完成后再发结果；已在现有 agent_worker 流程中支持。
- **Trade-off**: provider prompt 构建逻辑变重，但这是唯一注入点，维护成本可控。
