## ADDED Requirements

### Requirement: Running agent source categories
The system SHALL classify visible sub-agent executions using a closed source category set: `on-demand`, `scheduled`, `plan-step`, and `diagnostic`.

#### Scenario: Scheduled run has scheduled source
- **GIVEN** a scheduler-launched sub-agent is admitted for execution
- **WHEN** the run is registered as active
- **THEN** its source category is `scheduled`
- **AND** it is distinguishable from an on-demand `spawn_agent` run

#### Scenario: Plan-step run has plan-step source
- **GIVEN** an execution plan starts a normal step sub-agent
- **WHEN** the step runner is active
- **THEN** its source category is `plan-step`

#### Scenario: Diagnostic run has diagnostic source
- **GIVEN** the system starts an internal diagnostic or recovery sub-agent
- **WHEN** the diagnostic runner is active
- **THEN** its source category is `diagnostic`

### Requirement: Running agent visibility and capacity policy
The system SHALL separate operator visibility from global capacity counting.

#### Scenario: Visible categories appear in agents list
- **GIVEN** active sub-agent records exist for `on-demand`, `scheduled`, `plan-step`, and `diagnostic` sources
- **WHEN** an operator requests the active agents list
- **THEN** all four records are visible
- **AND** each record displays its source category distinctly

#### Scenario: Global capacity counted categories
- **GIVEN** active sub-agent records exist for all supported source categories
- **WHEN** the system evaluates the global `max_subagents` capacity guard
- **THEN** only `on-demand` and `scheduled` records count against that guard
- **AND** `plan-step` and `diagnostic` records do not count against the global guard

#### Scenario: Managed cancellation follows global capacity scope
- **GIVEN** active records include `on-demand`, `scheduled`, `plan-step`, and `diagnostic` sources
- **WHEN** an operator requests cancellation of all managed agents
- **THEN** the system cancels only the records that count against the global `max_subagents` guard
- **AND** plan-step and diagnostic records remain cancellable by explicit id or label

## MODIFIED Requirements

## REMOVED Requirements
