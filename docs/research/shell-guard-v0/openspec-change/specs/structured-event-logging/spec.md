## MODIFIED Requirements

### Requirement: Closed event taxonomy at hot call sites

The application MUST provide a closed, enumerable set of event types and emit them with structured fields at the tool lifecycle, LLM lifecycle, ReAct step/run boundaries, Shell Guard decision boundaries, and error paths.

Rule: The event vocabulary is a fixed, discoverable contract so the agent can query by event type rather than by prose substring.

#### Scenario: Tool failure emits a structured event
- **GIVEN** a tool invocation that exits non-zero
- **WHEN** the failure is logged
- **THEN** the record carries `event_type = "TOOL_FAILED"` with structured `tool`, `exit`, and `dur_ms` fields

#### Scenario: Shell Guard decision emits a structured event summary
- **GIVEN** Shell Guard evaluates a shell command in classify or active mode
- **WHEN** the normal structured log summary is emitted
- **THEN** the record MUST carry a Shell Guard event type from the closed event taxonomy
- **AND** the record MUST include basic mode, decision, risk, and metadata reference fields

#### Scenario: Event vocabulary is closed and discoverable
- **GIVEN** the set of defined event types
- **WHEN** a component emits an event
- **THEN** the event value is one of the enumerated `LogEvent` members
- **AND** the full set of valid event values can be enumerated programmatically

#### Scenario: Non-hot call sites still log
- **GIVEN** a call site outside the hot set that emits a plain message
- **WHEN** the record is written
- **THEN** it appears in both sinks with identity fields
- **AND** it simply has no `event_type` field

### Requirement: Secret redaction in logs

Known secret values MUST be redacted from both the prose message and the structured fields before either normal sink serializes the record. Shell Guard metadata and artifact summaries MUST use the same known-secret source and equivalent recursive string redaction before their separate sinks serialize records.

Rule: Redaction sources known values from the agent-scoped vault and runs at the shared filter or helper layer so normal sinks and Shell Guard sinks are covered uniformly.

#### Scenario: A vault secret value is scrubbed from structured fields
- **GIVEN** a vault entry whose value is `S3CR3T`
- **AND** a log record whose `err` field contains `S3CR3T`
- **WHEN** the record is written
- **THEN** the `agent.jsonl` `err` field does not contain `S3CR3T`
- **AND** the `agent.log` line does not contain `S3CR3T`

#### Scenario: A vault secret value is scrubbed from Shell Guard nested metadata
- **GIVEN** a vault entry whose value is `S3CR3T`
- **AND** a Shell Guard metadata event contains `S3CR3T` inside nested parsed command data
- **WHEN** the Shell Guard metadata event is written
- **THEN** the nested value MUST be redacted
- **AND** the detailed Shell Guard JSONL line MUST NOT contain `S3CR3T`

## ADDED Requirements

## REMOVED Requirements
