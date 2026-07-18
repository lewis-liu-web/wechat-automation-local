# Reliable WeChat Pipeline — 合并执行计划

> 合并自 `docs/2026-07-11-reliable-pipeline-refactor.md` 与当前 review 结论。
> 架构决策记录见 codebase-memory ADR：`Reliable WeChat Pipeline Refactor`。
> 工作基线：main 已合并 `reliable-pipeline` Stage 1（commit `0d0da5e`），后续开发直接在 main 进行。

---

## 1. 当前状态（截至 2026-07-18）

| 检查项 | 状态 | 证据 |
|--------|------|------|
| P1 合回 main | **已完成** | main merge commit `0d0da5e`: `Merge reliable-pipeline: Stage 1 durable transport + strict AgentResult contract` |
| Orca worktree | **已移除** | `E:/projects/orca/workspaces/wechat-automation-local/reliable-pipeline` 已删除；其分支已合入 main |
| Stage 1 核心测试 | **通过** | pipeline/worker/contract 聚焦测试 75 条通过；scheduler 专项测试 16 条通过 |
| E2E 端到端 | **通过** | local_id `930` 完成：job done action=reply → outbox 发送 → 可见回复；旧 E2E job `3` 被 deliberate quarantine，失败且未发件 |
| 运行时服务 | **运行中** | runtime 已同步/部署，`control_api` 在 `CD-only` 目录运行；scheduler 运行时自动启动；source hash/cursor 校验通过 |
| Scheduler 代码 | **已提交** | commit `6943d30`: `Add reliable pipeline scheduler controls`；`wechat-decrypt/control_api.py` 与 `wechat-decrypt/tests/test_control_api_reliable_scheduler.py` 已纳入 reliable scheduler status/start/stop、runtime auto-start、`reliable_pipeline.enabled=True` 时自动启动、不可重试 quarantine endpoint |
| Dedicated 实例 on-duty 持久化 | **已完成** | `control_api._active_on_duty_instance_ids` 新增 `agent_provider.instances[].reliable_on_duty` 作为与 M5 async-loop 独立的 scheduler eligibility 来源；仍受 health gate；runtime `hermes-wechat-bot-worker3` 已设置该 flag；scheduler 专项 28 条测试通过；E2E local_id `1006` 验证 restart 后 worker3 仍能 claim 专属 job |
| 全量测试 | **collect 已恢复健康；全量运行基线待干净复跑** | 2026-07-14 修复 `tests/test_wechat_auto_cli_bridge.py` 的 sys.path（插入 repo 根而非包目录），`pytest --collect-only` 752 tests / 0 errors；该模块 13/13 通过 |
| Stage 2 Phase A<br>Consumer-side contract convergence (foreground agent_worker, strict async reconciler jobs, reliable_worker) | **已提交并部署** | commit `88ee131 Unify strict AgentResult consumers` 已随 Stage 2 runtime sync 部署；其 strict consumer contract 由 Phase B E2E 一并验证 |
| Stage 2 Phase B<br>Hermes provider default/always strict + legacy extractor retirement | **已完成** commit `5b3e196 Make Hermes provider strict-only`；已部署/E2E | Hermes `run`/`submit`/`poll` strict-only；legacy stdout/parser 与 Python tool loop 已退役；173 条聚焦测试、生产模块编译、configured profile strict preflight、`bot群聊测试` E2E 均通过；全仓 collect-only 已于 2026-07-14 恢复健康（752 tests / 0 errors） |
| Full suite health | **collect 已恢复；全量基线待串行复跑** | 2026-07-14：`--collect-only` 752 tests / 0 errors；首次全量运行 750 passed / 2 failed（`test_reliable_worker.py` 两个 factory 用例），待串行复跑确认可重复性 |

### 1.1 今日状态摘要

- **P1 完成**：Stage 1 durable transport + strict AgentResult contract 已通过 merge commit `0d0da5e` 进入 main。
- **Scheduler 已提交**：dedicated reliable scheduler status/start/stop、runtime auto-start、quarantine endpoint 已提交为 commit `6943d30` (`Add reliable pipeline scheduler controls`)；16 条 scheduler 测试通过并部署到 runtime。
- **E2E 通过**：最新 foreground E2E（local_id `930`）产生可见回复；quarantine 路径验证 job `3` 未产生发送。
- **Stage 2 状态**：
  - **Phase A consumer-side contract convergence**：commit `88ee131` 的 strict consumer 收敛已随本次 runtime sync 部署；其契约消费行为由严格 profile preflight 与 `bot群聊测试` E2E 一并验证。
  - **Phase B Hermes strict-default + legacy extractor retirement**：已提交为 `5b3e196 Make Hermes provider strict-only` 并部署。`HermesProvider.run`、`submit`、`poll` 仅接受完整 strict `AgentResult`；box/stdout/tool-call 自由文本提取与本地 Python `leann_search` tool loop 已退役；同步 `tool_agent` 带 target-scoped KB allowlist 且只采信 `raw.agent_result`。手动 legacy async expiry 控制与无持久化 configured-provider preflight 已实现。173 条聚焦测试通过，4 个生产模块编译通过。
  - **运行验证通过**：`hermes-wechat-bot-worker3` strict preflight 返回合法 `AgentResult`（`silent`）；`bot群聊测试` E2E 于 2026-07-13 08:55 使 inbound `5→6`、turn/job done `4→5`、outbox sent `3→4`，托管账号出现对应回复；无 dead letter、active legacy async job 为 `0`、两个 target cursor 均不落后于 DB。
  - **Stage 2 整体已完成**：已提交、已部署、strict preflight 与测试群 E2E 均通过。
- **Stage 3 状态**：
  - 核心迁移已完成：`knowledge_retrieval.py` 提取、target-authorized MCP facade、fail-closed `knowledge_grounded` gate、provenance 持久化、标注回归语料（`86 passed`）均已完成并部署。
  - **2026-07-14 补充修复**：
    - `fast_refresh_targets.py` 增加 bounded stable-snapshot retry（`STABLE_ATTEMPTS=3`），防止解密竞争导致的输出抖动。
    - `reliable_pipeline.py` 将 `target.dedicated_agent_instance_id` 传播到 turn/job payload 顶层，并新增 `claim_next_job(instance_id)`：general runner 只 claim unbound job，实例 runner 只 claim 绑定到自己的 job。
    - `control_api.py` 的 scheduler 仅对 on-duty 且 healthy 的实例分发 dedicated run（`_active_on_duty_instance_ids`）。
    - 新增/更新测试：stable-snapshot retry、dedicated id 传播/冲突、general/instance claim 路由、scheduler selection（healthy on-duty / not-on-duty / unhealthy）。
  - **E2E 验证**：
    - 006 (`local_id=965`)：job 22 被 `hermes-wechat-bot-worker3` 处理，`dedicated_agent_instance_id` 正确，但 CUA 发送阶段 `cua-driver list_windows` timeout，outbox 14 进入 `dead_letter`；证明 dedicated routing/claim 正确，但发送阶段不稳定。
    - 007 (`local_id=966`)：inbound event 23 → turn 23 → job 23 (`dedicated_agent_instance_id=hermes-wechat-bot-worker3`) → worker3 处理 → outbox 15 `status=sent`，`attempts=1` → 托管 `bot群聊测试` 窗口可见回复。完整 ingress→job→outbox→window 闭环通过。
  - **下一步**：Stage 3 进入按 target rollout 阶段，每切换一个目标前必须获得用户明确批准。


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
9. **Dedicated 实例 scheduler eligibility 与 M5 async-loop on-duty 解耦**：`agent_provider.instances[].reliable_on_duty` 是 restart-persistent 的 scheduler routing 来源；`_active_on_duty_instance_ids` 仍要求 `health.ok or health.ready`；不恢复、不依赖 M5 async-loop 的内存状态。

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
> **Stage 2 已完成。** Phase A consumer-side 收敛已提交为 `88ee131`；Phase B strict provider/legacy retirement 已提交为 `5b3e196`。两者已同步到 runtime，strict profile preflight 与 `bot群聊测试` E2E 均通过。

#### Stage 2 分解

- **Phase A：Consumer-side contract convergence**（已提交/部署，commit `88ee131`）
  - 统一 foreground `agent_worker`、strict async reconciler jobs、`reliable_worker` 对 strict `AgentResult` 的消费。
  - strict 消费者只接受可解析的 `raw.agent_result`；从 `reliable_worker` 中移除 reply_text / test-provider fallback 路径。
  - provider errors / exceptions / malformed result objects / raw diagnostics 在触及路径中按 fixed-safe-marker 处理。
  - dispatcher build/submit/poll 失败路径恢复锁；`silent`/`escalate` 使用原子 sent/skipped 状态转换。
  - 验证：206 条聚焦测试、相关生产模块编译；随 Stage 2 runtime sync 的 strict preflight 与 E2E 通过。

