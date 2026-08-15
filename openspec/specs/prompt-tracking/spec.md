# Prompt Tracking Specification

## Purpose

Define the prompt registry that assigns a globally-unique ULID string prompt ID to each user-initiated agent run, persists the mapping to `data/prompts.jsonl`, and provides the `/prompts` Telegram command for operator visibility.

## Requirements

### Requirement: Prompt registry assigns globally-unique prompt IDs

The system SHALL assign a globally-unique, time-sortable string prompt ID (ULID format: 26-char Crockford base32, 48-bit millisecond timestamp + 80-bit random) to each user-initiated agent run, persisted to `data/prompts.jsonl` so the ID is stable across process restarts, registry resets, and day boundaries. The ID is generated inline with no external dependency.

Feature: Prompt tracking
Rule: The prompt ID is the single operator-facing handle for a run — globally unique and stable forever. The trace ID remains the high-cardinality join key for logs but is an implementation detail the operator does not need to know.

#### Scenario: A new prompt gets a globally-unique ULID
- **GIVEN** the registry is initialized
- **WHEN** the operator sends a new message that starts an agent run
- **THEN** the run is assigned a fresh 26-char ULID string prompt ID
- **AND** a record with `prompt_id` (ULID string), `trace_id`, `text` (first 200 chars), `started_at`, `status="running"`, and `sub_agent_ids=[]` is appended to `data/prompts.jsonl`

#### Scenario: Prompt ID is stable across restarts
- **GIVEN** the process restarts after a prompt with ULID `01J...` was assigned
- **WHEN** the registry initializes on startup and replays `data/prompts.jsonl`
- **THEN** the existing ULID record is restored into memory
- **AND** the operator's reference to that ULID still refers to the same run
- **AND** the next prompt gets a new ULID (no sequential counter, no `max_id+1`)

#### Scenario: Prompt ID survives registry reset
- **GIVEN** `data/prompts.jsonl` is deleted and the process restarts
- **WHEN** a new prompt is started
- **THEN** the new prompt gets a fresh ULID that does not collide with any previously-assigned ID
- **AND** old log records referencing the deleted prompts remain unambiguous

#### Scenario: Legacy integer IDs are tolerated on replay
- **GIVEN** `data/prompts.jsonl` contains records with integer `prompt_id` values from a prior version
- **WHEN** the registry initializes and replays the file
- **THEN** legacy integer-ID records are normalized to `str` at the replay boundary
- **AND** new prompts receive ULID string IDs
- **AND** no history rewrite occurs

#### Scenario: Prompt record is finalized on run completion
- **GIVEN** a prompt run is in progress with `status="running"`
- **WHEN** the run completes, fails, or is cancelled
- **THEN** a finalization record with `ended_at` and the terminal `status` is appended to `data/prompts.jsonl`
- **AND** the full `sub_agent_ids` list is included in the finalization record

#### Scenario: Sub-agent IDs are recorded against the originating prompt
- **GIVEN** a prompt run is active and the main agent spawns sub-agent A
- **WHEN** the supervisor accepts the sub-agent
- **THEN** sub-agent A's `agent_id` is appended to the prompt's `sub_agent_ids` list in the registry
- **AND** an update record is appended to `data/prompts.jsonl` so the mapping survives restarts

### Requirement: Operator can list recent prompts

The system SHALL provide a `/prompts` Telegram command that lists recent prompts with their ID, start timestamp, truncated prompt text, status, elapsed time, and sub-agent count. The list is sorted by start time descending (newest first), not by prompt ID, so mixed legacy-int and ULID-string IDs never cause a sort error. With no arguments, the command lists the most recent 20 prompts from the in-memory cache. The command also accepts `search` and `show` subcommands for text-based search and single-prompt display.

Feature: Prompt tracking
Rule: The operator recognizes prompts by their text and timestamp, not by the ID alone. The full ULID is shown without truncation so the operator can copy-paste it for log queries or `/prompts show`.

