## ADDED Requirements

### Requirement: 任务只携带 skill 引用
agent job payload 不再携带完整 skill markdown 文本，而是携带 `skill_name` 与少量上下文参数。

#### Scenario: 正常任务入队
- **WHEN** `reply_engine` 为一条消息创建 job
- **THEN** payload 中只包含 `"skill_name": "wechat_task"`、`mention_name`、知识库参数等，不含 skill 全文

### Requirement: Provider 负责渲染最终 prompt
`agent_provider.py` 在调用 agent 前，根据 `skill_name` 从 `wechat-decrypt/skills/` 加载模板并渲染最终 prompt。

#### Scenario: Hermes 执行任务
- **WHEN** HermesProvider.run/submit 收到 job
- **THEN** provider 读取 `skills/wechat_task/skill.md`，将模板变量（用户请求、知识库片段、mention_name 等）填充后生成 prompt

### Requirement: Skill 模板变量化
`wechat_task/skill.md` 拆分为稳定模板 + 动态数据区，模板中使用占位符（如 `{{mention_name}}`、`{{knowledge_hits}}`、`{{user_request}}`）。

#### Scenario: 修改模板
- **WHEN** 运营调整回复语气或规则
- **THEN** 只需修改 skill.md，无需改动 `reply_engine.py` 或每个 job payload

### Requirement: 向后兼容
旧 job payload 中如果仍包含完整 `skill_prompt`，provider 继续兼容处理，不破坏已有任务。

#### Scenario: 旧任务重试
- **WHEN** 队列中存在包含 `skill_prompt` 的旧 job
- **THEN** provider 优先使用 `skill_prompt` 字段，同时发出兼容性告警日志
