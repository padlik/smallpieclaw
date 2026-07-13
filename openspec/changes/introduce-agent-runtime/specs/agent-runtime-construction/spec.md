## ADDED Requirements

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

### Requirement: Runtime options preserve construction knobs
The runtime SHALL accept per-execution options for construction values currently duplicated across agent construction call sites.

#### Scenario: Model override and fallback trichotomy are preserved
- **GIVEN** a caller provides a model override and fallback model option
- **WHEN** the runtime constructs a sub-agent product
- **THEN** `fallback_models = null` inherits configured fallbacks
- **AND** `fallback_models = []` disables fallback inheritance
- **AND** a non-empty fallback list becomes the explicit fallback chain

#### Scenario: Per-call LLM overrides are preserved
- **GIVEN** a caller provides `max_tokens`, `temperature`, or `top_p` overrides
- **WHEN** the runtime constructs a sub-agent product
- **THEN** the constructed execution uses those overrides without mutating shared model configuration
- **AND** token usage accounting continues to use the configured usage registry
- **AND** caller tagging remains equivalent to the legacy construction path

#### Scenario: Context and prompt options are preserved
- **GIVEN** a caller provides `context_key`, `context_payload`, `prompt_variant`, `trace_id`, `cancel_event`, `label`, and `max_iterations`
- **WHEN** the runtime constructs an execution product
- **THEN** those values are applied to the product or generated context according to the selected profile

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
