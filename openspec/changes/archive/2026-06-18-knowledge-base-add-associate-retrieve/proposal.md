## Why

The current knowledge-base (KB) workflow is unreliable: users report that local KB search can only match document titles, not file contents, which defeats the purpose of keeping documents locally. At the same time, associating a KB with a WeChat target and adding new KBs is only partially exposed through the control API / CLI, and a target can end up bound to multiple KBs of different sources (local + online), which makes retrieval behavior hard to reason about. We need a focused branch to fix local content retrieval and enforce a simpler "one source per target" binding model.

## What Changes

- Fix local KB content indexing by switching the FTS5 index from the default tokenizer to `tokenize='trigram'` so that queries match document bodies, not just `rel_path`.
- Add retrieval diagnostics (index state, query tokenization, hit provenance) so operators can verify that body content is being searched.
- Enforce a single KB source per WeChat target: a target may be bound to one local KB or one online KB, but not both, eliminating cross-source fallback chains.
- Improve the KB association flow (binding a KB to a target) and the KB creation flow so that type, credentials, and scope are validated at add-time.
- Update tests and docs to cover the new retrieval contract and single-source binding UX.

## Capabilities

### New Capabilities

- `kb-local-content-retrieval`: Make local KB search index and query document body content reliably using a trigram tokenizer, with tokenization diagnostics.
- `kb-single-source-binding`: Enforce that each WeChat target is bound to exactly one knowledge-base source (local or online), and validate KB configuration at creation and binding time through the control API and CLI.
### Modified Capabilities

_None planned. If the retrieval pipeline introduces new `KnowledgeHit` fields, a delta spec for `knowledge-context-in-job` will be added during implementation._

## Impact

- `wechat-decrypt/reply_engine.py`: local KB tokenizer change and diagnostics.
- `wechat-decrypt/control_api.py` and `wechat-decrypt/manage_targets.py`: KB CRUD and target binding endpoints / commands.
- `wechat-decrypt/target_registry.py`: validation of KB references and single-source constraint at target enable/bind time.
- `wechat-decrypt/tests/test_reply_engine_knowledge.py` and related tests: new retrieval and single-source binding test cases.
- `wechat-control-ui/app.py`: optional UI updates for KB binding and diagnostics visibility.
