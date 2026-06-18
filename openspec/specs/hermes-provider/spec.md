## MODIFIED Requirements

`HermesProvider` 已提供基于 `hermes chat` CLI 的同步/异步调用能力。本次修复生产测试中暴露的两个问题：异步 runner 子进程没有正常写完 `result.json`、reconciler 对 `submitted`/`agent_running` 任务的轮询没有持续进行。

### Requirement: Hermes 异步 runner 必须可靠写出结果
`submit()` 启动的 helper 子进程应捕获 stdout/stderr，并在完成后写入 `result.json`，无论 agent 是否成功、超时或异常退出。`rc=None` 且 stdout 截断的情况不应出现。

#### Scenario: Hermes 正常完成
- **WHEN** `hermes chat` 成功输出最终回复
- **THEN** `result.json` 中包含完整 stdout、`rc=0`、`ok=true`

#### Scenario: Hermes 初始化卡住
- **WHEN** `hermes chat` 进程在初始化阶段被挂起或超时
- **THEN** helper 在 timeout 后写入 `result.json`，`ok=false`、`timeout=true`，stdout 保留已输出内容

### Requirement: Reconciler 必须持续轮询 submitted/agent_running 任务
`control_api.py` 的 M5 async reconciler 应定期扫描 `submitted` 和 `agent_running` 状态的任务，调用 `provider.poll()`，直到任务到达终端状态。

#### Scenario: 任务长时间运行
- **WHEN** Hermes 任务处于 `agent_running` 状态且尚未生成 `result.json`
- **THEN** reconciler 每 5 秒轮询一次，更新 `last_polled_at`/`next_poll_at`，不会停在初始 poll 后不再检查

### Requirement: Poll 应正确识别 result.json 未生成
`HermesProvider.poll()` 在 `result.json` 不存在时，应检查 deadline 并返回 `running` 状态；只有 `result.json` 存在时才解析结果，而不是把缺失结果当作失败。

#### Scenario: 任务仍在运行
- **WHEN** `poll()` 被调用但 `result.json` 尚未生成
- **THEN** 返回 `AgentResult(False, "running", ...)`，reconciler 继续下一轮 poll

### Requirement: Hermes 输出提取增强
`_extract_hermes_reply()` 应能处理更多 Hermes 输出格式：带框线回复、无框线回复、工具调用后无回复、以及只有初始化提示的情况。

#### Scenario: 工具调用后无有效回复
- **WHEN** stdout 中没有 Hermes 回复框且没有可识别中文回复
- **THEN** `_extract_hermes_reply()` 返回空，任务标记为 failed，错误为 "hermes returned no sendable reply"
