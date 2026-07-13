## Why

Agent execution now has separate supervision and visibility boundaries, but construction remains duplicated across `AgentController`, `SubAgentRunner`, `main.py`'s `sub_agent_factory`, `BuiltinExecutor.spawn_agent`, and `PlanExecutor`. Centralizing construction will reduce drift before the later builtin-tool split and make future runtime profiles explicit without changing execution behavior.

## What Changes

- Introduce an `AgentRuntime` construction layer with `RuntimeProfile` and `RuntimeOptions`.
- Centralize LLM client provisioning, per-call model overrides, fallback model handling, memory-layer assembly, trace/cancel wiring, prompt variant selection, and `ReactContext` assembly behind the runtime builder.
- Keep `AgentController` and `SubAgentRunner` as thin frontends over the shared construction path rather than merging them into one mode-flag class.
- Route `spawn_agent`, scheduler-launched agents, plan-step agents, and diagnostic agents through the runtime construction path while preserving their existing supervision/orchestration owners.
- Preserve the distinction between `RuntimeProfile` and `SubAgentRecord.source`: profiles define construction policy, while sources define visibility and capacity semantics.
- Add golden-equivalence tests proving runtime-built contexts/runners match the current behavior for main, on-demand, scheduled, plan-step, and diagnostic profiles.
- Leave `SubAgentSupervisor` lifecycle, `SubAgentRegistry` source/cap behavior, `PlanExecutor` DAG semantics, model-facing tool schemas, and builtin-tool splitting out of scope.

## Capabilities

### New Capabilities

- `agent-runtime-construction`: Defines profile-based construction of agent executions, runtime options, and equivalence requirements for building controllers, sub-agent runners, and ReAct contexts.

### Modified Capabilities

- `sub-agent-supervision`: Clarifies that supervised on-demand and scheduled agents use the shared runtime construction path while preserving supervision behavior.
- `execution-planning`: Clarifies that plan-step and diagnostic agents use the shared runtime construction path while preserving DAG, retry, diagnostic, timeout, and result aggregation behavior.

## Impact

- Affected code areas:
  - `agent_controller.py` `AgentController.run`, `SubAgentRunner.__init__`, and `SubAgentRunner.run`
  - `main.py` `sub_agent_factory` and post-init controller wiring
  - `builtin_executor.py` `spawn_agent` construction request assembly
  - `sub_agent_supervisor.py` submission request consumption only as needed to call the runtime-built runner
  - `execution_plan.py` plan-step and diagnostic runner creation
  - tests covering context construction, model index restoration, trace/cancel propagation, memory wiring, prompt variants, and runtime/profile equivalence
- No intended changes to:
  - `react_loop.py` reasoning behavior
  - model-facing `spawn_agent` or `get_agent_result` tool names/result shapes
  - `SubAgentSupervisor` background lifecycle
  - `SubAgentRegistry` source/visibility/capacity semantics
  - `PlanExecutor` orchestration semantics
  - builtin tool package layout
