## ADDED Requirements

### Requirement: SQLite WAL store as the structured log sink

The application MUST store every structured log record as a row in a SQLite database at `~/.local/state/<agent_name>/logs/agent_logs.sqlite`, configured with WAL journal mode. Each record MUST be mapped to wide columns (`ts`, `level`, `logger`, `agent`, `trace`, `prompt_id`, `event_type`, `msg`) with all remaining operational fields (tool, exit, dur_ms, err, model, …) stored in an `extra` JSON column, plus a `search_text` column holding the casefolded compact-JSON serialization of the full record.

Feature: sqlite-log-store
Rule: The store is the machine-readable primary sink; the prose `agent.log` remains the human surface. The structlog processor chain, closed `LogEvent` taxonomy, contextvars run identity, and secret redaction are unchanged — this capability governs only storage mechanics.

#### Scenario: A structured record is stored as a row
- **GIVEN** the logging system is configured
- **WHEN** any component not subject to component log isolation emits a log record
- **THEN** a row is inserted into `agent_logs.sqlite` containing the record's `ts`, `level`, `logger`, `agent`, `trace`, `prompt_id`, `event_type`, and `msg` as wide columns
- **AND** all remaining fields of the record are preserved in the row's `extra` JSON
- **AND** the row's `search_text` equals the casefolded compact-JSON serialization of the full record

#### Scenario: Level names are stored lowercase
- **GIVEN** structlog emits level names in lowercase
- **WHEN** a record is stored
- **THEN** the `level` column holds the lowercase name (e.g. `warning`, not `WARNING`)

#### Scenario: Committed records survive process kill
- **GIVEN** records have been committed to the store
- **WHEN** the process is killed with SIGKILL and restarted
- **THEN** all committed records are readable after WAL replay
- **AND** the loss is bounded to records still in the in-memory queue at kill time

### Requirement: Indexed lookups across full retention

The store MUST provide indexed lookups on `ts`, `trace`, `prompt_id`, and `agent`, plus a partial index on `event_type` (rows where it is not NULL), so trace-, prompt-, and time-scoped queries resolve as index seeks over the full retention window rather than linear scans.

Feature: sqlite-log-store

#### Scenario: Prompt-scoped query reaches a record from a previous day
- **GIVEN** a record with `prompt_id = "01JARYN6R0"` was stored three days ago
- **WHEN** the store is queried with `prompt_id = "01JARYN6R0"`
- **THEN** the record is returned via the `prompt_id` index

#### Scenario: Trace-scoped query reaches a scheduled job's old trace
- **GIVEN** a scheduled job ran two days ago under trace `r-9f3c` and its records are in the store
- **WHEN** the store is queried with `trace = "r-9f3c"`
- **THEN** the job's records are returned via the `trace` index

### Requirement: Single-writer queue ingestion

All structured records MUST flow through a bounded in-memory queue (`QueueHandler` → `QueueListener`) to one writer thread that owns the only database connection and performs batched INSERTs. The emitting hot path MUST never block on database I/O. On graceful shutdown the queue MUST be drained before exit.

Feature: sqlite-log-store
Rule: The queue is bounded; when full, the oldest records are dropped, a dropped counter increments, and a WARNING is emitted to the prose sink.

#### Scenario: Concurrent emitters are serialized
- **GIVEN** the react loop, a sub-agent pool thread, and a scheduler job emit records concurrently
- **WHEN** the records are ingested
- **THEN** every record is stored exactly once with no locking error surfaced to any emitter

#### Scenario: Hot path does not block on database I/O
- **GIVEN** the writer thread is mid-INSERT
- **WHEN** the react loop emits another record
- **THEN** the emit call returns immediately after enqueueing

#### Scenario: Queue overflow drops oldest rather than blocking
- **GIVEN** the bounded queue is full
- **WHEN** additional records are emitted
- **THEN** the oldest queued records are dropped
- **AND** a WARNING documenting the drop count is emitted to the prose sink
- **AND** the emitting thread is never blocked

#### Scenario: Graceful shutdown drains the queue
- **GIVEN** records are pending in the queue at shutdown
- **WHEN** the agent stops gracefully
- **THEN** all queued records are committed before process exit

### Requirement: Time-based retention

The store MUST delete rows older than the retention horizon (30 days) via a time-cutoff DELETE, executed at startup and daily, followed by a WAL checkpoint. This replaces gzip rotation for the structured stream.

Feature: sqlite-log-store

#### Scenario: Rows older than the retention horizon are deleted
- **GIVEN** the store contains rows 31 days old and rows from today
- **WHEN** the retention task runs
- **THEN** the 31-day-old rows are deleted
- **AND** recent rows are preserved

### Requirement: Graceful degradation when the store is unavailable

If the database cannot be opened or a write fails persistently, the agent MUST continue operating with the prose sink only: the structured sink is disabled, a WARNING is emitted to the prose sink, and the react loop never depends on store availability.

Feature: sqlite-log-store

#### Scenario: Unwritable store degrades to prose-only operation
- **GIVEN** the logs directory is read-only so `agent_logs.sqlite` cannot be created
- **WHEN** the agent starts
- **THEN** the agent starts and runs normally
- **AND** prose records continue to be written to `agent.log`
- **AND** a WARNING documents that the structured store is disabled

#### Scenario: log_query fails gracefully when the store is unavailable
- **GIVEN** the structured store is disabled
- **WHEN** the agent invokes `log_query`
- **THEN** a well-formed error or empty result is returned rather than a crash