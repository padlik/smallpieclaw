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
The supervision extraction SHALL preserve existing sub-agent behavior for model-facing contract validation, capacity checks, registry source values, context persistence, result retrieval, notification suppression after timeout, and cancellation.

#### Scenario: Model-facing spawn validation remains compatible
- **GIVEN** a caller uses the model-facing `spawn_agent` tool contract
- **WHEN** the caller provides task aliases, `response_format`, an invalid `context_key`, or attempts to spawn from a nested sub-agent
- **THEN** the system applies the same accepted aliases, response-format shaping, context-key rejection, and depth-guard behavior as before the supervision extraction
- **AND** invalid requests are rejected before a background sub-agent run is submitted

#### Scenario: Capacity semantics remain unchanged
- **GIVEN** the system has reached the same number of managed sub-agent records that would have triggered the previous max-subagents limit
- **WHEN** an on-demand or scheduled sub-agent launch is attempted through its supported launch path
- **THEN** the launch is accepted or rejected according to the same max-subagents behavior as before the supervision extraction
- **AND** the recorded source values used by managed-record counting remain unchanged

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
