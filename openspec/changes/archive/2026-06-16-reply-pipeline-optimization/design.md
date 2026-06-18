## Context

当前 `wechat-decrypt/message_aggregator.py` 已实现基于 debounce 窗口的消息聚合，`wechat_bot_monitor.py` 在 `flush_due()` 后对聚合消息调用 `reply_decision_decide` 与 `generate_reply`。`reply_engine.py` 已支持 `raw_agent` 模式，把任务入队到 `agent_jobs` SQLite 队列，由 `control_api.py` 的 M5 async loop 调度 `HermesProvider` / `GenericAgentProvider`。

生产测试（CD-only 实例）暴露三个问题：
1. **Hermes 输出被污染**：`agent_jobs.sanitize_agent_result_text` 虽已过滤部分 Hermes 噪声，但仍会把 "Initializing agent…" 等初始化文本保留为可发送回复；同时 `agent_provider._clean_agent_output` 是 stub，`_extract_hermes_reply` 在无 Hermes 回复框时 fall through 到该 stub，进一步导致工具日志被当作回复。
2. **聚合判定不清晰**：图片与后续描述文字、触发器跨消息命中、flush 边界等行为没有文档化，导致图片+描述被错误拆分为独立任务或 image-only 长期挂起。
3. **Skill 注入不经济**：`wechat_task/skill.md` 全文被复制到每个 job payload，浪费 token 并污染上下文。

本设计在现有模块基础上做最小调整，不新建独立聚合层或清洗层。

## Goals / Non-Goals

**Goals:**
- 修复 `HermesProvider` 异步执行与 reconciler 轮询，使长时间运行的 agent 任务能正常完成或超时失败。
- 强化输出提取：明确 `agent_jobs.sanitize_agent_result_text` 与 `agent_provider._clean_agent_output` / `_extract_hermes_reply` 的职责，过滤初始化/工具日志，空输出时显式失败。
- 调整 `message_aggregator.py` 判定规则：图片+文字合并、触发器跨消息继承、flush 边界可配置。
- 将 `wechat_task` skill 从"全文复制"改为"名称引用 + provider 模板渲染"。
- 任务 payload 携带聚合上下文（`is_aggregated`、`aggregated_local_ids`、`session_image_paths`）。

**Non-Goals:**
- 不替换 `message_aggregator.py` 的现有架构。
- 不新增独立"清洗服务"或"输出后处理模块"。
- 不改动 GenericAgent bridge 协议。
- 不调整微信 DB 解密或图片解码逻辑。

## Decisions

### 1. 统一输出清洗职责
- **选择**：`agent_provider.py` 的 `_extract_hermes_reply` / `_clean_agent_output` 负责从原始 stdout 提取最终回复；`agent_jobs.sanitize_agent_result_text` 负责轻量后处理（去 ANSI、去 Resume session 提示、合并行）。
- **理由**：当前两层都有清洗逻辑但职责重叠，导致 "Initializing agent…" 穿过两层后仍被保留。需要明确：provider 层决定"有没有可发送回复"，job 层做格式整理。
- **实现**：
  - `_clean_agent_output` 不再是 stub：基于行首/行特征过滤 `^Initializing\b`、`^Query:`、`^preparing\b`、`^\s*read\b`、`^\s*find\b`、纯框线行、空行、Resume session 提示。
  - `_extract_hermes_reply` 优先匹配 Hermes 框；无框时调用强化后的 `_clean_agent_output`；仍为空则返回空字符串。
  - `HermesProvider.run()` / `poll()` 在 `_extract_hermes_reply` 返回空时返回 `AgentResult(ok=False, status="failed", error="hermes returned no sendable reply")`，确保 reconciler 不会把空字符串当作有效回复。
  - `agent_jobs.sanitize_agent_result_text` 保持现有去 ANSI/框线/Resume 逻辑，但不再承担"识别初始化噪声"职责。

