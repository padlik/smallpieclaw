# Prompt Tracking Specification

## Purpose

Define the monotonic prompt registry that assigns a human-friendly "Prompt #N" ID to each user-initiated agent run, persists the mapping to `data/prompts.jsonl`, and provides the `/prompts` Telegram command for operator visibility.

## Requirements

### Requirement: Prompt registry assigns monotonic prompt IDs

The system SHALL assign a monotonic, human-friendly prompt ID ("Prompt #N") to each user-initiated agent run, persisted to `data/prompts.jsonl` so the ID is stable across process restarts.

Feature: Prompt tracking
Rule: The prompt ID is the operator-facing handle for a run; the trace ID remains the high-cardinality join key for logs.

#### Scenario: A new prompt gets the next sequential ID
- **GIVEN** the last assigned prompt ID was 7 and the registry has reloaded from `data/prompts.jsonl` on startup
- **WHEN** the operator sends a new message that starts an agent run
- **THEN** the run is assigned prompt ID 8
- **AND** a record with `prompt_id=8`, `trace_id`, `text` (first 200 chars), `started_at`, `status="running"`, and `sub_agent_ids=[]` is appended to `data/prompts.jsonl`

#### Scenario: Prompt ID is stable across restarts
- **GIVEN** the process restarts after prompt ID 7 was assigned
- **WHEN** the registry initializes on startup
- **THEN** the next prompt ID assigned is 8 (max existing + 1)
- **AND** the operator's "Prompt #7" reference still refers to the same run

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

The system SHALL provide a `/prompts` Telegram command that lists recent prompts with their ID, status, elapsed time, and sub-agent count.

Feature: Prompt tracking
Rule: The list is the operator's way to reference "Prompt #N" in conversation and log queries.

#### Scenario: /prompts lists recent prompts
- **GIVEN** prompts 5, 6, 7 have completed and prompt 8 is running
- **WHEN** the operator runs `/prompts`
- **THEN** the response lists the most recent N prompts (default 20)
- **AND** each entry shows the prompt ID, status, elapsed time, and sub-agent count