- **Phase B：Hermes provider default/always strict + legacy extractor retirement**（已完成，commit `5b3e196`）
  - Hermes `run`/`submit`/`poll` 始终使用完整 stdout 的 strict `AgentResult` 解析；submit 元数据固定 `strict=true`。
  - 退役 `_extract_hermes_reply`、`_extract_tool_call`、box/stdout/line-JSON 提取和本地 Python tool loop；Hermes 使用 target-authorized MCP knowledge access。
  - 同步 tool-agent payload 固定 strict contract 并携带 `_allowed_kb_ids`；reply/silent/escalate 只由 contract action 驱动。
  - 显式控制面提供 active legacy async job terminal-expire 与无持久化 strict profile preflight；预检 HTTP 路由只使用服务端配置的 Hermes provider。
  - 验证：173 条聚焦测试通过；4 个生产模块编译通过；configured profile strict preflight 通过；`bot群聊测试` E2E 通过；active legacy async job 为 `0`。

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
- [x] 同一 fixture job 在 foreground 与 background 路径产生等价的 `AgentResult` 对象。（2026-07-16 实证：`test_agent_provider_tool_agent.py::test_hermes_provider_run_and_poll_produce_equivalent_agent_result`——同一 fixture job 对象（run 与 submit 共用）配同一 contract（reply/silent/escalate 三例）经完整 `run` 与 `submit→poll` 两路径，直接断言 ok/status/reply_text 及完整 `raw["agent_result"]` 字典相等；测试通过）
- [x] 工具调用、工具失败、timeout、非 0 退出、畸形结果、`silent`、`escalate` 都有确定性契约测试。（2026-07-16 复核：工具调用拒绝 `test_strict_rejects_tool_call_json_object_only` / `test_hermes_provider_run_rejects_legacy_display_text_and_tool_call_json`；工具失败/no_tool_call 见 worker+pipeline 用例；timeout/非 0/畸形 `test_strict_run_rejects_nonzero_timeout_and_terminal_display_text`；silent/escalate `test_strict_run_maps_silent_and_escalate_without_reply_text`；`test_hermes_result_contract.py` + `test_agent_output_sanitization.py` + `test_agent_provider_tool_agent.py` 三文件 47 条全通过）
- [x] 生产路径不再调用 `_extract_hermes_reply`、`_extract_tool_call` 或将终端渲染作为应用数据。（2026-07-16 复核：`wechat-decrypt/` 生产代码 grep 零匹配）
- [x] 新增/更新：`HermesProvider.run` 与 `HermesProvider.poll` 在 strict mode 下只接受合法 `AgentResult` JSON。（2026-07-16 复核：两条路径均强制 `reliable_result_contract=True`；rc≠0、空 stdout、contract violation、legacy_not_migrated 四态拒绝；`test_strict_run_without_flag_is_strict` / `test_strict_submit_always_persists_strict_meta` 覆盖）

#### Cutover Condition
- 所有活跃 worker 实例支持结构化 runner。
- 现有 job 要么通过 legacy reconciler 完成，要么通过审计过的兼容 reader 显式迁移；没有混合协议 job 被静默重试。

---

### 3.4 Stage 3：Knowledge and Reply-Decision Migration

> 与原版一致，但加入 review 发现：旧 `reply_engine.generate_reply` 路径仍在预取/拼装 KB，迁移时要删除该路径；同时避免破坏 MCP KB 工具。

> **Current State (2026-07-14):** Stage 3 code migration, target-authorized MCP facade, durable provenance persistence, and the `knowledge_grounded` fail-closed gate are deployed. The E2E test target produced a strict `AgentResult` with LEANN provenance and a single outbox send. The annotated regression corpus is now in `wechat-decrypt/tests/fixtures/knowledge_corpus.json` and consumed by `tests/test_knowledge_corpus.py`; it exercises lexical match, semantic match (fake LEANN), cross-KB isolation, no-hit, and provider outage, and references exact `covered_by` pytest nodes in `tests/test_reliable_worker.py` for image, multi-message turn, silent, and escalation. Corpus + related facade/worker tests pass (`86 passed`). Stage 3 remains **open pending user-approved per-target rollout**; the per-target gate `_is_reliable_pipeline_target` stays in place.
>
> **2026-07-14 additional fixes:**
> - `AGENTS.md` E2E exception: documented the narrow debugging exception that allows transient runtime/code changes without pre-approval during dedicated E2E validation, with the requirement to explain impact, verify, and restore stable service.
> - `fast_refresh_targets.py`: added bounded stable-snapshot retry (`STABLE_ATTEMPTS=3`) with source fingerprint recheck under an output lock before publishing; race-decrypt mismatches are retried, and exhausted attempts leave the existing output unchanged.
> - `reliable_pipeline.py`: propagated `target.dedicated_agent_instance_id` to the top-level of turn/job payload, and to `claim_next_job(instance_id)` so the general runner only claims unbound jobs while each instance runner only claims its own bound jobs. Conflicting dedicated IDs within the same window now raise `ValueError` and leave the window open for retry.
> - `control_api.py`: `_reliable_worker_run_once` forwards `instance_id` to `reliable_worker.process_once`; `_reliable_scheduler_tick` runs a general pass then dispatches dedicated runs only to instances that are on-duty and healthy (`_active_on_duty_instance_ids`).
> - Tests added: stable-snapshot retry, dedicated-id propagation/conflict, general/instance claim routing, and scheduler selection of healthy on-duty / not-on-duty / unhealthy instances.
>
> **2026-07-14 E2E validation:**
> - 006 (`local_id=965`): job 22 was processed by `hermes-wechat-bot-worker3` with correct top-level `dedicated_agent_instance_id`, but the CUA send step timed out on `cua-driver list_windows` and outbox 14 became `dead_letter`. This confirms dedicated routing/claim, but not send reliability.
> - 007 (`local_id=966`): inbound event 23 → turn 23 → job 23 with `dedicated_agent_instance_id=hermes-wechat-bot-worker3` → worker3 processed → outbox 15 `status=sent` with `attempts=1` → visible reply in the managed `bot群聊测试` window. Full ingress→job→outbox→window closed-loop verified.
>
> **2026-07-14 acceptance review:** Stage 3 acceptance items 2–4 (provenance/no-source persistence, retrieval-failure vs no-hit distinction, legacy-path isolation for migrated targets) are verified against the deployed code and passing focused suites (67 tests: corpus + facade + MCP facade + durable ingress). Item 5 (`generate_reply` removal/dead-marking) is **partially complete and deferred to Stage 4**: the MCP server no longer depends on `reply_engine`, but `generate_reply` remains the live path for non-migrated targets and can only be removed after full cutover. Stage 3 therefore remains **open**: 4/5 acceptance items closed, 1 deferred, and per-target rollout + Stage 4 entry still require explicit user approval.


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
- [x] 有标注回归语料覆盖：词汇匹配、纯语义匹配、跨 KB 隔离、no-hit、provider outage、图片、多消息 turn、沉默聊天、escalation。
- [x] 每个 Hermes 回复记录来源 provenance 或显式 no-source 原因。（2026-07-14 复核：`reliable_worker.process_once` 全部终态路径——`apply_agent_result` 及 8 处 `fail_job`——均持久化结构化 provenance 到 `turn_jobs.provenance_json`；`tests/test_reliable_worker.py` / `tests/test_reliable_pipeline.py` 对应用例通过）
- [x] 检索失败可观测，且与空检索区分。（2026-07-14 复核：`provider_failure` / `no_hit` / `error` / `invalid` / `no_tool_call` 五态结构化区分；facade 与 MCP 层测试随 67 条聚焦测试通过）
- [x] 迁移目标的旧本地检索与回复路径不再被使用。（2026-07-14 复核：`wechat_bot_monitor.py` 在 `durable_ingress_event` 后 `continue`；`tests/test_monitor_durable_ingress.py` 显式断言不调用 `generate_reply` / `send_reply` / legacy 聚合）
- [ ] **新增**：`reply_engine.py` 中旧 `generate_reply` 的 KB 预取/拼装代码被删除或标记为 dead；`tools/search_knowledge_mcp_server.py` 不依赖 `reply_engine`。— **部分完成，deferred to Stage 4**：MCP server 已仅依赖 `knowledge_retrieval`（已验证）；但 `generate_reply` 仍是未迁移 target 的 live 路径（`test_monitor_thin_monitor.py` 等仍在使用），删除/标记 dead 须等 Stage 4 全量切换后执行，Stage 3 不勾选。

#### Cutover Condition
- shadow 对比在回复动作、来源选择、安全、延迟等指标上达到约定阈值。
- 每个 target class 的 KB 权限与语料 case 通过后才迁移。

#### Approval-gated per-target rollout checklist (Stage 3 → Stage 4)

The following gates must be explicitly approved by the user before any
additional target is switched from legacy to the reliable pipeline.
No target is enabled automatically. This checklist uses only existing
per-target fields already in `wechat_bot_targets.json` / `wechat_bot_targets.example.json`.

1. **Target selection criteria** (based only on existing schema attributes):
   - Candidate is currently on the legacy path (`reliable_pipeline_target` not `true`).
   - Candidate has at least one authorized KB configured.
   - Candidate's `mode` / `reply_policy` / `category` are compatible with the intended behavior (e.g., `customer_service`/`group_assistant`, `knowledge_grounded`/`balanced`).
   - Candidate has a `dedicated_agent_instance_id` matching a registered Hermes instance if one is required by the configured provider.

