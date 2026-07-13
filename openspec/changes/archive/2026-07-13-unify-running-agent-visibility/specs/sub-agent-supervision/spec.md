## ADDED Requirements

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

## MODIFIED Requirements

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

## REMOVED Requirements
