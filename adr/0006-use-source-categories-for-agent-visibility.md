# Use source categories for running agent visibility and capacity

## Status

Accepted

## Date

2026-07-13

## Supersedes

None

## Context

ADR-0005 introduced `SubAgentSupervisor` as the supervision boundary while intentionally deferring running-agent visibility and capacity semantics. At that point scheduled runs still reused `source="on-demand"`, and plan-step runners stayed outside the global sub-agent registry.

Future runtime work needs a clear execution visibility model before profiles encode policy. Operators also need `/agents` to show what is actually running, not just on-demand spawned agents.

## Considered Options

- Keep only `on-demand` and preserve scheduled runs as `on-demand` until `AgentRuntime` profiles exist.
- Count every visible agent against the global `max_subagents` limit.
- Introduce explicit source categories for sub-agent executions and separate visibility from global capacity counting.

## Decision

Use explicit `SubAgentRecord.source` categories for visible sub-agent executions: `on-demand`, `scheduled`, `plan-step`, and `diagnostic`.

Make all four categories visible to operators through `/agents`. Count only `on-demand` and `scheduled` records against the existing global `max_subagents` guard. Plan-step and diagnostic records are visible and explicitly cancellable, but their concurrency remains governed by `PlanExecutor` and related plan-local controls.

The main interactive agent is not represented in `SubAgentRegistry` by this decision.

## Consequences

- Good, because `/agents` can display a coherent view of running sub-agent executions.
- Good, because scheduled jobs become distinguishable from on-demand spawned agents.
- Good, because plan-step and diagnostic work becomes operator-visible without changing plan dependency/retry/timeout semantics.
- Good, because capacity-counted cancellation can target the same sources that consume the global supervisor cap.
- Bad, because source names now resemble future runtime profiles and must remain limited to visibility/cap semantics until `AgentRuntime` is introduced.
- Bad, because plan execution must register/unregister records carefully to avoid stale `/agents` entries.
- Neutral, because future `AgentRuntime` profiles may reuse or map to these source categories but are not defined here.
