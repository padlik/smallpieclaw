## MODIFIED Requirements

### Requirement: Prompt registry assigns monotonic prompt IDs

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
- **THEN** legacy integer-ID records are loaded into memory as-is
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

The system SHALL provide a `/prompts` Telegram command that lists recent prompts with their ID, start timestamp, truncated prompt text, status, elapsed time, and sub-agent count. The list is sorted by start time descending (newest first), not by prompt ID, so mixed legacy-int and ULID-string IDs never cause a sort error.

Feature: Prompt tracking
Rule: The operator recognizes prompts by their text and timestamp, not by the ID alone. The full ULID is shown without truncation so the operator can copy-paste it for log queries.

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