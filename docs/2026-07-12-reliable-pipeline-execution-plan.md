# Reliable WeChat Pipeline — 合并执行计划

> 合并自 `docs/2026-07-11-reliable-pipeline-refactor.md` 与当前 review 结论。
> 架构决策记录见 codebase-memory ADR：`Reliable WeChat Pipeline Refactor`。
> 工作基线：main 已合并 `reliable-pipeline` Stage 1（commit `0d0da5e`），后续开发直接在 main 进行。

---

## 1. 当前状态（截至 2026-07-12）

| 检查项 | 状态 | 证据 |
|--------|------|------|
| P1 合回 main | **已完成** | main merge commit `0d0da5e`: `Merge reliable-pipeline: Stage 1 durable transport + strict AgentResult contract` |
| Orca worktree | **已移除** | `E:/projects/orca/workspaces/wechat-automation-local/reliable-pipeline` 已删除；其分支已合入 main |
| Stage 1 核心测试 | **通过** | pipeline/worker/contract 聚焦测试 75 条通过；scheduler 专项测试 16 条通过 |
| E2E 端到端 | **通过** | local_id `930` 完成：job done action=reply → outbox 发送 → 可见回复；旧 E2E job `3` 被 deliberate quarantine，失败且未发件 |
| 运行时服务 | **运行中** | runtime 已同步/部署，`control_api` 在 `CD-only` 目录运行；scheduler 运行时自动启动；source hash/cursor 校验通过 |
| Scheduler 代码 | **已提交** | commit `6943d30`: `Add reliable pipeline scheduler controls`；`wechat-decrypt/control_api.py` 与 `wechat-decrypt/tests/test_control_api_reliable_scheduler.py` 已纳入 reliable scheduler status/start/stop、runtime auto-start、`reliable_pipeline.enabled=True` 时自动启动、不可重试 quarantine endpoint |
| 全量测试 | **未直接验证** | 历史上的 `tests/test_wechat_auto_cli_bridge.py` 收集失败问题**未在本次验证中复测/解决**；不能断言全 suite 已恢复健康 |
| Stage 2 Phase A<br>Consumer-side contract convergence (foreground agent_worker, strict async reconciler jobs, reliable_worker) | **已提交** commit `88ee131 Unify strict AgentResult consumers`；未部署，未运行 E2E | 206 条聚焦测试通过；相关生产模块编译通过；已提交，尚未部署，未运行 E2E |
| Stage 2 Phase B<br>Hermes provider default/always strict + legacy extractor retirement | **源码验证完成，待提交/部署/E2E** | Hermes `run`/`submit`/`poll` strict-only；legacy stdout/parser 与 Python tool loop 已退役；173 条聚焦测试通过、4 个生产模块编译通过；全仓 collect-only 仍被既有 `wechat_auto` 缺失阻断 |
| Full suite health | **未复测/解决** | 历史测试收集问题：`test_wechat_auto_cli_bridge.py` 缺少 `wechat_auto` 导致的收集失败未在本次验证中复测/解决 |

### 1.1 今日状态摘要

- **P1 完成**：Stage 1 durable transport + strict AgentResult contract 已通过 merge commit `0d0da5e` 进入 main。
- **Scheduler 已提交**：dedicated reliable scheduler status/start/stop、runtime auto-start、quarantine endpoint 已提交为 commit `6943d30` (`Add reliable pipeline scheduler controls`)；16 条 scheduler 测试通过并部署到 runtime。
- **E2E 通过**：最新 foreground E2E（local_id `930`）产生可见回复；quarantine 路径验证 job `3` 未产生发送。
- **Stage 2 状态**：
  - **Phase A consumer-side contract convergence**：已提交为 commit `88ee131 Unify strict AgentResult consumers`；strict 消费者仅接受可解析的 `raw.agent_result`，reliable_worker 的 reply_text/test-provider fallback 已移除；provider errors/exceptions/malformed result objects/raw diagnostics 在触及路径中按 fixed-safe-marker 处理；dispatcher build/submit/poll 失败路径恢复锁；silent/escalate 使用原子 sent/skipped 转换。聚焦测试 206 条通过；相关生产模块编译通过。**未部署，未运行 E2E。**
  - **Phase B Hermes strict-default + legacy extractor retirement**：源码实现与本地验收完成，尚待提交/部署。`HermesProvider.run`、`submit`、`poll` 始终严格解析完整 `AgentResult`；退役 box/stdout/tool-call 自由文本提取与本地 Python `leann_search` tool loop；同步 `tool_agent` 路径带 target-scoped KB allowlist 且只采信 `raw.agent_result`。新增显式 legacy async job expire 控制与无持久化 strict profile preflight；HTTP 预检只允许已配置 Hermes provider。173 条聚焦测试通过，4 个生产模块编译通过；全仓 collect-only 仍被既有 `wechat_auto` import 缺失阻断。
  - **Stage 2 整体未完成**：尚待 Phase B 提交、运行时 strict profile preflight、同步部署和 `bot群聊测试` E2E；不可标记为已部署或已 E2E。
