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
