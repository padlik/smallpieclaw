# Structured Event Logging Specification

## Purpose

Define the structured-primary dual-sink logging behaviour: a machine-readable JSONL event stream (`agent.jsonl`, primary) written alongside the human prose log (`agent.log`, secondary) from one processor chain, carrying structured run identity, a closed event taxonomy, and secret redaction.

## Requirements

### Requirement: Structured JSONL event sink

The application MUST write a machine-readable structured log to `agent.jsonl` alongside the human prose `agent.log`, with one JSON object per line. Both sinks are written from the same log record so their content never drifts.

Feature: Structured event logging
Rule: `agent.jsonl` is the primary machine surface; `agent.log` is the retained secondary human surface. Every record emitted to one is emitted to the other — with the exception of components under component log isolation (see the component log isolation requirement), whose records route to their dedicated component log instead.

#### Scenario: A log record is written to both sinks
- **GIVEN** the logging system is configured
- **WHEN** any component not subject to component log isolation emits a log record
- **THEN** a prose line is appended to `agent.log`
- **AND** a single JSON object encoding the same record is appended to `agent.jsonl`
- **AND** the JSON object contains at least `ts`, `level`, `logger`, and `msg` fields

#### Scenario: JSONL line is independently parseable
- **GIVEN** a record has been written to `agent.jsonl`
- **WHEN** a reader parses any single line as JSON
- **THEN** parsing succeeds without depending on any other line

### Requirement: Structured run identity on every record

Run identity — the trace id (`r-<hex>`), the agent label, the source tag, and the prompt id — MUST be present as structured fields on the log record, not only as text inside the message. Both formatters render identity from those fields.

Rule: Identity is ambient logging context (observability), sourced from context-local state; correctness-critical trace propagation remains explicit and unchanged. The prompt id is bound into the structlog context at `run()` start via `bind_run_context()`, alongside the existing trace/agent/run-label, and inherited by sub-agents via the existing context propagation.

#### Scenario: Identity appears as JSON fields
- **GIVEN** a run with trace id `r-9f3c` executing under agent label `sa-1a2b` with prompt id 7
- **WHEN** that run emits a log record
- **THEN** the `agent.jsonl` object includes `trace = "r-9f3c"`, `agent = "sa-1a2b"`, and `prompt_id = 7` as fields
- **AND** the `agent.log` line still renders the human prefix `[sa-1a2b r-9f3c]`

#### Scenario: Identity present without a manually threaded prefix
- **GIVEN** a call site that does not pass an explicit log prefix argument
- **WHEN** it emits a record during an active run
- **THEN** the record still carries the current `trace`, `agent`, and `prompt_id` fields

#### Scenario: Sub-agent logs inherit the parent prompt id
- **GIVEN** a sub-agent spawned during prompt #7 runs on a pool thread
- **WHEN** the sub-agent's supervisor calls `bind_run_context` before `runner.run(task)`
- **THEN** the sub-agent's log records carry `prompt_id = 7`
- **AND** the sub-agent's records are correlatable with the main agent's records by `prompt_id`

#### Scenario: Missing run context degrades gracefully
- **GIVEN** a log record emitted outside any run context (e.g. during startup)
- **WHEN** the record is written
- **THEN** the identity fields are absent or empty rather than causing an error

### Requirement: Closed event taxonomy at hot call sites

The application MUST provide a closed, enumerable set of event types and emit them with structured fields at the tool lifecycle, LLM lifecycle, ReAct step/run boundaries, and error paths. Each lifecycle moment MUST be recorded by exactly one structured event — the `log_event` record is the record of truth — and that event MUST carry the operational fields previously held by any duplicate prose line. An unexpected tool exception MUST close its lifecycle span with a single `TOOL_FAILED` record; a separate `ERROR` record for the same failure MUST NOT be emitted.

Rule: The event vocabulary is a fixed, discoverable contract so the agent can query by event type rather than by prose substring. Duplicate prose records that restate a structured event for the same lifecycle moment are removed, not kept in parallel. The `LogEvent` enumeration stays closed at its ten core members (TOOL_START, TOOL_END, TOOL_FAILED, LLM_CALL, LLM_FAILED, STEP_BEGIN, STEP_END, RUN_BEGIN, RUN_END, ERROR); optional background components do not extend it. `ERROR` remains a reserved member — no call site emits it after this change; it stays a valid (zero-match) `event_type` filter value for future error paths.

#### Scenario: Tool failure emits a structured event
- **GIVEN** a tool invocation that exits non-zero
- **WHEN** the failure is logged
- **THEN** the record carries `event_type = "TOOL_FAILED"` with structured `tool`, `exit`, and `dur_ms` fields

#### Scenario: Tool completion is recorded exactly once
- **GIVEN** a built-in tool invocation completes with a result dict
- **WHEN** the completion is logged
- **THEN** exactly one structured record (`TOOL_END` on success, `TOOL_FAILED` on failure) is emitted for that completion
- **AND** no unstructured prose record restating the same completion (tool name, success flag, error) is emitted