#### Scenario: /prompts lists recent prompts with text and timestamp
- **GIVEN** prompts have been started with recognizable text
- **WHEN** the operator runs `/prompts`
- **THEN** the response lists the most recent N prompts (default 20), sorted by start time descending
- **AND** each entry shows the full prompt ID (ULID, no truncation)
- **AND** each entry shows the start timestamp
- **AND** each entry shows the truncated prompt text (first ~80 characters)
- **AND** each entry shows the status, elapsed time, and sub-agent count

#### Scenario: /prompts sorts by start time, not by prompt ID
- **GIVEN** the registry contains a mix of legacy integer IDs and new ULID string IDs
- **WHEN** the operator runs `/prompts`
- **THEN** the list is ordered by `started_at` descending
- **AND** no `TypeError` is raised from comparing mixed int/str IDs

#### Scenario: /prompts with unknown subcommand falls back to list
- **GIVEN** the operator runs `/prompts foobar`
- **WHEN** the command is processed
- **THEN** the response lists recent prompts (same as `/prompts` with no arguments)

### Requirement: Prompt registry evicts old finalized records to bound memory

The system SHALL cap in-memory prompt records at 100. When a new prompt is started and the in-memory record count exceeds 100, the oldest finalized record SHALL be evicted from memory. Running records SHALL never be evicted. Evicted records remain accessible via the archive file.

Feature: Prompt tracking
Rule: The in-memory registry is a hot cache of recent prompts, not a full history store. The archive file holds the full history.

#### Scenario: Oldest finalized record is evicted when cap is exceeded
- **GIVEN** the registry contains 100 finalized records and 0 running records in memory
- **WHEN** a new prompt is started
- **THEN** the new record is added to memory
- **AND** the oldest finalized record is removed from `_records` and `_trace_to_id`
- **AND** the in-memory record count remains 100

#### Scenario: Running records are never evicted
- **GIVEN** the registry contains 100 records, all with `status="running"`
- **WHEN** a new prompt is started
- **THEN** the new record is added to memory
- **AND** no running record is evicted
- **AND** the in-memory record count is 101

#### Scenario: Evicted record is accessible via archive
- **GIVEN** a finalized record has been evicted from memory
- **WHEN** the operator runs `/prompts show <evicted_prompt_id>`
- **THEN** the record is found in the archive file
- **AND** the full record details are displayed

### Requirement: Finalized prompts are archived to a snapshot file

The system SHALL write one self-contained snapshot line per finalized prompt to `data/prompts_archive.jsonl`. Each line SHALL contain `prompt_id`, `trace_id`, `text`, `started_at`, `ended_at`, `status`, and `sub_agent_ids`. The snapshot is written from `finish()` before any eviction can occur. The archive file is not rotated.

Feature: Prompt tracking
Rule: The archive is the cold-store for search and show. One line per prompt, self-contained, no replay logic needed to read it.

#### Scenario: Finish writes a snapshot to the archive
- **GIVEN** a prompt run with `prompt_id="01H..."` is completing with `status="done"`
- **WHEN** `finish()` is called
- **THEN** a snapshot line is appended to `data/prompts_archive.jsonl`
- **AND** the line contains all 7 fields: `prompt_id`, `trace_id`, `text`, `started_at`, `ended_at`, `status`, `sub_agent_ids`

#### Scenario: Archive snapshot is written before eviction
- **GIVEN** a finalized record exists in memory and the registry is at capacity
- **WHEN** a new prompt is started, triggering eviction of the old finalized record
- **THEN** the old record's snapshot is already in the archive (written by its `finish()` call)
- **AND** the evicted record is findable via `find_in_archive()`

#### Scenario: Archive is backfilled from event log on first startup
- **GIVEN** `data/prompts_archive.jsonl` does not exist on startup
- **AND** `data/prompts.jsonl` contains finalized prompt records
- **WHEN** the registry initializes
- **THEN** all finalized records from the event log are written to `data/prompts_archive.jsonl`
- **AND** running records from the event log are not archived
- **AND** the backfill runs only once (subsequent startups skip backfill if the archive exists)

