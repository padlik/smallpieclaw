# Proposal: upgrade-schedule-tool

## Why

The `schedule` built-in tool cannot faithfully manage scheduled jobs. There is no way to modify an existing job without `remove` + `add`, which silently destroys configuration (`model`, `preserve_context`, `notify`, raw `cron`) that the `list` output never shows in the first place, resets `created_at` and `enabled`, and orphans run history. Two `add_job` parameters (`context_max_messages`, `overlap_policy`) never reach the tool schema, and `run_now` reports bare `triggered` — hiding that it fires a supervised scheduled-run sub-agent, silently no-ops on paused jobs, and auto-removes once-jobs. The agent is left guessing, or worse, emulating the run itself.

## What Changes

- **New `modify` action** on the `schedule` tool: partial-patch semantics on content fields (`task`, `cron`, `schedule_type` + `run_at`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`); `created_at`, `last_run`, run history, and `source` are preserved; tag immutable; `enabled` explicitly rejected with an error pointing at `pause`/`resume`.
- **New `show` action** on the `schedule` tool: full single-job metadata (raw `cron`, `run_at`, `next_run`, `enabled`, `is_running`, `source`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`, `last_run`, `last_error`); `list` stays compact.
- **`run_now` paused-job override**: manual triggers execute even when the job is paused (output notes the job remains paused). Testing a deliberately-paused job is a primary `run_now` use case. Scheduled firings continue to respect `enabled=false`.
- **`run_now` output clarity**: returns immediately with an explicit statement that the job runs through the standard scheduled-run machinery as a supervised sub-agent, the result is delivered per the job's `notify` setting, `schedule show` is the follow-up, and once-jobs are removed after the manual run — replacing the ambiguous `triggered`.
- **Exposed hidden parameters**: `context_max_messages` and `overlap_policy` added to the tool schema and descriptor for `add` and `modify` (with validation: `overlap_policy` in `skip`/`parallel`).
- **Descriptor rewritten** to document all actions, the supervised-sub-agent semantics of `run_now`, and the do-not-emulate guidance.
- **Sub-agent read access widened**: the depth guard becomes an allowlist — sub-agents gain the read-only actions `list` and `show`; all mutating actions remain hard-blocked at `caller_depth >= 1`.
- Backward compatible: existing actions, parameters, and Telegram `/jobs` behavior unchanged.

## Capabilities

### New Capabilities

- `schedule-tool-surface`: The model-facing contract of the `schedule` built-in tool — action set (list, show, add, remove, pause, resume, modify, run_now), parameter surface per action, sub-agent depth-guard rules, tool output semantics, and `run_now` supervised-execution semantics.

### Modified Capabilities

- `telegram-progress-panel`: New brief-formatting scenarios for the `schedule modify` and `schedule show` calls in the Running line (existing `schedule` scenarios unchanged).

## Impact

- `builtin_tools/schedule.py` — `exec_schedule`: new `show`/`modify` branches, depth-guard condition, `run_now` output wording.
- `builtin_tools/schemas.py` — `schedule` tool schema: action enum extended, new parameters.
- `builtin_tools/descriptors.py` — `schedule` descriptor rewrite.
- `scheduler.py` — new `modify_job()` (partial patch, `_resolve_schedule` reuse, `schedule.clear`/`_register_job` re-registration), `show_job()` metadata accessor; `run_now()` threading a manual-override flag past the `enabled` check in `_run_job`.
- `tool_formatting.py` — `_format_schedule` extension for new actions.
- Tests — new coverage for modify/show/depth-guard/run_now-override (no dedicated file exists today); `test_tool_brief_panel.py` additions; `make check` must pass (baseline 2097 passed, 1 skipped).
- Documentation — AGENTS.md "Key Modules" table entries unchanged (no new modules); `builtin_tools/schedule.py` docstring updated to describe the new action set.