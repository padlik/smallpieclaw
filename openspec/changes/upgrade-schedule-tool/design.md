# Design: upgrade-schedule-tool

## Context

The `schedule` tool (`builtin_tools/schedule.py` → `exec_schedule`) is a leaf dispatcher over the `Scheduler` class (`scheduler.py`). Jobs live in `_jobs_meta`, persist to `scheduler.toml` (single source of truth) plus `scheduler_state.json` (run history), and execute as supervised sub-agents through the `SubAgentSupervisor` with `source=SCHEDULED` (ADR-0005, ADR-0007, ADR-0008 in force). The tool currently exposes six actions with a depth guard that blocks sub-agents from everything except `list`.

Component view of the touched surface (ASCII, lightweight C4-inspired):

```
+----------------------------------- agent process (single container) ----------------------------------+
|                                                                                                       |
|  react loop / Telegram /jobs                                                                          |
|        |                                                                                              |
|        v                                                                                              |
|  +-------------------+  dispatch (action, args, caller_depth)   +-----------------------------+        |
|  | exec_schedule     |----------------------------------------->| Scheduler                   |        |
|  | builtin_tools/    |                                          |  _jobs_meta (in-memory)     |        |
|  | schedule.py       |<-----------------------------------------|  scheduler.toml (persist)   |        |
|  | leaf dispatcher   |   list/show/add/remove/pause/             |  scheduler_state.json       |        |
|  +-------------------+   resume/modify/run_now                   |  add_job, modify_job (NEW), |        |
|        |                             |                            |  show_job (NEW), run_now    |        |
|        |                             |                            +---------------+-------------+        |
|        |                             |                                            |                      |
|  +-----+------------------+  schema  |                                    spawn under lock      |                      |
|  | schemas.py           |<----------+                                    (SupervisionOptions, |                      |
|  | descriptors.py       |  descriptor                                     SOURCE_SCHEDULED)     |                      |
|  | tool_formatting.py   |                                                 v              v                      |
|  +----------------------+                                    +----------------+  +-----------------+       |
|                                                               | SubAgent       |  | run_history /   |       |
|                                                               | Supervisor     |  | execution_log   |       |
|                                                               +----------------+  +-----------------+       |
+-------------------------------------------------------------------------------------------------------+
```

- **Boundaries:** `exec_schedule` stays a stateless leaf (ADR-0008); all job-state ownership, locking, validation, and persistence stay in `Scheduler`.
- **Responsibilities:** new actions are new dispatch branches in `exec_schedule` mapping to new `Scheduler` methods — the existing pattern (`add` → `add_job`, `remove` → `remove_job`).
- **Key relationships:** schema/descriptors feed the LLM; `tool_formatting.py` renders the Telegram brief; the supervisor path is untouched except for the manual-override flag.

## Goals / Non-Goals

**Goals:**

- Faithful job modification without lossy remove+add (partial patch, `created_at`/history/`source` preserved).
- Full single-job visibility (`show`) including previously invisible parameters.
- `run_now` that executes paused jobs on demand and tells the model exactly what happens (supervised scheduled-run sub-agent, async delivery, once-job removal).
- Expose `context_max_messages` and `overlap_policy` end-to-end (schema, descriptor, add, modify).
- Sub-agents gain read-only `show`; mutations stay main-agent-only.

**Non-Goals:**

- Tag rename (remove + add remains the path).
- Upsert semantics on `add` (Option C) — `add` stays create-only; its existing duplicate-tag error is unchanged.
- Changes to `scheduler.toml` format, state files, or persistence (all fields already round-trip).
- Changes to Telegram `/jobs` command surface or the file-based scheduler-commands interface (they keep calling `Scheduler` methods directly).
- Modifying a job that is currently running (the patch applies to the next firing; the running sub-agent already captured its task).
- Legacy interval fields (`hours`/`minutes`/`time`) in `modify` — add-only compatibility.

## Decisions

### D1: `modify_job` lives in `Scheduler`, not `exec_schedule`

`exec_schedule` stays a stateless leaf dispatcher (ADR-0008 pattern). `Scheduler.modify_job(tag, updates: dict)` owns the patch under `_running_lock`, validation, persistence (`_save_state` + `_save_scheduler_toml`), and re-registration.

*Alternative rejected:* assembling the patch inside `exec_schedule` — would leak job-meta internals into the tool layer and duplicate locking discipline.

### D2: Partial-patch semantics via field-merge, not remove+add

`modify_job` merges only provided updatable fields into `_jobs_meta[tag]`. For schedule fields (`cron`, `schedule_type`, `run_at`), the merged values are re-validated through the existing `_resolve_schedule` and re-registered via the same primitives `resume_job` already uses (`schedule.clear(tag)` + `_register_job(tag, meta)`). `created_at`, `last_run`, `last_error`, `source`, and run history are untouched by construction.

