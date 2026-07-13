## Context

The previous `extract-sub-agent-supervisor` change created a dedicated `SubAgentSupervisor` boundary and deliberately preserved historical visibility/cap semantics: both on-demand and scheduled supervised runs still create `SubAgentRecord.source = "on-demand"`, and plan-step runners remain tracked separately inside `PlanExecutor._active_runners`.

That preservation was intentional, but it leaves operator visibility inconsistent:

- `/agents` displays every registered record, but scheduled jobs currently masquerade as managed/on-demand records because their source is not distinct.
- `/status` reports the total active global registry count; making plan-step and diagnostic records registry-visible will intentionally make that count include every visible active sub-agent record.
- `SubAgentRegistry.count_managed()` counts only `source == "on-demand"`, which currently includes scheduled jobs only because scheduled jobs reuse that source value.
- Plan-step and diagnostic sub-agents are not registered in the global sub-agent registry, so they are invisible to `/agents` and cannot be cancelled through the registry even though they consume agent execution resources.
- The main interactive agent is a different lifecycle from sub-agent runs and is not represented by `SubAgentRecord` today.

In-force ADRs reviewed:

- ADR-0003: TOML vault format — not affected.
- ADR-0004: structured-primary logging — compatible; source changes must not replace explicit trace/log propagation.
- ADR-0005: `SubAgentSupervisor` boundary — this change builds on the deferred visibility/cap semantics noted there.

Lightweight component view:

```text
                  ┌─────────────────────────────┐
                  │ SubAgentRegistry             │
                  │ records visible sub-agent    │
                  │ executions + capacity flags  │
                  └──────────────▲──────────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
          │                      │                      │
┌─────────┴─────────┐  ┌─────────┴─────────┐  ┌─────────┴─────────┐
│ SubAgentSupervisor│  │ Scheduler          │  │ PlanExecutor       │
│ on-demand/scheduled│ │ scheduled metadata │  │ plan-step/diagnostic│
└─────────▲─────────┘  └─────────▲─────────┘  └─────────▲─────────┘
          │                      │                      │
          └──────────────┬───────┘                      │
                         │                              │
                  ┌──────┴──────┐                       │
                  │ /agents      │◀──────────────────────┘
                  │ list/cancel  │
                  └─────────────┘
```

## Goals / Non-Goals

**Goals:**

- Define the complete set of `SubAgentRecord.source` values used for visible sub-agent executions.
- Make scheduled sub-agent runs visibly distinct from on-demand runs.
- Make plan-step and diagnostic sub-agents visible in `/agents` while preserving `PlanExecutor` DAG semantics.
- Define which source categories count against the existing `max_subagents` capacity guard.
- Keep operator cancellation behavior coherent for capacity-counted and visible agents.
- Preserve existing `spawn_agent`, scheduler, `get_agent_result`, context persistence, graph-memory, and trace/log behavior.

**Non-Goals:**

- Do not put the main interactive agent into `SubAgentRegistry`; it is not a sub-agent execution and remains surfaced through existing status/run flows.
- Do not introduce `AgentRuntime`, runtime profiles, or a generalized run registry in this change.
- Do not redesign `PlanExecutor` retry/dependency/timeout semantics.
- Do not change model-facing tool names or result shapes.
- Do not split built-in tools.

## Decisions

### 1. Use explicit source categories for sub-agent executions

`SubAgentRecord.source` becomes an explicit category with this closed set for this change:

```text
on-demand   — created by model-facing/root `spawn_agent`
scheduled   — created by scheduler-launched sub-agent jobs
plan-step   — created for normal execution-plan steps
diagnostic  — created for internal diagnostic/recovery sub-agents
```

The main interactive agent is intentionally excluded from this set.

Alternative considered: keep using `source="on-demand"` for scheduled runs until `AgentRuntime` profiles exist. Rejected because it preserves the current ambiguity and makes `/agents` and capacity behavior harder to reason about.

### 2. Separate visibility from capacity counting

Visibility and capacity are related but not identical:

| Source | Visible in `/agents` | Counts against `max_subagents` | Capacity owner |
|---|---:|---:|---|
| `on-demand` | yes | yes | `SubAgentSupervisor` global cap |
| `scheduled` | yes | yes | `SubAgentSupervisor` global cap |
| `plan-step` | yes | no | `PlanExecutor.max_concurrent` |
| `diagnostic` | yes | no | `PlanExecutor` recovery/diagnostic flow |

Rationale:

- On-demand and scheduled runs both use the supervisor pool and should continue to share the existing global `max_subagents` guard.
- Plan steps already have plan-local concurrency controls (`PlanExecutor.max_concurrent`) and should not also consume the supervisor global cap; double-counting would make plan behavior harder to predict and could create unnecessary admission failures.
- Diagnostic agents are internal to plan recovery/debugging and should be visible for operators but governed by the plan/recovery path that spawned them.