### Requirement: Operator can search prompts by text content and metadata

The system SHALL provide a `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` Telegram command that searches all prompt history (in-memory records and the archive file) using case-insensitive substring matching on prompt `text`. Optional filters narrow results by status (`running`/`done`/`failed`/`cancelled`), exact `trace_id` match, absolute time range (`since`/`until` as ISO 8601 timestamps), and pagination (`--page`, 1-indexed, 20 results per page). An optional relative time-window suffix (`Nd` for days, `Nh` for hours) filters results to prompts started within the window; when both `days` and `since`/`until` are supplied, `since`/`until` take precedence and `days` is ignored. Search matches on `text` only for substring matching; `trace_id` and `status` are exact-match filters. The `search()` method returns a `SearchPage` dataclass with `results` (the page slice) and `total_matched` (the full match count before offset/limit).

Feature: Prompt tracking
Rule: Search covers the full history, not just the in-memory cache. An empty query with a time window acts as a wildcard listing all prompts in that window. Filters can be combined to narrow results for execution-analysis workflows.

#### Scenario: Search finds prompts by substring match
- **GIVEN** prompts with text "PTO request for next week" and "review worklogs" exist in history
- **WHEN** the operator runs `/prompts search PTO`
- **THEN** the response includes the prompt containing "PTO request for next week"
- **AND** the response does not include the prompt containing "review worklogs"

#### Scenario: Search is case-insensitive
- **GIVEN** a prompt with text "PTO request" exists in history
- **WHEN** the operator runs `/prompts search pto`
- **THEN** the response includes the prompt containing "PTO request"

#### Scenario: Search with time window filters by start time
- **GIVEN** a prompt with text "PTO" was started 3 days ago
- **AND** a prompt with text "PTO" was started 10 days ago
- **WHEN** the operator runs `/prompts search PTO 7d`
- **THEN** the response includes only the prompt from 3 days ago
- **AND** the prompt from 10 days ago is excluded

#### Scenario: Search with hours window
- **GIVEN** a prompt with text "worklogs" was started 2 hours ago
- **AND** a prompt with text "worklogs" was started 20 hours ago
- **WHEN** the operator runs `/prompts search worklogs 12h`
- **THEN** the response includes only the prompt from 2 hours ago

#### Scenario: Search with empty query and time window returns all prompts in window
- **GIVEN** 5 prompts were started within the last 7 days
- **WHEN** the operator runs `/prompts search 7d`
- **THEN** the response includes all 5 prompts from the last 7 days

#### Scenario: Search with empty query and no time window returns most recent prompts
- **GIVEN** prompts exist in history with various dates
- **WHEN** the operator runs `/prompts search` with no query and no time window
- **THEN** the response returns the 20 most recent prompts from all history (wildcard match)

#### Scenario: Search with no time window searches all history
- **GIVEN** prompts exist from various dates spanning months
- **WHEN** the operator runs `/prompts search worklogs`
- **THEN** the response includes all prompts matching "worklogs" regardless of age

#### Scenario: Search results are sorted by start time descending
- **GIVEN** 3 prompts match the query, started at times T1 < T2 < T3
- **WHEN** the operator runs `/prompts search <query>`
- **THEN** the results are ordered T3, T2, T1 (most recent first)

#### Scenario: Search deduplicates in-memory and archive results
- **GIVEN** a prompt exists both in memory (not yet evicted) and in the archive
- **WHEN** the operator runs `/prompts search <matching_query>`
- **THEN** the prompt appears only once in the results

#### Scenario: Search returns no more than 20 results
- **GIVEN** 30 prompts match the query
- **WHEN** the operator runs `/prompts search <query>`
- **THEN** the response shows at most 20 results (the 20 most recent)

#### Scenario: Search with no matches returns a message
- **GIVEN** no prompts contain the text "nonexistent"
- **WHEN** the operator runs `/prompts search nonexistent`
- **THEN** the response says no prompts match the query

