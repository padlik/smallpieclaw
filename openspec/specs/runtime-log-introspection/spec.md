# Runtime Log Introspection Specification

## Purpose

Define the `log_query` built-in tool that lets the agent read and filter its own structured log store during (or after) a run, returning structured operational facts for mid-run self-correction across the full retention window.

## Requirements

### Requirement: Agent can query its own structured log at runtime

The application MUST provide a built-in `log_query` tool that lets the agent read and filter the structured log store during (or after) a run, returning structured operational facts.

Feature: Runtime log introspection
Rule: Queries read the SQLite log store over the full retention window (30 days) and default to the current run's trace, so mid-run self-analysis is cheap and scoped. Trace-scope resolution is unchanged: an explicit `trace` argument wins; otherwise the query auto-widens to all traces when `text` or `prompt_id` is given; otherwise it defaults to the current run's contextvars trace. The `prompt_id` filter reads the first-class `prompt_id` column directly — no join on the registry is needed. The `prompt_id` field is a globally-unique ULID string, so filtering is unambiguous across day boundaries — and, unlike the previous active-file behavior, records from previous days are now actually returned. Every returned record MUST carry its immutable store `id` (the SQLite row primary key), so each record has a stable reference for follow-up reads. The optional `before_id` parameter restricts matches to records with `id < before_id`, enabling newest→oldest enumeration of match sets larger than one result page, without any cursor state — the anchor is simply the smallest `id` seen in the previous page. Because pages anchor to immutable ids rather than result positions, a walk MUST stay correct while the store is still appending (mid-run self-research). `before_id` composes with all other filters, and `total_matched` MUST remain the exact count of the entire match set (not just the page). Because the whole-set count can grow while the store is appending, `total_matched` is a set-size signal, not a completion signal: a walk is complete when a page returns no records (exhaustion), never by comparing retrieved counts against `total_matched`. The returned page MUST be ordered by `id` (newest id first, re-ascended for output) — always, including the first page of a walk and results later trimmed by the per-call output size (a post-SQL trim unknowable at query-build time) — so that consecutive `before_id` pages cannot skip or duplicate records even when emit-time `ts` order and drain-order `id` diverge (concurrent producers). All existing filter behavior (defaults, Option C high-signal view, trace auto-widening, truncation reporting) is unchanged.

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

#### Scenario: Every returned record carries its store id
- **GIVEN** the store contains records matching a filter
- **WHEN** the agent invokes `log_query`
- **THEN** every returned record includes its `id` field
- **AND** the `id` matches the record's immutable SQLite row primary key

#### Scenario: before_id pages the match set newest to oldest
- **GIVEN** the store contains 120 records matching a filter and a first page returned records with ids 900 down to 851
- **WHEN** the agent invokes `log_query` with the same filters, `before_id = 851`, and a limit of 50
- **THEN** the returned records have ids 850 down to 801
- **AND** `total_matched` reports 120 exactly (the whole match set, not the page)
- **AND** continuing the walk until `before_id` is below the oldest matching id yields every matching record exactly once, in strictly decreasing id order
- **AND** the walk is complete when a page returns no records — never by comparing retrieved counts against `total_matched`

#### Scenario: Walk is gapless when ts order and id order diverge
- **GIVEN** concurrent producers have written matching records where emit-time `ts` order differs from drain-order `id` (a record with a higher id has an older `ts`)
- **WHEN** the agent reads pages (whether split by `limit` or trimmed by the per-call output size) and continues the walk with `before_id` until exhaustion
- **THEN** every matching record is returned exactly once across the pages
- **AND** no record is skipped at the boundary between the first page and the second

#### Scenario: Walk stays correct while the store is appending
- **GIVEN** the agent has read a page of matching records with the smallest seen id 851, and the run then emits new matching records with higher ids
- **WHEN** the agent continues the walk with `before_id = 851`
- **THEN** the new records do not shift, duplicate, or omit any records in the walk
- **AND** the agent never needs to re-read a page it has already seen

### Requirement: Agent can retrieve a single log record at full fidelity

The application MUST provide a built-in `log_detail` tool that returns one complete log record by its store `id`, lifting the per-field truncation applied to `log_query` results, so the agent can read a failure's full text (error output, stack traces, payloads) and classify its nature and source. The tool takes a required `id` (integer) argument. The serialized record is subject to a hard cap of 65536 chars; records above the cap are handled by the oversized-record dump requirement. The tool is read-only with respect to the log store (it never writes to the store); its oversized path may write one redacted artifact to the results directory.

#### Scenario: Full-fidelity read of a verbose record
- **GIVEN** the store contains a record whose `err` field holds a multi-thousand-character browser failure text longer than 500 chars
- **WHEN** the agent invokes `log_detail` with that record's `id`
- **THEN** the returned record contains the full `err` text with no per-field truncation
- **AND** the failure's distinguishing signature (timeout vs unexpected pop-up vs JavaScript error) is readable in full