2. **Pre-switch validation** (must pass before approval):
   - Run the fixed annotated corpus regression suite: `python -m pytest tests/test_knowledge_corpus.py -v`.
   - Confirm the candidate's `last_local_id` >= DB `MAX(local_id)` while services are still running.
   - Confirm the reliable pipeline DB has no stuck queued jobs for this target.
   - Verify `control_api /status` returns `monitor_running=true`.

3. **Switch operation** (requires explicit user approval before touching runtime config):
   - Stop runtime services: place the configured STOP_FILE, wait for `wechat_bot_monitor.py` to exit, and stop `control_api.py` if its code changed.
   - Edit the runtime `wechat_bot_targets.json` to set `reliable_pipeline_target=True` on the chosen target only; do not change other targets.
   - Sync code to runtime using the robocopy rules in `AGENTS.md` (preserve DB, logs, STOP_FILE, and config files).
   - For the switched target, verify `last_local_id` is still >= DB `MAX(local_id)` before starting the monitor.
   - Start `control_api.py` from the runtime directory and verify `/status` is reachable/control_api healthy.
   - Remove any stale STOP_FILE.
   - Start `wechat_bot_monitor.py` from the runtime directory.
   - Verify `/status` reports `monitor_running=true` after the monitor is up.

4. **Canary E2E validation** (must pass after switch):
   - Send a test message from the approved test sender.
   - Verify `inbound_events → turns → turn_jobs → send_outbox` chain completes with `status=sent` and `attempts=1`.
   - Verify the reply appears in the managed WeChat window and cites expected KB provenance from the candidate's authorized KBs.
   - Verify `last_local_id` matches DB `MAX(local_id)` after the reply.
   - Observe for at least one full `poll_interval` cycle without duplicate sends or missed messages.

5. **Rollback trigger** (stop and revert if any of the following occur):
   - Duplicate WeChat send for the same inbound event.
   - Outbox status stuck in `retry` after the configured maximum send attempts.
   - Visible reply missing or content inconsistent with authorized KB.
   - `last_local_id` lags DB `MAX(local_id)` after a full poll cycle.
   - Rollback procedure:
     1. Stop runtime services (STOP_FILE → monitor exit, stop `control_api`).
     2. Set `reliable_pipeline_target=False` for the target in runtime config.
     3. Sync code if needed, preserving runtime state.
     4. Verify the target's `last_local_id` >= DB `MAX(local_id)` before restarting the monitor.
     5. Start `control_api` from runtime and verify `/status` is reachable.
     6. Remove stale STOP_FILE; start `wechat_bot_monitor.py` from runtime.
     7. Verify `/status` reports `monitor_running=true` after the monitor is up.
     8. Send a test message and verify the legacy path resumes.

6. **Stage 3 completion criterion**:
   - All intended targets have passed the canary and remain stable for the agreed observation window.
   - The user explicitly approves closing Stage 3 and entering Stage 4.
   - Until then, `_is_reliable_pipeline_target` remains the per-target gate.

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

#### Current State (2026-07-14)

> **2026-07-16 验收更新**：canary 观察窗（2026-07-14 00:00 → 2026-07-16 00:10，约 48h，`--since 1783958400`）经 `scripts/canary_audit.py` 最终审计三项全 PASS（窗口 14 events/turns/jobs、11 outbox；sent 6 全 `confirmed=true`；12 个 KB 命中 job 全在授权内；零静默丢失/零未确认重复/零未授权 KB），**用户已批准 canary 验收**，Stage 4 验收表第 2 项与 Stage 2 验收四项同步勾选。Stage 4 剩余执行项：target-scoped 回滚流程测试 → legacy `generate_reply` 删除（含测试/控制平面/文档清理）→ 全量切换与 `_is_reliable_pipeline_target` 移除。

- **Shadow 模式已实现**：新增 per-target 配置 `reliable_pipeline_shadow`（示例已同步）。当目标同时启用 `reliable_pipeline_target=true` 与 `reliable_pipeline_shadow=true` 时，整条 `ingress→turn→job→worker→provenance` 链路照常运行，但 `apply_agent_result` 在持久化层强制跳过 `send_outbox` INSERT，转而按决策类型把 `shadow_json` 写入 `turn_jobs`（reply 时记录 `shadow`/`would_send`/`reply_text`/`reply_chars`/`mention_name`/`recorded_at`；silent/escalate/filter-reject 时记录 `action`/`reason_code` 及失败原因）；provenance 仍独立持久化在 `provenance_json`。
- **Shadow 实测（`bot群聊测试`）**：
  - 发送方窗口（`微信机器人测试`）向群里发送 `公交卡充值失败未到账，怎么申请退款？`，对应托管账号消息 `local_id=968`。
  - 可靠 pipeline DB 产生 inbound event 24 → turn 24 → job 24；job 24 由 `hermes-wechat-bot-worker3` 处理，结果 `action=reply`、provenance 命中 `leann.bus`（5 hits）、`shadow_json.would_send=true`、回复文本 227 字符。
  - 对 job 24 直接查询 `send_outbox`：0 行；`send_outbox` 总表无新增（保持 15 行）。
  - 托管账号 `bot群聊测试` 窗口未出现该条消息的任何回复。
- **恢复测试（`bot群聊测试`，shadow 已关闭）**：
  - 发送第二条消息 `帮我查一下公交卡充值失败没到账怎么退款`，对应 `local_id=969`。
  - 新事件 job 25 `action=reply`、`payload.shadow=False`、`shadow_json=None`，正常产生 send_outbox 16 并尝试发送。
  - **发送阶段失败**：send_outbox 16 在 5 次尝试后进入 `dead_letter`，错误为 `cua-driver list_windows TimeoutExpired`（与 006 同类）。`shadow=false` 时 outbox 物化已验证；CUA 发送失败，端到端发送恢复未验证。
- **CUA sender remediation（2026-07-14）**：
  - 缓解措施（待继续归因）：服务进程中调用 `cua-driver list_windows` 出现 30s 命令超时（interactive plain CLI 复现 ~10.6s，worker 连续 5 次超时）。修复：`_cua_run` 增加 `stdin=subprocess.DEVNULL`；所有 `list_windows` 调用统一为 `--json` 变体。单条 E2E 恢复后无法确定 `stdin` 与 `--json` 何者为主因，待后续多次发送继续归因。
  - 验证：`tests/test_cua_sender.py` 22 条通过（含新增 `_cua_run` subprocess kwargs 断言）；`tests/test_reliable_worker.py` 59 条通过。
  - 非 shadow E2E（`bot群聊测试`）：event 26 / `local_id=970` → job 26 → outbox 17 `status=sent`、`attempts=1`、`confirmed=true`，无 dead-letter。
  - **canary 观察开始（2026-07-14）**：`bot群聊测试` 已进入约定观察窗口；判定标准为零静默丢失、零未确认重复发送、零未授权 KB 访问，窗口结束后方可勾选 canary acceptance。
- **family canary 启动与 provider failure（2026-07-14）**：
  - `family` 目标 `reliable_pipeline_target` 已启用（`response_mode=free`，专绑 `hermes-wechat-bot-worker3`）。
  - 发送方窗口（`家人们`）发送唯一测试消息 `E2E family canary test 2026-07-14 unique-12345`，对应托管账号消息 `local_id=24`。
  - 可靠 pipeline DB 产生 inbound event 27 → turn 27 → job 27；job 27 由 worker3 处理但 `status=failed`、`error=provider result failed`、`provenance.status=no_tool_call`，无 outbox。
  - **诊断能力补齐**：新增 `turn_jobs.provider_diagnostics_json` 列（migration 添加），`fail_job` 支持可选 `provider_diagnostics` 参数；`reliable_worker` 在 `ok=False`/失败 status 时写入固定 schema `{result_type, ok, status}`，禁止泄露 provider error/reply 内容。
  - 验证：`tests/test_reliable_pipeline.py` + `tests/test_reliable_worker.py` 共 79 条通过。
- **family 二次 E2E 与 test-mode gate 死信（2026-07-15）**：
  - 发送新唯一测试消息 `E2E family canary test 2026-07-15 unique-67890`，对应托管账号消息 `local_id=25`。
  - 可靠 pipeline DB 产生 inbound event 28 → turn 28 → job 28；job 28 `status=done`，产生 outbox 18。
  - **发送被 test-mode gate 拒绝**：outbox 18 在 5 次尝试后进入 `dead_letter`，错误为 `test_mode_target_rejected: target name 'family' is not 'bot群聊测试'`。根因为 `reliable_pipeline.test_target_only=true` 且当时仅允许 `bot群聊测试`。
  - **allowlist 实现（未部署）**：`reliable_worker` 新增 `_resolve_test_target_names`/`_enforce_test_target_only` 支持 `test_target_names` 配置；默认 `['bot群聊测试']`，可扩展为 `['bot群聊测试', 'family']`。测试已验证：默认仅 bot 允许、配置 allowlist 允许 family、未列 target 仍拒绝。
  - 验证：`tests/test_reliable_worker.py` 61 条通过。
  - **allowlist 部署（已完成）**：runtime 配置 `test_target_names: ['bot群聊测试', 'family']`；allowlist 代码已同步并按停机流程重启验证生效。
  - **outbox 18 受控恢复（2026-07-15，已完成）**：实现并部署了 durable dead-letter 恢复机制（详见 Stage 4 recovery 小节），经 pinned legacy recovery 恢复 outbox 18 后由 scheduler 重新发送，`status=sent`、`attempts=1`、`confirmed=true`，family 窗口消息 `local_id` 25→26。
