## ADDED Requirements

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

### Requirement: Operator can search prompts by text content

The system SHALL provide a `/prompts search <query> [Nd/Nh]` Telegram command that searches all prompt history (in-memory records and the archive file) using case-insensitive substring matching on prompt `text`. An optional time-window suffix (`Nd` for days, `Nh` for hours) filters results to prompts started within the window. Search matches on `text` only; `trace_id` search is out of scope.

Feature: Prompt tracking
Rule: Search covers the full history, not just the in-memory cache. An empty query with a time window acts as a wildcard listing all prompts in that window.

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

## MODIFIED Requirements

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