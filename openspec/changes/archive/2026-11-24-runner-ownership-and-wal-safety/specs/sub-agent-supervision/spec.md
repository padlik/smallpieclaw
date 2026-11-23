## MODIFIED Requirements

### Requirement: Background sub-agent supervision

The system SHALL supervise on-demand and scheduled sub-agent runs through an internal supervision boundary that owns background execution lifecycle while preserving the model-facing `spawn_agent` contract. On-demand children SHALL carry the identity of their spawning **operator turn**; scheduled runs SHALL carry job-owned origin without acquiring a parent turn. From successful runner construction, the supervisor SHALL own resource release on submission rejection or accepted-task termination, including accepted work cancelled before it starts.

#### Scenario: On-demand spawn returns a registered agent id
- **GIVEN** the model calls `spawn_agent` with a valid task from a root agent
- **WHEN** the spawn request is accepted
- **THEN** the system registers a sub-agent run and returns an `agent_id` before background work continues
- **AND** that turn can later join the child with `get_agent_result`
- **AND** the child cannot become detached from its owning turn

#### Scenario: Background run completion is signaled
- **GIVEN** a spawned sub-agent is running in the background
- **WHEN** the sub-agent finishes, fails, or is cancelled
- **THEN** the system records the final status and result for the `agent_id`
- **AND** any owning-turn waiter using `get_agent_result` is released with the recorded outcome
- **AND** the runner is cleaned up after completion before the parent turn can close

## ADDED Requirements

### Requirement: Rejected submissions release constructed runners

On-demand/scheduled pre-acceptance failure SHALL independently attempt runner close and rollback of all potentially installed registry, prompt and owned-child state, including registration that inserts then raises. Cleanup faults SHALL NOT skip later stages or mask the primary error. Cancellation SHALL propagate without ordinary-failure classification. Rejected runners SHALL NOT execute or invoke accepted-task finish callbacks.

#### Scenario: Prompt association fails
- **GIVEN** an on-demand runner has been constructed and registered
- **WHEN** associating it with its prompt fails
- **THEN** runner close and all applicable tracking rollback stages are attempted once
- **AND** it neither executes nor invokes the finish callback, and the original failure remains primary

#### Scenario: Scheduled executor submission fails
- **GIVEN** a scheduled runner has been constructed and registered
- **WHEN** the executor rejects submission
- **THEN** the runner is closed and its active registration is removed
- **AND** execution and the accepted-task finish callback do not occur

#### Scenario: Registration raises after inserting a record
- **GIVEN** a constructed runner's registration inserts a record before raising
- **WHEN** the submission unwinds
- **THEN** rollback locates the record using the runner identity and attempts its removal and runner close
- **AND** a failure in one rollback stage does not prevent the remaining stages or replace the registration error

#### Scenario: Registration fails before inserting a record
- **GIVEN** a constructed runner whose registration has not installed any state
- **WHEN** registration fails
- **THEN** the runner is closed without execution or finish callback
- **AND** absent tracking entries do not prevent the original failure from propagating

#### Scenario: Submission is attempted after shutdown begins
- **GIVEN** the supervisor has begun shutdown
- **WHEN** an on-demand or scheduled submission reaches the final handoff
- **THEN** it is rejected and any constructed runner is closed with applicable tracking rollback
- **AND** direct supervisor callers receive `SupervisorStoppingError`, a `RuntimeError` subtype, after rollback
- **AND** neither runner execution nor the accepted-task finish callback occurs

### Requirement: Accepted queued cancellation receives terminal cleanup

Accepted-before-start cancellation SHALL signal the existing cancelled outcome without running the runner, saving requested context first. Active-capacity release, close, grant cleanup, finish and owned-cleanup signaling SHALL be independently attempted exactly once despite stage failures. Existing result-log, notify/completion, timeout suppression and retained-owner-outcome rules SHALL remain unchanged. Started clients SHALL close only after execution unwinds.

#### Scenario: Shutdown cancels queued accepted work
- **GIVEN** one executor worker is blocked and a second accepted runner is queued
- **WHEN** supervisor shutdown cancels queued work
- **THEN** the second runner never executes and its cancelled outcome becomes available
- **AND** context save precedes result signaling, active capacity is released, and close, grant cleanup, finish and owned-cleanup signaling are attempted once without waiting for the blocked worker to finish

#### Scenario: Terminal cleanup encounters a failure
- **GIVEN** a queued accepted task is cancelled before starting
- **WHEN** context persistence, deregistration, close or a bound callback raises during its terminal path
- **THEN** the remaining cleanup stages are attempted and cleanup errors are recorded
- **AND** its runner is not executed or retried

#### Scenario: One queued-entry cancellation does not abandon the shutdown batch
- **GIVEN** two queued accepted runners are cancelled by shutdown
- **WHEN** the first runner's terminal cleanup raises a cancellation
- **THEN** the second runner is still terminalized and the bounded started-worker drain is still attempted
- **AND** that cancellation propagates only after both attempts

#### Scenario: A started runner is cooperatively cancelled
- **GIVEN** an accepted runner is already executing when shutdown begins
- **WHEN** cancellation is signaled
- **THEN** its worker retains cleanup responsibility until execution unwinds
- **AND** shutdown does not close its clients while they are still in use

#### Scenario: Repeated shutdown does not duplicate terminal effects
- **GIVEN** a queued task has already received cancellation-only terminal cleanup
- **WHEN** shutdown is called again
- **THEN** its close, finish callback and terminal signaling are not repeated
- **AND** a terminal outcome still required by its owner remains retrievable under existing access rules

