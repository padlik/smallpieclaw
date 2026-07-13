## Why

`spawn_agent` currently mixes the model-facing tool contract with background runtime supervision: it validates tool arguments, builds context payloads, creates registry records, submits work to a thread pool, saves context, formats notifications, and handles scheduler callbacks. This makes sub-agent spawning difficult to maintain and blocks a safe later split of `builtin_executor.py`.

## What Changes

- Introduce a first-class `SubAgentSupervisor` seam for on-demand and scheduled sub-agent execution.
- Keep `spawn_agent`/`_exec_spawn_agent` as a thin model-facing compatibility shim responsible for argument parsing, aliases, `response_format`, context-key syntax validation, depth-guard/friendly policy errors, and returning an `agent_id`.
- Move background sub-agent lifecycle work out of `builtin_executor._exec_spawn_agent`, including thread-pool submission, `SubAgentRecord` creation, registry register/unregister, result event signaling, context save, scheduler callbacks, notification delivery, and runner cleanup.
- Stop routing scheduler/internal control fields through the model-facing `spawn_agent` argument dictionary. Internal scheduling data must flow through supervisor-owned options instead. The fields removed from the model-facing argument path are `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, and `expandable`.
- Rewire the scheduler to call the supervisor seam with internal supervision options instead of calling `_exec_spawn_agent` with underscore-prefixed control keys.
- Preserve `get_agent_result` as the model-facing rendezvous tool; it may read through the supervisor or the shared registry, but result lookup, blocking, timeout, and cancellation behavior must remain compatible.
- Preserve existing external behavior for normal `spawn_agent` and `get_agent_result` callers, including depth guard, max-subagent cap, context persistence, cancellation/timeout semantics, and result retrieval.
- Leave `PlanExecutor`, runtime profiles, running-agent visibility policy, and broad builtin tool splitting out of scope.

## Capabilities

### New Capabilities

- `sub-agent-supervision`: Defines the internal supervision boundary for spawned and scheduled sub-agent runs, including lifecycle ownership, result delivery, scheduler callback routing, and preserved cancellation/timeout behavior.

### Modified Capabilities

- `sub-agent-context`: Clarifies channel separation for spawned sub-agents: model-facing context payloads remain governed by the existing context rules, while scheduler/internal supervision data must not be delivered through the model-facing `spawn_agent` argument dictionary.

## Impact

- Affected code areas:
  - `builtin_executor.py` sub-agent tools: `spawn_agent` and `get_agent_result`
  - sub-agent lifecycle tracking in `sub_agent_registry.py`
  - scheduler-to-sub-agent launch path in `scheduler.py`
  - sub-agent construction path in `agent_controller.py` and `main.py` only as needed to route through the supervisor seam
  - tests covering spawn, context payloads, context persistence, scheduler fallback/job logging, cancellation, and trace behavior
- No intended changes to:
  - `react_loop.py` reasoning behavior
  - `PlanExecutor` DAG orchestration semantics
  - public tool names
  - normal model-facing `spawn_agent`/`get_agent_result` usage
  - graph-memory admission behavior for sub-agent results

The migration stance for this change is to keep `_exec_spawn_agent` as the compatibility entry point for tool dispatch while moving scheduled-job launches and background lifecycle supervision behind the new supervisor seam. Tests that currently assert scheduler control keys in `_exec_spawn_agent` arguments should migrate to assert that the scheduler passes those controls through internal supervision options, while normal model-facing `spawn_agent` tests continue to exercise the shim.
