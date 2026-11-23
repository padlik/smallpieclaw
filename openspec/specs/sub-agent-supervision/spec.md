# sub-agent-supervision Specification

## Purpose

Define supervision behavior for on-demand and scheduled sub-agent runs.

## Requirements

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

### Requirement: Spawn validation and capacity remain compatible

The supervision boundary SHALL preserve model-facing `spawn_agent` validation, task aliases, response-format shaping, context-key rejection, and the nested depth guard, and SHALL keep the global capacity policy that counts both on-demand and scheduled runs.

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

### Requirement: Owner-scoped retrieval and finish accounting

`get_agent_result` and `wait_for_any_agent` SHALL expose results only to the owning **operator turn**; any other ID SHALL return an error naming the supplied ID and stating that no child is accessible in this turn, without revealing whether another run exists. Normal finish with any unaccounted child, completed-but-unretrieved included, SHALL be refused until each is retrieved or cancelled.

#### Scenario: Finishing with a completed but unread child is refused
- **GIVEN** a turn-owned child has completed but its result has not been retrieved or cancelled
- **WHEN** the operator agent attempts normal finish
- **THEN** the finish is refused with actionable feedback to retrieve or cancel the child

#### Scenario: Cancel-on-timeout accounts only after cancellation request
- **GIVEN** a turn-owned child is awaited through `get_agent_result` with timeout and `cancel_on_timeout=true`
- **WHEN** timeout expires before completion and cancellation is requested
- **THEN** the child is accounted for, but cleanup must finish before the turn closes
- **AND** without an actual cancellation request, timeout alone leaves it unaccounted

### Requirement: Timeout cancellation remains compatible

Timeout cancellation SHALL preserve existing behavior and suppress a later stale notice only for a timeout-cancelled scheduled run; turn-owned children never send independent completion notices. Cross-source cancellation remains an operator `/agents` and `/stop` capability, not a model-facing capability.

#### Scenario: Timeout cancellation remains compatible
- **GIVEN** an operator turn waits for its on-demand child result with a timeout and `cancel_on_timeout` enabled
- **WHEN** the timeout expires before the sub-agent completes
- **THEN** the system cancels the underlying sub-agent run using the same cancellation behavior as before
- **AND** the turn-owned child does not later send an independent completion notification

### Requirement: Context persistence, graph admission, and record lifetime remain compatible

Sub-agent context persistence SHALL keep the same context-key storage behavior, and results SHALL NOT be auto-admitted to graph memory. Turn-owned records remain available for retrieval/accounting beyond registry TTL; a resumed run has a new unique turn owner even if its checkpoint trace is reused.

#### Scenario: Context persistence remains compatible
- **GIVEN** a spawned or scheduled sub-agent uses a valid `context_key`
- **WHEN** the sub-agent run completes or is cleaned up
- **THEN** the system persists the sub-agent short-term context using the same context-key storage behavior as before

#### Scenario: Sub-agent results are not auto-admitted to graph memory
- **GIVEN** a spawned or scheduled sub-agent completes with a result
- **WHEN** the supervisor records and delivers that result
- **THEN** the result is not automatically stored in graph memory
- **AND** any graph-memory admission remains governed by the existing explicit graph-memory tools or policies

### Requirement: Source labels distinguish on-demand and scheduled runs

The supervisor SHALL assign distinct source categories to on-demand and scheduled runs while preserving existing launch and completion contracts. Source labels govern visibility and capacity accounting, not result access or ownership.

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
- **GIVEN** an on-demand supervised run is owned by the calling operator turn
- **WHEN** it completes, fails, or is cancelled and that turn calls `get_agent_result` for its ID
- **THEN** the owning turn receives the terminal status and result, and existing timeout cancellation, context persistence, and graph-memory non-admission behavior remains compatible
- **AND** an on-demand timeout cancellation does not produce an independent completion notification

### Requirement: Supervised submissions carry ownership and bound callbacks

On-demand submissions carry immutable owning **operator turn** identity; scheduled submissions carry job-owned origin without a parent turn. Scheduler completion/result callbacks stay bound to their submitted run, and scheduled notification options are applied to that run's outcome.