- **遗留事项**：
  - 提交并部署 Stage 2 Phase A + Phase B；在运行时执行 strict profile preflight 与测试群 E2E；
  - 不处理 Stage 3/4 设计范围之外的功能变更。

---

## 2. 边界与不变量（维持原意，微调表述）

1. **WeChat 侧只拥有确定性传输、授权、安全边界、真实发送确认**；Hermes 拥有任务解释与回复生成。
2. **monitor cursor 只在前一个 source event 已 durable 入本地 inbox 后才推进**。
3. **每个被接受的 source event 有唯一 `source_event_id`**；重复读取无害。
4. **turn、worker result、outbound send 各自 durable**，进程重启后可恢复。
5. **一条回复最多只发送一次**；数据库确认后发送，或进入可审计的 terminal failure/dead-letter 状态。
6. **Hermes stdout 不被视为回复**，除非它是合法的版本化 `AgentResult` 文档且进程成功退出。
7. **本地确定性安全过滤、target 授权、预算、真实发送确认始终留在 Hermes 外**。
8. **Stage 1 不迁移 KB 检索/回复决策语义**；Stage 3 才迁移，但本地授权、安全边界、预算、确认仍保留。

---

## 3. 分阶段计划（合并版）

### 3.1 合回 main 前置项（新增）

> 这是把 Stage 1 从 `reliable-pipeline` worktree 合并到 main 之前必须完成的步骤。截至 2026-07-12，P1.1–P1.3 已完成， Stage 1 已通过 merge commit `0d0da5e` 进入 main。

- [x] **P1.1 修复全量测试收集错误**
  - 目标：`tests/test_wechat_auto_cli_bridge.py` 能导入成功或被移除/标记跳过；验收 `python -m pytest tests/ --collect-only` 不再报错。
  - 实际结果：作为 merge 前置项已处理，不再阻塞 Stage 1 合回 main；**但验收标准 `pytest --collect-only` 未在本次会话中复测，全 suite 健康状态未确认**。
  - 证据：merge commit `0d0da5e` 已完成；Stage 1 聚焦测试（75 条）与 scheduler 测试（16 条）通过。历史收集失败问题不能断言已恢复。

- [x] **P1.2 删除主仓过时的 `test_hermes_result_contract.py`（cleanup-only 前置）**
  - 实际结果：已完成；合并后 `wechat-decrypt/tests/test_hermes_result_contract.py` 为权威版本，断言新 `{schema_version, action, reply_text, reason_code, risk_level}` 契约。
  - 证据：merge commit `0d0da5e` 带入权威版本；旧 untracked 版本不再存在于主仓。

- [x] **P1.3 从 worktree 合并到 main（含带入权威测试）**
  - 职责：把 worktree 的全部变更合并回主仓，并让权威 `test_hermes_result_contract.py` 成为主仓的 tracked 文件。
  - 实际结果：已完成；合并后已同步到 `CD-only` 运行时目录，重启服务，并通过 E2E 验证。
  - 证据：
    1. `git log --oneline` 显示 merge commit `0d0da5e`；
    2. 运行时同步/部署后 `control_api` 运行正常；
    3. E2E local_id `930` 产生可见回复；旧 E2E job `3` quarantine 失败且未发件。

---

### 3.2 Stage 1：Durable Transport Foundation（已完成）

