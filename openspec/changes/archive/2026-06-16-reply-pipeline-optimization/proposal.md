## Why

刚刚在生产环境（CD-only 实例）测试发现三个影响可用性的严重问题：Hermes agent 回复被污染（输出初始化/工具日志而非最终答案）、微信群消息聚合规则没有正确处理图片与描述的拆分场景、以及 `wechat_task` skill 被作为完整 prompt 逐次注入导致 token 浪费和上下文污染。这些问题共同导致回复链路无法真正跑通，需要优化现有聚合判定、agent 上下文结构和输出清洗。

## What Changes

- 修复 HermesProvider 异步执行与 reconciler 轮询链路，确保 agent 输出被正确提取、过滤和超时回收，不再把"Initializing agent…"当作回复发送。
- **调整消息聚合器判定规则**：明确图片+文字分离消息何时合并为同一 turn、触发器跨消息命中的处理、以及 flush 边界条件（最长等待 vs 收到描述后提前 flush）。
- 重构 `wechat_task` skill 的注入方式：由"每次任务复制全文 prompt"改为"任务携带 skill 名称 + 轻量参数"，provider 在构造最终 prompt 时引用 skill 模板，避免污染上下文和重复 token。
- 在 `agent_provider.py` 增加输出清洗层（`_extract_hermes_reply` 强化 + `sanitize_agent_result_text` 兜底），过滤工具调用痕迹和空初始化输出。
- 新增或修改 control_api / UI 端点，支持查看聚合任务状态、手动触发 flush、以及展示 agent 原始输出 vs 最终发送文本。

## Capabilities

### New Capabilities

- `skill-template-injection`: 通过 skill 名称引用模板，替代每次复制完整 prompt。
- `agent-output-sanitization`: 从 agent 原始输出中提取最终可发送回复，过滤工具日志和初始化噪声。

### Modified Capabilities

- `message-to-task-aggregation`: 调整现有 `message_aggregator.py` 的聚合判定规则，优化图片+文字分离消息、触发器跨消息命中、flush 边界条件等行为。
- `agent-job-queue`: 任务 payload 增加聚合上下文、skill 引用、消息类型列表，调度逻辑支持聚合任务超时。
- `hermes-provider`: 异步执行与轮询超时策略调整，输出提取逻辑增强。

## Affected Areas

- `wechat-decrypt/agent_provider.py`
- `wechat-decrypt/agent_jobs.py`
- `wechat-decrypt/reply_engine.py`
- `wechat-decrypt/wechat_bot_monitor.py`
- `wechat-decrypt/message_aggregator.py`
- `wechat-decrypt/skills/wechat_task/skill.md`
- `wechat-decrypt/control_api.py`
- `wechat-decrypt/wechat-control-ui/app.py`