- **Stage 4 recovery 小节：durable dead-letter 恢复机制（2026-07-15 实现/部署）**
  - **Send-intent 标记**：`send_outbox.send_started_at` 在调用 sender 前由 `record_send_started` 持久化；为发送*授权*的 durable 记录（非发送已发生证明）。不变量单向：`send_started_at IS NULL` ⇒ 从未授权发送 ⇒ 消息必未发出 ⇒ 可安全 requeue；非 NULL ⇒ 已授权（可能已发/已崩）⇒ 默认拒绝 requeue。migration 将存量行回填为 `created_at` 哨兵（非 NULL），使 safe path 默认拒绝所有 pre-tracking 行。
  - **Lease-token 所有权**：`send_outbox.lease_id` 为每次 claim 生成的唯一 token（owner 字符串是共享默认值，不能作为租约凭据）。`record_send_started`/`record_send_result` 均以 `lease_id` 为防护条件并在转移时清除；同 owner 的 stale claim 也无法 finalize/标记被 reclaim 的行（含 same-owner reclaim 回归测试）。`record_send_started` 同时将 lease 原子延长至 `reliable_pipeline.send_lease_seconds`（默认 180s，且 >= claim lease），覆盖发送+确认最坏耗时。**残余风险（已接受未闭合）**：固定窗口，若发送实际超过配置值仍可能 mid-flight 被 reclaim 重复发送；部署时应把该值调到高于真实最坏发送+确认时间。
  - **审计表**：`send_outbox_recovery_audit` append-only，trigger 拒绝 UPDATE/DELETE；记录 prior status/attempts/error/result/dead_at/send_started_at、legacy_override、verification_evidence、reason、actor、requeued_at。
  - **两条恢复路径**：`requeue_dead_letter`（safe，仅 `send_started_at IS NULL`）；`recover_legacy_gate_rejection`（一次性 legacy 例外，不上 HTTP，要求精确 error 串 + reply sha256 pin + result.skipped=true + gate error 前缀 + 人工 `verification_evidence`）。control_api `POST /reliable-pipeline/outbox/{id}/requeue` 仅暴露 safe path，`actor='control_api'`。
  - **验证**：Stage 4 recovery 相关聚焦套件（`tests/test_reliable_pipeline.py` / `test_reliable_worker.py` / `test_control_api_reliable_scheduler.py`）共 138 passed（含 same-owner stale finalize 拒绝、lease 延长、audit 不可变、legacy pin 校验）；全仓串行 784 passed。
- **canary 观察窗审计方法与首轮基线（2026-07-15）**：新增可重复审计脚本 `wechat-decrypt/scripts/canary_audit.py`（只读，只输出 id/计数/状态/KB id 及分类错误指纹 category+sha256 前缀，不回显消息、回复文本或错误原文；任一检查 FAIL 时退出码非零）。**观察窗硬边界**：`--since 1783958400`（2026-07-14 00:00:00 本地，固定值，不从 job 编号/created_at 推断）；窗口外历史行只计 `out_of_window_jobs`，不参与 PASS 判定；`--min-job-id` 仅作辅助过滤。脚本输出各链路表窗口内首/末行时间作为边界证据。三项判定口径（全部 fail-closed）：
  - 零静默丢失：窗口内 inbound_events 全部 `turned` 且 `turn_id` 非空；turns 与 turn_jobs 1:1；turn_jobs 全部处于终态集合 `done/failed/timeout/escalated`；send_outbox 全部处于终态集合 `sent/dead_letter`。非终态行按**各自调度时刻 + 宽限期**（`--grace-seconds`，默认 900s）判定 stuck：outbox `sending` 看 `lease_until`、`retry/pending` 看 `next_attempt_at`（仅 outbox 概念），job `running` 看 `min(lease_until, deadline_at)`，其余看 `created_at`；宽限内的合法 in-flight 行只计数（events/turns/jobs/outbox 四类）不判 FAIL。failed/timeout jobs 与 dead letters 属终态可审计记录，以错误指纹单列展示。
  - 零未确认重复发送：同一 job 不得有多于 1 条 `sent` outbox；所有 `sent` 行 `result_json.confirmed=true`。
  - 零未授权 KB 访问（fail-closed）：窗口内每个 job 必须可验证——provenance_json 缺失/损坏、缺 `provenance` 键、条目非 dict 或 `kb_id` 非非空字符串、事件非 dict、无 worker-valid snapshot、末个 valid snapshot（与 worker `_normalize_turn_payload` 完全同一选择规则）缺 `_allowed_kb_ids` 或含非字符串项，一律计入 `unverifiable_jobs` 并判 FAIL；可验证 job 的 provenance 命中（strip 后比较）必须是**末个 valid snapshot** 的入队 allowlist（`build_event_payload` 写入、`_close_window_tx` 原样保留、worker `_normalize_turn_payload` 提升——端到端已有测试 `test_process_once_multi_event_aggregates_text_and_uses_last_auth` 覆盖）的子集。
  - 正式命令：`python scripts/canary_audit.py --db <pipeline.sqlite> --target-id 47965620946@chatroom --target-id 56467477042@chatroom --since 1783958400`。
  - 首轮基线（runtime DB `temp/reliable_pipeline_e2e_20260712_125228.sqlite`）：**三项全部 PASS**。窗口内 11 events / 11 turns / 11 jobs / 9 outbox；sent 4 条全 confirmed；9 个 KB 命中 job 全部命中授权内 `leann.bus`；family 授权为空且 0 命中；窗口内 failed job 27（provider_failed）与 dead-letter 11-14（confirm_error/cua_timeout）均为终态有记录；in_flight 四类全 0；历史 out_of_window 17 jobs 不参与判定。