> 调整后边界：Stage 1 只负责**管道侧**消费并拒绝/应用 `AgentResult`；Hermes 是否**产出**该契约由 Stage 2 统一。

#### Goal
让入站捕获、turn 组装、worker result 处理、出站发送都具备可恢复性，不移动 KB 检索或回复决策语义。

#### Scope
- SQLite inbox 与唯一 `source_event_id`。
- durable debounce window 与 turn assembly，含重启恢复。
- durable turn job 生命周期：lease、per-group serial claim、deadline、retry metadata、terminal failure。
- durable outbox：atomic claim、指数退避、数据库确认发送完成、dead letter 审计。
- **管道侧**严格版本化 `AgentResult` 消费：
  - `reply` / `silent` / `escalate` 三 action；
  - 非 0 退出、timeout、畸形 JSON、Hermes box/ANSI、日志文本、任意 JSON -looking stdout 均视为失败，不会发送。
- 围绕现有 listener、provider、sender 入口的兼容性适配。
- 一个 Hermes 异步 runner 协议用于已提交工作。

#### Explicit Non-goals（保持原意）
- 不迁移 KB 检索策略、排序、工具权限、prompt 策略。
- 不扩展 Hermes 文件系统、数据库或 MCP 权限。
- 不替换现有 target/admin/mute 策略。
- 不超越 `bot群聊测试` 的 shadow-send/生产切换。

#### Boundaries
- 本地保留：进程退出验证、结果 schema 验证、target 授权、安全过滤、发送所有权。
- Hermes 可决定任务流，但只能使用 target 授权的工具。
- 不新增直接任意文件系统/数据库能力给 Hermes。

#### Acceptance（已验证）
- [x] 恢复性测试覆盖：入站持久化后、job 创建后 worker 认领前、worker 完成后发送前、发送尝试后确认前四个崩溃切点。
- [x] 一个 source event 最终只产生：一次确认发送、显式 silent/escalated 处理、或可审计的失败/dead-letter 记录。
- [x] 发送方重试使用有界指数退避，不尝试 terminal/dead-letter outbox 行。
- [x] 严格解析拒绝：非 0 退出、timeout、畸形 JSON、Hermes box、ANSI/日志输出、不符合契约的 JSON 文本。

#### Cutover Condition（已满足）
- 仅在显式配置的目标上启用 durable pipeline；初始真实 E2E 限制在 `bot群聊测试`；发送方拒绝其他目标的测试模式投递。

---

### 3.3 Stage 2：Unified Hermes Worker Protocol

> 与原版一致，但明确把“Hermes 产出契约”的责任从 Stage 1 移到 Stage 2，避免重叠。
> **Stage 2 整体未完成。** 当前 Phase A（consumer-side 收敛）已提交为 commit `88ee131 Unify strict AgentResult consumers`；Phase B（Hermes strict-default + legacy extractor 退役）源码验证完成、待提交/部署/E2E。

#### Stage 2 分解

- **Phase A：Consumer-side contract convergence**（已提交 commit `88ee131`）
  - 统一 foreground `agent_worker`、strict async reconciler jobs、`reliable_worker` 对 strict `AgentResult` 的消费。
  - strict 消费者只接受可解析的 `raw.agent_result`；从 `reliable_worker` 中移除 reply_text / test-provider fallback 路径。
  - provider errors / exceptions / malformed result objects / raw diagnostics 在触及路径中按 fixed-safe-marker 处理。
  - dispatcher build/submit/poll 失败路径恢复锁；`silent`/`escalate` 使用原子 sent/skipped 状态转换。
  - 验证：206 条聚焦测试通过；相关生产模块编译通过。
  - **限制：未部署，未运行 E2E。**

- **Phase B：Hermes provider default/always strict + legacy extractor retirement**（源码验证完成，待提交/部署/E2E）
  - Hermes `run`/`submit`/`poll` 始终使用完整 stdout 的 strict `AgentResult` 解析；submit 元数据固定 `strict=true`。
  - 退役 `_extract_hermes_reply`、`_extract_tool_call`、box/stdout/line-JSON 提取和本地 Python tool loop；Hermes 使用 target-authorized MCP knowledge access。
  - 同步 tool-agent payload 固定 strict contract 并携带 `_allowed_kb_ids`；reply/silent/escalate 只由 contract action 驱动。
  - 显式控制面提供 active legacy async job terminal-expire 与无持久化 strict profile preflight；预检 HTTP 路由只使用服务端配置的 Hermes provider。
  - 验证：173 条聚焦测试通过；4 个生产模块编译通过；全仓 collect-only 仍被既有 `tests/test_wechat_auto_cli_bridge.py` 的 `wechat_auto` 缺失阻断。
  - **Stage 2 整体在 Phase B 提交、strict profile preflight、部署和测试群 E2E 完成前不可标记为完成。**

