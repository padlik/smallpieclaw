## MODIFIED Requirements

### Requirement: Structured JSONL event sink

The application MUST write a machine-readable structured log to the SQLite store (`agent_logs.sqlite`, see the `sqlite-log-store` capability for storage mechanics) alongside the human prose `agent.log`. Both sinks are written from the same log record so their content never drift.

Feature: Structured event logging
Rule: The SQLite store is the primary machine surface; `agent.log` is the retained secondary human surface. Every record emitted to one is emitted to the other — with the exception of components under component log isolation (see the component log isolation requirement), whose records route to their dedicated component log instead. The structured stream is no longer gzip-rotated; retention is time-based DELETE (see the `sqlite-log-store` capability).

#### Scenario: A log record is written to both sinks
- **GIVEN** the logging system is configured
- **WHEN** any component not subject to component log isolation emits a log record
- **THEN** a prose line is appended to `agent.log`
- **AND** the same record is stored as a row in `agent_logs.sqlite`
- **AND** the stored record contains at least `ts`, `level`, `logger`, and `msg` fields

#### Scenario: JSONL line is independently parseable
- **GIVEN** a record has been written to the SQLite store
- **WHEN** a reader retrieves that record's row and parses its fields (including the `extra` JSON)
- **THEN** parsing succeeds without depending on any other row

### Requirement: Structured run identity on every record

Run identity — the trace id (`r-<hex>`), the agent label, the source tag, and the prompt id — MUST be present as structured fields on the log record, not only as text inside the message. Both formatters render identity from those fields.

Rule: Identity is ambient logging context (observability), sourced from context-local state; correctness-critical trace propagation remains explicit and unchanged. The prompt id is bound into the structlog context at `run()` start via `bind_run_context()`, alongside the existing trace/agent/run-label, and inherited by sub-agents via the existing context propagation.

#### Scenario: Identity appears as JSON fields
- **GIVEN** a run with trace id `r-9f3c` executing under agent label `sa-1a2b` with prompt id 7
- **WHEN** that run emits a log record
- **THEN** the stored record includes `trace = "r-9f3c"`, `agent = "sa-1a2b"`, and `prompt_id = 7` as fields
- **AND** the `agent.log` line still renders the human prefix `[sa-1a2b r-9f3c]`

#### Scenario: Identity present without a manually threaded prefix
- **GIVEN** a call site that does not pass an explicit log prefix argument
- **WHEN** it emits a record during an active run
- **THEN** the record still carries the current `trace`, `agent`, and `prompt_id` fields

#### Scenario: Sub-agent logs inherit the parent prompt id
- **GIVEN** a sub-agent spawned during prompt #7 runs on a pool thread
- **WHEN** the sub-agent's supervisor calls `bind_run_context` before `runner.run(task)`
- **THEN** the sub-agent's stored records carry `prompt_id = 7`
- **AND** the sub-agent's records are correlatable with the main agent's records by `prompt_id`

#### Scenario: Missing run context degrades gracefully
- **GIVEN** a log record emitted outside any run context (e.g. during startup)
- **WHEN** the record is written
- **THEN** the identity fields are absent or empty rather than causing an error

### Requirement: Secret redaction in logs

Known secret values MUST be redacted from both the prose message and the structured fields before either sink serializes the record.

Rule: Redaction sources known values from the agent-scoped vault and runs at the shared filter layer so both sinks are covered uniformly.

#### Scenario: A vault secret value is scrubbed from structured fields
- **GIVEN** a vault entry whose value is `S3CR3T`
- **AND** a log record whose `err` field contains `S3CR3T`
- **WHEN** the record is written
- **THEN** the stored record's `err` field does not contain `S3CR3T`
- **AND** the `agent.log` line does not contain `S3CR3T`

### Requirement: Component log isolation for optional background components

The application MUST route log records emitted by the optional background graph-memory component (logger name `graph_memory`) to a dedicated `graph_memory.log` file under the agent's XDG logs directory instead of the primary SQLite store / `agent.log` sinks. The component log MUST use the same daily gzip rotation and retention policy as the prose sink. Console (stdout) output for this component MUST be limited to WARNING+ — INFO and DEBUG records are file-only.

Feature: Structured event logging
Rule: Optional background components are operationally isolated. Their diagnostics are fire-and-forget enrichment, not run-scoped work: no trace/agent identity is bound for these records and no structured `event_type` events are emitted for them. Routing is static configuration of the `graph_memory` logger (propagation disabled, dedicated handlers) and is independent of whether graph memory is enabled in config. The one-time `backfill_graph_memory.py` CLI is unaffected — it configures its own logging and never touches the primary sinks. The structured store remains purely agent lifecycle — tool, LLM, step, and run events plus non-component diagnostics; component records never reach it. The `log-store-backfill` capability's CLI (`backfill_log_store.py`) is a distinct program and equally unaffected by this routing.

#### Scenario: Graph-memory records do not appear in the primary sinks
- **GIVEN** logging is configured for the agent
- **WHEN** the graph-memory component emits any record (e.g. store initialisation, batch processing, health warnings)
- **THEN** the record is appended to `graph_memory.log`
- **AND** no record from the `graph_memory` logger appears in the structured store or `agent.log`

#### Scenario: Graph-memory log uses the shared rotation policy
- **GIVEN** logging is configured with daily gzip rotation and a backup count
- **WHEN** `graph_memory.log` rotates at midnight
- **THEN** the rotated backup is gzip-compressed with a date suffix
- **AND** retention prunes backups using the same backup count as the prose sink

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
- **AND** any incidental records from the module (e.g. the disabled notice) do not reach the structured store

#### Scenario: Backfill CLI is unaffected
- **GIVEN** `backfill_graph_memory.py` runs as a standalone CLI with its own `logging.basicConfig`
- **WHEN** it imports and uses `GraphMemoryStore`
- **THEN** its records flow to the CLI's own handler via the root logger
- **AND** the CLI never reads or writes the agent's `graph_memory.log` handlers