*Alternatives rejected:* internal remove+add (resets identity fields — the exact bug being fixed); full-replace semantics (destructive; forces the caller to know all fields).

### D3: `run_now` threads a manual-override flag past the enabled check

`run_now()` passes `manual=True` through to `_run_job`. `_run_job` skips the `enabled` check when `manual=True`, but the overlap policy still applies (a manual trigger while a run is in flight respects `skip`/`parallel`). Scheduled firings keep the enabled check. `exec_schedule`'s `run_now` output notes the paused state when the job is disabled, and notes that the trigger is subject to the job's overlap policy (a `skip`-policy job with a run in flight will skip this trigger — visible via `show`/`is_running`).

*Alternative rejected:* erroring on paused jobs — testing a deliberately-paused job is a primary `run_now` use case (decided in exploration).

### D4: `show_job` accessor returns full meta for one tag

`Scheduler.show_job(tag) -> dict | None` returns the per-job dict `list_jobs()` already builds, plus `created_at`. The returned dict is guaranteed to carry the full `show` field set: `tag`, `task`, `schedule` (prose), `cron`, `schedule_type`, `run_at`, `next_run`, `last_run`, `last_error`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`, `source`, `enabled`, `is_running`, and `created_at` — `list_jobs()` already includes all of these except `created_at` (verified: `scheduler.py:578-600`). `exec_schedule` formats it as labeled lines. `list` output stays compact.

*Alternative rejected:* filtering `list_jobs()` in the tool layer — tool layer shouldn't know meta internals (same rationale as D1).

### D5: Depth guard becomes an allowlist

`caller_depth >= 1` blocks every action except `list` and `show` (`action not in ("list", "show")`). The blocked-action error message names both allowed read actions.

*Alternative rejected:* blocklist of mutating actions — an allowlist fails closed when future actions are added.

### D6: `overlap_policy` validated at the tool layer

`exec_schedule` validates `overlap_policy` ∈ {`skip`, `parallel`} for `add` and `modify` with an actionable error before reaching `Scheduler`. TOML-authored jobs stay permissive at the scheduler layer (operator-authored config is validated by the operator; existing files already round-trip).

### D7: `modify` rejects `enabled` and empty patches

`enabled` is verb-owned (pause/resume) per the Option-A decision; passing it to `modify` returns an error pointing at the verbs. An empty patch returns an error listing the updatable fields. Both errors are `recoverable` (the model can fix the call immediately).

### D8: Schema/descriptor/brief updated together

`schemas.py` extends the action enum and adds `context_max_messages`/`overlap_policy`; `descriptors.py` documents the full action set and `run_now` semantics (supervised scheduled-run sub-agent, async delivery per `notify`, `show` follow-up, once-job removal, do-not-emulate); `tool_formatting.py` renders `modify`/`show` in the `action + tag` brief style. Builtin tool defs are built at startup, so schema changes take effect on restart — no hot-reload path needed.

## Risks / Trade-offs

- [Manual override fires a paused job the operator forgot about] -> The tool output explicitly notes "job remains paused"; scheduled firings still skip; the trigger is a deliberate main-agent action.
- [run_now + overlap_policy=skip while a run is in flight silently skips] -> Accepted: the overlap policy governs manual triggers too; the `run_now` output mentions the overlap policy, and `show` (`is_running`) reveals an in-flight run that absorbed the trigger.
- [Patch while a run is in flight] -> Accepted: the patch applies to the next firing; overlap policy governs the current run. No interruption of the running sub-agent.
- [Models trained on the old 6-action surface emit stale calls] -> New actions are additive; the unknown-action error already lists valid actions; schema enum + descriptor guide providers supporting structured output.
- [`modify` on a once-job with `cron`] -> Guarded: schedule-field validation goes through `_resolve_schedule`, which enforces `once` requires `run_at` and cron implies `cron` type; mixed updates are rejected as invalid.
- [Descriptor length growth] -> The descriptor documents eight actions and `run_now` semantics; kept in the established prose style of neighboring tools rather than a per-action schema explosion.

## Migration Plan

1. Implement `Scheduler.modify_job`/`show_job` + `run_now` override (scheduler layer, no persistence change).
2. Extend `exec_schedule` dispatch, depth guard, and validation; update schema + descriptor + briefs.
3. Add tests; run `make check` (baseline 2097 passed, 1 skipped).
4. Rollback: single revert — no data format change; all job files remain readable by the pre-change code (fields were already persisted).

## Open Questions

None blocking — all decisions were resolved during exploration. No in-force ADR needs revisiting: `run_now` manual trigger still flows through the `SubAgentSupervisor` with `SOURCE_SCHEDULED` under ADR-0005/0007, and the leaf-dispatcher shape honors ADR-0008.