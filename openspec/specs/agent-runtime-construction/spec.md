# agent-runtime-construction Specification

## Purpose
Define the runtime construction boundary that builds agent execution products and per-run contexts from explicit runtime profiles and runtime options.

## Requirements
### Requirement: Profile-based runtime construction
The system SHALL provide a runtime construction boundary that builds agent execution products or per-run contexts from an explicit runtime profile and runtime options.

#### Scenario: Runtime profiles are distinct from visibility sources
- **GIVEN** the runtime constructs an agent execution using a runtime profile
- **WHEN** the execution is later exposed through the running-agent registry
- **THEN** the profile determines construction policy
- **AND** any registry source remains the visibility and capacity category, not the construction profile itself

#### Scenario: Supported runtime profiles exist
- **GIVEN** the runtime construction boundary is available
- **WHEN** callers request `MAIN`, `ON_DEMAND_SUBAGENT`, `SCHEDULED_AGENT`, `PLAN_STEP_AGENT`, or `DIAGNOSTIC_AGENT`
- **THEN** sub-agent profiles have defined construction defaults for depth, prompt variant, memory layering, trace/cancel behavior, and model configuration
- **AND** the `MAIN` profile has defined per-run context assembly semantics while top-level main controller construction remains outside this change

### Requirement: Runtime-built products preserve consumer surface
Runtime-built sub-agent products SHALL preserve the surface consumed by existing supervisors, plan orchestration, and registry helpers without requiring callers to depend on a concrete class.

#### Scenario: Product surface remains compatible
- **GIVEN** `SubAgentSupervisor` or `PlanExecutor` receives a runtime-built sub-agent product
- **WHEN** it runs, registers, cancels, or cleans up that product
- **THEN** the product supports `.run()`, `.agent_id`, `._model_id`, `._cancel_event`, `._llm`, `._agent`, `._short_term`, `.close()`, and `.notify_fn`

### Requirement: Runtime context equivalence
Runtime construction and per-run context assembly SHALL be behavior-preserving and verified by golden-equivalence tests over generated controller, runner, and `ReactContext` state.

#### Scenario: Main context equivalence
- **GIVEN** a main agent execution uses runtime-owned per-run context assembly
- **WHEN** its `ReactContext` is compared to the legacy context assembly path
- **THEN** equivalent fields are present for LLM, tool registry, builtin executor, memory, working memory, results memory, confirmation, trace, cancel ownership, graph memory, strategy memory, and iteration limits

#### Scenario: Sub-agent context equivalence
- **GIVEN** an on-demand, scheduled, plan-step, or diagnostic sub-agent execution is constructed through the runtime
- **WHEN** its runner and `ReactContext` state are compared to the legacy construction path
- **THEN** equivalent fields are present for depth, prompt variant, context payload, short-term memory source, working memory, model configuration, trace, cancel event, and callbacks

#### Scenario: Post-init graph and strategy memory are preserved
- **GIVEN** graph memory or strategy memory is wired onto a controller after construction today
- **WHEN** the runtime constructs the equivalent execution path
- **THEN** those fields remain visible to the resulting `ReactContext`

#### Scenario: Step callback ordering is preserved
- **GIVEN** registry helpers install an `_on_step` callback on a runtime-built sub-agent product after construction
- **WHEN** the product runs and reports iterations
- **THEN** runtime construction does not overwrite the registry-installed callback

### Requirement: Runtime options preserve supported construction values

Runtime options SHALL accept the immutable run target produced by direct admission — an explicit selector resolved against the fixed connection map, or a snapshot of the active main selection when no selector is given — with the existing supported context, prompt, and run options. Construction SHALL NOT re-admit, re-resolve credentials, or consult the catalog cache. Runtime options SHALL reject per-call `max_tokens`, `temperature`, and `top_p` before model admission or provider I/O.

#### Scenario: Model override is preserved
- **GIVEN** direct admission resolves an explicit selector for a configured connection, even when the model is not cached and no main selection exists
- **WHEN** runtime constructs a sub-agent product
- **THEN** it uses that immutable admitted target for its full run
- **AND** token usage accounting and caller tagging remain equivalent to the legacy path

#### Scenario: Inherited target is snapshotted once
- **GIVEN** a sub-agent is requested without a selector while a main selection is active
- **WHEN** the main selection changes after admission
- **THEN** the constructed product keeps the target snapshotted at admission

#### Scenario: Per-call generation overrides are rejected
- **GIVEN** a caller supplies `max_tokens`, `temperature`, or `top_p`
- **WHEN** builtin argument validation runs
- **THEN** it returns an actionable obsolete-argument error before admission
- **AND** it neither ignores nor forwards the override

#### Scenario: Context and prompt options are preserved
- **GIVEN** a caller provides `context_key`, `context_payload`, `prompt_variant`, `trace_id`, `cancel_event`, `label`, and `max_iterations`
- **WHEN** the runtime constructs an execution product
- **THEN** the supported values are applied according to the existing profile
- **AND** runtime/profile, supervision, callback, and context ownership remain unchanged

#### Scenario: Explicit target needs no main selection or catalog membership
- **GIVEN** an explicit selector names a configured connection
- **WHEN** runtime construction receives its admitted target
- **THEN** construction SHALL NOT require an active main selection or catalog membership

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

### Requirement: Launcher cleanup uses the local operation's primary

Plan and diagnostic cleanup SHALL receive an explicitly identified exception escaping the protected local operation. An unrelated caller handler or an error already handled into an outcome SHALL NOT establish a primary. Remaining cleanup SHALL be attempted before propagating a sole cleanup cancellation; a genuine local primary SHALL retain identity.

#### Scenario: Launcher cleanup runs inside an unrelated handler
- **GIVEN** a plan or diagnostic operation finishes without an escaping local error while its caller handles an unrelated exception
- **WHEN** a cleanup stage raises a cancellation
- **THEN** remaining tracking cleanup and runner close are attempted once
- **AND** the cleanup cancellation propagates rather than being suppressed by the caller's unrelated exception

#### Scenario: Execution and cleanup both raise cancellation
- **GIVEN** plan or diagnostic execution raises a cancellation escaping its local operation
- **WHEN** a cleanup stage also raises cancellation
- **THEN** every remaining cleanup stage is attempted once
- **AND** the exact original execution cancellation remains primary

#### Scenario: A handled diagnostic failure does not suppress cleanup cancellation
- **GIVEN** a diagnostic execution error has been handled into an unavailable-analysis outcome
- **WHEN** cleanup raises a cancellation
- **THEN** remaining cleanup and runner close are attempted once
- **AND** the cleanup cancellation propagates because the handled error is not an escaping local primary
