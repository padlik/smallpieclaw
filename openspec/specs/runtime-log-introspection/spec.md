# Runtime Log Introspection Specification

## Purpose

Define the `log_query` built-in tool that lets the agent read and filter its own active structured log during a run, returning structured operational facts for mid-run self-correction.

## Requirements

### Requirement: Agent can query its own structured log at runtime

The application MUST provide a built-in `log_query` tool that lets the agent read and filter the active structured log during a run, returning structured operational facts.

Feature: Runtime log introspection
Rule: Queries read the active `agent.jsonl` only and default to the current run's trace, so mid-run self-analysis is cheap and scoped. The `prompt_id` filter reads the first-class `prompt_id` field directly from each log line — no join on the registry is needed. The `prompt_id` field is a globally-unique ULID string, so filtering is unambiguous across day boundaries even though only the active day's log is read.

#### Scenario: Query defaults to the current run
- **GIVEN** an agent executing under trace id `r-9f3c` and prompt id `01JARYN6R0`
- **WHEN** the agent invokes `log_query` without specifying a trace or prompt id
- **THEN** only records with `trace = "r-9f3c"` are returned

#### Scenario: Filter by prompt id
- **GIVEN** the active log contains records for prompt id `01JARYN6R0` and prompt id `01JARYZ3W2`
- **WHEN** the agent invokes `log_query` with `prompt_id="01JARYN6R0"`
- **THEN** only records with `prompt_id = "01JARYN6R0"` are returned
- **AND** records for prompt id `01JARYZ3W2` are excluded

#### Scenario: Filter by prompt id is unambiguous across days
- **GIVEN** a prompt with ULID `01JARYN6R0` started before midnight and its log records span two daily `agent.jsonl` files
- **WHEN** the agent invokes `log_query` with `prompt_id="01JARYN6R0"` on the active day
- **THEN** only records for that ULID in the active log are returned
- **AND** no other prompt can collide with this ID even if the registry was reset

#### Scenario: Filter by level and event
- **GIVEN** the active log contains mixed-level records for the current run
- **WHEN** the agent invokes `log_query` with a minimum level of `WARNING` and event type `TOOL_FAILED`
- **THEN** only `TOOL_FAILED` records at `WARNING` or above are returned

#### Scenario: Default filter surfaces anomalies and tool/LLM lifecycle without step noise
- **GIVEN** the current run has emitted `STEP_BEGIN`/`STEP_END`, `TOOL_START`/`TOOL_END`, `LLM_CALL`, and a `TOOL_FAILED` record
- **WHEN** the agent invokes `log_query` without specifying a level
- **THEN** the `TOOL_FAILED` record and the `TOOL_START`/`TOOL_END` and `LLM_CALL` records are returned
- **AND** the `STEP_BEGIN`/`STEP_END` boundary records are excluded

#### Scenario: Results are capped to protect context budget
- **GIVEN** a run that has emitted more matching records than the result cap
- **WHEN** the agent invokes `log_query`
- **THEN** the number of returned records does not exceed the cap
- **AND** the response indicates that results were truncated

#### Scenario: Rotated history is not queried
- **GIVEN** structured records exist only in a rotated, compressed backup
- **WHEN** the agent invokes `log_query`
- **THEN** those rotated records are not returned
- **AND** only the active `agent.jsonl` is read

#### Scenario: Empty result is well-formed
- **GIVEN** no records match the query filters
- **WHEN** the agent invokes `log_query`
- **THEN** a well-formed empty result is returned rather than an error