#### Scenario: Detail record matches the list projection's identity
- **GIVEN** `log_query` returned a record with `id = 3108` and a 500-char-truncated preview of its `err` field
- **WHEN** the agent invokes `log_detail` with `id = 3108`
- **THEN** the returned record is the same store row (same `id`, `trace`, `event_type`, and timestamps)
- **AND** the `err` field is the complete text the preview was truncated from

#### Scenario: Not-found id yields a well-formed empty result
- **GIVEN** the store contains no record with `id = 999999`
- **WHEN** the agent invokes `log_detail` with `id = 999999`
- **THEN** a well-formed empty result is returned rather than an error

#### Scenario: Unreadable or missing store yields a well-formed empty result
- **GIVEN** the log store is missing or cannot be opened
- **WHEN** the agent invokes `log_detail` with any `id`
- **THEN** a well-formed empty result is returned rather than an error

### Requirement: Oversized log records are dumped to the run results directory

When a record's serialized form exceeds the 65536-char hard cap, `log_detail` MUST write the full record as an artifact under the run-scoped results subdirectory of the record's own trace (`~/.<agent>/results/<trace-id>/`), following the established oversized-artifact destination (Tier 1, jail-mounted read-write, `file_read`-accessible without confirmation). Stored record content is untrusted as a path component: the record's `trace` MUST be validated against the trace-id format (`r-[0-9a-f]{8}`) before being used as the subdirectory name; a non-conforming trace falls back to the current run's trace. When the record carries no trace, the current run's trace subdirectory is used. The resolved dump path MUST be asserted to remain within the results directory. Log records are already vault-redacted at ingestion by `agent_logging`; the dump artifact preserves that redaction (whether the dump adds defense-in-depth re-redaction is a design decision, but MUST NOT reduce existing redaction). The tool result MUST report the artifact path plus a truncated preview of the record, so the agent can decide whether to `file_read` the full dump.

#### Scenario: Oversized record is dumped and pointed to
- **GIVEN** the store contains a record whose serialized form exceeds 65536 chars for trace `r-1a2b3c4d`
- **WHEN** the agent invokes `log_detail` with that record's `id`
- **THEN** the full record is written under `~/.<agent>/results/r-1a2b3c4d/`
- **AND** the tool result reports the artifact file path
- **AND** the tool result includes a truncated preview of the record
- **AND** the agent can read the artifact back with `file_read` without operator confirmation

#### Scenario: Record without a trace is dumped under the current run's trace
- **GIVEN** the store contains an oversized record with no `trace` field and the agent is executing under trace `r-current01`
- **WHEN** the agent invokes `log_detail` with that record's `id`
- **THEN** the full record is written under `~/.<agent>/results/r-current01/`

#### Scenario: Malformed stored trace never becomes a path component
- **GIVEN** the store contains an oversized record whose `trace` field does not conform to the `r-[0-9a-f]{8}` trace-id format, and the agent is executing under trace `r-current01`
- **WHEN** the agent invokes `log_detail` with that record's `id`
- **THEN** the full record is written under the current run's trace subdirectory (`~/.<agent>/results/r-current01/`)
- **AND** no directory is created from the malformed stored trace value

### Requirement: The log_query tool descriptor teaches budget-aware usage patterns

The `log_query` tool descriptor MUST document the effective usage patterns that keep self-analysis within the per-call output budget: that raising `limit` alone does not return more records when the output size cap binds; that a `limit: 1` probe yields an exact `total_matched` count of any filter combination; the walk-then-drill pattern — `before_id` paging to enumerate a match set plus `log_detail` for per-record full-fidelity inspection; and walk-scoping discipline — that an exhaustive walk is completed by empty-page exhaustion (not by matching counts against `total_matched`, which can grow on a live store), and that walks should be scoped by `prompt_id` or an explicit `trace`, because `text`-only queries auto-widen to all traces over the full retention window.

#### Scenario: Descriptor documents the caps interplay and probe pattern
- **GIVEN** the `log_query` tool descriptor exposed to the LLM
- **WHEN** the descriptor is read
- **THEN** it states that the number of returned records may be reduced by the per-call output size cap, so raising `limit` alone does not return more records
- **AND** it presents the `limit: 1` probe as the way to obtain an exact match count

#### Scenario: Descriptor documents walk-then-drill
- **GIVEN** the `log_query` tool descriptor exposed to the LLM
- **WHEN** the descriptor is read
- **THEN** it presents the `before_id` paging pattern for enumerating match sets larger than one page
- **AND** it presents `log_detail` as the follow-up read for full-fidelity per-record inspection

#### Scenario: Descriptor documents walk completion and scoping discipline
- **GIVEN** the `log_query` tool descriptor exposed to the LLM
- **WHEN** the descriptor is read
- **THEN** it states that a walk ends when a page returns no records, not when retrieved counts reach `total_matched`
- **AND** it advises scoping exhaustive walks by `prompt_id` or an explicit `trace`, warning that `text`-only walks span all traces over the full retention window