- **Stage 4 rollback 小节：target-scoped 回滚流程（2026-07-16 定义/测试）**
  - **适用场景**：新 pipeline 在某 target 上出现阻塞性回归，需退回 legacy 路径，且不影响其他 target。
  - **流程（全部使用现有机制，无手改 DB）**：
    1. **禁新 ingress（停机切换）**：停止 monitor → 原子 JSON 更新目标 target `reliable_pipeline_target=false`（临时文件 + `os.replace`）→ 启动 monitor。**禁止在 monitor 运行中直接改 live 配置**：monitor 每 cycle `load_config` 后以内存 targets 回写 `last_local_id`，运行中改 flag 会被旧内存覆盖（2026-07-16 E2E 实证竞态：恢复写入落在一个 cycle 的 load 与 save 之间，被回写为旧值）。若未来要免重启，须先实现控制面原子 toggle。启动后新消息 fall through 到该 target 的 legacy 分支——thin-monitor（`_handle_thin_monitor_target`，event_log `thin_monitor_buffered`/`thin_monitor_enqueued`）或 aggregator（`message_aggregator → flush_due → generate_reply → send_reply_detailed`），由 `_thin_monitor_enabled(cfg, t)` 判定。
    2. **Drain 在途决策**：对该 target 的每个 non-terminal `turn_jobs`（`queued`/`running`）逐条调用 `POST /reliable-pipeline/turn-jobs/{id}/quarantine` → `failed`（终态、带 reason、可审计）。quarantine 在 pipeline 全局禁用时也可用。
    3. **Outbox drain-to-send**：已决的 `send_outbox`（`pending`/`retry`）**不取消**，由 scheduler 自然发送完毕——这些是已授权回复。**禁止**用全局 `test_target_only` 收紧 allowlist 来制造 dead-letter（副作用波及所有 target 且不可逆）。**已知缺口**：紧急按 target 取消 outbox 需要新增的受控、审计化 cancel API，当前不存在。
    4. **验证**：`turn_jobs`/`send_outbox` 无 non-terminal 行；新消息走 legacy——thin-monitor 分支见 event_log `thin_monitor_buffered`/`thin_monitor_enqueued`，aggregator 分支见 `decision`/`aggregator_flush_due`/`send_ok`（按 target 的 legacy 分支而定），且 pipeline DB 无新行；其他 target 的 job/outbox 流转不受影响。
    5. **恢复（roll forward）**：同样停机切换——停止 monitor → 原子恢复 `reliable_pipeline_target=true` → 启动 monitor；新消息恢复 durable ingress。**startup-ready 判据**：启动后先确认目标 cursor 已等于当前 DB max 且 `/status` 正常，再注入流量（monitor 启动有 `_advance_startup_cursors` 推进与首轮 cycle 的瞬态窗口；2026-07-16 演练中一条在启动窗口内落 DB 的消息未被自动 ingress，带日志重启后第二条消息秒级 ingress）。恢复后跨至少两个 monitor cycle 读回断言 flag 稳定为 true（防竞态回退）。已 quarantine 的 job 不复活（终态），历史保留审计。
  - **自动化测试**：`tests/test_rollback_runbook.py` 4 条通过——quarantine 仅 drain 目标 target（其他 target queued 不受影响且下一个被 claim）、opt-out 后 pending outbox 仍正常 `sent`（drain-to-send）、回滚期间其他 target 完整 claim→outbox→sent 流转、ingress flag 开→关→开序列（opt-out 拒绝不持久化、恢复后继续持久化）。
  - **旧 E2E 演练（`bot群聊测试`，2026-07-16，历史记录——其 live 切换步骤已废止）**：以下演练按当时的热切换流程执行；**该热切换流程已被同日证实的 live JSON 竞态废止，不再有效**，保留仅为机制证据与竞态发现来源。现行流程见上方 runbook（停机切换），停机版演练见本节末尾。
    1. 原子 JSON 更新关闭 flag（临时文件 + `os.replace`）；12s 后生效，无需重启。
    2. CUA 发送方窗口（`微信机器人测试`，备注隔离）后台发送 `E2E rollback legacy test 2026-07-16 unique-alpha 飞扬的跟屁虫`（含 trigger 词）。结果：pipeline DB 计数全程 31/31/31/20 不变（durable 未接管）；event_log 出现 `thin_monitor_buffered`（legacy 接管证据）。non-terminal jobs/outbox 为 0，无需 quarantine 步骤（第 2/3 步空转）。
    3. family 隔离：演练期间 family flag 保持 `true`，最新 family job 仍为 28 无新增，cursor 26 = db max 26。
    4. 原子恢复 flag=true（恢复函数先 dry-run 验证 + 读回断言后才进入演练）。
    5. CUA 发送 `E2E rollback restore test 2026-07-16 unique-beta`：inbound 32 → turn 32 → job 32 `done` → outbox 21 `sent`/`confirmed=true`/`attempts=1`。roll-forward 全链恢复。
    6. **演练后审计**：beta 进入观察窗后重跑正式 `canary_audit.py`（runtime 目录）：三项全 PASS——窗口 15 events/turns/jobs、12 outbox；sent 7 条全 `confirmed=true`；13 个 KB 命中 job 全部授权内；in_flight 四类全 0。
  - **停机版 E2E 演练（`bot群聊测试`，2026-07-16，按现行 runbook 执行）**：
    - **Phase A（禁新 ingress）**：停 monitor → 原子 `flag=false` → 启 monitor（全程无竞态）；CUA 发送 `unique-epsilon`（local_id 989）→ event_log `thin_monitor_buffered`（legacy 接管），pipeline DB 全程 33/33/33/22 不变（durable 未接管）；family 隔离：无新增 job、flag 保持 true。non-terminal 为 0，quarantine/outbox 步骤空转。
    - **Phase B（roll forward）**：停 monitor → 原子 `flag=true` → 启 monitor；CUA 发送 `unique-zeta`（local_id 991）→ **未自动 ingress**（B3 monitor 启动瞬态，具体分支因 stdout 丢弃不可考；该消息后被一次手工 `durable_ingress_event` replay 写入 pipeline——**zeta 证据受污染，不计入验收**）。带日志重启 monitor 并按 startup-ready 判据等待后，CUA 发送 `unique-eta`（local_id 992）→ monitor **自动** durable ingress（inbound 35）→ job 35 `done` → outbox 23 `sent`/`confirmed=true`。
    - **restart 复现测试（2026-07-16，针对 zeta 跳过）**：带日志重启 monitor，startup-ready 判据确认（cursor 993 = db max 993、`/status` 正常）后发送 `unique-theta`（local_id 994）→ monitor 正常自动 ingress（event 36 `turned`、cycle 日志 `new=1`、cursor 994）；job 36 终态 `failed`（provider_failed，下游独立事件，非静默丢失）。**结论：zeta 跳过一次未复现**。
    - **B3 未解释运行异常（zeta，local_id 991）**：B3 monitor（pid 9108，stdout/stderr 被 DEVNULL 丢弃）在 991 落 DB（启动后 35.5s）后约 4 分钟内未处理，之后 cursor 推进到 991 但无 durable event、无 legacy/self/muted/admin/sender_not_allowed 任一 event_log 分支记录（已逐项排除）；`reliable_pipeline.db_path` 无漂移、默认 DB 为空、fence 与 startup cursor 竞争均与证据矛盾。**根因未定位，标记为未解释运行异常**；zeta 后被我手工 replay 污染，不可追溯。由此回滚验收条目（Acceptance「完整切换有测试过的 target-scoped 回滚流程」）保持未勾，直至该异常有解释或同类停机切换场景经更多样本证明可靠。
    - **收尾**：flag 跨两 cycle 稳定 true；cursor 994 = db max 994（theta 994 后）；canary 审计三项 PASS（窗口 19/19/19/14）；family 全程隔离。
    - **停机切换完整重跑（2026-07-16 02:00-02:05，带日志，全新消息）**：**Phase A（roll-back）**：停 → 原子 `flag=false` → 启（pid 33936，startup-ready 通过）→ `unique-kappa`（local_id 995）→ event_log `thin_monitor_buffered` + legacy job 31 `submitted`（legacy thin-monitor 接管），pipeline inbound 36 不变，cursor 996（含 996 self-ack）。**Phase B（roll-forward）**：停 → 原子 `flag=true` → 启（pid 47772，startup-ready 通过）→ `unique-lambda`（local_id 998）→ monitor **自动** durable ingress（event 37）→ job 37 `done` → outbox `sent`/`confirmed=true`。重跑后审计三项 PASS（窗口 20/20/20/15）。**重跑两相全过（单样本：一次完整切换成功）**；zeta 异常未解决、未定位，作为残余风险保留——单样本重跑通过不证明流程已稳定可靠，仅证明本次切换按 runbook 可执行成功。
    - **教训固化**：runbook 第 5 步新增 startup-ready 判据；E2E 期间 monitor 一律带日志启动；禁止手工 `durable_ingress_event` 写真实 local_id（证据污染）。
  - **配置安全教训**：E2E 临时 flag 切换必须先验证原子恢复函数可用（dry-run + 读回断言），且恢复动作置于任何失败/超时分支第一动作；kernel 崩溃丢失 helper 后须立即重建并优先恢复配置。**live JSON 竞态（2026-07-16 实证）**：运行中改 flag 会被 monitor 内存回写覆盖，切换必须停机执行（见第 1/5 步）。
  - **Stage 4 scoped-auth 小节：legacy enqueue MCP 授权缺口修复（2026-07-16 实现/部署/E2E）**
  - **缺口**：Stage 2 强制所有入队 agent job 走 strict AgentResult 契约，其 prompt 指示 Hermes 调用 `mcp__wechat_kb_search__search_knowledge`；但 legacy fast（`agent_provider_enqueued`）分支 payload 缺 `_config_path`/`_allowed_kb_ids`/trace 字段 → MCP server 看到空 allowlist 并 fail-closed `invalid`（无越权，已验证）→ legacy 回复永远无 KB 支撑且工具调用不可审计。ROUTE_DEEP 分支已有两个 scope 键。
  - **修复**：新增共享 helper `reply_engine._scoped_mcp_payload_fields(selected_kbs, config_path)`（`_allowed_kb_ids` 快照 + `_config_path` + `_knowledge_trace_id`/`_dir`；trace 目录由 MCP 写入端自建）；fast 与 deep 两 enqueue 分支统一调用；monitor 三处 `generate_reply` 调用点（thin `:198`、主循环 `:1936`、aggregator `:2033`）显式透传 `config_path`——无 default fallback，payload 写入与生成该 job 的同一配置文件。
  - **回归测试**：`tests/test_reply_engine_enqueue.py` 新增 5 条（fast/deep 两分支四字段断言、`knowledge_base_specs` id ⊆ allowlist、`_prepare_model_job` scoped env 注入端到端、快照不可变、monitor 三调用点静态断言）；`tests/test_monitor_thin_monitor.py` 扩展 config_path 传递断言；影响面 186 条通过。
  - **E2E（2026-07-16，bot opt-out 窗口）**：`unique-delta` 消息经 thin_monitor → legacy fast_reply enqueue → dispatch（submit）→ reconcile。job 29 payload `_config_path` = runtime 配置绝对路径、`_allowed_kb_ids` = `['leann.bus']`（与 target 快照一致）；trace 文件 `status=ok`、provenance `[{kb_id: leann.bus, hit, count 5}]` ⊆ 快照；job 29 终态 `sent`。修复前该链路 MCP 恒 `invalid`。
  - **部署**：停机 robocopy（6 文件）→ cursor 核对（bot 983/983、family 26/26）→ 重启验证；E2E 后 cursor 988/988、flag 跨两 cycle 稳定、canary 审计三项 PASS（窗口 16/16/16/13）。
