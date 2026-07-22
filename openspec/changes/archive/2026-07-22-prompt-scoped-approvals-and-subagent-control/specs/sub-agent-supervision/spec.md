## ADDED Requirements

### Requirement: Sub-agents share the main agent's per-prompt approval set

Sub-agents SHALL check the main agent's per-prompt `auto_approve_tools` set (via a shared reference on the `BuiltinExecutor`) before prompting the operator for sensitive file operations. A single "Approve all `<tool>`" granted during the prompt covers the main agent and all its sub-agents for that prompt.

Feature: Sub-agent supervision
Rule: The shared approval set is per-prompt — set at `run()` start, cleared at `run()` end. One grant covers the whole council for one task.

#### Scenario: Sub-agent auto-approves a tool the operator approved for the prompt
- **GIVEN** the operator tapped "Approve all file_read" during the current prompt
- **AND** sub-agent A attempts a sensitive `file_read` (agent-internal or UNRECOGNISED path)
- **WHEN** the sub-agent's confirmation bridge checks the shared approval set
- **THEN** the operation is auto-approved without a new operator prompt
- **AND** zone classification still ran first — only the zone-triggered confirmation was auto-satisfied

#### Scenario: Sub-agent prompts when the tool is not in the approval set
- **GIVEN** no "Approve all file_write" has been granted for the current prompt
- **AND** sub-agent B attempts a sensitive `file_write`
- **WHEN** the sub-agent's confirmation bridge checks the shared approval set
- **THEN** the operator is prompted with an "Approve all file_write" button alongside Approve/Deny
- **AND** the sub-agent blocks until the operator responds or the confirmation times out

#### Scenario: Shared approval set is cleared at run end
- **GIVEN** "Approve all file_read" was granted during prompt #7
- **WHEN** prompt #7's run ends (results presented)
- **THEN** the shared approval set is cleared
- **AND** any orphaned sub-agent that attempts a sensitive `file_read` after run end re-prompts the operator (fail-closed)

#### Scenario: Shared approve-all reaches agent-internal and UNRECOGNISED zones for sub-agents
- **GIVEN** the operator tapped "Approve all file_write" during the current prompt
- **AND** sub-agent C attempts a `file_write` to an agent-internal path
- **WHEN** the sub-agent's confirmation bridge checks the shared approval set
- **THEN** the operation proceeds without a new prompt, because confirmation is the enforcement mechanism for agent-internal paths and the approve-all auto-satisfies it
- **AND** this is the intended council-pattern behavior — the operator's single grant covers sub-agent writes to any zone for the duration of the prompt

### Requirement: Sub-agent confirmation prompts offer per-tool approve-all

Sub-agent confirmation prompts SHALL include a per-tool "Approve all `<tool>`" button that, when tapped, adds the tool name to the shared `auto_approve_tools` set and confirms the current pending operation.

Feature: Sub-agent supervision
Rule: Per-tool, not blanket — `file_read`, `file_write`, `file_patch` are granted separately, matching the main-agent UI.

#### Scenario: Approve-all button on a sub-agent prompt
- **GIVEN** sub-agent A's sensitive `file_write` triggered a confirmation prompt
- **WHEN** the prompt is rendered
- **THEN** the prompt includes "Approve", "Deny", and "Approve all file_write" buttons

#### Scenario: Tapping approve-all confirms the current op and grants future ops
- **GIVEN** sub-agent A's sensitive `file_write` triggered a confirmation prompt
- **WHEN** the operator taps "Approve all file_write"
- **THEN** the current `file_write` is confirmed and A proceeds
- **AND** "file_write" is added to the shared `auto_approve_tools` set
- **AND** subsequent `file_write` calls by A, other sub-agents, and the main agent for this prompt are auto-approved

### Requirement: Supervisor records spawned sub-agents against the active prompt

The supervisor SHALL record each spawned sub-agent's `agent_id` against the active prompt in the `PromptRegistry` at submission time, so the prompt's `sub_agent_ids` list is complete.

Feature: Sub-agent supervision
Rule: The supervisor reads the active prompt ID and registry reference from the shared `BuiltinExecutor` fields set at `run()` start.

#### Scenario: Spawned sub-agent is recorded against the active prompt
- **GIVEN** prompt #8 is active and the main agent spawns sub-agent D
- **WHEN** the supervisor accepts the submission
- **THEN** D's `agent_id` is appended to prompt #8's `sub_agent_ids` list in the registry
- **AND** an update record is appended to `data/prompts.jsonl`