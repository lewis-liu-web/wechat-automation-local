## ADDED Requirements

### Requirement: Each target is bound to at most one knowledge base
The system SHALL enforce that a WeChat target's `knowledge_bases` list contains zero or one KB alias.

#### Scenario: Binding a single KB
- **WHEN** an operator binds one KB alias to a target
- **THEN** the target's `knowledge_bases` SHALL contain exactly that alias

#### Scenario: Rejecting multiple KBs
- **WHEN** an operator attempts to bind two or more KB aliases to the same target
- **THEN** the operation SHALL fail with an error explaining that only one KB source is allowed per target
- **AND** the target SHALL remain unchanged

### Requirement: Target binding validates that the KB exists and is enabled
The system SHALL reject binding a target to a KB alias that does not exist or is disabled.

#### Scenario: Binding to unknown KB
- **WHEN** `bind_wiki()` or `enable_candidate()` is called with a `knowledge_bases` list containing an unknown alias
- **THEN** the operation SHALL raise a clear error naming the invalid alias
- **AND** the target SHALL NOT be updated

#### Scenario: Binding to disabled KB
- **WHEN** a target is bound to a KB whose `enabled` field is `false`
- **THEN** the operation SHALL reject with an error explaining that the KB is disabled

### Requirement: Target binding supports replace semantics
The system SHALL allow replacing the KB bound to a target, including clearing it.

#### Scenario: Replace target KB via API
- **WHEN** the control API receives `POST /targets/{key}/kbs/replace` with a `knowledge_bases` array of length 0 or 1
- **THEN** the target's `knowledge_bases` SHALL be set exactly to that array

#### Scenario: Replace target KB via CLI
- **WHEN** the operator runs `python manage_targets.py kb "<target>" <alias> --replace`
- **THEN** the target SHALL use only the listed alias

### Requirement: KB creation validates type-specific required fields
The system SHALL reject a KB configuration that is missing required fields for its type.

#### Scenario: Local KB with missing or invalid path
- **WHEN** `add_knowledge_base()` is called with `kb_type="local"` and a `path` that does not exist or is not a directory
- **THEN** the operation SHALL raise a validation error
- **AND** the KB SHALL NOT be added to the config

#### Scenario: Online KB missing external ID
- **WHEN** `add_knowledge_base()` is called with `kb_type="getnote"` or `kb_type="ima"` and no `knowledge_base_id`
- **THEN** the operation SHALL raise a validation error

#### Scenario: Online KB missing credentials
- **WHEN** an `ima` KB is created with `client_id_env` and `api_key_env` that are not present in the environment
- **THEN** the operation SHALL raise a validation error
- **AND** a `getnote` KB created with a non-existent `executable` SHALL raise a validation error unless a default executable is configured

### Requirement: KB alias uniqueness
The system SHALL reject creating a KB with an alias that already exists unless `replace=True`.

#### Scenario: Duplicate alias
- **WHEN** `add_knowledge_base()` is called with an existing `kb_id` and `replace=False`
- **THEN** the operation SHALL raise a validation error

#### Scenario: Replace existing KB
- **WHEN** `add_knowledge_base()` is called with an existing `kb_id` and `replace=True`
- **THEN** the existing KB SHALL be overwritten with the new spec
