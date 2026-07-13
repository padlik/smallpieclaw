# Explore Brief: Extract Sub-Agent Supervisor

## Decision Direction

The first change should focus narrowly on extracting sub-agent supervision out of `builtin_executor._exec_spawn_agent`, without introducing `AgentRuntime` profiles yet and without splitting all built-in tools.

Final direction:

```text
Change 1: extract-sub-agent-supervisor
Change 2: unify-running-agent-visibility
Change 3: introduce-agent-runtime
Change 4: split-builtin-tools
```

## Rejected Alternatives

- **One large AgentRuntime + builtin split change** — rejected as too broad and too risky for the load-bearing spawn path.
- **Split `builtin_executor.py` first** — rejected because `spawn_agent` would drag its mini-runtime into a new file.
- **Absorb `PlanExecutor` into one supervisor** — rejected because DAG execution has distinct orchestration semantics: dependencies, parallel batches, retries, diagnostics, deadlines, and aggregation.
- **Introduce profiles before registry semantics are settled** — deferred because visibility/cap behavior should be decided before profiles encode it.

## Final Boundary for Change 1

`spawn_agent` remains the model-facing tool shim. It should keep argument parsing, aliases, `response_format`, friendly LLM-facing errors, and `agent_id` return behavior.

`SubAgentSupervisor` should own background sub-agent lifecycle concerns:

- thread-pool submission
- `SubAgentRecord` creation
- registry register/unregister
- background run lifecycle currently in `_run_and_notify`
- result event signaling
- context save after run
- notification delivery
- scheduler result/finish callbacks
- cleanup and runner close

Scheduler/internal control data must move out of the LLM-facing args dict and into supervisor-owned options:

```text
_job_tag
_finish_cb
_result_log_cb
_notify
expandable
```

## Cross-Module Data Flow

Current target flow:

```text
spawn_agent tool args
  -> validate model-facing contract
  -> build Runtime/Supervision request
  -> SubAgentSupervisor.submit(...)
  -> SubAgentRunner.run(...)
  -> AgentController.run(...)
  -> react_loop(...)
  -> supervisor records/delivers/cleans up
```

Scheduler should call the supervisor seam directly rather than smuggling private callback keys through `_exec_spawn_agent` args.

PlanExecutor should remain out of scope for Change 1, except that later changes may reuse shared registry/runtime primitives.

## Open Questions Deferred

- Should plan-step agents appear in `/agents`?
- Should scheduled agents or plan steps count against `max_subagents`?
- Should the main agent appear in the same run registry?
- Should `depth` and confirmation mode be entirely derived from future runtime profiles?
- Should `get_agent_result` wait on `SubAgentRecord` or a future `RunHandle`?

## Stabilization Gate Before Follow-Up Changes

Change 1 should be considered stable only after it is tested, merged, and verified with spawn, scheduler, context persistence, cancellation/timeout, notification, and plan-run burn-in behavior preserved.