#### Scenario: Scheduled completion uses its callback, not model join
- **GIVEN** a scheduled supervised run has scheduler completion and result-logging callbacks associated with its submission
- **WHEN** the run completes, fails, or is cancelled
- **THEN** the associated scheduler callbacks receive that run's outcome
- **AND** the model cannot join the scheduled run through `get_agent_result` or `wait_for_any_agent`
- **AND** any notification presentation follows `scheduled-job-execution`, not an independent supervisor message

### Requirement: Supervised agents use runtime construction

Supervised on-demand and scheduled runs SHALL use the shared runtime construction boundary while preserving existing supervision behavior. Construction policy is selected from immutable run origin, not an ambient executor mode. Scheduled notification options remain associated with the scheduled run; notification presentation follows `scheduled-job-execution`.

#### Scenario: On-demand supervised construction uses runtime
- **GIVEN** the model-facing `spawn_agent` path admits an on-demand run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for on-demand sub-agents
- **AND** lifecycle, registry source, capacity, result retrieval, context persistence, timeout cancellation, and graph-memory non-admission remain compatible

#### Scenario: Scheduled supervised construction uses runtime
- **GIVEN** the scheduler admits a scheduled sub-agent run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for scheduled agents
- **AND** scheduler callbacks, result logging, notification-option association, registry source, and capacity behavior remain compatible
- **AND** notification presentation follows `scheduled-job-execution`

### Requirement: Supervisor records spawned sub-agents against the active prompt

At on-demand submission, the supervisor SHALL record the admitted child ID against the immutable prompt ID explicitly associated with that **operator turn**. It MUST NOT infer the prompt ID from mutable ambient executor fields. Scheduler-owned runs do not become children of the active operator prompt.

#### Scenario: Spawned sub-agent is recorded against the active prompt
- **GIVEN** prompt #8 is active and the main agent spawns sub-agent D
- **WHEN** the supervisor accepts the submission
- **THEN** D's `agent_id` is appended to prompt #8's `sub_agent_ids` list in the registry
- **AND** an update record is appended to `data/prompts.jsonl`

#### Scenario: Scheduler-owned run is not recorded as an operator child
- **GIVEN** an operator prompt is active while the scheduler submits a scheduled run
- **WHEN** the supervisor accepts the scheduled submission
- **THEN** the scheduled run is not appended as a child of the operator prompt

### Requirement: Operator turn owns its on-demand children

An operator-spawned on-demand sub-agent SHALL be admitted with a unique immutable **operator turn** identity as a **turn-owned child**. Only that turn SHALL consume its result via `get_agent_result` or `wait_for_any_agent`; no independent completion Telegram message or automatic result-recall entry SHALL be created. A resumed run, even reusing a checkpoint trace, SHALL get a new turn identity and SHALL NOT inherit prior children or outcomes.

#### Scenario: Parent consumes its own completed child
- **GIVEN** an operator turn spawned on-demand child A
- **WHEN** A finishes and that turn calls `get_agent_result` for A
- **THEN** the turn receives A's terminal status and result
- **AND** A does not independently notify Telegram or enter operator result recall

#### Scenario: Resumed run receives a new owner
- **GIVEN** a run resumes a prior checkpoint trace after an earlier turn spawned child A
- **WHEN** the resumed run is admitted as an operator turn
- **THEN** it receives a unique owner distinct from the earlier turn
- **AND** it neither inherits A nor receives A's outcome in its context

#### Scenario: Non-owned child results cannot be read by the model
- **GIVEN** a model-facing result tool is called in an operator turn with a scheduled, unknown, or other-turn agent ID
- **WHEN** the result is requested through `get_agent_result` or `wait_for_any_agent`
- **THEN** it returns an error naming the supplied ID and saying no child is accessible in this turn, without waiting
- **AND** it does not disclose whether a non-owned run exists or reveal its result

### Requirement: Operator turn ends only after its children are accounted and drained

