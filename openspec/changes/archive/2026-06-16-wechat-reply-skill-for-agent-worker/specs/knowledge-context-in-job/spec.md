## ADDED Requirements

### Requirement: Job payload accepts knowledge context fields
- **WHEN** `agent_jobs.enqueue_job()` is called for a WeChat reply task
- **THEN** the `payload` dictionary MUST accept and preserve the following optional fields:
  - `knowledge_hits` (list of KnowledgeHit dicts)
  - `knowledge_bases` (list of configured KB ids)
  - `reply_mode` (str)
  - `retrieval_debug` (dict)

### Requirement: Worker passes knowledge context to provider prompt
- **WHEN** an agent worker dequeues a job whose payload contains `knowledge_hits`
- **THEN** the worker MUST include those hits in the prompt sent to the agent provider
- **AND** the prompt MUST instruct the agent to prefer the provided knowledge hits over its own prior knowledge

### Requirement: Backward compatibility
- **WHEN** an older job without `knowledge_hits` is dequeued
- **THEN** the worker/provider MUST continue to work as before, without requiring the field

### Requirement: Knowledge hits survive SQLite serialization
- **WHEN** a job with `knowledge_hits` is stored in and read from the SQLite queue
- **THEN** the list of hits MUST remain intact as JSON

### Requirement: Knowledge hits render with source and optional relative path
- **WHEN** `_build_wechat_deep_prompt()` renders `knowledge_hits` into the agent prompt
- **THEN** it MUST produce a section titled `[知识库片段]`
- **AND** each hit MUST be formatted as `### 片段 i (来源: <source>)`
- **AND** if `rel_path` is non-empty, the origin MUST append ` <rel_path>` to `<source>`
- **AND** if `rel_path` is empty, the origin MUST contain only `<source>` with no trailing whitespace