#### Scenario: Search with results fitting on a single page shows a single-page footer
- **GIVEN** 5 prompts match the query "worklogs"
- **WHEN** the operator runs `/prompts search worklogs`
- **THEN** the response shows all 5 results
- **AND** a footer shows `📄 Page 1 of 1` (no "next" tail)

#### Scenario: Search filters by status
- **GIVEN** prompts with statuses "done", "failed", and "running" exist in history
- **WHEN** the operator runs `/prompts search --status=failed`
- **THEN** the response includes only prompts with status "failed"
- **AND** prompts with status "done" or "running" are excluded

#### Scenario: Search with invalid status value returns an error
- **GIVEN** prompts exist in history
- **WHEN** the operator runs `/prompts search --status=unknown`
- **THEN** the response says the status is invalid
- **AND** the response lists valid status values: running, done, failed, cancelled

#### Scenario: Search filters by trace_id
- **GIVEN** a prompt with trace_id "r-abc123" and text "PTO request" exists in history
- **AND** a prompt with trace_id "r-def456" and text "PTO review" exists in history
- **WHEN** the operator runs `/prompts search PTO --trace=r-abc123`
- **THEN** the response includes only the prompt with trace_id "r-abc123"
- **AND** the prompt with trace_id "r-def456" is excluded

#### Scenario: Search with absolute time range using since and until
- **GIVEN** a prompt with text "deploy" was started on 2026-08-03
- **AND** a prompt with text "deploy" was started on 2026-08-10
- **AND** a prompt with text "deploy" was started on 2026-08-14
- **WHEN** the operator runs `/prompts search deploy --since=2026-08-05 --until=2026-08-12`
- **THEN** the response includes only the prompt from 2026-08-10
- **AND** the prompts from 2026-08-03 and 2026-08-14 are excluded

#### Scenario: Search with since only filters by start time lower bound
- **GIVEN** a prompt with text "worklogs" was started 3 days ago
- **AND** a prompt with text "worklogs" was started 10 days ago
- **WHEN** the operator runs `/prompts search worklogs --since=<ISO date 5 days ago>`
- **THEN** the response includes only the prompt from 3 days ago

#### Scenario: Search with naive ISO timestamp interprets input as UTC
- **GIVEN** a prompt with text "deploy" was started at epoch time corresponding to 2026-08-10T12:00:00Z
- **WHEN** the operator runs `/prompts search deploy --since=2026-08-10T12:00:00` (no timezone offset)
- **THEN** the naive timestamp is interpreted as UTC
- **AND** the prompt is included in the results

#### Scenario: since and until take precedence over days
- **GIVEN** a prompt with text "deploy" was started on 2026-08-10
- **WHEN** the operator runs `/prompts search deploy 7d --since=2026-08-01 --until=2026-08-15`
- **THEN** the `7d` relative window is ignored
- **AND** the absolute range 2026-08-01 to 2026-08-15 is applied

#### Scenario: Search with combined status and trace_id filters
- **GIVEN** a prompt with text "PTO", status "failed", and trace_id "r-abc" was started 2 days ago
- **AND** a prompt with text "PTO", status "done", and trace_id "r-def" was started 1 day ago
- **WHEN** the operator runs `/prompts search PTO --status=failed --trace=r-def`
- **THEN** the response includes no prompts (the filters are mutually exclusive on the two records)

#### Scenario: Search with status filter alone narrows results
- **GIVEN** a prompt with text "PTO", status "failed", and trace_id "r-abc" was started 2 days ago
- **AND** a prompt with text "PTO", status "done", and trace_id "r-def" was started 1 day ago
- **WHEN** the operator runs `/prompts search PTO --status=failed`
- **THEN** the response includes only the failed prompt with trace_id "r-abc"

#### Scenario: Search results are paginated
- **GIVEN** 30 prompts match the query "worklogs"
- **WHEN** the operator runs `/prompts search worklogs`
- **THEN** the response shows 20 results (page 1)
- **AND** a footer indicates `Page 1 of 2 — use --page=2 for next`

