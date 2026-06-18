## ADDED Requirements

### Requirement: Provider reads skill fields from job payload
- **WHEN** `_build_wechat_deep_prompt(job)` is called
- **THEN** it MUST read the following fields from `job["payload"]` if present:
  - `skill_name` (str)
  - `skill_prompt` (str)
  - `knowledge_hits` (list of dicts)

### Requirement: Skill prompt is injected at the top of agent prompt
- **WHEN** `skill_prompt` is non-empty
- **THEN** `_build_wechat_deep_prompt()` MUST place the skill text before the existing "wechat-deep-reply" task description
- **AND** the combined prompt MUST still contain all existing hard rules (no process log, no Markdown plan, no JSON wrapper, max 600 chars, etc.)

### Requirement: Knowledge hits are formatted as structured context
- **WHEN** `knowledge_hits` is present and non-empty
- **THEN** the provider MUST insert a "[知识库片段]" section after the skill prompt and before the user request
- **AND** each hit MUST include `source`, `kb_id`, `rel_path`, and truncated `content`

### Requirement: Backward compatibility when skill fields are absent
- **WHEN** a job payload does not contain `skill_name` or `skill_prompt`
- **THEN** `_build_wechat_deep_prompt()` MUST fall back to the existing prompt format without raising an error
