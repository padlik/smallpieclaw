## ADDED Requirements

### Requirement: Plan agents use runtime construction
Plan-step and diagnostic sub-agent runs SHALL use the shared runtime construction boundary while preserving execution-plan orchestration behavior.

#### Scenario: Plan-step construction uses runtime
- **GIVEN** an execution plan starts a normal sub-agent step
- **WHEN** the step runner is constructed
- **THEN** construction flows through the runtime profile for plan-step agents
- **AND** existing DAG dependency, parallelism, cancellation, timeout, registry visibility, and result aggregation behavior remain compatible

#### Scenario: Diagnostic construction uses runtime
- **GIVEN** plan recovery or diagnostics starts an internal diagnostic sub-agent
- **WHEN** the diagnostic runner is constructed
- **THEN** construction flows through the runtime profile for diagnostic agents
- **AND** existing diagnostic error handling, cleanup, visibility, and recovery behavior remain compatible
