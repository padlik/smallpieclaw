## ADDED Requirements

### Requirement: Shell Guard records detailed metadata events
Shell Guard SHALL write detailed decision metadata to append-only JSONL events for classify and active evaluations.

#### Scenario: Metadata event captures decision context
- **GIVEN** Shell Guard evaluates a shell command in classify or active mode
- **WHEN** the evaluation completes
- **THEN** a JSONL metadata event MUST be written
- **AND** the event MUST include schema version, event id, timestamp, trace, mode, raw command, normalized command where available, parse status, parsed units, semantic flags, matched rules, LLM fields when used, effective decision, and decision source
- **AND** when LLM classification is attempted or skipped, the event MUST record whether LLM was used, any LLM error or timeout, and classifier duration when available

#### Scenario: Classify event records decision if active
- **GIVEN** Shell Guard evaluates a command in classify mode
- **WHEN** the metadata event is written
- **THEN** it MUST record what active mode would have decided
- **AND** it MUST indicate that Shell Guard did not enforce the decision

#### Scenario: Large artifacts are referenced rather than embedded
- **GIVEN** Shell Guard captures local referenced-script content or other bulky classification evidence
- **WHEN** the metadata event is written
- **THEN** the event MUST reference the artifact by path or id and hash
- **AND** the full bulky content MUST NOT be embedded inline in every JSONL event

### Requirement: Shell Guard redacts known secrets in metadata and artifacts
Shell Guard SHALL apply known-secret redaction recursively before writing metadata or artifact summaries.

#### Scenario: Nested metadata strings are redacted
- **GIVEN** a vault secret value appears inside a parsed argv element or nested metadata field
- **WHEN** Shell Guard writes the metadata event
- **THEN** the secret value MUST be replaced with the configured redaction placeholder
- **AND** redaction MUST apply recursively to strings in nested objects and arrays

#### Scenario: Artifact summaries are redacted
- **GIVEN** a local referenced-script summary or artifact metadata contains a known vault secret value
- **WHEN** Shell Guard stores the summary or metadata
- **THEN** the known secret value MUST be redacted before storage

### Requirement: Normal logs reference Shell Guard metadata
Shell Guard SHALL write compact summaries to the normal application logs and reference detailed metadata events.

#### Scenario: Normal log contains metadata reference
- **GIVEN** Shell Guard writes a detailed metadata event
- **WHEN** the normal log summary is emitted
- **THEN** the summary MUST include basic mode, decision, risk, and trace information
- **AND** it MUST include a metadata event id or location reference sufficient for debugging

## MODIFIED Requirements

## REMOVED Requirements
