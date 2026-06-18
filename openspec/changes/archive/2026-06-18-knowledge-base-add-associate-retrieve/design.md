## Context

The reply engine already has a local knowledge-base path (`_retrieve_local_kb` and `_retrieve_local_kb_fts` in `reply_engine.py`) that builds a SQLite FTS5 index over `rel_path` and `body`. Operators report that local retrieval only matches document titles (`rel_path`) and not file contents. Reproduction confirms this is an FTS5 CJK tokenization issue: the index is created with the default tokenizer, which does not break Chinese text into searchable tokens. A quick test shows that recreating the index with `tokenize='trigram'` makes body-content queries return results.

Online KB retrieval exists for `getnote` and `ima` types. Targets are bound to KBs through `control_api.py` and `manage_targets.py`, but there is little validation at add/bind time, and a target can currently be bound to both a local KB and an online KB. This creates ambiguous retrieval behavior and complicates debugging.

This branch will fix local content retrieval with a minimal tokenizer change and simplify the binding model to one KB per target.

## Goals / Non-Goals

**Goals:**
- Fix local KB search so it reliably matches document body content using SQLite FTS5's built-in trigram tokenizer.
- Enforce a single KB source per WeChat target (local or online), eliminating cross-source fallback chains.
- Improve KB creation and target-binding UX with upfront validation and clear error messages.
- Add retrieval diagnostics (index state, query tokenization, hit provenance) so operators can verify behavior.

**Non-Goals:**
- Rewriting the general reply-engine pipeline or agent prompt formatting.
- Adding new online KB providers beyond `getnote` and `ima`.
- Adding automatic fallback from local to online KBs within a single query.
- Changing the `KnowledgeHit` schema unless required by diagnostics.
- Removing the legacy `wiki_dir` / `group_wiki_dirs` migration path.

## Decisions

### 1. Fix local KB with FTS5 trigram tokenizer
- **Rationale**: The reproduction test shows `tokenize='trigram'` resolves the body-match failure for Chinese text without adding any new dependency. Trigram is built into SQLite FTS5 and works for CJK as well as alphanumeric tokens.
- **Alternative considered**: `unicode61` and `jieba` pre-tokenization. `unicode61` still failed in the reproduction test. `jieba` would add a Python dependency and require a tokenized column. Trigram is the cheapest correct fix.
- **Alternative considered**: Integrate `zvec`. Rejected for this change: the project already uses SQLite FTS5, Python version compatibility (>=3.10) is not the issue, and the only missing feature is body-content keyword matching, which trigram solves. zvec is worth revisiting only if future requirements include vector/semantic or hybrid search.

### 2. Allow per-target source selection, not a fallback chain
- **Rationale**: A target should have one clearly defined knowledge source. If a group needs online retrieval, bind it to an online KB; if it needs local retrieval, bind it to a local KB. This removes latency variance and makes failures easier to attribute.
- **Default**: if a target has no KBs, it still falls back to the global `wiki/core` boundaries as before.

### 3. Validate KB configuration at creation/update time, not at retrieval time
- **Rationale**: Failing early in `add_knowledge_base` and `bind_wiki` prevents silent runtime misses.
- **Validation rules**:
  - `local`: `path` exists and is readable; `.kb_index.sqlite` can be created.
  - `getnote` / `ima`: `knowledge_base_id` is present; required environment variables are set; executable or network path is reachable.

### 4. Add a `/kbs/{key}/diagnose` control API endpoint and `kb-diagnose` CLI command
- **Rationale**: Operators need a way to see why a query returns no hits (index exists? tokenizer correct? rows returned?).
- **Output**: index path, document count, tokenizer name, last indexed mtime, sample tokens for a query, and a test query result.

## Risks / Trade-offs

- [FTS5 trigram index size] → Trigram indexes are larger than the default tokenizer's. Mitigation: local KBs are typically small (scenes with dozens to hundreds of markdown files); monitor `.kb_index.sqlite` size after deploy.
- [Existing `.kb_index.sqlite` files use the old tokenizer] → Mitigation: bump the index schema version in the code so the existing file is detected as stale and rebuilt automatically on first query.
- [Index corruption / stale `.kb_index.sqlite`] → Mitigation: add `reindex` command and automatic rebuild when schema version changes.
- [Single-source constraint breaks existing targets with both local and online KBs] → Mitigation: treat this as a validation warning at bind/enable time; existing configs continue to work but operators are warned and guided to pick one.
- [Backward compatibility of KB config] → Mitigation: new fields are optional; existing `type`, `path`, `knowledge_base_id` semantics unchanged.

## Migration Plan

1. Ship the tokenizer fix behind the existing config format; no config migration required.
2. Existing `.kb_index.sqlite` files will be auto-rebuilt because the schema version changes.
3. After deploy, run `python manage_targets.py kb-diagnose <kb_id>` on each local KB to verify body-content hits.
4. For targets currently bound to both local and online KBs, the UI/CLI will warn and guide the operator to pick one source.
5. Rollback: revert the commit; no schema changes affect persisted data.

## Open Questions

- Should the trigram index also index `rel_path`, or only `body`? Indexing both is simplest and keeps path-based queries working.
- Should the diagnostics endpoint be exposed in the Streamlit UI immediately or kept CLI/API-only for this branch?
