# sub-agent-supervision Specification

## Purpose

Define supervision behavior for on-demand and scheduled sub-agent runs.

## Requirements

### Requirement: Background sub-agent supervision
The system SHALL supervise on-demand and scheduled sub-agent runs through an internal supervision boundary that owns background execution lifecycle while preserving the model-facing `spawn_agent` contract.

#### Scenario: On-demand spawn returns a registered agent id
- **GIVEN** the model calls `spawn_agent` with a valid task from a root agent
- **WHEN** the spawn request is accepted
- **THEN** the system registers a sub-agent run and returns an `agent_id` before the background task begins or continues independently
- **AND** the background run can later be joined with `get_agent_result`

#### Scenario: Background run completion is signaled
- **GIVEN** a spawned sub-agent is running in the background
- **WHEN** the sub-agent finishes, fails, or is cancelled
- **THEN** the system records the final status and result for the `agent_id`
- **AND** any waiter using `get_agent_result` is released with the recorded outcome
- **AND** the runner is cleaned up after completion

### Requirement: Scheduler supervision channel separation
Scheduled sub-agent launches SHALL provide scheduler/internal control data through an internal supervision channel, not through the model-facing `spawn_agent` argument dictionary.

#### Scenario: Scheduled launch uses internal control channel
- **GIVEN** a scheduled job launches a sub-agent with job metadata and completion callbacks
- **WHEN** the job is submitted for background execution
- **THEN** `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, and `expandable` are not passed as model-facing `spawn_agent` arguments
- **AND** the scheduler's completion and result logging callbacks remain associated with that submitted run

#### Scenario: Concurrent scheduled launches keep callback isolation
- **GIVEN** two scheduled jobs launch sub-agents at nearly the same time
- **WHEN** both jobs complete
- **THEN** each job's finish and result logging callbacks are invoked for the correct job
- **AND** one job's callback data does not overwrite the other job's callback data

### Requirement: Existing spawn supervision behavior is preserved
The supervision extraction SHALL preserve existing sub-agent behavior for model-facing contract validation, capacity checks, context persistence, result retrieval, notification suppression after timeout, and cancellation. Source values MAY change when a later visibility/capacity policy explicitly assigns distinct source categories, but managed-record counting behavior SHALL remain compatible with that policy.

#### Scenario: Model-facing spawn validation remains compatible
- **GIVEN** a caller uses the model-facing `spawn_agent` tool contract
- **WHEN** the caller provides task aliases, `response_format`, an invalid `context_key`, or attempts to spawn from a nested sub-agent
- **THEN** the system applies the same accepted aliases, response-format shaping, context-key rejection, and depth-guard behavior as before the supervision extraction
- **AND** invalid requests are rejected before a background sub-agent run is submitted

#### Scenario: Capacity semantics remain compatible after scheduled source retagging
- **GIVEN** the system has reached the same number of globally capacity-counted sub-agent records that would trigger the max-subagents limit
- **WHEN** an on-demand or scheduled sub-agent launch is attempted through its supported launch path
- **THEN** the launch is accepted or rejected according to the global max-subagents behavior defined by the running-agent visibility policy
- **AND** scheduled runs carry `source="scheduled"` while continuing to count against the global guard

#### Scenario: Timeout cancellation remains compatible
- **GIVEN** a caller waits for a spawned sub-agent result with a timeout and `cancel_on_timeout` enabled
- **WHEN** the timeout expires before the sub-agent completes
- **THEN** the system cancels the underlying sub-agent run using the same cancellation behavior as before
- **AND** the timed-out run does not later send a stale success notification

#### Scenario: Context persistence remains compatible
- **GIVEN** a spawned or scheduled sub-agent uses a valid `context_key`
- **WHEN** the sub-agent run completes or is cleaned up
- **THEN** the system persists the sub-agent short-term context using the same context-key storage behavior as before

#### Scenario: Sub-agent results are not auto-admitted to graph memory
- **GIVEN** a spawned or scheduled sub-agent completes with a result
- **WHEN** the supervisor records and delivers that result
- **THEN** the result is not automatically stored in graph memory
- **AND** any graph-memory admission remains governed by the existing explicit graph-memory tools or policies

### Requirement: Source-aware supervised runs
The sub-agent supervisor SHALL assign source categories that distinguish on-demand and scheduled supervised runs while preserving the existing spawn and scheduler behavior.

#### Scenario: On-demand supervised run remains on-demand
- **GIVEN** the model-facing `spawn_agent` tool admits a root-agent sub-agent run
- **WHEN** the run is registered
- **THEN** the record source is `on-demand`
- **AND** the run counts against the global `max_subagents` capacity guard

#### Scenario: Scheduled supervised run is scheduled
- **GIVEN** a scheduled job admits a sub-agent run through the supervisor
- **WHEN** the run is registered
- **THEN** the record source is `scheduled`
- **AND** the run counts against the global `max_subagents` capacity guard

#### Scenario: Supervision compatibility remains stable
- **GIVEN** an on-demand or scheduled supervised run is active
- **WHEN** the run completes, fails, is cancelled, or is joined with `get_agent_result`
- **THEN** existing result retrieval, timeout cancellation, context persistence, scheduler callback delivery, and graph-memory non-admission behavior remains compatible

### Requirement: Supervised agents use runtime construction
Supervised on-demand and scheduled sub-agent runs SHALL use the shared runtime construction boundary while preserving existing supervision behavior.

#### Scenario: On-demand supervised construction uses runtime
- **GIVEN** the model-facing `spawn_agent` path admits an on-demand run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for on-demand sub-agents
- **AND** the supervision lifecycle, registry source, capacity behavior, result retrieval, context persistence, timeout cancellation, and graph-memory non-admission behavior remain compatible

#### Scenario: Scheduled supervised construction uses runtime
- **GIVEN** the scheduler admits a scheduled sub-agent run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for scheduled agents
- **AND** scheduler callbacks, result logging, notification options, registry source, and capacity behavior remain compatible

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
