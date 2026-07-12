## Context

`react_loop.py` is already the shared reasoning loop for main agents, spawned sub-agents, scheduled jobs, and plan steps. The maintenance problem is the launch and supervision envelope around sub-agents: `builtin_executor._exec_spawn_agent` currently mixes model-facing tool argument handling with thread-pool submission, `SubAgentRecord` lifecycle, registry updates, scheduler callback routing, context persistence, notification delivery, and cleanup.

The current scheduler path also passes internal callback data through the same argument dictionary used for model-facing `spawn_agent` calls. That includes `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, and `expandable`. These values are runtime supervision controls, not tool contract inputs.

In-force ADRs checked before this design:

- ADR-0003: Use TOML for agent-scoped vault files — not affected.
- ADR-0004: Use structlog for structured-primary agent logging — compatible; this change must preserve existing trace/log behavior and avoid relying on process-global trace state for correctness.

Lightweight component view:

```text
┌──────────────────────────────┐
│ react_loop                    │
│ shared reasoning/tool loop    │
└───────────────▲──────────────┘
                │
┌───────────────┴──────────────┐
│ SubAgentRunner                │
│ runs one sub-agent task       │
└───────────────▲──────────────┘
                │
┌───────────────┴──────────────┐
│ SubAgentSupervisor            │
│ register, submit, signal,     │
│ notify, log, save, cleanup    │
└───────────────▲──────────────┘
                │
       ┌────────┴────────┐
       │                 │
┌──────┴──────┐   ┌──────┴──────┐
│ spawn_agent │   │ Scheduler   │
│ tool shim   │   │ job launch  │
└─────────────┘   └─────────────┘
```

Dynamic view for on-demand spawn:

```text
LLM -> spawn_agent shim -> SubAgentSupervisor.submit
  -> create/register SubAgentRecord
  -> submit background run
  -> return agent_id

background run:
  SubAgentRunner.run -> AgentController.run -> react_loop
  -> save context if needed
  -> set record status/result/event
  -> notify/log if configured
  -> unregister/close/cleanup
```

Dynamic view for scheduled job launch:

```text
Scheduler job fire
  -> construct Runtime/Supervision request
  -> SubAgentSupervisor.submit with job callbacks/options
  -> background run lifecycle identical to spawned sub-agent
  -> result_log_cb and finish_cb invoked from supervisor-owned options
```

## Goals / Non-Goals

**Goals:**

- Make `SubAgentSupervisor` the first-class owner of background sub-agent supervision.
- Keep `spawn_agent`/`_exec_spawn_agent` as the model-facing compatibility shim for normal tool dispatch.
- Move scheduler/internal control fields out of model-facing `spawn_agent` args and into supervisor-owned options.
- Preserve existing behavior for depth guard, max-subagent cap, registry source values, context-key validation, response format handling, context persistence, result retrieval, notification suppression after timeout, and cancellation/timeout behavior.
- Preserve per-run scheduler callback isolation so concurrent scheduled jobs cannot overwrite each other's finish/result callbacks.
- Keep `react_loop.py` untouched except for incidental call-site compatibility if unavoidable.

**Non-Goals:**

- Do not introduce `AgentRuntime`, runtime profiles, or a generalized `RunHandle` abstraction in this change.
- Do not change `PlanExecutor` DAG orchestration, retry, diagnosis, deadline, or aggregation behavior.
- Do not decide whether plan steps, scheduled jobs, or the main agent should appear in `/agents`.
- Do not change `max_subagents` semantics beyond preserving current on-demand behavior.
- Do not split all built-in tools into a package.
- Do not support nested sub-agents or depth greater than 1.

## Decisions

### 1. Extract a supervisor seam, not a full runtime

Create a `SubAgentSupervisor` component responsible for the background lifecycle currently embedded in `_exec_spawn_agent`'s `_run_and_notify` closure.

The supervisor owns:

- thread-pool submission
- `SubAgentRecord` creation and registration
- synchronous `agent_id` minting before returning to the caller
- background execution via `SubAgentRunner.run(task)`
- context save after run and during cleanup
- `record.status`, `record.result`, and completion event signaling
- stale-notification suppression after timeout cancellation
- scheduler result logging and finish callbacks
- registry unregister and runner close

Alternative considered: introduce `AgentRuntime` first. Rejected for this change because it would mix construction refactoring with supervision extraction and widen the risk surface.

### 2. Keep `_exec_spawn_agent` as the compatibility shim

`_exec_spawn_agent` remains the model-facing tool-dispatch entry point. It should continue to own model-facing validation and LLM-friendly responses:

- task alias handling: `task`, `prompt`, `goal`, `description`
- required task validation
- `response_format` instruction shaping
- caller-depth guard and friendly policy error
- context-key syntax validation before supervisor submission
- max-subagents cap behavior for normal on-demand spawns
- translation from validated args to a supervisor submission request

The synchronous boundary is: `supervisor.submit(...)` must create/register the record and return an `agent_id` before `_exec_spawn_agent` returns. Only the actual agent run is backgrounded.

Alternative considered: make callers bypass `_exec_spawn_agent` immediately. Rejected because normal model-facing tests and tool dispatch should keep a stable compatibility entry point during this change.

### 2a. Preserve current cap and record-source semantics while moving scheduler launch

Although scheduler launches should stop carrying internal control fields through `_exec_spawn_agent` args, they must not accidentally bypass the existing max-subagents behavior in this change. Today scheduled jobs reach the same `_exec_spawn_agent` cap check before the background run is submitted, and records created through that path use `source="on-demand"`. Because running-agent visibility and cap semantics are deferred to `unify-running-agent-visibility`, this change must preserve that behavior exactly unless a test proves otherwise.

The supervisor seam therefore needs a shared pre-submit capacity check usable by both `_exec_spawn_agent` and the scheduler launch path, or an equivalent scheduler adapter that performs the same check before submission. The supervisor must also preserve the current `SubAgentRecord.source` value used by spawned/scheduled jobs so `count_managed()` behavior does not change as a side effect.

Alternative considered: tag scheduled records as `source="scheduled"` and exempt scheduled jobs from the cap. Rejected for this change because that would decide the deferred running-agent visibility/cap policy.

### 3. Route scheduler controls through supervision options

The scheduler should no longer call `_exec_spawn_agent` with `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, or `expandable` in the args dictionary. Instead, scheduler launches should call a supervisor-facing method or adapter with explicit supervision options.

