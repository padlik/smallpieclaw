## MODIFIED Requirements

### Requirement: Prompt registry assigns globally-unique prompt IDs

The system SHALL assign a globally-unique, time-sortable string prompt ID (ULID format: 26-char Crockford base32, 48-bit millisecond timestamp + 80-bit random) to each user-initiated agent run, persisted to `data/prompts.jsonl` so the ID is stable across process restarts, registry resets, and day boundaries. The ID is generated inline with no external dependency. The terminal status SHALL distinguish between successful completion (`done`), cancellation (`cancelled`), and failure (`failed`). A run whose result string starts with "❌" SHALL be classified as `failed`.

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

#### Scenario: Failed run is classified as "failed" not "done"
- **GIVEN** a run completes with a result string starting with "❌"
- **WHEN** `_classify_final_status()` processes the result
- **THEN** the terminal status is set to `failed`
- **AND** the prompt registry records `status="failed"` in the finalization record
- **AND** the Telegram status message shows "❌ Failed" instead of "✅ Done"

#### Scenario: Cancelled run is classified as "cancelled"
- **GIVEN** a run completes with result string "[Cancelled]"
- **WHEN** `_classify_final_status()` processes the result
- **THEN** the terminal status is set to `cancelled`
- **AND** the prompt registry records `status="cancelled"` in the finalization record

#### Scenario: Successful run is classified as "done"
- **GIVEN** a run completes with a normal result string not starting with "❌" and not "[Cancelled]"
- **WHEN** `_classify_final_status()` processes the result
- **THEN** the terminal status is set to `done`
- **AND** the prompt registry records `status="done"` in the finalization record

#### Scenario: Sub-agent IDs are recorded against the originating prompt
- **GIVEN** a prompt run is active and the main agent spawns sub-agent A
- **WHEN** the supervisor accepts the sub-agent
- **THEN** sub-agent A's `agent_id` is appended to the prompt's `sub_agent_ids` list in the registry
- **AND** an update record is appended to `data/prompts.jsonl` so the mapping survives restarts