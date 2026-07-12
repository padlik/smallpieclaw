## 1. Characterize Current Spawn and Scheduler Behavior

- [ ] 1.1 Add or update tests that pin model-facing `spawn_agent` compatibility: task aliases, `response_format` shaping, invalid `context_key` rejection before submission, depth guard, and unchanged friendly error shapes.
- [ ] 1.2 Add or update tests that pin current max-subagents behavior for on-demand and scheduled launches, including the `SubAgentRecord.source` values used by managed-record counting.
- [ ] 1.3 Add or update tests that pin `get_agent_result` behavior for unknown agents, completion wait, timeout, `cancel_on_timeout`, returned status/result/result type, and stale-notification suppression after timeout.
- [ ] 1.4 Add or update tests that pin scheduled job callback behavior, including finish callback, result log callback, notify suppression/format options, and concurrent job callback isolation.
- [ ] 1.5 Add or update tests that pin context persistence and graph-memory non-admission for sub-agent results.
- [ ] 1.6 Add or update tests that pin sub-agent context channel separation: scheduler/internal controls are not injected into `PARENT CONTEXT`, while existing context payload size, auto-summary, prompt injection, and persistence-exclusion behavior remains unchanged.

## 2. Introduce the Supervisor Boundary

- [ ] 2.1 Add a `SubAgentSupervisor` component and per-submission supervision options for scheduler/internal controls: `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, and `expandable`.
- [ ] 2.2 Move `SubAgentRecord` creation, synchronous registration, `agent_id` minting, and thread-pool submission into the supervisor while preserving the synchronous registration-before-return boundary.
- [ ] 2.3 Move background run execution, result event signaling, context save, notification, result logging, finish callback, unregister, runner close, and cleanup into the supervisor.
- [ ] 2.4 Preserve explicit trace/log identity behavior at the supervisor's background thread or executor entry point in accordance with ADR-0004 and ADR-0005.
- [ ] 2.5 Preserve the existing pre-submit capacity check and current record-source behavior for both on-demand and scheduled launch paths.

## 3. Rewire Spawn and Scheduler Callers

- [ ] 3.1 Refactor `_exec_spawn_agent` into a thin compatibility shim that validates model-facing args, builds the same task/context inputs, delegates accepted runs to `SubAgentSupervisor.submit(...)`, and returns the existing `agent_id` response shape.
- [ ] 3.2 Rewire scheduled job launches to call the supervisor seam with supervision options instead of passing `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, or `expandable` through `_exec_spawn_agent` args.
- [ ] 3.3 Preserve or intentionally translate any existing `_scheduler_finish_cb` fallback into per-submission supervision options after confirming the current composition path.
- [ ] 3.4 Keep `get_agent_result` compatible by reading from the same registry or a supervisor-mediated equivalent without changing timeout/cancellation semantics.

## 4. Update Tests and Documentation Surfaces

- [ ] 4.1 Migrate tests that currently assert scheduler control keys in `_exec_spawn_agent` args to assert those controls are delivered through supervisor options.
- [ ] 4.2 Ensure normal model-facing `spawn_agent` tests still exercise `_exec_spawn_agent` as the compatibility shim.
- [ ] 4.3 Update any inline comments or developer-facing notes needed to explain that scheduler controls use supervisor options, not model-facing tool args.

## 5. Verification

- [ ] 5.1 Run focused tests for spawn, context payloads, context persistence, scheduler fallback/job logging, timeout cancellation, trace/log propagation, and graph-memory admission behavior.
- [ ] 5.2 Run `openspec validate extract-sub-agent-supervisor --type change --strict`.
- [ ] 5.3 Run `ruff check .`.
- [ ] 5.4 Run `vulture . vulture_whitelist.py --min-confidence 80`.
- [ ] 5.5 Run `make check` before considering the implementation complete.
- [ ] 5.6 Perform manual or scripted burn-in for one on-demand spawn, one scheduled job, and one plan run to verify no registry, thread-pool, notification, cleanup, or scheduler callback regressions.
