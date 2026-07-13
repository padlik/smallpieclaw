## Context

`react_loop.py` is already the shared reasoning loop. The remaining runtime problem is construction: main runs, spawned agents, scheduled agents, plan steps, and diagnostics each assemble LLM clients, memory layers, traces, cancel events, prompt variants, and `ReactContext` inputs through scattered code paths.

Previous changes deliberately separated adjacent concerns:

- ADR-0005 established `SubAgentSupervisor` as the background supervision boundary.
- ADR-0006 established `SubAgentRecord.source` as the visibility/capacity category model.
- ADR-0004 requires trace/log identity to remain explicit and correctly bound across thread/executor entries.

This change introduces a construction layer only. It does not absorb supervision, visibility, plan orchestration, or model-facing tools.

Lightweight component view:

```text
                           ┌────────────────────┐
                           │     react_loop      │
                           │ shared core loop    │
                           └──────────▲─────────┘
                                      │ ReactContext
                                      │
                         ┌────────────┴────────────┐
                         │      AgentRuntime        │
                         │ profile + options        │
                         │ -> controller/runner     │
                         └────────────▲────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
┌───────┴────────┐           ┌────────┴────────┐           ┌────────┴────────┐
│ AgentController│           │ SubAgentSupervisor│          │ PlanExecutor    │
│ MAIN frontend  │           │ background owner │           │ DAG owner       │
└────────────────┘           └─────────────────┘           └─────────────────┘
```

## Goals / Non-Goals

**Goals:**

- Introduce `RuntimeProfile`, `RuntimeOptions`, and `AgentRuntime` as the explicit construction boundary for agent executions.
- Centralize LLM client provisioning, per-call model overrides, fallback model trichotomy, memory-layer assembly, trace/cancel wiring, prompt variant selection, and `ReactContext` assembly.
- Preserve the current runner-shaped product surface consumed by `SubAgentSupervisor`, `PlanExecutor`, and tests.
- Preserve graph-memory and strategy-memory post-init wiring and prevent those fields from being silently dropped.
- Preserve `_on_step` ordering so registry iteration tracking installed by `register_run()` is not clobbered by runtime construction.
- Add golden-equivalence tests for runtime-built products/contexts across `MAIN`, `ON_DEMAND_SUBAGENT`, `SCHEDULED_AGENT`, `PLAN_STEP_AGENT`, and `DIAGNOSTIC_AGENT`.

**Non-Goals:**

- Do not change `react_loop.py` reasoning behavior.
- Do not merge `AgentController` and `SubAgentRunner` into one class.
- Do not change `SubAgentSupervisor` lifecycle, thread pool, scheduler callbacks, notification, or cleanup behavior.
- Do not change `SubAgentRegistry.source`, `/agents`, `/status`, or capacity semantics.
- Do not change `PlanExecutor` DAG ordering, retry, diagnostic, timeout, or aggregation semantics.
- Do not change model-facing `spawn_agent` or `get_agent_result` tool shapes.
- Do not split built-in tools in this change.

## Decisions

### 1. Separate runtime profiles from registry sources

`RuntimeProfile` defines construction policy. `SubAgentRecord.source` defines operator visibility and capacity behavior.

Mapping for this change:

| RuntimeProfile | SubAgentRecord.source |
|---|---|
| `MAIN` | none |
| `ON_DEMAND_SUBAGENT` | `on-demand` |
| `SCHEDULED_AGENT` | `scheduled` |
| `PLAN_STEP_AGENT` | `plan-step` |
| `DIAGNOSTIC_AGENT` | `diagnostic` |

Alternative considered: make profile names identical to source names. Rejected because `MAIN` has no source and because construction policy must remain independent from operator visibility/capacity policy.

### 2. Runtime builds existing frontend products, not a new execution engine

`AgentRuntime` should initially build existing frontend products rather than replace them:

- `MAIN` builds or configures an `AgentController`/`ReactContext` path equivalent to today's main run.
- Sub-agent profiles build `SubAgentRunner`-compatible products for supervisor and plan consumers.

The runtime-created sub-agent product must preserve this consumer-facing surface:

```text
.run()
.agent_id
._model_id
._cancel_event
._llm
._agent
._short_term
.close()
.notify_fn
```

Alternative considered: make `AgentRuntime.run(...)` execute everything directly. Rejected because that would blur construction with supervision/orchestration and risk absorbing `SubAgentSupervisor` or `PlanExecutor` responsibilities.

