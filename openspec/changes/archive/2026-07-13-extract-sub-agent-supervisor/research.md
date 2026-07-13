# Research Notes: Agent Execution Model and Spawn Refactor

## Summary

The project already has one core agent loop: `react_loop.py`. The pain is not multiple reasoning runtimes. The pain is that `builtin_executor._exec_spawn_agent` currently acts as a tool, runtime factory bridge, background supervisor, scheduler callback carrier, notification formatter, context persistence hook, and result rendezvous manager.

The first refactor should therefore extract a `SubAgentSupervisor` before introducing a shared `AgentRuntime` or splitting `builtin_executor.py` into many files.

## Current Execution Model

```text
Main agent
  Telegram/user -> AgentController.run -> ReactContext -> react_loop

On-demand sub-agent
  spawn_agent -> SubAgentRunner -> AgentController.run -> ReactContext -> react_loop

Scheduled job
  scheduler -> spawn-agent-like path -> SubAgentRunner -> react_loop

Plan step
  PlanExecutor -> SubAgentRunner directly -> react_loop
```

The core loop is shared. The duplicated and tangled part is the launch/supervision envelope.

## Current `spawn_agent` Responsibilities

`spawn_agent` currently owns too many concerns:

- model-facing argument parsing and validation
- task aliases: `task`, `prompt`, `goal`, `description`
- `response_format` task mutation
- depth guard
- max-subagents cap check
- context payload construction
- sub-agent factory invocation
- `SubAgentRecord` creation
- registry registration/unregistration
- thread-pool submission
- background `_run_and_notify` lifecycle
- context persistence
- scheduler result logging callbacks
- notification formatting and delivery
- result event signaling
- cleanup and runner close

This makes `spawn_agent` a mini-runtime embedded in a builtin tool.

## Proposed Conceptual Layers

```text
Invocation Layer
  user message | spawn_agent | scheduler | execution plan

Supervision Layer
  background handle | get_agent_result rendezvous | job log | DAG aggregation

Runtime Layer
  profile/options/model/context/trace/confirmation -> ReactContext

Execution Layer
  react_loop(ctx, goal, ...)

Tool Layer
  shell/files/memory/MCP/spawn/etc.
```

The first change should separate the tool layer from the supervision layer for sub-agent spawning.

## Recommended Change Sequence

### Change 1: `extract-sub-agent-supervisor`

Goal: make `spawn_agent` a thin model-facing shim and move background lifecycle management into `SubAgentSupervisor`.

Included:

- Extract background run lifecycle from `_exec_spawn_agent`.
- Move registry/thread/result/notify/scheduler-callback handling into a supervisor seam.
- Move scheduler/internal control callbacks out of model-facing args.
- Preserve public `spawn_agent` and `get_agent_result` behavior.

Out of scope:

- AgentRuntime profiles
- PlanExecutor behavior changes
- registry visibility changes
- builtin tool file split
- AppConfig migration
- depth greater than 1

### Change 2: `unify-running-agent-visibility`

Goal: define which executions are visible as running agents.

Questions:

- Should plan steps appear in `/agents`?
- Should scheduled jobs appear in `/agents`?
- Should the main agent appear in the registry?
- Should plan/scheduled executions count against `max_subagents`?

This is behavior-facing and should be handled separately after Change 1 stabilizes.

### Change 3: `introduce-agent-runtime`

Goal: introduce explicit runtime construction concepts after registry behavior is settled.

Candidate vocabulary:

- `AgentRuntime`
- `RuntimeProfile`
- `RuntimeOptions`
- `SupervisionOptions`
- `RunHandle`

Candidate profiles:

```text
MAIN
ON_DEMAND_SUBAGENT
SCHEDULED_AGENT
PLAN_STEP_AGENT
DIAGNOSTIC_AGENT
```

This change should rely on golden-equivalence tests proving the new runtime builder produces the same `ReactContext` as legacy paths.

### Change 4: `split-builtin-tools`

Goal: split `builtin_executor.py` only after `spawn_agent` is thin.

Possible shape:

```text
builtin_tools/
  dispatcher.py
  shell.py
  files.py
  patching.py
  memory.py
  agents.py
  secrets.py
  logs.py
  schedule.py
```

At this point `builtin_tools/agents.py` should delegate to the supervisor rather than contain a relocated mini-runtime.

## Must-Preserve Invariants for Change 1

- depth 0 can spawn
- depth >= 1 cannot spawn
- task aliases still work
- invalid `context_key` is rejected before factory/supervisor execution
- max-subagents cap is preserved
- `response_format` behavior is preserved
- context persistence via `context_key` is preserved
- `get_agent_result` waits on completion and returns status/result
- timeout cancellation still propagates to the running agent/LLM client
- stale notifications are suppressed after timeout cancellation
- scheduled job result logging still fires through the internal path
- graph memory is not auto-populated by sub-agent results

## Proposal Readiness Review

Architecture review concluded:

- The full research is not proposal-ready as one change.
- It should be saved as design/ADR seed material.
- The first proposal should be narrowly scoped to `extract-sub-agent-supervisor`.
- Later changes should be proposed only after Change 1 is tested, stabilized, and merged.

Recommended proposal gates for Change 1:

- Commit to the supervisor extraction boundary.
- Explicitly state the behavior delta: internal scheduler/control keys no longer travel through model-facing `spawn_agent` args.
- Keep `PlanExecutor` out of scope.
- Defer registry/cap semantics to Change 2.
- Defer AgentRuntime/profiles to Change 3.
- Declare cancellation/timeout behavior frozen.
- Decide whether `_exec_spawn_agent` remains a thin compatibility shim during the change or tests migrate directly to the new supervisor seam.

## Stabilization Criteria Before Change 2

Change 1 is stable only when:

- it is merged to `main`
- `make check` passes
- spawn/scheduler/context/cancellation tests pass
- arg-contract narrowing has explicit test coverage
- on-demand spawn burn-in passes
- scheduled job burn-in passes
- plan-run burn-in passes
- no registry/thread/pool cleanup regressions are observed

Before `introduce-agent-runtime`, a `ReactContext` golden-equivalence harness should exist.
