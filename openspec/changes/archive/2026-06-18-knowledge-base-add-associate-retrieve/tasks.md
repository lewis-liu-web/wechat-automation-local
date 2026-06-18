## 1. Local KB content retrieval

- [x] 1.1 Add a failing test that queries body-only keywords in `tests/test_reply_engine_knowledge.py`
- [x] 1.2 Change `_ensure_local_kb_fts()` to create the FTS5 index with `tokenize='trigram'`
- [x] 1.3 Bump the local KB index schema version so existing `.kb_index.sqlite` files are auto-rebuilt
- [x] 1.4 Implement `_clean_query_for_fts()` in `reply_engine.py` to strip sender prefixes, @mentions, filler words, and punctuation before local KB search
- [x] 1.5 Add `--reindex` option to the KB rebuild CLI command to force-delete and rebuild the index
- [x] 1.6 Verify the new body-content tests pass and existing local KB tests still pass

## 2. Single-source binding

- [x] 2.1 Add validation helpers in `target_registry.py` for local path existence, online required fields, and KB enabled state
- [x] 2.2 Enforce that a target's `knowledge_bases` list contains at most one alias in `bind_wiki()` and `enable_candidate()`
- [x] 2.3 Wire validation into `add_knowledge_base()` and reject invalid configs early
- [x] 2.4 Update `manage_targets.py kb` command so `--replace` sets exactly one KB and appending a second KB raises an error
- [x] 2.5 Add tests for missing local path, missing external ID, missing credentials, duplicate alias, and multiple-KB rejection

## 3. Diagnostics

- [x] 3.1 Implement `kb-diagnose` CLI command and `/kbs/{key}/diagnose` API endpoint, reporting index path, tokenizer, document count, and a sample query result
- [x] 3.2 Add `reply_engine.disable_local_kb` global toggle and enforce per-KB `enabled` flag
- [x] 3.3 Add tests for diagnostics output and the disable-local toggle

## 4. Documentation and cleanup

- [x] 4.1 Update `wiki/README.md` with the trigram tokenizer behavior and single-source binding rule
- [x] 4.2 Update `wechat_bot_targets.example.json` comments if new fields are added
- [x] 4.3 Run the full relevant test suite (`pytest wechat-decrypt/tests/test_reply_engine_knowledge.py wechat-decrypt/tests/test_reply_engine_enqueue.py`)
- [x] 4.4 Refresh the CodeGraph index


## 5. Knowledge hits in agent job payload

 [x] 5.1 Fix raw_agent enqueue to inject real KB hits instead of an empty list
 [x] 5.2 Raise local KB per-hit content cap from 1200 to 4000 characters (configurable via `hit_max_chars`)
 [x] 5.3 Apply per-hit and total budgets in agent prompt construction (5 hits / 12000 chars)
 [x] 5.4 Add tests for raw_agent payload knowledge hits and prompt truncation behavior
 [x] 5.5 Deploy fixes to CD-only instance and verify end-to-end (job_id=34 sent hits=4)