### 3. RuntimeOptions carries construction knobs only

`RuntimeOptions` should cover the knobs currently repeated across factory call sites:

```text
model
fallback_models        # None = inherit, [] = disable, list = explicit fallback chain
max_iterations
max_tokens
temperature
top_p
context_key
context_payload
prompt_variant
trace_id
cancel_event
label
```

Task text and task preview remain caller/supervisor/orchestrator concerns unless a design-time test proves they must be in the runtime request.

`SupervisionOptions` remains owned by `SubAgentSupervisor` and should not be renamed or folded into `RuntimeOptions` in this change.

### 4. Preserve construction variance explicitly

LLM client provisioning:

- Main runs reuse the configured main client.
- Sub-agent profiles build isolated clients from reordered config with the selected model first.
- Per-call overrides must preserve `max_tokens`, `temperature`, `top_p`, `fallback_models`, `cancel_event`, `usage_registry`, and caller tag.

Memory and context layering:

- Main short-term memory remains the controller's existing memory.
- Sub-agent short-term memory is fresh or loaded from `context_key` before construction.
- Working memory remains fresh per controller/sub-agent wrapper.
- Base memory and results memory remain shared.
- Graph memory and strategy memory currently attached after controller construction must be preserved and equivalence-tested.

Trace/cancel/confirmation:

- Trace IDs remain explicit and compatible with ADR-0004.
- Cancel event ownership must preserve existing behavior: owned events may be cleared at run start; forwarded/shared events must not be cleared.
- Confirmation manager wiring remains a controller concern preserved through runtime-built contexts.

### 5. Preserve `_on_step` ordering

`register_run()` assigns `runner._agent._on_step` after a runner has been constructed so registry iteration counts can update while the agent runs. Runtime construction must not overwrite `_on_step` after registry registration.

Implementation should either avoid setting `_on_step` in the runtime or set only an initial/default value before any registry helper wires the final callback.

## Risks / Trade-offs

- [Risk] Runtime abstraction becomes a new mega-runtime -> Mitigation: restrict it to construction and `ReactContext` assembly; keep supervision and orchestration out.
- [Risk] Golden-equivalence misses post-init wiring -> Mitigation: explicitly assert graph memory, strategy memory, prompt variant, context payload, confirmation, trace, cancel, and memory fields.
- [Risk] Runtime construction clobbers `_on_step` after registry registration -> Mitigation: test registry iteration tracking for runtime-built plan/supervised runners.
- [Risk] Model fallback behavior changes -> Mitigation: test `fallback_models` trichotomy (`None`, `[]`, explicit list) and per-call model overrides.
- [Risk] Main profile adoption changes interactive controller behavior -> Mitigation: keep `AgentController` frontend and test existing `resume_*`, cancel, confirmation, and context assembly paths where affected.
- [Risk] Runtime profile names leak into visibility/cap semantics -> Mitigation: maintain explicit profile-to-source mapping and do not alter `SubAgentRegistry` behavior.

## Migration Plan

1. Add `RuntimeProfile`, `RuntimeOptions`, and `AgentRuntime` with no behavior changes.
2. Add profile/default tests and profile-to-source mapping tests.
3. Add golden-equivalence tests around existing construction paths before moving callers.
4. Move sub-agent factory construction into `AgentRuntime.create(profile, options)` while preserving the runner-shaped product surface.
5. Move `spawn_agent`/scheduler, plan-step, and diagnostic construction call sites to use the runtime builder, keeping their existing supervisor/PlanExecutor ownership.
6. Extract `ReactContext` assembly into runtime-owned builder logic and prove main/sub-agent context equivalence.
7. Preserve `AgentController` and `SubAgentRunner` as thin frontends.
8. Run focused runtime/profile/context tests and the standard repository checks.

Rollback strategy: restore call sites to `sub_agent_factory` / direct `SubAgentRunner` construction and remove the runtime builder. No data migration is required.

## Open Questions

- Should the first implementation fully adopt `MAIN`, or should `MAIN` get equivalence tests first and route through runtime in a later step?
- Should `AgentRuntime.create()` return `SubAgentRunner`, `AgentController`, a wrapper object, or profile-dependent products?
- Should LLM client construction live directly inside `AgentRuntime`, or should runtime depend on an injected LLM-client provider?
- Should confirmation mode eventually be derived from `RuntimeProfile`, or only preserved through existing `AgentController` wiring in this change?