#### Goal
消除同步/异步 Hermes 执行的分歧，用统一结构化 worker 协议替换显示文本解析。

#### Scope
- 同步与异步 worker 调用使用同一 runner 与 result.json 格式。
- 将剩余 Python 管理的 tool loop 移到 Hermes runner 后，或替换为 Hermes-native 受控 MCP 调用。
- 用仅契约解析替换 legacy stdout/box/line-JSON 提取。
- 基于真实 provider 能力契约实现或移除恢复端点。
- 一旦 durable contract 成为权威，就退役重复的结果清洗器。
- **新增：确保 Hermes 产出的 `AgentResult` 严格符合 `{schema_version, action, reply_text, reason_code, risk_level}`，没有任何 display-text 回退路径。**

#### Boundaries
- Hermes 可决定任务流，仅使用 target 授权工具。
- 本地保留：进程退出验证、结果 schema 验证、target 授权、安全过滤、发送所有权。
- 不新增直接任意文件系统/数据库能力给 Hermes。

#### Acceptance
- [ ] 同一 fixture job 在 foreground 与 background 路径产生等价的 `AgentResult` 对象。
- [ ] 工具调用、工具失败、timeout、非 0 退出、畸形结果、`silent`、`escalate` 都有确定性契约测试。
- [ ] 生产路径不再调用 `_extract_hermes_reply`、`_extract_tool_call` 或将终端渲染作为应用数据。
- [ ] 新增/更新：`HermesProvider.run` 与 `HermesProvider.poll` 在 strict mode 下只接受合法 `AgentResult` JSON。

#### Cutover Condition
- 所有活跃 worker 实例支持结构化 runner。
- 现有 job 要么通过 legacy reconciler 完成，要么通过审计过的兼容 reader 显式迁移；没有混合协议 job 被静默重试。

---

### 3.4 Stage 3：Knowledge and Reply-Decision Migration

> 与原版一致，但加入 review 发现：旧 `reply_engine.generate_reply` 路径仍在预取/拼装 KB，迁移时要删除该路径；同时避免破坏 MCP KB 工具。

#### Goal
把任务解释、检索选择、回复决策/编写移到 Hermes，同时保留本地授权与安全边界。

#### Scope
1. **提取独立知识检索模块（新增）**
   - 从 `reply_engine.py` 抽出 `retrieve_knowledge_layers` 与 `_knowledge_hits_to_payload` 到 `knowledge_retrieval.py`。
   - 让 `reply_engine.py` 与 `tools/search_knowledge_mcp_server.py` 都依赖 `knowledge_retrieval.py`。
   - 目的：删除旧 `generate_reply` 路径时，不会连带打断 Hermes 的 `search_knowledge` MCP 工具。

2. **暴露 target 授权的统一 `search_knowledge` facade（原版）**
   - 不再在 prompt 中直接暴露 provider-specific FTS、LEANN、hook、IMA、GetNote 行为。
   - 传给 Hermes 的是 durable turn 引用与允许的 KB/tool 标识符，而非预拼装 KB 片段。
   - Hermes 按需调用 `mcp__wechat_kb_search__search_knowledge`。

3. **让 Hermes 选择 `reply`/`silent`/`escalate` 并检索内容（原版）**
   - 对已迁移目标，旧 `reply_engine.generate_reply` 中的 trigger/session/KB/路由/小聊天/raw prompt 构建不再使用；未迁移目标仍由原逻辑处理。

4. **迁移并统一路由决策（更新）**
   - 保留 `_is_reliable_pipeline_target` per-target gate 作为迁移开关：未启用目标仍走（或保持沉默）旧路径，已启用目标统一走新 pipeline。
   - 在 Stage 3 结束时应保证：所有**已启用**目标不再调用旧 `generate_reply` 路径；未启用目标仍由原逻辑处理。