- **测试基线**：全仓串行全量运行 **793 passed**，0 failed（2026-07-16：scoped-auth 5 + rollback runbook 4 + thin monitor 断言扩展等新增后基线；此前为 784）。

#### Boundaries
- shadow job 不创建可发送的 outbox 行。
- 回滚保持 target-scoped，直到 legacy path 被主动删除。
- 删除仅在 telemetry 证明无活跃 legacy job 且保留窗口期满后进行。

#### Legacy telemetry 判据（2026-07-16 定义，删除 legacy 的前置验证）

「无活跃 legacy job」须同时满足以下三条，在**无回滚演练活动**的观察窗内判定：

1. **判据 A（agent_jobs DB）**：`agent_jobs.list_legacy_async_jobs()` == `[]`。其语义（源码实证）：状态 ∈ {`dispatching`,`submitted`,`agent_running`}（**设计排除 `queued`**——queued 会被 strict-only provider 正确处理）且有 `external_session_id` 且 payload 无 `reliable_result_contract`；终态 legacy job 保留供审计，不阻止删除。
2. **判据 B（session 标记）**：`.wechat_agent_jobs` session root 下 `strict: false`（legacy 契约）的 incomplete session 数 **== 0**。
3. **判据 C（窗口事件白名单）**：观察窗内，**已启用 reliable pipeline 的 target** 不产生 legacy-only 事件（`thin_monitor_buffered`/`thin_monitor_enqueued`/`aggregator_buffered`/`aggregator_flush_due`/`decision`）。未启用 target（如回滚演练中的 opt-out target）产生这些事件属预期，不计入违规；回滚演练窗口本身排除在本判据观察窗之外。

#### Legacy telemetry 验证结果（2026-07-16，三判据全过）

- **判据 A**：runtime `temp/agent_jobs.sqlite` 执行 `list_legacy_async_jobs()` → `[]`。最近的 legacy 链 job 31（kappa 演练产物）已终态 `sent` 且 payload `reliable_result_contract: true`（按定义非 legacy）。
- **判据 B**：session root `<cwd>/.wechat_agent_jobs` 在 runtime 不存在 → incomplete legacy session == 0。`meta.json.strict` 字段契约已读源码确认（`agent_provider.py:1122`：`strict is False` → legacy not migrated）。
- **判据 C**：近 48h event_log 全部 kind 仅 `thin_monitor_buffered`（4 次）+ `admin_command`（1 次）；全部 6 次历史 `thin_monitor_buffered` 均落在 bot target opt-out 演练窗口（scoped-auth E2E 01:06/01:31、epsilon 01:36、kappa 02:02）内；**已启用窗口内零 legacy-only 事件**。aggregator 路径事件当前零出现（bot target 走 thin-monitor）。

**结论**：无活跃 legacy job 前置验证通过。注：判据 C 的 vocabulary 含 `aggregator_*`/`decision`（当前未出现），未来若出现需按同一规则判定。

#### Shadow 对比验收阈值（2026-07-16 预写，replay 实施前冻结）

真实 replay：同一注入消息集合驱动真实 legacy 链（thin-monitor/aggregator → agent_jobs → control_api dispatch/reconcile）与真实 strict 链（durable ingress → turn → job → outbox，sender mocked/blocked），临时 DB、假 target、真实 Hermes provider。四维度：

1. **动作一致性**：同一消息两链最终动作（reply/silent/escalate）一致率 **≥ 80%**（分母 = 双链均产出决策的样本），且全部不一致样本可归类到预登记白名单差异点（strict 链独有：knowledge gate、安全拦截、AgentResult 契约拒绝；预算拦截 N/A——机制不存在，见样本量小节）。不可归类的不一致 = FAIL。
   - **执行失败口径（2026-07-16 补充，自下次 replay 生效，不溯及 2026-07-16 首轮全量）**：单链执行失败（provider 间歇失败、timeout）单独分类为 `execution_failure`，**不计入一致率分母**；判定依据须可复现（同 payload 重跑成功或 stage 指纹——stage 指纹持久化前仅以复现为准）。两链 `execution_failure` 率分别报告；任一链 **> 20%** 时 overall 不得 PASS（执行稳定性为 cutover 前置，与决策 parity 独立）。**首轮全量（shadow_replay_20260716_023608）维持 execution-stability FAIL 原判，不按本口径重判。**
2. **来源一致性**：reply 动作样本中 KB 引用 kb_id 集合一致率 **≥ 90%**（同 corpus 同 query 检索确定性；排序差异容忍；legacy 链以 scoped-auth 修复后的 trace provenance 为准）。
3. **安全性**：strict 链 provenance ⊆ 入队快照 **100%**（零违规）；strict 链不得出现 legacy 链没有的白名单外新增风险动作。
4. **延迟**：ingress/enqueue → 终态延迟分布，strict 链 p50 / p95 不超过 legacy 链 **1.5× / 2×**（durable 持久化与契约校验开销预算）。

**样本量与分层（未达 = INCONCLUSIVE，非 PASS/FAIL）**：
- 总样本 **≥ 20 条**消息（每链同等处理）。
- KB-hit reply 子集 **≥ 10 条**（leann.bus corpus 可命中类，供维度 2 统计）。
- 动作分层覆盖：期望 reply **≥ 12**、silent **≥ 4**、escalate **≥ 2**。
- 白名单差异类别（knowledge-gate 拒绝、契约拒绝）= **条件性要求**：真实 replay 中若触发则必须正确归类并逐条记录；**不作样本量下限**（真实 provider 正常行为下契约拒绝无法稳定构造）。拒绝分支行为由受控 fixture/单元测试覆盖（`tests/test_hermes_result_contract.py`、fail-closed knowledge gate 测试），不依赖真实 replay 触发。**预算拦截类 = N/A**（2026-07-16 全仓检索证实代码库不存在预算拦截机制，仅 prompt 字符预算；未来实现预算拦截机制后才加入）。
- 任一子集未达下限 → 对应维度结论记 **INCONCLUSIVE**（需补样本重跑），不得以百分比形式判 PASS。

#### Shadow 对比首轮全量结果（2026-07-16，shadow_replay_20260716_023608）：**execution-stability FAIL**

工作台 `scripts/shadow_replay.py`（单文件、monkeypatch 全在 replay 进程内、三层发送阻断含 wechat_sender 全函数 trap、runtime-DB guard PASS 零生产写入）。20 样本 × 2 链 × 真实 Hermes（worker3/MiniMax-M3，40 次调用，32m24s）。

- **维度 1 动作一致性 FAIL**：一致率 65.0%（13/20 < 80%）；6 个不可归类不一致中 5 个为 strict 链 `provider result failed`（job 1/3/12/16/17，provenance 均 ok——检索成功但 provider 返回 failed），1 个 legacy 90s `agent_timeout` 过期（S09）；S08 触发 knowledge_gate 并正确归类（白名单条件性要求生效）。
- **维度 2 来源一致性 INCONCLUSIVE**：kb_id 集合一致率 100%（12/12），但双方 KB-hit reply 子集 7 < 下限 10（strict 失败侵蚀了子集）。
- **维度 3 安全性 FAIL**：provenance 越权 **0**（全部 ⊆ 入队快照 `leann.bus` ✓）；但 S09 strict reply vs legacy 过期构成白名单外「新增风险动作」1 起。
- **维度 4 延迟 PASS**：strict p50/p95 = 37.5s/44.5s vs legacy 56.6s/92.7s（strict 显著更快）。

**根因分类（初步）**：strict 5 次 `provider result failed` 与生产 canary 期 job 27/29/36 归为同一宽泛失败分类（`provider_failed`）——**是否同根因未证实**（stage 指纹未持久化，无法比对）。疑似 Hermes/MiniMax 间歇性失败（S01 同 payload 手动复现 `ok=True`/40.2s——**仅提示性证据**；`_strict_finalize_run` 的 stage 分类指纹（rc_nonzero/empty_stdout/contract_violation/timeout）被 `_classify_provider_failure` 固定 schema 丢弃、未持久化，无法断言 5 次失败同为间歇）。legacy 1 次 expired 为生产 90s `agent_timeout` 设计上限。

**结论**：execution-stability FAIL——strict provider 失败率 25%（5/20），parity 在执行稳定化前无法判定；不按新口径重判本轮。

**Follow-up（优先级序）**：
1. [x] provider 失败 stage 指纹持久化：`reliable_worker._classify_provider_failure` 固定 schema 增加 `stage` 枚举字段（安全，无原文泄漏），`HermesProvider.run` 的 `TimeoutExpired`/`Exception` 路径同步写入 `raw['stage']`。
   - 实现：改动 `wechat-decrypt/reliable_worker.py`（`_classify_provider_failure` 返回 `stage` 枚举；`process_once` 全部 provider run/result 失败路径写入 `provider_diagnostics`）与 `wechat-decrypt/agent_provider.py`（`run` 的 timeout/exception 返回 `raw['stage']`）。
   - 测试：`python -m pytest wechat-decrypt/tests/test_reliable_worker.py wechat-decrypt/tests/test_agent_provider_tool_agent.py -v`，结果 `95 passed`。新增用例验证 `contract_violation` 等枚举被持久化到 `turn_jobs.provider_diagnostics_json`，且不回显 raw stdout/错误原文。
   - 部署：control_api 原已停止；robocopy 同步 `reliable_worker.py` + `agent_provider.py` 到 `E:/projects/GA-projects/CD-only/wechat-decrypt` 后，从 runtime 目录启动 `python control_api.py --host 127.0.0.1 --port 18590`；`/status` 返回 `{"ok": true, "monitor_running": false, ...}`，控制平面无异常（monitor 未启动，本次改动不涉及 monitor）。
   - 索引：已刷新 codebase-memory fast index（3474 nodes / 19807 edges）。
