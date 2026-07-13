# Explore Brief: Introduce Agent Runtime

## Decision Direction

Introduce a focused `AgentRuntime` construction layer: `RuntimeProfile` + `RuntimeOptions` + an `AgentRuntime` builder that centralizes agent/sub-agent construction and `ReactContext` assembly without changing supervision, running-agent visibility, `PlanExecutor` orchestration semantics, model-facing tool shapes, or built-in tool packaging.

## Rejected Alternatives

- **Merge `AgentController` and `SubAgentRunner` into one mode-flag class** — rejected because interactive controller behavior and sub-agent wrapper behavior remain distinct frontends.
- **Fold `SubAgentSupervisor` into `AgentRuntime`** — rejected because supervision lifecycle is already settled and should remain separate from construction.
- **Fold `PlanExecutor` into `AgentRuntime`** — rejected because DAG ordering, retries, diagnostics, timeout, and aggregation are orchestration concerns.
- **Make `RuntimeProfile` identical to `SubAgentRecord.source`** — rejected because source is visibility/capacity semantics, while profile is construction policy. They should be explicitly mapped, not conflated.
- **Split built-in tools in this change** — rejected; this change prepares the runtime construction seam needed before the later builtin split.

## Final Profile and Source Mapping

```text
RuntimeProfile          SubAgentRecord.source
MAIN                    none
ON_DEMAND_SUBAGENT      on-demand
SCHEDULED_AGENT         scheduled
PLAN_STEP_AGENT         plan-step
DIAGNOSTIC_AGENT        diagnostic
```

`RuntimeProfile` owns construction defaults and policy: depth, prompt variant defaults, model/default/fallback behavior, memory layering, context payload, trace/cancel wiring, and confirmation wiring.

`SubAgentRecord.source` remains the operator visibility/capacity category established by `unify-running-agent-visibility`.

## Runtime Options Dimensions

Runtime options must cover every currently duplicated factory/construction knob:

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
task / task preview when needed by caller
```

## Construction Variance to Preserve

### LLMClient provisioning

- Main agent reuses its configured client.
- Sub-agent profiles build isolated LLM clients from reordered config with chosen model first.
- Per-call overrides must be preserved: `max_tokens`, `temperature`, `top_p`, `fallback_models`, `cancel_event`, `usage_registry`, and caller tag.

### Memory and post-init wiring

- Main short-term memory is the controller's existing memory.
- Sub-agent short-term memory is fresh or loaded from `context_key` before construction.
- Working memory is fresh per controller/sub-agent run wrapper.
- Base memory and results memory are shared.
- Graph memory and strategy memory are currently post-init controller attributes and must not be silently dropped.

### Step callback ordering

- `register_run()` assigns `runner._agent._on_step` for registry iteration tracking.
- Runtime construction must not clobber this callback after registry registration.

## Cross-Module Data Flow

Target flow:

```text
spawn_agent / scheduler / PlanExecutor
  -> RuntimeProfile + RuntimeOptions
  -> AgentRuntime.create(...)
  -> runner-shaped product or ReactContext-compatible controller
  -> existing SubAgentSupervisor or PlanExecutor orchestration
  -> react_loop unchanged
```

Compatibility constraint: runtime-created sub-agent products must remain compatible with current consumers:

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

## Known Open Questions

- Should `AgentRuntime.create()` return a `SubAgentRunner`, an `AgentController`, a `ReactContext`, or a small wrapper around those?
- Should `AgentRuntime` own LLMClient construction directly or receive an injected provider/builder?
- Should `task` be part of `RuntimeOptions`, or remain owned by caller/supervisor/orchestrator?
- Where should model `_active_idx` save/restore live?
- Should MAIN profile adoption happen in the first implementation, or should the first implementation prove sub-agent profile equivalence only?
- Should confirmation mode be derived from profile in this change, or only preserved through existing `AgentController` wiring?