#### Boundaries
- 本地 facade 验证 target-to-KB 授权、应用资源预算、返回结构化 provenance、区分 no-hit 与 provider failure。
- Hermes 不能选择 allowlist 之外的 KB。
- 本地确定性安全 block/handoff 规则保持强制，并合并到单一策略来源。

#### Acceptance
- [ ] 有标注回归语料覆盖：词汇匹配、纯语义匹配、跨 KB 隔离、no-hit、provider outage、图片、多消息 turn、沉默聊天、escalation。
- [ ] 每个 Hermes 回复记录来源 provenance 或显式 no-source 原因。
- [ ] 检索失败可观测，且与空检索区分。
- [ ] 迁移目标的旧本地检索与回复路径不再被使用。
- [ ] **新增**：`reply_engine.py` 中旧 `generate_reply` 的 KB 预取/拼装代码被删除或标记为 dead；`tools/search_knowledge_mcp_server.py` 不依赖 `reply_engine`。

#### Cutover Condition
- shadow 对比在回复动作、来源选择、安全、延迟等指标上达到约定阈值。
- 每个 target class 的 KB 权限与语料 case 通过后才迁移。

---

### 3.5 Stage 4：Shadow Operation, Gradual Cutover, and Legacy Removal

> 与原版一致，但强调“删除双路径”而不是“保留两个路径”的当前状态。

#### Goal
在真实流量下证明新 pipeline 无重复发送，然后逐步切换并移除旧编排。

#### Scope
- shadow 执行记录决策与来源，但不发送。
- 按 target 的 feature flag 启用新 pipeline 投递，通过 parity review 后打开。
- 运营面板暴露：入站年龄、turn 年龄、worker 状态、outbox 尝试次数、确认延迟、失败、dead letters。
- 删除旧内存聚合、monitor 侧 prompt 拼装、Python tool loop、重复路由/安全列表、不再使用的 control endpoint。
- 在 telemetry 证明所有目标已稳定运行新 pipeline 后，删除 `_is_reliable_pipeline_target` per-target gate，因为所有目标已统一走 durable ingress；迁移期必须保留该 gate。

#### Boundaries
- shadow job 不创建可发送的 outbox 行。
- 回滚保持 target-scoped，直到 legacy path 被主动删除。
- 删除仅在 telemetry 证明无活跃 legacy job 且保留窗口期满后进行。

#### Acceptance
- [ ] shadow 模式不产生任何 WeChat 发送。
- [ ] canary target 在约定观察窗口内：零静默丢失、零未确认重复发送、零未授权 KB 访问。
- [ ] 完整切换有测试过的 target-scoped 回滚流程。
- [ ] 旧代码删除包含测试、控制平面清理、文档更新。
- [ ] **新增**：所有启用目标默认走新 pipeline；legacy `generate_reply` 路径被完全移除或隔离为只读历史参考。
- [ ] 在 legacy 路径删除后，所有目标统一走 durable pipeline，`_is_reliable_pipeline_target` gate 失去意义并被删除。

#### Cutover Condition
- 所有启用目标使用新 pipeline。
- 未完成的 legacy job 已 terminal 并保留用于审计。
- 约定观察窗口内无阻塞可靠性回归。

---

## 4. 关键执行顺序

1. [x] **P1 合回 main 前置项** — 已完成（merge commit `0d0da5e`）。全 suite 收集健康度未在本次会话中复测。
2. [x] **worktree → main 合并** + 同步到 `CD-only` + 服务重启 + E2E 回归 — 已完成；E2E local_id `930` 通过，quarantine job `3` 验证失败且未发件。
3. [x] **提交 scheduler 代码** — 已完成，commit `6943d30`: `Add reliable pipeline scheduler controls`。
4. [x] **Stage 2 Phase A**：consumer-side strict AgentResult 收敛 — 已提交为 commit `88ee131 Unify strict AgentResult consumers`；聚焦测试 206 条通过；相关生产模块编译通过；未部署，未运行 E2E。
5. [ ] **Stage 2 Phase B**：Hermes provider default/always strict + 剩余 legacy extractor 退役 — 源码验证完成（173 条聚焦测试、编译通过）；待提交、strict profile preflight、部署与 E2E。
6. [ ] **Stage 3**：提取 `knowledge_retrieval.py`，迁移 KB/回复决策到 Hermes，删除旧 `generate_reply` 路径。
7. [ ] **Stage 4**：shadow → canary → 全量切换 → 删除 legacy。