2. 按 stage 分布修复 Hermes 间歇失败（或引入 bounded retry）。
3. 稳定后重跑全量 replay（执行失败口径生效），补 KB-hit 子集 ≥10。

#### Acceptance
- [x] shadow 模式不产生任何 WeChat 发送。（2026-07-14 `bot群聊测试` shadow E2E：job 24 `action=reply`、provenance 命中、shadow_json `would_send=true`，但 `send_outbox` 表对该 job 0 行，托管窗口无回复）
- [x] canary target 在约定观察窗口内：零静默丢失、零未确认重复发送、零未授权 KB 访问。（**2026-07-16 用户批准验收**。观察窗 2026-07-14 00:00 → 2026-07-16 00:10（约 48h，固定边界 `--since 1783958400`）。最终审计自 runtime 目录执行：窗口 14 events/turns/jobs、11 outbox；sent 6 条全 `confirmed=true`；12 个 KB 命中 job 全部在末个 valid snapshot 入队 allowlist 内；三项检查全 PASS。窗口内 failed job 27/29（provider_failed）与历史死信 11-14/16 均为已记录终态并带分类指纹，非静默丢失。前期证据：2026-07-14 `bot群聊测试` canary event 26 → job 26 → outbox 17 `sent`；2026-07-15 `family` canary event 28 → job 28 `done` → outbox 18 死信 → pinned legacy recovery → `sent`/`confirmed=true`）
- [ ] 完整切换有测试过的 target-scoped 回滚流程。（2026-07-16：停机版 runbook 已定义；机制层 `tests/test_rollback_runbook.py` 4 条通过；停机版 Phase A/B 首次演练中 Phase B 出现 zeta（local_id 991）未解释跳过（证据已污染，标为未解释运行异常）。**同日带日志重跑两相通过（单样本：lambda 998 → event 37 → job 37 done → outbox sent/confirmed，审计窗口 20/20/20/15 PASS）**。zeta 根因未定位，作为残余风险保留；单样本成功 ≠ 流程已证明可靠——勾选与否待用户决策。）
- [ ] 旧代码删除包含测试、控制平面清理、文档更新。
- [ ] **新增**：所有启用目标默认走新 pipeline；legacy `generate_reply` 路径被完全移除或隔离为只读历史参考。
- [ ] 在 legacy 路径删除后，所有目标统一走 durable pipeline，`_is_reliable_pipeline_target` gate 失去意义并被删除。

#### Cutover Condition
- 所有启用目标使用新 pipeline。
- 未完成的 legacy job 已 terminal 并保留用于审计。
- 约定观察窗口内无阻塞可靠性回归。

---

## 4. 关键执行顺序

1. [x] **P1 合回 main 前置项** — 已完成（merge commit `0d0da5e`）。全 suite collect 健康已于 2026-07-14 验证恢复（752 tests / 0 errors）；全量运行基线待串行复跑。
2. [x] **worktree → main 合并** + 同步到 `CD-only` + 服务重启 + E2E 回归 — 已完成；E2E local_id `930` 通过，quarantine job `3` 验证失败且未发件。
3. [x] **提交 scheduler 代码** — 已完成，commit `6943d30`: `Add reliable pipeline scheduler controls`。
4. [x] **Stage 2 Phase A**：consumer-side strict AgentResult 收敛 — 已提交为 commit `88ee131 Unify strict AgentResult consumers`；聚焦测试 206 条通过；相关生产模块编译通过；未部署，未运行 E2E。
5. [x] **Stage 2 Phase B**：Hermes provider default/always strict + 剩余 legacy extractor 退役 — commit `5b3e196`；严格 profile preflight、runtime sync 与 `bot群聊测试` E2E 已通过。
6. [~] **Stage 3**：`knowledge_retrieval.py` 提取、KB/回复决策到 Hermes、启用目标旧 `generate_reply` 调用移除、runtime sync 与 fail-closed knowledge gate 已完成；`bot群聊测试` 的带 provenance 知识回复 E2E 已通过。标注回归语料已补齐（`86 passed`）。**2026-07-14 补充：** dedicated-worker 路由与 scheduler active/on-duty 选择已修复并测试；006 发送阶段 CUA timeout 失败，007 完成完整 ingress→job→outbox→window 闭环。按 target 的 rollout 仍需要用户逐项批准后才能继续。
7. [ ] **Stage 4**：shadow → canary → 全量切换 → 删除 legacy。

---

## 5. 风险与注意事项

1. **Stage 2 已完成**：Phase A `88ee131` 与 Phase B `5b3e196` 均已部署；configured Hermes profile strict preflight、`bot群聊测试` E2E、cursor 检查与 active legacy async job 检查均通过。Stage 3 迁移仍须保持 target 授权、确定性安全拦截、预算与发送确认在本地。
2. **双路径风险**：当前 monitor 同时存在 legacy 路径与 reliable path。Stage 3 必须显式移除 legacy 调用点（`wechat_bot_monitor.py:1675-1707`、`1801-1803`）中已启用目标对旧 `generate_reply` 的调用，但保留 per-target gate 作为未启用目标的迁移开关；Stage 4 在全部切换后再删除该 gate。
3. **KB 工具依赖**：删除 `reply_engine.py` 前，必须先迁移 `tools/search_knowledge_mcp_server.py` 的依赖到 `knowledge_retrieval.py`。
4. **测试基线**：主仓与 worktree 的 `test_hermes_result_contract.py` 分裂问题已通过 merge commit `0d0da5e` 解决；后续全 suite 健康度需单独复测。
5. **全量测试 collect 已恢复（2026-07-14）**：`test_wechat_auto_cli_bridge.py` 的 sys.path 已修复，`--collect-only` 752 tests / 0 errors；首次全量运行 750 passed / 2 failed（`test_reliable_worker.py` 两个 factory 用例），待串行复跑确认可重复性。
6. **Scheduler 已提交**：scheduler 实现已提交为 commit `6943d30`，相关测试通过并部署到 runtime。
7. **安全边界不外移**：Stage 3 只迁移“解释、检索、决策/编写”，target 授权、确定性安全拦截、预算、发送确认仍留在本地。
8. **Stage 3 MCP runtime isolation (2026-07-13)**：
   - `wechat-bot-worker3` 已注册 `mcp__wechat_kb_search__search_knowledge`，并通过请求级 `HERMES_TOOL_CHOICE_REQUIRED=1` 触发工具调用。
   - 真实 E2E 曾因 Hermes 注入的 `PYTHONPATH`/`PYTHONHOME` 污染指定的 leann-core 解释器，使检索无法在有效预算内完成。`target_registry._leann_env` 已剥离这些 Python runtime 覆盖并禁用 user-site 注入；在 Hermes venv 中直接复测为 `9.36s`、3 hits，真实 job `15` 获取 5 hits、provenance `ok`、严格回复与一次 outbox 发送均完成。
   - 后续本地 stdio smoke 复现 MCP 工具调用在 `~30s` 超时，而同 interpreter 直接 facade 仅 `~8s`；根因锁定为 FastMCP stdio server 启动的 `leann_search_direct.py` 子进程继承 server 的 stdin 管道，在特定环境下死锁。修复：为 `target_registry.py` 中所有 `capture_output=True` 的 `subprocess.run`/`subprocess.Popen` 添加 `stdin=subprocess.DEVNULL`，并显式把 `WECHAT_MCP_CONFIG` 路径从 MCP server 经 `knowledge_retrieval.search_knowledge(..., config_path=...)` 传递到 `search_leann_kb`，避免默认取 `target_registry.CONFIG_PATH` 导致 runtime/git 路径漂移。
   - 修复后真实 MCP server stdio smoke 返回 `leann.bus` 5 hits、`provenance.status=ok`、`~8.6s` 完成；扩展回归测试（147 passed）通过；runtime 已同步，`control_api`/`monitor`/`async loop` 已恢复，`/status` 显示 `monitor_running=true`。
9. **Stage 3 E2E 复测（2026-07-13）**：CUA 从发送方账号向 `bot群聊测试` 发送测试消息 `“帮我查一下公交卡充值失败未到账怎么退款”`（local_id `949`）。`reliable_pipeline` 入站事件 `18`、turn `18`、turn_job `18` 均完成；outbox `10` 状态 `sent`、attempts `1`、reply 引用 `leann.bus《超级卡投诉处理知识库_QA-260623》案例2`。托管账号 `bot群聊测试` 窗口可见对应回复；`last_local_id` 与 DB `MAX(local_id)` 均为 `950`，cursor 未落后。

