# Reliable WeChat Pipeline Refactor

## Decision

The WeChat side owns deterministic transport, authorization, safety boundaries, and real-world send confirmation. Hermes owns task interpretation and reply generation. This plan keeps those boundaries explicit and prevents a later return to monitor-side business orchestration.

The implementation is deliberately staged. Only Stage 1 is in scope for the accompanying code change. Stages 2 through 4 are recorded here as required follow-on work and must not be treated as complete by Stage 1 tests.

## Invariants

- A monitor cursor advances only after the source event is durable in the local inbox.
- Every accepted source event has one durable `source_event_id`; duplicate reads are harmless.
- A turn, worker result, and outbound send are individually durable and recoverable after process restart.
- A reply is sent at most once after database confirmation, or it reaches an auditable terminal failure/dead-letter state.
- Hermes stdout is never treated as a reply unless it is a valid versioned `AgentResult` document and the process exited successfully.
- Local deterministic safety filtering, target authorization, budgets, and actual send confirmation remain outside Hermes.
- The current knowledge-base retrieval and reply-decision semantics remain unchanged during Stage 1.

## Stage 1: Durable Transport Foundation

### Goal

Make inbound capture, turn assembly, worker result handling, and outbound sending recoverable without moving knowledge-base retrieval or reply-decision semantics.

### Scope

- SQLite inbox with unique `source_event_id`.
- Durable debounce windows and turn assembly, including restart recovery.
- Durable turn job lifecycle with leases, per-group serial claims, deadlines, retry metadata, and terminal failure states.
- Durable outbox with atomic claims, exponential retry, database-confirmed send completion, and auditable dead letters.
- Strict versioned `AgentResult` contract with exactly `reply`, `silent`, and `escalate` actions.
- One Hermes async runner protocol for submitted work. A non-zero exit, timeout, malformed result, box/ANSI output, log text, or arbitrary JSON-looking stdout is a failure and is never sent.
- Compatibility adapters around existing listener, provider, and sender entry points.

### Explicit Non-goals

- No migration of KB retrieval policy, ranking, tool permissions, or prompt strategy.
- No expansion of Hermes filesystem, database, or MCP permissions.
- No replacement of existing target/admin/mute policy.
- No shadow-send or production cutover beyond the restricted `bot群聊测试` E2E validation.

### Acceptance

- Recovery tests cover termination after inbound persistence, after job creation before worker claim, after worker completion before sending, and after a send attempt before confirmation.
- A source event ends in exactly one confirmed send, explicit silent/escalated handling, or an auditable failed/dead-letter record.
- Sender retries use bounded exponential backoff and never attempt a terminal/dead-letter outbox row.
- Strict result parsing rejects non-zero exits, timeouts, malformed JSON, Hermes boxes, ANSI/log output, and JSON text not matching the contract.

### Cutover Condition

Enable durable pipeline processing only for explicitly configured targets after all recovery tests pass. Initial real E2E is limited to target name `bot群聊测试`; the sender refuses test-mode delivery to any other target.

## Stage 2: Unified Hermes Worker Protocol

### Goal

Eliminate divergent synchronous/asynchronous Hermes execution and replace display-text parsing with a single structured worker protocol.

### Scope

- Make synchronous and asynchronous worker invocations use the same runner and result file format.
- Move any remaining Python-managed tool loop behind the Hermes runner or retire it in favor of Hermes-native controlled MCP calls.
- Replace legacy stdout/box/line-JSON extraction with contract-only parsing.
- Implement or remove recovery endpoints based on a real provider capability contract.
- Retire duplicated result sanitizers once the durable contract is authoritative.

### Boundaries

- Hermes may decide task flow and use only target-authorized tools.
- Local code retains process exit validation, result schema validation, target authorization, safety filtering, and send ownership.
- No direct arbitrary filesystem or database capability is added to Hermes.

### Acceptance

- Identical fixture jobs produce equivalent `AgentResult` objects in foreground and background paths.
- Tool invocation, tool failure, timeout, non-zero exit, malformed result, `silent`, and `escalate` have deterministic contract tests.
- No production path calls `_extract_hermes_reply`, `_extract_tool_call`, or treats terminal rendering as application data.

### Cutover Condition

All active worker instances support the structured runner. Existing jobs either complete through the legacy reconciler or are explicitly migrated with an audited compatibility reader; no mixed protocol job is silently retried.

## Stage 3: Knowledge and Reply-Decision Migration

### Goal

Move task interpretation, retrieval decisions, and reply composition to Hermes while preserving local authorization and safety boundaries.

### Scope

- Expose one target-authorized `search_knowledge` facade rather than provider-specific FTS, LEANN, hook, IMA, or GetNote behavior in prompts.
- Pass Hermes durable turn references and permitted KB/tool identifiers instead of preassembled KB snippets.
- Let Hermes select `reply`, `silent`, or `escalate` and retrieve content on demand.
- Retire monitor-side complexity routing and duplicated reply-decision rules after parity validation.

### Boundaries

- The local facade validates target-to-KB authorization, applies resource budgets, returns structured provenance, and distinguishes no-hit from provider failure.
- Hermes cannot choose KBs outside the target allowlist.
- Local deterministic safety block/handoff rules remain mandatory and are consolidated into one policy source.

### Acceptance

- A labeled regression corpus covers lexical matches, semantic-only matches, cross-KB isolation, no-hit, provider outage, images, multiple-message turns, silent chat, and escalation.
- Every Hermes reply records source provenance or an explicit no-source reason.
- Retrieval failure is observable and distinct from empty retrieval.
- Legacy local retrieval and reply paths are unused for migrated targets.

### Cutover Condition

Shadow comparison meets agreed reply-action, source-selection, safety, and latency thresholds for each target class. A target is migrated only after its KB permissions and corpus cases pass.

## Stage 4: Shadow Operation, Gradual Cutover, and Legacy Removal

### Goal

Prove the new pipeline under real traffic without duplicate sends, then remove obsolete orchestration.

### Scope

- Shadow execution records decisions and sources without sending.
- Per-target feature flag enables new pipeline delivery after parity review.
- Operational dashboards expose inbound age, turn age, worker state, outbox attempts, confirmation latency, failures, and dead letters.
- Remove legacy in-memory aggregation, monitor-side prompt assembly, Python tool loops, duplicate route/safety lists, and unused control endpoints after all targets migrate.

### Boundaries

- Shadow jobs never create sendable outbox rows.
- Rollback remains target-scoped until the legacy path is deliberately removed.
- Deletion happens only after telemetry demonstrates no active legacy jobs and the retention window expires.

### Acceptance

- Shadow mode produces no WeChat sends.
- Canary targets sustain the agreed observation window with zero silent loss, zero unconfirmed duplicate sends, and no unauthorized KB access.
- Full cutover has a tested target-scoped rollback procedure.
- Legacy code removal includes tests, control-plane cleanup, and documentation updates.

### Cutover Condition

All enabled targets use the new pipeline, outstanding legacy jobs are terminal and retained for audit, and the defined observation window has no blocking reliability regression.
