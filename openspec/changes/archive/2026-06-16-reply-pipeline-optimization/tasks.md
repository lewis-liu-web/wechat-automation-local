## 1. Agent 输出清洗（agent_provider.py / agent_jobs.py）

- [x] 1.1 强化 `_clean_agent_output()`：使用行级正则过滤噪声行，包括 `^Initializing\b`、`^Query:`、`^preparing\b`、`^\s*read\b`、`^\s*find\b`、纯框线行（`^[─╭╮╰╯│\s]+$`）、Resume session 提示、空行
- [x] 1.2 强化 `_extract_hermes_reply()`：优先匹配 Hermes 回复框；无框时调用增强后的 `_clean_agent_output()`；仍为空则返回空字符串
- [x] 1.3 审查 `agent_jobs.sanitize_agent_result_text()`：把"识别初始化/工具日志"的职责迁移到 `agent_provider._clean_agent_output`；`sanitize` 仅保留 ANSI 去除、框线清理、Resume 提示截断、行合并等格式整理职责
- [x] 1.4 修改 `HermesProvider.run()`：`_extract_hermes_reply()` 返回空时返回 `AgentResult(ok=False, status="failed", error="hermes returned no sendable reply")`
- [x] 1.5 修改 `HermesProvider.poll()`：无有效回复时同样返回 failed 状态，原始 stdout 保留在 `raw` 字段
- [x] 1.6 添加单元测试：覆盖只有初始化提示、有框线回复、无框线但含中文回复、纯工具日志等场景

## 2. Hermes 异步执行与 Reconciler 修复

- [x] 2.0 **诊断先行**：root cause 为 `provider.poll(timeout=10)` 被 HermesProvider 忽略，导致 submit_timeout（240/30）与 DB `agent_deadline_at`（600/300）不一致；超时后 reconciler 回退使 `next_poll_at` 超过 DB deadline，`list_pollable` 不再返回该任务，从而卡在 `agent_running`
- [x] 2.1 修复 `HermesProvider.submit()` 的 helper `runner.py`：使用 `try/finally` 保证 `result.json` 在 timeout/异常时仍被写出
- [x] 2.2 修复 `HermesProvider.poll()`：`result.json` 不存在时检查 `agent_deadline_at`，未超时返回 `AgentResult(False, "running")`，已超时返回 `timeout`
- [x] 2.3 修复 reconciler 调用链：捕获 `provider.poll()` 异常并按 `poll_error` 回退，避免整批中断
- [x] 2.4 修复 reconciler 空回复处理：`sanitize_agent_result_text` 后为空时不写入 `done`，标记 `failed` 并保留 `agent_raw_output`
- [x] 2.5 在 UI / API 中暴露 agent 原始输出与清洗后回复：reconciler 各分支合并 `poll_result.raw` 到 `payload.agent_raw_output`；UI 异常/待发送任务 expander 展示原始输出

## 3. 聚合器判定规则调整（message_aggregator.py）

- [x] 3.1 调整 `has_image_task_description()`：在 trigger/session 状态下，任意非空文字即视为有效描述；支持通过配置扩展关键词列表
- [x] 3.2 调整 `ingest_event()`：窗口内后续消息命中触发器时，将整个 turn 标记为 `trigger_matched=true`
- [x] 3.3 增加终止词检测：收到 "好了"、"就这样"、"结束" 等词时立即 flush 当前窗口
- [x] 3.4 增加 `max_aggregated_messages` 配置支持：达到上限立即 flush
- [x] 3.5 调整 `AggregatedTurn.to_generate_reply_message()`：输出 `is_aggregated`、`aggregated_local_ids`、`text_parts_count`、`session_image_paths` 等字段
- [x] 3.6 添加/更新 `message_aggregator.py` 单元测试：图片+描述合并、触发器跨消息继承、终止词 flush、最大消息数 flush

## 4. Skill 模板引用

- [x] 4.1 将 `wechat-decrypt/skills/wechat_task/skill.md` 改写为模板：使用 `{{mention_name}}`、`{{user_request}}`、`{{knowledge_hits}}` 等占位符
- [x] 4.2 修改 `reply_engine.py`：入队 payload 只携带 `skill_name` 和相关参数，不再复制 `skill_prompt` 全文
- [x] 4.3 修改 `agent_provider._build_wechat_deep_prompt()`：根据 `skill_name` 从 `skills.load_skill()` 加载模板并渲染；兼容旧 payload 中的 `skill_prompt`
- [x] 4.4 修改 `skills/__init__.py`：支持模板渲染（可基于 Jinja2 或 simple string replace）
- [x] 4.5 更新 `test_skill_injection.py`：验证 payload 不再含全文、provider 能正确渲染模板

## 5. Job Payload 与调度调整

- [x] 5.1 修改 `agent_jobs.py`：`enqueue_job` 接受并保存聚合元数据（`is_aggregated`、`aggregated_local_ids`、`session_image_paths`）
- [x] 5.2 修改 `reply_engine.py`：根据任务是否含图片/聚合上下文设置 `agent_timeout`，图片 deep_agent 默认 240s
- [x] 5.3 修改 `wechat_bot_monitor.py`：把聚合元数据透传给 `generate_reply`，确保进入 job queue 的任务携带完整上下文
- [x] 5.4 确保旧 job（含 `skill_prompt`）在新 provider 逻辑下仍可被消费

## 6. 端到端验证

- [x] 6.1 在本地运行单元测试：`pytest tests/test_skill_injection.py tests/test_reply_engine_enqueue.py tests/test_agent_jobs_payload.py tests/test_cua_sender.py`（33 passed）
- [ ] 6.2 在 CD-only 实例启动控制台，发送纯文本触发消息，验证 Hermes 能生成最终回复并发送 *[CD-only 环境，需单独验证]*
- [ ] 6.3 在 CD-only 实例发送"图片 + 后续文字描述"消息，验证被聚合为同一任务并正常处理 *[CD-only 环境，需单独验证]*
- [ ] 6.4 检查 job 状态流：queued → submitted → agent_running → done → sent，无 "Initializing agent…" 污染 *[CD-only 环境，需单独验证]*
- [ ] 6.5 验证 async loop 停止/重启后旧任务不会导致崩溃 *[CD-only 环境，需单独验证]*
