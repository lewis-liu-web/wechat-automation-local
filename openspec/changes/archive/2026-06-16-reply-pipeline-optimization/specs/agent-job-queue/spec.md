## MODIFIED Requirements

`agent_jobs.py` 已提供 SQLite-backed job queue 和基本调度能力。本次调整任务 payload 与调度语义，使其适配聚合后的消息任务和 skill 模板引用。

### Requirement: 任务 payload 应携带聚合上下文
由 `reply_engine` 入队的 agent job，payload 中应包含聚合来源信息（`is_aggregated`、`aggregated_local_ids`、`session_image_paths`、`text_parts_count` 等），供 agent 理解任务是否为多消息合并而来。

#### Scenario: agent 接收聚合任务
- **WHEN** `reply_engine` 为一个 `AggregatedTurn` 创建 deep_agent job
- **THEN** payload 中包含 `is_aggregated=true`、`aggregated_local_ids=[526,527]`、`session_image_paths=[...]`

### Requirement: 任务 payload 应引用 skill 而非复制全文
入队 payload 不再携带 `skill_prompt` 全文，改为携带 `skill_name`、`mention_name`、`knowledge_hits`、`knowledge_bases` 等参数。

#### Scenario: 正常 deep_agent 入队
- **WHEN** `reply_engine` 创建 raw_agent job
- **THEN** payload 中只含 `"skill_name": "wechat_task"`，不含 skill.md 全文

### Requirement: 调度逻辑支持聚合任务超时
聚合任务可能包含图片识别、多轮工具调用，默认超时需要比单文本任务更长，且应可配置。

#### Scenario: 图片分析任务
- **WHEN** 任务包含图片且路由为 deep_agent
- **THEN** `agent_deadline_at` 使用 `agent_timeout` 配置（默认 240 秒），而不是文本任务的 60 秒

### Requirement: 向后兼容旧 payload
队列中已存在的包含 `skill_prompt` 字段的旧 job，应继续被消费，不因为缺少 `skill_name` 而失败。

#### Scenario: 旧任务重试
- **WHEN** 重启后队列中有旧格式的 job
- **THEN** `provider_from_config` 或 `AgentProvider` 仍可使用 `skill_prompt` 字段构造 prompt
