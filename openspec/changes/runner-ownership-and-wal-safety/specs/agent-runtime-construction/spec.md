## ADDED Requirements

### Requirement: Runner resources retain explicit construction and caller ownership

Constructor failure SHALL close allocated owned chat/embedding clients. Returned runners SHALL remain launcher/direct-caller owned, not self-closing in `run()`. Plan/diagnostic launchers SHALL protect registration, execution, outcomes and cleanup, independently attempting tracking removal and close without masking primary errors. Repeated close SHALL be safe; borrowed root services SHALL stay open. Visibility, sources and capacity SHALL remain unchanged.

#### Scenario: Partial construction fails after client allocation
- **GIVEN** runner construction has allocated an embedding client and a chat client
- **WHEN** later controller construction fails
- **THEN** both allocated clients receive cleanup before the construction failure propagates
- **AND** no runner is submitted for execution

#### Scenario: Chat allocation fails after embedding allocation
- **GIVEN** runner construction has allocated its embedding client
- **WHEN** chat-client construction fails
- **THEN** the embedding client is closed before the original failure propagates
- **AND** borrowed root services remain open

#### Scenario: Plan-step registration fails
- **GIVEN** a plan-step runner was successfully constructed
- **WHEN** registration fails before or after inserting a record
- **THEN** registry removal, plan active-runner removal and runner close are independently attempted
- **AND** the registration error remains primary

#### Scenario: Plan outcome handling fails
- **GIVEN** a registered plan-step runner has returned from execution
- **WHEN** plan outcome processing raises
- **THEN** its active-runner entry and registry record are removed and its runner is closed
- **AND** the outcome-processing error remains primary

#### Scenario: Diagnostic registration fails
- **GIVEN** a diagnostic runner was successfully constructed
- **WHEN** registration fails before or after inserting a record
- **THEN** registry removal and runner close are independently attempted
- **AND** the registration error remains primary and execution does not start

#### Scenario: Diagnostic deregistration fails
- **GIVEN** a diagnostic runner has completed or failed
- **WHEN** its registry cleanup raises
- **THEN** its runner still receives close and remaining cleanup is attempted
- **AND** an existing execution failure is not replaced by the cleanup failure

#### Scenario: Plan or diagnostic execution fails
- **GIVEN** a constructed plan-step or diagnostic runner is registered
- **WHEN** execution raises or cancellation unwinds
- **THEN** the launcher attempts all applicable tracking cleanup and runner close
- **AND** cancellation is not classified as an ordinary model failure

#### Scenario: Direct caller closes explicitly
- **GIVEN** a direct synchronous caller owns a constructed runner
- **WHEN** the caller runs the task and then closes the runner twice
- **THEN** run itself has not assumed close ownership and repeated close remains safe
- **AND** borrowed root services remain open