Alternative considered: count every visible record against `max_subagents`. Rejected because plan-step and diagnostic executions already have their own plan-local concurrency constraints.

### 3. Make plan and diagnostic runs registry-visible without changing plan semantics

`PlanExecutor` should register plan-step and diagnostic sub-agent records in the same global registry used by `/agents`, and unregister them when their runner completes or is discarded. Registration should be observational and cancellable, not a change to DAG ordering, retry, diagnosis, timeout, or aggregation behavior.

If a plan-step record is cancelled through `/agents`, cancellation should signal the underlying runner's cancellation event/LLM client in the same way as other sub-agent records. `PlanExecutor` should continue to interpret the runner result according to its existing failure/cancellation handling.

Diagnostic/recovery registration must wrap the synchronous diagnostic `runner.run(...)` path in `try/finally` so records are unregistered even when diagnostic creation or execution fails and the diagnostic error is swallowed by the recovery path.

Alternative considered: leave plan-step agents invisible and wait for `AgentRuntime`. Rejected because invisibility is an operator-facing gap independent of runtime construction.

### 4. Keep `/agents cancel managed` as capacity-scope cancellation

The existing `/agents cancel managed` command should continue to cancel records that count against the global `max_subagents` guard. After this change, that means `on-demand` and `scheduled` records.

`/agents cancel <id-or-label>` remains available for every visible source category, including `plan-step` and `diagnostic`.

`/agents` display should show source/category labels directly rather than mapping everything into `[managed]` or `[autonomous]`.

Alternative considered: make `managed` mean only `on-demand`. Rejected because scheduled runs still count against the global cap in this design, so excluding them from bulk capacity cancellation would make cap relief less predictable.

### 5. Keep compatibility surfaces stable

The model-facing `spawn_agent` and `get_agent_result` contracts stay stable. Existing context persistence, timeout cancellation, graph-memory non-admission, and scheduler callback delivery behavior must remain compatible.

## Risks / Trade-offs

- [Risk] Plan-step registration changes cancellation timing or plan result interpretation -> Mitigation: keep registration observational, preserve `PlanExecutor` result handling, and test cancellation/timeout paths.
- [Risk] Scheduled source retagging changes cap behavior accidentally -> Mitigation: add explicit capacity-source tests showing `scheduled` counts against `max_subagents`.
- [Risk] `/agents cancel managed` becomes surprising if it cancels scheduled runs -> Mitigation: update `/agents` help text to explain that managed means global-cap-counted sources.
- [Risk] Registry records outlive completed plan steps -> Mitigation: register/unregister in `try/finally` around runner execution and add cleanup tests.
- [Risk] `/status` active-agent count changes when plan-step and diagnostic records become registry-visible -> Mitigation: document and test that `/status` reflects all visible active sub-agent records, not only global-cap-counted records.
- [Risk] This creates profile-like source names before `AgentRuntime` exists -> Mitigation: keep source categories limited to visibility/cap behavior; do not move construction or prompt policy into this change.

## Migration Plan

1. Add source-category constants/helpers to `sub_agent_registry.py`, including helpers for visible records and global-cap-counted records.
2. Update `SubAgentSupervisor` submission requests/options so on-demand and scheduled launches assign distinct `source` values while preserving global cap behavior for both.
3. Update registry count/cancel helpers so global-cap-managed records are `on-demand` + `scheduled`.
4. Register plan-step and diagnostic runners in `PlanExecutor` with `source="plan-step"` and `source="diagnostic"`, sharing cancel-event/LLM-client wiring and unregistering in existing cleanup paths.
5. Update `/agents` list/cancel/help text to display explicit source categories and explain managed/cap-counted cancellation; update `/status` expectations so its active-agent count includes visible registry records.
6. Add tests for source assignment, cap counting, `/agents` display/cancel behavior, `/status` count behavior, plan-step visibility, diagnostic visibility/cleanup on diagnostic failure, and plan cancellation compatibility.
7. Run focused registry/supervisor/scheduler/plan/Telegram command tests and the standard repository checks.

Rollback strategy: restore scheduled source assignment to the previous `on-demand` value, remove plan/diagnostic registry registration, and restore prior `/agents` display/cancel helpers. No data migration is required because records are in-memory runtime state.

## Open Questions

- Exact operator-facing labels/icons for each `/agents` source category can be finalized during specs/tasks.
- Whether future `AgentRuntime` profiles should reuse these source names or introduce separate profile names is intentionally deferred to `introduce-agent-runtime`.
