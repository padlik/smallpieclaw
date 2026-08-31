## MODIFIED Requirements

### Requirement: Agent can query its own structured log at runtime

The application MUST provide a built-in `log_query` tool that lets the agent read and filter the structured log store during (or after) a run, returning structured operational facts.

Feature: Runtime log introspection
Rule: Queries read the SQLite log store over the full retention window (30 days) and default to the current run's trace, so mid-run self-analysis is cheap and scoped. Trace-scope resolution is unchanged: an explicit `trace` argument wins; otherwise the query auto-widens to all traces when `text` or `prompt_id` is given; otherwise it defaults to the current run's contextvars trace. The `prompt_id` filter reads the first-class `prompt_id` column directly — no join on the registry is needed. The `prompt_id` field is a globally-unique ULID string, so filtering is unambiguous across day boundaries — and, unlike the previous active-file behavior, records from previous days are now actually returned.

#### Scenario: Query defaults to the current run
- **GIVEN** an agent executing under trace id `r-9f3c` and prompt id `01JARYN6R0`
- **WHEN** the agent invokes `log_query` without specifying a trace or prompt id
- **THEN** only records with `trace = "r-9f3c"` are returned

#### Scenario: Filter by prompt id
- **GIVEN** the store contains records for prompt id `01JARYN6R0` and prompt id `01JARYZ3W2`
- **WHEN** the agent invokes `log_query` with `prompt_id="01JARYN6R0"`
- **THEN** only records with `prompt_id = "01JARYN6R0"` are returned
- **AND** records for prompt id `01JARYZ3W2` are excluded

#### Scenario: Filter by prompt id is unambiguous across days
- **GIVEN** a prompt with ULID `01JARYN6R0` started three days ago and all its records are in the store
- **WHEN** the agent invokes `log_query` with `prompt_id="01JARYN6R0"` today
- **THEN** all records for that ULID within the retention window are returned, including those stored on previous days
- **AND** no other prompt can collide with this ID even if the registry was reset

#### Scenario: Filter by level and event
- **GIVEN** the store contains mixed-level records for the current run
- **WHEN** the agent invokes `log_query` with a minimum level of `WARNING` and event type `TOOL_FAILED`
- **THEN** only `TOOL_FAILED` records at `WARNING` or above are returned

#### Scenario: Default filter surfaces anomalies and tool/LLM lifecycle without step noise
- **GIVEN** the current run has emitted `STEP_BEGIN`/`STEP_END`, `TOOL_START`/`TOOL_END`, `LLM_CALL`, and a `TOOL_FAILED` record
- **WHEN** the agent invokes `log_query` without specifying a level
- **THEN** the `TOOL_FAILED` record and the `TOOL_START`/`TOOL_END` and `LLM_CALL` records are returned
- **AND** the `STEP_BEGIN`/`STEP_END` boundary records are excluded

#### Scenario: Text search matches the full record content
- **GIVEN** the store contains a record whose `extra` field holds `tool = "log_query"` and another whose `err` field holds `timeout exceeded`
- **WHEN** the agent invokes `log_query` with `text="log_query"` and then with `text="timeout exceeded"`
- **THEN** each query returns the record whose content matches, including matches inside `extra` fields
- **AND** matching is case-insensitive with full Unicode folding (e.g. `ß` matches `ss`)

#### Scenario: Results are capped to protect context budget
- **GIVEN** a run that has emitted more matching records than the result cap
- **WHEN** the agent invokes `log_query`
- **THEN** the number of returned records does not exceed the cap
- **AND** the response indicates that results were truncated

#### Scenario: Match count is exact across the retention window
- **GIVEN** the store contains 120 records matching a filter across multiple days
- **WHEN** the agent invokes `log_query` with a limit of 50
- **THEN** the response reports `total_matched = 120` exactly
- **AND** the response does not contain window-saturation flags

#### Scenario: Rotated history is not queried
- **GIVEN** structured records older than the 30-day retention horizon have been deleted by the retention task
- **WHEN** the agent invokes `log_query`
- **THEN** those expired records are not returned
- **AND** only records within the retention window are visible to queries

#### Scenario: Empty result is well-formed
- **GIVEN** no records match the query filters
- **WHEN** the agent invokes `log_query`
- **THEN** a well-formed empty result is returned rather than an error