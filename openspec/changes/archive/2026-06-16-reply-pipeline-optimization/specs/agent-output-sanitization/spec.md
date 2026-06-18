## 修改说明

本 spec 不新增独立清洗层，仅强化 `wechat-decrypt/agent_provider.py` 中已有的 `_extract_hermes_reply` 和 `_clean_agent_output`。当前 `_clean_agent_output` 是 stub（仅 `strip()`），且 `_extract_hermes_reply` 在没有 Hermes 回复框时会 fall through 到该 stub，把 "Initializing agent…" 等初始化/工具日志当作可发送回复——这正是 job 11 出问题的直接路径。

## MODIFIED Requirements

### Requirement: `_extract_hermes_reply` 在 box 不存在时应丢弃初始化/工具日志
当 Hermes stdout 中没有 `╭─ ⚕ Hermes ─╮...╰─...╯` 回复框时，函数应识别并丢弃初始化提示（"Initializing agent…"）、工具调用日志（"preparing read_file…"、"read <path>" 等）、空行和终端分隔线，而不是把剩余文本当作回复返回。

#### Scenario: 只有初始化提示
- **WHEN** stdout 为 "Query: …\nInitializing agent…\n────────────────────────────────────────\n\n"
- **THEN** `_extract_hermes_reply()` 返回空字符串

#### Scenario: 工具日志中夹杂最终回复
- **WHEN** stdout 先打印工具日志，最后出现 Hermes 回复框
- **THEN** 提取框内内容，工具日志被忽略

### Requirement: `_clean_agent_output` 不再是 stub
`_clean_agent_output` 应至少过滤以下行：
- `Initializing agent…`
- `Query:` 前缀行
- `preparing .*…`、`read .*`、`find .*` 等工具调用日志
- 纯 ANSI 框线字符行（`─`、`│`、`╭`、`╮`、`╰`、`╯`）
- 空行和 Resume session 提示

#### Scenario: 无回复框的输出
- **WHEN** stdout 没有 Hermes 框，只有上述噪声
- **THEN** `_clean_agent_output()` 返回空字符串

### Requirement: 清洗结果为空时必须显式导致任务失败
当 `_extract_hermes_reply` 或 `_clean_agent_output` 返回空时，`HermesProvider.run()` / `poll()` 不应把空字符串作为 `reply_text`，而应返回 `AgentResult(ok=False, status="failed", error="hermes returned no sendable reply")`。

#### Scenario: agent 无有效输出
- **WHEN** Hermes 进程结束后 stdout 中没有可发送回复
- **THEN** job 状态变为 failed，error 为 "hermes returned no sendable reply"，原始 stdout 保留在 `raw` 中用于排查

### Requirement: 输出长度限制应作为后处理保留
在 box 提取或 clean 之后，仍对最终文本应用 `postcheck` 的长度限制（当前 1200 字符截断）。

#### Scenario: 超长有效回复
- **WHEN** Hermes 框内回复超过 1200 字符
- **THEN** 截断为 1200 字符并追加 "…"
