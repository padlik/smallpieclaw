## ADDED Requirements

### Requirement: Plan sub-agent registry visibility
Execution-plan sub-agents SHALL be visible in the global running-agent registry while preserving plan execution semantics.

#### Scenario: Plan step appears while running
- **GIVEN** an execution plan starts a normal sub-agent step
- **WHEN** the step is running
- **THEN** an active registry record exists for that step with source `plan-step`
- **AND** the record is visible in `/agents`
- **AND** the record does not count against the global `max_subagents` capacity guard

#### Scenario: Diagnostic agent appears while running
- **GIVEN** plan recovery or diagnostics starts an internal diagnostic sub-agent
- **WHEN** the diagnostic agent is running
- **THEN** an active registry record exists for that diagnostic run with source `diagnostic`
- **AND** the record is visible in `/agents`
- **AND** the record does not count against the global `max_subagents` capacity guard

#### Scenario: Plan semantics are preserved
- **GIVEN** plan-step and diagnostic records are visible in the global registry
- **WHEN** the plan completes, fails, retries, times out, or is cancelled
- **THEN** existing DAG dependency, retry, diagnostic, timeout, and result aggregation behavior remains compatible
- **AND** registry records are removed when their corresponding runner is no longer active

## MODIFIED Requirements

## REMOVED Requirements