#### Scenario: Search pagination with --page returns the next page
- **GIVEN** 30 prompts match the query "worklogs"
- **WHEN** the operator runs `/prompts search worklogs --page=2`
- **THEN** the response shows 10 results (page 2)
- **AND** a footer indicates `Page 2 of 2`

#### Scenario: Search with out-of-range page returns a page-out-of-range message
- **GIVEN** 30 prompts match the query "worklogs" (2 pages of 20)
- **WHEN** the operator runs `/prompts search worklogs --page=5`
- **THEN** the response says `Page 5 is past the last page (2 pages total).`
- **AND** the response does NOT say "no prompts matching"

#### Scenario: Search returns a SearchPage with total_matched
- **GIVEN** 30 prompts match the query "worklogs"
- **WHEN** the search method is called with query "worklogs", limit=20, offset=0
- **THEN** the returned SearchPage contains 20 results in the `results` field
- **AND** the `total_matched` field is 30

#### Scenario: Unknown flag is treated as query text
- **GIVEN** a prompt with text "--verbose PTO request" exists in history
- **WHEN** the operator runs `/prompts search --verbose PTO`
- **THEN** `--verbose` is not a recognized flag
- **AND** it is treated as query text
- **AND** the search query becomes "--verbose PTO"
- **AND** the prompt is included in the results

#### Scenario: Search does not block concurrent registry operations
- **GIVEN** a search is in progress scanning the archive file
- **WHEN** a new prompt is started concurrently
- **THEN** the `start()` call is not blocked by the search
- **AND** the search completes with a snapshot of in-memory records taken before the archive scan

### Requirement: Operator can view a single prompt's full record

The system SHALL provide a `/prompts show <id>` Telegram command that displays the full record for a single prompt by its ULID. The lookup checks in-memory records first, then falls back to the archive file. The display includes the full 200-char prompt text (untruncated), trace ID, start and end timestamps, elapsed time, status, and sub-agent IDs.

Feature: Prompt tracking
Rule: `show` is the archive-aware lookup. `get()` and `by_trace()` remain in-memory-only for the sub-agent supervisor's hot path.

#### Scenario: Show displays full record for an in-memory prompt
- **GIVEN** a prompt with `prompt_id="01H..."` exists in memory with `status="done"`
- **WHEN** the operator runs `/prompts show 01H...`
- **THEN** the response shows the full prompt text (up to 200 chars, untruncated)
- **AND** the response shows the trace ID, start timestamp, end timestamp, elapsed time, status, and sub-agent IDs

#### Scenario: Show on a running prompt displays elapsed time without end timestamp
- **GIVEN** a prompt with `prompt_id="01H..."` exists in memory with `status="running"` and no `ended_at`
- **WHEN** the operator runs `/prompts show 01H...`
- **THEN** the response shows the full prompt text, trace ID, start timestamp, and status
- **AND** the end timestamp is shown as "(running)" or "—"
- **AND** the elapsed time shows time elapsed since start (not "None" or an error)

#### Scenario: Show displays full record for an evicted prompt via archive
- **GIVEN** a prompt with `prompt_id="01H..."` has been evicted from memory
- **AND** its snapshot exists in `data/prompts_archive.jsonl`
- **WHEN** the operator runs `/prompts show 01H...`
- **THEN** the response shows the full prompt record retrieved from the archive

#### Scenario: Show for a non-existent prompt ID returns not-found
- **GIVEN** no prompt with `prompt_id="01HNONEXIST..."` exists in memory or the archive
- **WHEN** the operator runs `/prompts show 01HNONEXIST...`
- **THEN** the response says the prompt was not found

#### Scenario: Show with no ID argument returns usage
- **GIVEN** the operator runs `/prompts show` with no ID argument
- **WHEN** the command is processed
- **THEN** the response shows a usage message: "Usage: /prompts show <id>"