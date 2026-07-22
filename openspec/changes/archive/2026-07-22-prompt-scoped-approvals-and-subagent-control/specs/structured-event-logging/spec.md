## MODIFIED Requirements

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