At normal finish the operator SHALL retrieve or cancel all its admitted children before the turn ends. Owned records SHALL remain available to their turn beyond registry expiry for retrieval/accounting. On stop, error, or exhausted run budget, outstanding children SHALL be cancelled and the turn SHALL remain active until their lifecycle cleanup completes or the fixed drain window (180 s currently) expires; confirmation callbacks and `/stop` MUST remain responsive throughout.

#### Scenario: Finishing with an outstanding child is refused
- **GIVEN** a turn spawned child A, which is still running
- **WHEN** the operator agent attempts its normal finish
- **THEN** the turn remains active and the agent is told to wait for or cancel A
- **AND** no final operator reply is presented as if all work ended

#### Scenario: Wait timeout does not detach a child
- **GIVEN** a turn waits for one of its children and the wait times out without cancellation
- **WHEN** the agent attempts to finish
- **THEN** the still-running child remains that turn's responsibility
- **AND** the finish is refused until the child completes or is cancelled

#### Scenario: Result timeout cancellation accounts for the child
- **GIVEN** an operator turn waits for its own child with `get_agent_result` and `cancel_on_timeout=true`
- **WHEN** the wait times out and cancellation is actually requested
- **THEN** the child is accounted for without retrieving its result
- **AND** the turn remains draining until lifecycle cleanup completes or the drain window expires (see the bounded-drain scenario below)

#### Scenario: Non-cancelling timeout leaves the child outstanding
- **GIVEN** an operator turn waits with `get_agent_result` and `cancel_on_timeout=false`, or uses `wait_for_any_agent`
- **WHEN** the wait times out
- **THEN** the child remains unaccounted and owned by that turn
- **AND** normal finish is refused until its outcome is retrieved or it is cancelled

#### Scenario: Stop drains children without blocking control callbacks
- **GIVEN** an operator turn has two running children, one awaiting operator confirmation
- **WHEN** the operator sends `/stop`
- **THEN** the existing stop behavior requests cancellation of the turn and managed agents
- **AND** the turn remains active until its children finish cleanup or the drain window expires, while Telegram control/confirmation callbacks remain processable

#### Scenario: Operator cancellation accounts for its turn-owned child
- **GIVEN** a turn-owned child has not been accounted for
- **WHEN** the operator cancels it through `/agents`
- **THEN** the child is accounted for in that turn
- **AND** the turn remains draining until lifecycle cleanup completes or the drain window expires (see the bounded-drain scenario below)

#### Scenario: Bounded drain releases a stuck child without hiding it
- **GIVEN** a turn-owned child's lifecycle cleanup has not completed within the fixed drain window
- **WHEN** the drain timeout expires
- **THEN** the operator turn is released with an error naming the stuck child
- **AND** the child's record is retained with a drain-expired marker so later drains do not wait on it again
- **AND** its own late cleanup still finalizes and removes the record

#### Scenario: Owned result remains retrievable beyond completed-record expiry
- **GIVEN** a turn-owned child record has passed registry expiry
- **WHEN** its owning turn retrieves or accounts for the child
- **THEN** the supervisor's per-turn owned record remains available to complete that operation
- **AND** registry expiry alone cannot permanently prevent the turn from finishing

### Requirement: Only turn-owned children may use interactive confirmation

An on-demand child MAY request a confirmation through its own active operator turn's depth-0 coordinator under the existing path policy and grant ledger. Its pending tokens and prompt-scoped grants SHALL expire with that run/turn as prescribed by `approval-grants`. Scheduled sub-agents MUST NOT consult the operator's grant ledger, use the operator's confirmation coordinator, or render confirmation prompts.

#### Scenario: Child confirmation reaches active operator
- **GIVEN** a turn-owned child requests a file operation requiring confirmation
- **WHEN** the path policy permits an operator decision
- **THEN** the operator can approve or deny that request in chat while the parent awaits the child
- **AND** the child proceeds only with the authorised operation and scope

#### Scenario: Scheduled child cannot borrow an active grant
- **GIVEN** an operator turn is active with a session grant
- **WHEN** a scheduled sub-agent attempts a confirmable operation
- **THEN** the operation is denied without a grant lookup or Telegram prompt

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