---

## 6. 验收检查表（汇总）

| 阶段 | 关键交付 | 当前状态 | 完成标准 |
|------|---------|----------|----------|
| P1.1 | 修复全量测试收集 | **已完成（2026-07-14）** | `pytest --collect-only` 752 tests / 0 errors；`test_wechat_auto_cli_bridge.py` 13/13 通过；全量运行基线待串行复跑确认 |
| P1.2 | 同步契约测试到主仓 | **已完成** | merge commit `0d0da5e` 带入权威 `test_hermes_result_contract.py` |
| P1.3 | worktree → main 合并 | **已完成** | merge commit `0d0da5e`；E2E local_id `930` 通过 |
| Stage 1 | Durable transport + 管道侧契约消费 | **已完成** | 75 条聚焦测试 + 16 条 scheduler 测试通过 + E2E 通过 |
| Stage 2 Phase A | Consumer-side strict AgentResult 收敛 | **已提交并部署** `88ee131` | 聚焦测试、生产模块编译、strict profile preflight 与 E2E 均通过 |
| Stage 2 Phase B | Hermes provider strict-default + legacy extractor 退役 | **已完成** `5b3e196` | 173 条聚焦测试、生产模块编译、strict profile preflight、runtime sync 与 E2E 通过；active legacy async job 为 `0` |
| Stage 2 整体 | 统一 Hermes runner/strict contract 产出 | **已完成；验收四项 2026-07-16 实证闭合** | Phase A + Phase B 已提交/部署；验收四项（fixture 等价/契约测试覆盖/legacy extractor 退役/run+poll strict-only）2026-07-16 复核证据齐全，三测试文件 47 条通过 |
| Stage 3 | KB/决策迁移到 Hermes | **验收 5 项中 4 项已实证闭合（provenance 持久化/失败可观测/旧路径隔离/语料）；第 5 项 `generate_reply` 删除 deferred to Stage 4；按 target 的 rollout 与 Stage 4 进入需用户明确批准** | 已启用目标（`bot群聊测试`）不走旧 `generate_reply`；目标授权 MCP 检索产生 provenance 后才能创建 outbox；覆盖标注语料；006 为发送阶段诊断失败（dedicated routing/claim 正确但 CUA send timeout），007 为完整 ingress→job→outbox→window E2E 通过；per-target gate `_is_reliable_pipeline_target` 仍保留 |
| Stage 4 | Shadow/切换/legacy 删除 | **Shadow no-send E2E 通过；canary 验收 2026-07-16 用户批准通过；target-scoped 回滚流程已定义/测试/E2E 演练通过（2026-07-16，含演练后审计三项 PASS）；剩余执行项：legacy `generate_reply` 删除（含测试/控制平面/文档清理，前置：telemetry 无活跃 legacy job 验证）、全量切换后 `_is_reliable_pipeline_target` 移除** | shadow 不创建可发送 outbox；provenance/决策全记录；canary 目标观察窗口零重复/零静默丢失（已通过）；target-scoped 回滚流程（已测试）；legacy 删除后 `_is_reliable_pipeline_target` 删除 |

---

## 8. Cursor 覆盖审计（2026-07-17 修复）

### 背景
Stage 4 回滚演练中出现一次未解释运行异常（zeta，local_id 991）：monitor cursor 推进到该 local_id，但 durable pipeline、legacy aggregator、admin、self-sent、target_muted、sender_not_allowed 任一事件分支均无记录，形成「cursor 前进而无 branch/ingress 记录」的 zeta 类事件。为在生产中提前发现此类异常，在 `wechat_bot_monitor.py` 增加 cursor 覆盖审计机制。

### 实现
- 在 `wechat_bot_monitor.py` 引入模块级全局状态 `_CURSOR_AUDIT`，以稳定 target key `db|table|username` 索引，避免 `load_config()` 重建 target dict 时丢失审计状态，也避免 `save_config()` 将临时状态序列化进 targets json。
- 状态内保存 `prev`（上次成功保存后的审计基线）与 `trace`（覆盖区间列表 `[start, end, branch]`）。区间表示保证 startup/runtime_min 等大幅跳变不分配 per-id 内存。
- 新增内部 helper：
  - `_advance_cursor(t, new_cursor, branch_name)`：推进 cursor 并记录覆盖区间。
  - `_target_key(t)`：生成稳定的 target 键 `db|table|username`，用于关联 trace 与 target。
  - `_init_cursor_baseline(t)`：在 monitor 启动并加载 targets 后，将审计基线设为当前 cursor。这样只审计本进程开始后的 cursor 移动，历史 cursor 不被误判为 gap。
  - `_audit_cursor_coverage(targets, *, consume=False)`：检查每个 target 的 `[prev+1, current]` 是否被 trace 完全覆盖。若缺失，记录 `cursor_coverage_gap target=... before=... after=... missing_count=... sample=...`；只输出 target 与 local_id 范围，不回显消息内容。缺失 local_id 采样总数上限为 50，多个 gap 共享该上限；missing_count 计算完整缺失数量。`consume=True` 时清空 trace 并滚动基线，仅在成功 `save_config` 后调用；`consume=False` 时只记录告警，不滚动基线，用于 `--no-save-state` 等场景。
- 所有 cursor 推进点统一收敛到 `_advance_cursor` 并标记 reason：
  - `startup_advance`：`_advance_startup_cursors` startup 推进到 DB max。
  - `runtime_min_sync`：主循环 runtime_min reconciliation 向前覆盖。
  - `admin_command`：`_try_handle_admin_command` 内部推进；在保存前审计。
  - `self_sent_skip`：状态 2 自发自跳过；同时新增 `_record_event('self_sent_skip', ...)` 事件，便于独立追溯。
  - `target_muted_skip` / `reliable_pipeline_sender_not_allowed` / `durable_ingress` / `precheck_boundary`：per-message 分支，期望每次只推进一个 local_id。
  - `thin_monitor` / `aggregator_buffered` / `aggregator_dropped` / `image_only_no_task` / `decision_skip` / `deferred_flush` / `aggregator_flush`：聚合/延迟 flush 分支，允许覆盖一个 local_id 范围。
- 区分 per-message 与 aggregation reason：
  - per-message reason（`durable_ingress`、`self_sent_skip`、`admin_command`、`target_muted_skip`、`reliable_pipeline_sender_not_allowed`、`precheck_boundary`）若一次推进超过 `old+1`，记录 `cursor_coverage_unexpected_jump` 告警，并且不为整个跳跃写入覆盖区间，使 `[old+1, new]` 全部被审计为缺失。这是检测 zeta 类异常的关键：任何 per-message 分支不应一次性跳过多个 local_id。
  - aggregation reason 允许范围跳跃，正常记录覆盖区间。
- `durable_ingress` 仅当主循环观测到 `_rl_result['advanced'] == True` 时标记；`failure_fence` 时 cursor 未推进，不标记。
- 当前阶段为**只记录/告警，不 fail-closed**（不抛 AssertionError、不 sys.exit），待带日志 monitor 稳定运行并验证覆盖无误后，再决定是否切换为 fail-closed。

### 验证
- 新增 `wechat-decrypt/tests/test_monitor_cursor_coverage.py`（13 条）：覆盖 helper 契约、区间合并、per-message/aggregation reason 的跳转契约、unexpected jump 全量 gap 检测、基线初始化、consume 语义、mutation 静态约束、多 gap 全局采样上限。
- 相关现有测试不受影响：
  - `tests/test_monitor_thin_monitor.py`
  - `tests/test_monitor_durable_ingress.py`
  - `tests/test_monitor_session_followup.py`
  - `tests/test_message_aggregator.py`
  - `tests/test_admin_commands.py`
  - `tests/test_reliable_pipeline.py`
  - `tests/test_reliable_worker.py`
- 聚焦套件运行结果：13 passed，0 failed（`test_monitor_cursor_coverage.py` 单文件）。全量套件 809 passed，1 warning。

### 改动文件
- `wechat-decrypt/wechat_bot_monitor.py`（新增审计 helper、所有 cursor 推进点收敛、self_sent_skip event_log）
- `wechat-decrypt/tests/test_monitor_cursor_coverage.py`（新增）
- `docs/2026-07-12-reliable-pipeline-execution-plan.md`（本节）

### 部署
由主 agent 统一执行一次 stop monitor → robocopy 改动文件 → cursor 核对 → 启动 control_api → `/status` → 启动 monitor。本次子任务不自行停机/同步/重启。带日志 monitor 至少稳定运行 2 个 cycle 无 assertion 错误后，再评估是否 fail-closed。

---

## 7. 维护说明

若本计划发生**架构层面**调整（如本地/Hermes 边界变化、阶段职责变化、迁移策略变化），需同步更新 codebase-memory ADR `Reliable WeChat Pipeline Refactor` 中的 Context/Decision/Consequences 与 Changelog，并在交付报告中声明 ADR revision。执行细节变更（任务顺序、负责人、时间线）只需更新本计划文件。

