## ADDED Requirements

### Requirement: Local KB uses a trigram FTS5 tokenizer
The system SHALL create the SQLite FTS5 index with `tokenize='trigram'` so that CJK body content is searchable without extra dependencies.

#### Scenario: Creating a local KB index
- **WHEN** `_ensure_local_kb_fts()` creates a new FTS5 index for a local KB
- **THEN** it SHALL declare `tokenize='trigram'` on the indexed columns
- **AND** existing indexes using the default tokenizer SHALL be detected as stale and rebuilt

### Requirement: Local KB indexes document body content
The system SHALL index the full text body of every `.md` file under a local KB path, not only the file path.

#### Scenario: Indexing a local KB folder
- **WHEN** `_ensure_local_kb_fts()` is called for a local KB with markdown documents
- **THEN** it SHALL create or update a SQLite FTS5 index over `rel_path` and `body`
- **AND** each document's body SHALL be stored in the `docs` table

### Requirement: Long chat queries are pre-cleaned before FTS5 search
The system SHALL strip WeChat sender prefixes, @mentions, and filler words from a raw chat message before passing it to the local KB FTS query builder, so that the remaining keywords are long enough for the trigram tokenizer.

#### Scenario: Query with sender prefix and @mentions
- **WHEN** the user's raw message contains a sender name, @mention, and filler words such as "简单介绍一下"
- **THEN** the retrieval pipeline SHALL remove those prefixes and filler words
- **AND** the resulting FTS query SHALL match body content containing the product keywords

### Requirement: Local KB search matches body content
The system SHALL return hits whose body content matches the query tokens, not only hits whose file path matches.

#### Scenario: Query matches file content
- **WHEN** a user asks a question whose keywords appear only inside a markdown document's body
- **THEN** `_retrieve_local_kb_fts()` SHALL return that document as a hit
- **AND** the hit's `content` SHALL contain the matching text

#### Scenario: Query does not match any content
- **WHEN** a query's keywords do not appear in any document body or path
- **THEN** `_retrieve_local_kb_fts()` SHALL return an empty list

### Requirement: Local KB diagnostics expose index and tokenization state
The system SHALL provide a way to inspect whether a local KB index exists, how many documents it contains, and how a query is tokenized.

#### Scenario: Diagnostic endpoint reports index health
- **WHEN** the operator calls the KB diagnose endpoint for a local KB
- **THEN** the response SHALL include index path, document count, last update time, and a sample test query result

### Requirement: Local KB can be rebuilt on demand
The system SHALL allow the operator to force a full rebuild of the local KB index.

#### Scenario: Rebuild command clears and re-indexes
- **WHEN** the operator invokes the KB rebuild command
- **THEN** the system SHALL remove the existing `.kb_index.sqlite`
- **AND** re-index all markdown files under the KB path