#### Scenario: Unexpected exception closes the span with a single record
- **GIVEN** a built-in tool invocation raises an unexpected exception
- **WHEN** the executor handles the exception
- **THEN** exactly one `TOOL_FAILED` record with `event_type`, `tool`, `dur_ms`, `exit`, and `err` fields is emitted
- **AND** no additional `ERROR`-type record is emitted for the same failure

#### Scenario: LLM failure is recorded exactly once
- **GIVEN** an LLM chat request fails
- **WHEN** the failure is handled
- **THEN** exactly one `LLM_FAILED` structured record carries the model id, duration, and error
- **AND** no duplicate unstructured prose record restating the same failure is emitted

#### Scenario: Step boundary carries the active model
- **GIVEN** a ReAct step begins
- **WHEN** the step-begin event is emitted
- **THEN** the record carries `event_type = "STEP_BEGIN"` with structured `step` and `model` fields

#### Scenario: Run boundaries carry model, goal, and step count
- **GIVEN** a ReAct run starts and later finishes
- **WHEN** the run-begin and run-end events are emitted
- **THEN** the run-begin record carries `model` and `goal` fields
- **AND** the run-end record carries `model` and `steps` fields in addition to `dur_ms`

#### Scenario: Event vocabulary is closed and discoverable
- **GIVEN** the set of defined event types
- **WHEN** a component emits an event
- **THEN** the event value is one of the enumerated `LogEvent` members
- **AND** the full set of valid event values can be enumerated programmatically

#### Scenario: Non-hot call sites still log
- **GIVEN** a call site outside the hot set that emits a plain message
- **WHEN** the record is written
- **THEN** it appears in both primary sinks with identity fields, unless the call site belongs to a component with isolated component logging (see the component log isolation requirement)
- **AND** it simply has no `event_type` field

### Requirement: Secret redaction in logs

Known secret values MUST be redacted from both the prose message and the structured fields before either sink serializes the record.

Rule: Redaction sources known values from the agent-scoped vault and runs at the shared filter layer so both sinks are covered uniformly.

#### Scenario: A vault secret value is scrubbed from structured fields
- **GIVEN** a vault entry whose value is `S3CR3T`
- **AND** a log record whose `err` field contains `S3CR3T`
- **WHEN** the record is written
- **THEN** the `agent.jsonl` `err` field does not contain `S3CR3T`
- **AND** the `agent.log` line does not contain `S3CR3T`

### Requirement: Component log isolation for optional background components

The application MUST route log records emitted by the optional background graph-memory component (logger name `graph_memory`) to a dedicated `graph_memory.log` file under the agent's XDG logs directory instead of the primary `agent.jsonl` / `agent.log` sinks. The component log MUST use the same daily gzip rotation and retention policy as the primary sinks. Console (stdout) output for this component MUST be limited to WARNING+ — INFO and DEBUG records are file-only.

Feature: Structured event logging
Rule: Optional background components are operationally isolated. Their diagnostics are fire-and-forget enrichment, not run-scoped work: no trace/agent identity is bound for these records and no structured `event_type` events are emitted for them. Routing is static configuration of the `graph_memory` logger (propagation disabled, dedicated handlers) and is independent of whether graph memory is enabled in config. The one-time `backfill_graph_memory.py` CLI is unaffected — it configures its own logging and never touches the primary sinks. `agent.jsonl` remains purely agent lifecycle — tool, LLM, step, and run events plus non-component diagnostics; component records never reach it.

#### Scenario: Graph-memory records do not appear in the primary sinks
- **GIVEN** logging is configured for the agent
- **WHEN** the graph-memory component emits any record (e.g. store initialisation, batch processing, health warnings)
- **THEN** the record is appended to `graph_memory.log`
- **AND** no record from the `graph_memory` logger appears in `agent.jsonl` or `agent.log`

#### Scenario: Graph-memory log uses the shared rotation policy
- **GIVEN** logging is configured with daily gzip rotation and a backup count
- **WHEN** `graph_memory.log` rotates at midnight
- **THEN** the rotated backup is gzip-compressed with a date suffix
- **AND** retention prunes backups using the same backup count as the primary sinks

#### Scenario: Console shows only graph-memory warnings and errors
- **GIVEN** logging is configured with stdout output
- **WHEN** the graph-memory component emits an INFO record and then a WARNING record
- **THEN** only the WARNING record appears on stdout
- **AND** both records appear in `graph_memory.log`

#### Scenario: Component records carry no run identity by design
- **GIVEN** no `bind_run_context` call is made for the graph-memory worker thread
- **WHEN** the component emits a record
- **THEN** the record is written without error
- **AND** it carries no `trace` or `agent` identity fields (or empty ones)

#### Scenario: Routing is independent of component enablement
- **GIVEN** graph memory is disabled in config
- **WHEN** logging is set up
- **THEN** the `graph_memory` logger is still routed to `graph_memory.log`
- **AND** any incidental records from the module (e.g. the disabled notice) do not reach `agent.jsonl`

#### Scenario: Backfill CLI is unaffected
- **GIVEN** `backfill_graph_memory.py` runs as a standalone CLI with its own `logging.basicConfig`
- **WHEN** it imports and uses `GraphMemoryStore`
- **THEN** its records flow to the CLI's own handler via the root logger
- **AND** the CLI never reads or writes the agent's `graph_memory.log` handlers