Those options must be per-submission values, not shared mutable supervisor attributes. This preserves the current race-avoidance rationale where callbacks are passed per call so concurrent scheduled jobs cannot overwrite each other. If the existing shared `_scheduler_finish_cb` fallback is still wired in production, the migration should either translate it into a per-submission option at the scheduler boundary or remove it only after confirming no current composition path depends on it.

Alternative considered: keep legacy underscore keys as a private convention. Rejected because it keeps internal runtime callbacks in the model-facing argument channel and prevents a clean tool/runtime boundary.

### 4. Preserve registry-backed result rendezvous

`get_agent_result` remains the model-facing rendezvous tool. It may continue to read from the shared registry directly or delegate through the supervisor, but its observable behavior must stay compatible:

- unknown agent IDs return the same style of recoverable error
- waiting honors the requested timeout
- timeout with `cancel_on_timeout=True` cancels the underlying run
- completed records return status, result, and result type consistently

Alternative considered: introduce a new `RunHandle` abstraction. Deferred to the later AgentRuntime/profile change.

### 5. Preserve construction path for now

The existing `sub_agent_factory`/`SubAgentRunner` construction path remains in place. The supervisor receives or uses the same factory rather than replacing runner construction. Context payload auto-summary behavior remains unchanged.

Alternative considered: move context payload construction into a runtime builder. Deferred to the later AgentRuntime change.

## Risks / Trade-offs

- [Risk] Threaded lifecycle regressions around completion events, cancellation, and cleanup -> Mitigation: preserve event/cancel wiring, add focused supervisor lifecycle tests, and keep cancellation/timeout tests green.
- [Risk] Scheduled job callbacks race if moved to shared supervisor attributes -> Mitigation: model scheduler metadata as per-submission supervision options.
- [Risk] Scheduler launches bypass the existing max-subagents cap or change `SubAgentRecord.source` values -> Mitigation: preserve the current pre-submit cap check and current source value semantics until the later visibility/cap policy change.
- [Risk] Normal `spawn_agent` callers observe behavior changes -> Mitigation: keep `_exec_spawn_agent` as compatibility shim and preserve output shape, errors, depth guard, response format, and `agent_id` behavior.
- [Risk] Tests currently assert private scheduler keys inside `_exec_spawn_agent` args -> Mitigation: migrate those tests to assert scheduler controls are delivered through supervision options while model-facing spawn tests continue through the shim.
- [Risk] The supervisor becomes a new mini-runtime -> Mitigation: limit it to background supervision only; do not add AgentRuntime/profile/context-builder responsibilities in this change.
- [Risk] Logging/trace identity is lost across background threads -> Mitigation: preserve existing explicit trace propagation and structured logging behavior; do not introduce new correctness dependence on ambient context.

## Migration Plan

1. Add `SubAgentSupervisor` and any small submission/options data structure needed to represent per-run supervision controls.
2. Move the `_run_and_notify` lifecycle into the supervisor while preserving existing registry, result event, context save, notification, timeout, and cleanup semantics.
3. Keep `_exec_spawn_agent` as a thin compatibility shim that validates model-facing args and delegates to the supervisor.
4. Rewire scheduler launches to call the supervisor seam with supervision options instead of passing underscore-prefixed control fields through `_exec_spawn_agent` args, while preserving the same capacity check and record-source behavior that scheduler launches currently receive through `_exec_spawn_agent`.
5. Keep `get_agent_result` behavior compatible by reading from the same registry or a supervisor-mediated equivalent.
6. Update tests that asserted scheduler control keys in spawn args to assert the new internal supervision path.
7. Run focused spawn/scheduler/context/cancellation tests, then the repository checks.

Rollback strategy: because this is an internal refactor, rollback is restoring the prior `_exec_spawn_agent` lifecycle path and scheduler `_exec_spawn_agent` invocation. The proposal requires no data migration.

## Open Questions

- Should plan-step agents appear in `/agents`? Deferred to `unify-running-agent-visibility`.
- Should scheduled agents or plan steps count against `max_subagents`? Deferred to `unify-running-agent-visibility`.
- Should the main agent appear in the same run registry? Deferred to `unify-running-agent-visibility`.
- Should `depth` and confirmation mode be entirely derived from future runtime profiles? Deferred to `introduce-agent-runtime`.
- Should `get_agent_result` eventually wait on a `RunHandle` instead of `SubAgentRecord`? Deferred to `introduce-agent-runtime`.
- No in-force ADR needs supersession for this change.