### Requirement: Queued cancellation precedes the started-worker drain

Shutdown SHALL terminalize cancelled queued entries before beginning the bounded drain of started work. This ordering SHALL NOT impose a new time bound on synchronous callbacks or require started workers to finish immediately. Only shutdown SHALL cancel accepted Futures; timeout and owner cancellation SHALL retain record signaling.

#### Scenario: Queued cleanup precedes the started-worker drain
- **GIVEN** a started worker is blocked and a second accepted runner is queued
- **WHEN** supervisor shutdown begins
- **THEN** executor shutdown cancels the queued Future and the supervisor terminalizes it before beginning the bounded worker drain
- **AND** the queued runner never executes and its cleanup is attempted exactly once
- **AND** the started runner remains worker-owned and open until execution unwinds

### Requirement: Terminal stages remain independent

Terminal processing SHALL attempt context persistence, result signaling, lifecycle logging, result recording and eligible completion handoff independently and at most once. A stage failure SHALL NOT skip later eligible attempts or cause a second terminalization through execution-error handling.

#### Scenario: Result signaling fails after persistence cancellation
- **GIVEN** context persistence raises a cancellation and result signaling raises an ordinary failure
- **WHEN** terminal processing completes its remaining attempts
- **THEN** lifecycle logging, result recording and eligible completion handoff are each attempted once
- **AND** the original cancellation remains the propagated exception

#### Scenario: Completion cancellation follows an ordinary terminal failure
- **GIVEN** result signaling raises an ordinary failure and eligible completion handoff raises a cancellation
- **WHEN** terminal processing finishes
- **THEN** the cancellation takes precedence over the ordinary failure
- **AND** every remaining eligible terminal and cleanup stage is attempted once without retrying execution or re-entering terminal processing through execution-error handling

### Requirement: Local failures have explicit precedence

Cleanup SHALL receive the exception escaping its protected local operation explicitly; unrelated ambient handlers and errors already handled into outcomes SHALL NOT establish a primary. Preserve that primary; otherwise propagate the first cancellation/non-`Exception` failure, or the first otherwise-propagating ordinary failure. Existing swallowed callback errors SHALL remain contained.

#### Scenario: Cleanup runs inside an unrelated caller handler
- **GIVEN** the protected local operation has no escaping failure but its caller is handling an unrelated exception
- **WHEN** a cleanup stage raises a cancellation
- **THEN** all remaining cleanup stages are attempted and that cancellation propagates
- **AND** the unrelated caller exception does not suppress it

#### Scenario: Genuine primary cancellation is paired with cleanup failure
- **GIVEN** a local execution or terminal cancellation is escaping
- **WHEN** cleanup also raises cancellation or an ordinary owned-signaling failure
- **THEN** every remaining cleanup stage is attempted
- **AND** the exact original local cancellation remains primary

### Requirement: Stopping refusals are translated only at the spawn boundary

Only the spawn compatibility shim SHALL translate `SupervisorStoppingError` into its existing structured refusal shape after rollback. Other submission errors and cancellation SHALL retain existing propagation. Scheduled launches SHALL retain existing generic safe failure presentation and exactly-once run release.

#### Scenario: Spawn returns a safe stopping refusal
- **GIVEN** a spawn request reaches the stopping supervisor's final handoff
- **WHEN** the supervisor rolls back and raises `SupervisorStoppingError`
- **THEN** the shim returns `success=false`, empty `output`, `exit_code=-1`, `error_type="impossible_with_current_tools"` and `recoverable=false`
- **AND** the result is stamped `refused` and contains a fixed safe stopping message and operator restart guidance
- **AND** the rejected runner never executes or invokes accepted-task finish

#### Scenario: Unexpected submission faults are not stopping refusals
- **GIVEN** registration or executor submission raises an unexpected ordinary error or cancellation
- **WHEN** the spawn request unwinds
- **THEN** the shim does not translate it into a stopping refusal
- **AND** independent rollback is attempted without replacing the original exception

#### Scenario: Scheduled stopping rejection releases its launch once
- **GIVEN** a scheduled launch reaches a stopping supervisor through the spawn shim
- **WHEN** the scheduler receives the structured stopping refusal
- **THEN** it retains existing generic safe launch-failure reporting and releases that admitted run exactly once
- **AND** no runner execution or supervisor accepted-task finish callback occurs

### Requirement: Shutdown handoff avoids orphaned tasks and locked callbacks

Concurrent submission and shutdown SHALL resolve to either rejection with complete attempted rollback or accepted work covered by cooperative cancellation and terminal cleanup. Cancellation-only terminal effects SHALL run outside executor and supervisor submission locks. Shutdown cleanup SHALL NOT clear the shutdown caller's thread-local run context. This SHALL NOT alter source categories, capacity limits or overload policy.

#### Scenario: Submission races shutdown
- **GIVEN** a submission is paused at its final executor handoff
- **WHEN** shutdown begins concurrently
- **THEN** the submission is either rejected with rollback or accepted with tracked cancellation and cleanup
- **AND** no accepted runner or active registration is orphaned

#### Scenario: Finish callback attempts a new submission
- **GIVEN** queued cancellation cleanup has a finish callback that attempts another submission
- **WHEN** shutdown invokes that callback
- **THEN** the attempt is rejected through the stopping rejection path without deadlock
- **AND** the rejected runner is closed and the shutdown caller's run context remains unchanged
