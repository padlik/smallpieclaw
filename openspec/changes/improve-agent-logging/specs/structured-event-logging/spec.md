## ADDED Requirements

### Requirement: Structured JSONL event sink

The application MUST write a machine-readable structured log to `agent.jsonl` alongside the human prose `agent.log`, with one JSON object per line. Both sinks are written from the same log record so their content never drifts.

Feature: Structured event logging
Rule: `agent.jsonl` is the primary machine surface; `agent.log` is the retained secondary human surface. Every record emitted to one is emitted to the other.

#### Scenario: A log record is written to both sinks
- **GIVEN** the logging system is configured
- **WHEN** any component emits a log record
- **THEN** a prose line is appended to `agent.log`
- **AND** a single JSON object encoding the same record is appended to `agent.jsonl`
- **AND** the JSON object contains at least `ts`, `level`, `logger`, and `msg` fields

#### Scenario: JSONL line is independently parseable
- **GIVEN** a record has been written to `agent.jsonl`
- **WHEN** a reader parses any single line as JSON
- **THEN** parsing succeeds without depending on any other line

### Requirement: Structured run identity on every record

Run identity — the trace id (`r-<hex>`), the agent label, and the source tag — MUST be present as structured fields on the log record, not only as text inside the message. Both formatters render identity from those fields.

Rule: Identity is ambient logging context (observability), sourced from context-local state; correctness-critical trace propagation remains explicit and unchanged.

#### Scenario: Identity appears as JSON fields
- **GIVEN** a run with trace id `r-9f3c` executing under agent label `sa-1a2b`
- **WHEN** that run emits a log record
- **THEN** the `agent.jsonl` object includes `trace = "r-9f3c"` and `agent = "sa-1a2b"` as fields
- **AND** the `agent.log` line still renders the human prefix `[sa-1a2b r-9f3c]`

#### Scenario: Identity present without a manually threaded prefix
- **GIVEN** a call site that does not pass an explicit log prefix argument
- **WHEN** it emits a record during an active run
- **THEN** the record still carries the current `trace` and `agent` fields

#### Scenario: Missing run context degrades gracefully
- **GIVEN** a log record emitted outside any run context (e.g. during startup)
- **WHEN** the record is written
- **THEN** the identity fields are absent or empty rather than causing an error

### Requirement: Closed event taxonomy at hot call sites

The application MUST provide a closed, enumerable set of event types and emit them with structured fields at the tool lifecycle, LLM lifecycle, ReAct step/run boundaries, and error paths.

Rule: The event vocabulary is a fixed, discoverable contract so the agent can query by event type rather than by prose substring.

#### Scenario: Tool failure emits a structured event
- **GIVEN** a tool invocation that exits non-zero
- **WHEN** the failure is logged
- **THEN** the record carries `event = "TOOL_FAILED"` with structured `tool`, `exit`, and `dur_ms` fields

#### Scenario: Event vocabulary is closed and discoverable
- **GIVEN** the set of defined event types
- **WHEN** a component emits an event
- **THEN** the event value is one of the enumerated `LogEvent` members
- **AND** the full set of valid event values can be enumerated programmatically

#### Scenario: Non-hot call sites still log
- **GIVEN** a call site outside the hot set that emits a plain message
- **WHEN** the record is written
- **THEN** it appears in both sinks with identity fields
- **AND** it simply has no `event` field

### Requirement: Secret redaction in logs

Known secret values MUST be redacted from both the prose message and the structured fields before either sink serializes the record.

Rule: Redaction sources known values from the agent-scoped vault and runs at the shared filter layer so both sinks are covered uniformly.

#### Scenario: A vault secret value is scrubbed from structured fields
- **GIVEN** a vault entry whose value is `S3CR3T`
- **AND** a log record whose `err` field contains `S3CR3T`
- **WHEN** the record is written
- **THEN** the `agent.jsonl` `err` field does not contain `S3CR3T`
- **AND** the `agent.log` line does not contain `S3CR3T`
