## ADDED Requirements

### Requirement: Skill definition ships with project
- **WHEN** the project is installed or checked out
- **THEN** `wechat-decrypt/skills/wechat_task/skill.md` exists and is versioned with the repository

### Requirement: Skill declares available tools for WeChat task handling
- **WHEN** an agent worker handles a WeChat reply job
- **THEN** the skill system prompt lists the following conceptual tools:
  - `retrieve_knowledge(query, kb_ids)` — search configured knowledge bases if needed
  - `assess_complexity(query, context, knowledge_hits)` — decide if deeper reasoning is required
  - `generate_reply(query, knowledge_hits, context, tone)` — produce a WeChat-friendly Chinese reply
  - `refuse_or_escalate(reason)` — when the query involves high-risk actions or missing context

### Requirement: Skill constrains agent behavior
- **WHEN** the agent generates a reply
- **THEN** it MUST:
  - answer only based on provided `knowledge_hits` and `context_messages`
  - not impersonate the group owner or make promises/authorizations/quotes/decisions
  - not leak keys, paths, decrypted DB details, or internal implementation
  - keep replies under 300 Chinese characters
  - prefix with `@mention_name ` when a mention is provided

### Requirement: Skill provides deterministic output format
- **WHEN** the agent finishes reasoning
- **THEN** it MUST output exactly one JSON object with fields:
  - `should_reply` (bool)
  - `reply_text` (str, may be empty)
  - `intent` (str, e.g. "wiki_qa", "deep_analysis", "smalltalk", "escalate")
  - `risk_level` (one of "low", "medium", "high")
  - `need_human` (bool)
  - `reason` (str)