### 2. 先诊断再修复 Hermes 异步执行与 reconciler
- **选择**：`control_api.py` 的 `_async_reconciler_run_once` 已实现 `mark_expired`、running backoff、`update_poll_state`。生产测试中 `agent_deadline_at` 过期但任务仍在 `agent_running`，说明调用链存在 bug，需要先定位再修改。
- **理由**：避免在不确定根因的情况下重写已实现的分支。
- **实现**：
  - 诊断：通过日志/SQL 查询确认 `list_pollable()` 是否返回过期任务、reconciler 是否被调用、`poll()` 返回值是什么。
  - 根据诊断结果修复以下可能点之一：
    - `list_pollable()` SQL 条件漏了 deadline 过滤或 next_poll_at 未设置；
    - `HermesProvider.poll()` 在 `result.json` 不存在时返回错误状态而非 `running`；
    - `HermesProvider.poll()` 在 `result.json` 存在但无有效回复时错误返回 `ok=True`；
    - async loop 未正确调度 reconciler。
  - 同步修复 helper `runner.py`：用 `try/finally` 保证 `result.json` 在 timeout/异常时仍被写出。

### 3. 调整聚合判定规则
- **选择**：在 `message_aggregator.py` 内修改 `ingest_event` 与 `AggregatedTurn` 行为，新增配置项。
- **理由**：现有模块已承担聚合职责，只需明确边界条件。
- **实现**：
  - `has_image_task_description()`：在 `trigger_matched` / `in_session` / `event_context.trigger_matched` / `event_context.session_active` 为真时，只要有非空文字即视为有效描述。
  - 允许通过 `target.policy.image_task_markers` 扩展关键词列表，默认保留当前关键词。
  - `ingest_event` 在检测到"终止词"（如 "好了"、"就这样"、"结束"）时立即调用 `_build_turn` 返回当前 turn。
  - 支持 `max_aggregated_messages` 配置，达到上限立即 flush。
  - 窗口打开时如果新消息命中触发器，则把 `trigger_matched` 传播到整个 turn。

### 4. Skill 模板引用
- **选择**：`reply_engine.py` 入队时只传 `skill_name`；`agent_provider._build_wechat_deep_prompt()` 从 `skills.load_skill(skill_name)` 读取模板并渲染。
- **理由**：减少 payload 大小，避免 skill 文本污染任务上下文，便于统一修改模板。
- **实现**：
  - payload 中删除 `skill_prompt` 字段，保留 `skill_name`。
  - `_build_wechat_deep_prompt` 优先使用 `skill_name` 加载模板；若 payload 仍有 `skill_prompt` 则兼容使用并记录告警。
  - `wechat_task/skill.md` 改为 Jinja2/simple 占位符模板（如 `{{mention_name}}`、`{{user_request}}`、`{{knowledge_hits}}`）。

### 5. Job payload 携带聚合上下文
- **选择**：`AggregatedTurn.to_generate_reply_message()` 增加聚合元数据字段，`reply_engine.py` 透传到 job payload。
- **理由**：agent 需要知道任务是否由多消息聚合而来，尤其是图片+描述分离场景。
- **实现**：
  - payload 增加 `is_aggregated`、`aggregated_local_ids`、`text_parts_count`、`session_image_paths`。
  - 任务超时根据是否包含图片/聚合上下文调整：图片 deep_agent 任务默认 240s，文本 90s。

## Risks / Trade-offs

- **Hermes 响应时间长**：即使修复轮询，Hermes 图片分析仍可能耗时 60-120 秒。需要设置合理的用户预期或在 UI 显示"处理中"。
- **聚合窗口配置敏感**：窗口过长会增加延迟，过短会拆分会话。默认保持 `_DEBOUNCE_SECONDS=5.0`、`_MAX_WINDOW_SECONDS=12.0`，后续根据实际场景微调。
- **Skill 模板向后兼容**：旧 job 仍可能包含 `skill_prompt`。兼容逻辑会增加少量代码复杂度，但避免重启后旧任务失败。
- **输出清洗误伤**：过滤规则基于行首/行特征匹配（如 `^Initializing\b`、`^preparing\b`），尽量避免误删有效回复中的技术词汇。
- **M5 reconciler 轮询频率**：默认 5 秒轮询可能导致用户感觉慢；可接受，因为 Hermes 执行本身较长。
