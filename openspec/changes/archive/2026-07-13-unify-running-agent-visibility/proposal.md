## Why

Sub-agent supervision now has a clear lifecycle boundary, but running-agent visibility and capacity semantics are still historical: scheduled runs currently reuse `source="on-demand"`, plan-step agents are managed separately, and `/agents` cannot present a coherent model of all agent executions. This change will define and implement the visibility/cap-counting policy before the later `AgentRuntime` work codifies execution profiles.

## What Changes

- Define the authoritative set of running-agent source categories, expected to include `on-demand`, `scheduled`, `plan-step`, and `diagnostic`.
- Decide which execution types appear in `/agents`, including scheduled agents and plan-step agents.
- Decide which execution types count against `max_subagents`, including scheduled jobs and plan steps.
- Update `SubAgentRecord.source` assignment so scheduled jobs no longer rely on ambiguous `on-demand` source semantics if the new policy calls for distinct scheduled visibility.
- Update `/agents` display, filtering, cancellation, and managed-count behavior to reflect the chosen source/cap policy.
- Keep the previous sub-agent supervision behavior stable: `spawn_agent`, scheduler execution, `get_agent_result`, timeout cancellation, and context persistence remain compatible.
- Leave generalized `AgentRuntime` profiles and broad builtin-tool splitting out of scope.

## Capabilities

### New Capabilities

- `running-agent-visibility`: Defines which active agent executions are visible to operators, how they are categorized, and which categories count toward capacity limits.

### Modified Capabilities

- `sub-agent-supervision`: Updates source assignment and capacity semantics for on-demand, scheduled, plan-step, and diagnostic sub-agent runs while preserving existing supervision behavior.
- `execution-planning`: Clarifies whether plan-step sub-agents are registered, visible, cancellable, and capacity-counted during DAG execution.
- `telegram-command-surface`: Updates `/agents` operator-facing behavior so visible running agents are displayed with distinct source/category information.

## Impact

- Affected code areas:
  - `sub_agent_registry.py` source/category modeling, managed counts, and cancellation helpers
  - `sub_agent_supervisor.py` source assignment for on-demand and scheduled runs
  - `scheduler.py` scheduled launch options and visibility metadata
  - `execution_plan.py` plan-step and diagnostic runner registration/visibility if included by the chosen policy
  - Telegram `/agents` command formatting/cancellation paths and `/status` active-agent count surface
  - tests for registry counting, `/agents` output, scheduler runs, and plan execution
- No intended changes to:
  - `react_loop.py` reasoning behavior
  - model-facing `spawn_agent` or `get_agent_result` tool names and result shapes
  - context persistence and graph-memory admission behavior
  - full `AgentRuntime` profile construction
