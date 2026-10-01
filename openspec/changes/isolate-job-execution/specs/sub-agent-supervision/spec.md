## ADDED Requirements

### Requirement: Operator turn owns on-demand child results and lifetime

An on-demand sub-agent spawned by the operator SHALL be associated with the active **operator turn** at admission as a **turn-owned child**. Only that turn SHALL consume its result through `get_agent_result` or `wait_for_any_agent`; no independent child-completion Telegram message or automatic operator result-recall entry SHALL be created. At normal finish the operator SHALL retrieve or cancel all its admitted children before the turn ends. On stop, error, or exhausted run budget, outstanding children SHALL be cancelled and the turn SHALL remain active until their lifecycle cleanup completes; confirmation callbacks and `/stop` MUST remain responsive throughout.

#### Scenario: Parent consumes its own completed child
- **GIVEN** an operator turn spawned on-demand child A
- **WHEN** A finishes and that turn calls `get_agent_result` for A
- **THEN** the turn receives A's terminal status and result
- **AND** A does not independently notify Telegram or enter operator result recall

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

#### Scenario: Stop drains children without blocking control callbacks
- **GIVEN** an operator turn has two running children, one awaiting operator confirmation
- **WHEN** the operator sends `/stop`
- **THEN** the existing stop behavior requests cancellation of the turn and managed agents
- **AND** the turn remains active until its children finish cleanup while Telegram control/confirmation callbacks remain processable

#### Scenario: Non-owned child results cannot be read by the model
- **GIVEN** a model-facing result tool is called in an operator turn with a scheduled, unknown, or other-turn agent ID
- **WHEN** the result is requested through `get_agent_result` or `wait_for_any_agent`
- **THEN** it returns an error naming the supplied ID and saying no child is accessible in this turn, without waiting
- **AND** it does not disclose whether a non-owned run exists or reveal its result

#### Scenario: Managed cancellation remains broad
- **GIVEN** on-demand and scheduled agents are active
- **WHEN** the operator agent calls `cancel_agent` with a matching ID or `managed`
- **THEN** existing managed-source cancellation behavior is preserved, including scheduled agents
- **AND** cancelling a scheduled agent does not put its result in the operator's tool response

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

## MODIFIED Requirements

### Requirement: Background sub-agent supervision

The system SHALL supervise on-demand and scheduled sub-agent runs through an internal supervision boundary that owns background execution lifecycle while preserving the model-facing `spawn_agent` contract. On-demand children SHALL carry the identity of their spawning operator turn; scheduled runs SHALL carry job-owned origin without acquiring a parent turn.

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

### Requirement: Existing spawn supervision behavior is preserved

The supervision boundary SHALL preserve model-facing spawn validation and response behavior, capacity policy, context persistence, cancellation, and graph-memory non-admission. `get_agent_result` and `wait_for_any_agent` SHALL expose results only to the owning **operator turn**; a non-owned, scheduled, or unknown ID SHALL return an error naming the supplied ID and stating that no child is accessible in this turn, without waiting or revealing whether another run exists. Timeout cancellation SHALL suppress a later stale notice only for a timeout-cancelled scheduled run; turn-owned children never send independent completion notices. When an operator turn attempts normal finish with any unaccounted child, completed-but-unretrieved children included, finish SHALL be refused until each result is retrieved or the child is cancelled. Existing broad managed cancellation remains unchanged.

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
- **GIVEN** an operator turn waits for its on-demand child result with a timeout and `cancel_on_timeout` enabled
- **WHEN** the timeout expires before the sub-agent completes
- **THEN** the system cancels the underlying sub-agent run using the same cancellation behavior as before
- **AND** the turn-owned child does not later send an independent completion notification

#### Scenario: Context persistence remains compatible
- **GIVEN** a spawned or scheduled sub-agent uses a valid `context_key`
- **WHEN** the sub-agent run completes or is cleaned up
- **THEN** the system persists the sub-agent short-term context using the same context-key storage behavior as before

#### Scenario: Sub-agent results are not auto-admitted to graph memory
- **GIVEN** a spawned or scheduled sub-agent completes with a result
- **WHEN** the supervisor records and delivers that result
- **THEN** the result is not automatically stored in graph memory
- **AND** any graph-memory admission remains governed by the existing explicit graph-memory tools or policies

#### Scenario: Finishing with a completed but unread child is refused
- **GIVEN** a turn-owned child has completed but its result has not been retrieved or cancelled
- **WHEN** the operator agent attempts normal finish
- **THEN** the finish is refused with actionable feedback to retrieve or cancel the child

### Requirement: Source-aware supervised runs

The supervisor SHALL assign distinct source categories to on-demand and scheduled runs while preserving existing launch and completion contracts. Source labels govern visibility and capacity accounting, not result access or ownership. On-demand submissions carry immutable owning **operator turn** identity; scheduled submissions carry job-owned origin without a parent turn. Scheduler completion/result callbacks stay bound to their submitted run, and scheduled notification options are applied to that run's outcome.

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

## REMOVED Requirements

### Requirement: Sub-agents share the main agent's per-prompt approval set

**Reason**: The old `auto_approve_tools` mechanism has already been replaced by ADR-0027's depth-0-owned scoped `GrantLedger`; scheduled sub-agents must not consult interactive grants or prompt the operator. This change restricts origin access without changing that ledger's mechanics.

**Migration**: Turn-owned on-demand children use the current depth-0 ledger subject to tool, directory, lifetime and run scope. Scheduled runs deny confirmable operations without grant lookup or prompts. The old per-tool approve-all set is not restored.

### Requirement: Sub-agent confirmation prompts offer per-tool approve-all

**Reason**: Per-tool blanket approval and the associated UI were superseded by ADR-0027's grant-ledger confirmation contract and are incompatible with scheduled runs that must never consult the ledger or prompt.

**Migration**: Interactive file confirmations use `approval-grants`' scoped Confirm / Till `/reset` / Deny controls; scheduled runs return a noninteractive denial. No approve-all button or `auto_approve_tools` state is introduced.