---

## 5. 风险与注意事项

1. **Stage 2 状态风险**：Phase A 已提交为 `88ee131 Unify strict AgentResult consumers`；Phase B 源码验证完成但尚未提交/部署。Stage 2 仍待 strict profile preflight 与 `bot群聊测试` E2E；任何文档、报告或运行时状态都不可将 Stage 2 标记为完成、已部署或已验证端到端。
2. **双路径风险**：当前 monitor 同时存在 legacy 路径与 reliable path。Stage 3 必须显式移除 legacy 调用点（`wechat_bot_monitor.py:1675-1707`、`1801-1803`）中已启用目标对旧 `generate_reply` 的调用，但保留 per-target gate 作为未启用目标的迁移开关；Stage 4 在全部切换后再删除该 gate。
3. **KB 工具依赖**：删除 `reply_engine.py` 前，必须先迁移 `tools/search_knowledge_mcp_server.py` 的依赖到 `knowledge_retrieval.py`。
4. **测试基线**：主仓与 worktree 的 `test_hermes_result_contract.py` 分裂问题已通过 merge commit `0d0da5e` 解决；后续全 suite 健康度需单独复测。
5. **全量测试未复测**：历史上 `test_wechat_auto_cli_bridge.py` 导致的收集失败问题**未在本次会话中复测**，不能作为已恢复断言；后续需作为仓库健康项验证。
6. **Scheduler 已提交**：scheduler 实现已提交为 commit `6943d30`，相关测试通过并部署到 runtime。
7. **安全边界不外移**：Stage 3 只迁移“解释、检索、决策/编写”，target 授权、确定性安全拦截、预算、发送确认仍留在本地。

---

## 6. 验收检查表（汇总）

| 阶段 | 关键交付 | 当前状态 | 完成标准 |
|------|---------|----------|----------|
| P1.1 | 修复全量测试收集 | **前置项已清，验收未验证** | 全 `pytest --collect-only` 健康（未确认） |
| P1.2 | 同步契约测试到主仓 | **已完成** | merge commit `0d0da5e` 带入权威 `test_hermes_result_contract.py` |
| P1.3 | worktree → main 合并 | **已完成** | merge commit `0d0da5e`；E2E local_id `930` 通过 |
| Stage 1 | Durable transport + 管道侧契约消费 | **已完成** | 75 条聚焦测试 + 16 条 scheduler 测试通过 + E2E 通过 |
| Stage 2 Phase A | Consumer-side strict AgentResult 收敛 | **已提交** commit `88ee131`；未部署，未运行 E2E | 206 条聚焦测试通过；相关生产模块编译通过；未部署，未 E2E |
| Stage 2 Phase B | Hermes provider strict-default + legacy extractor 退役 | **源码验证完成，待提交/部署/E2E** | 173 条聚焦测试通过、生产模块编译通过；无 production path 调用 legacy extractor；`HermesProvider` 默认 strict；完成 strict profile preflight 与 E2E |
| Stage 2 整体 | 统一 Hermes runner/strict contract 产出 | **未完成** | Phase A + Phase B 已提交、strict profile preflight 通过、已部署并完成 E2E |
| Stage 3 | KB/决策迁移到 Hermes | 未开始 | 旧 `generate_reply` 删除、KB 工具独立 |
| Stage 4 | Shadow/切换/legacy 删除 | 未开始 | 全目标新 pipeline、零重复发送 |

---

## 7. 维护说明

若本计划发生**架构层面**调整（如本地/Hermes 边界变化、阶段职责变化、迁移策略变化），需同步更新 codebase-memory ADR `Reliable WeChat Pipeline Refactor` 中的 Context/Decision/Consequences 与 Changelog，并在交付报告中声明 ADR revision。执行细节变更（任务顺序、负责人、时间线）只需更新本计划文件。

