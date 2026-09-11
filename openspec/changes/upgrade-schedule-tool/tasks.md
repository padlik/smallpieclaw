## 1. Scheduler layer (`scheduler.py`)

- [x] 1.1 Implement `Scheduler.show_job(tag)` (with docstring) returning the full single-job metadata dict (`tag`, `task`, `schedule` prose, `cron` raw, `schedule_type`, `run_at`, `next_run`, `last_run`, `last_error`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`, `source`, `enabled`, `is_running`, `created_at`) or `None` when the tag is unknown
- [x] 1.2 Implement `Scheduler.modify_job(tag, updates: dict)` (with docstring) with partial-patch semantics under `_running_lock`: merge only provided updatable fields (`task`, `cron`, `schedule_type`+`run_at`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`); never touch `created_at`, `last_run`, `last_error`, `source`, or run history
- [x] 1.3 In `modify_job`, re-validate merged schedule fields via `_resolve_schedule` (invalid cron → error, job unchanged) and re-register via `schedule.clear(tag)` + `_register_job(tag, meta)` only when schedule fields changed
- [x] 1.4 In `modify_job`, return explicit errors for: unknown tag (listing known tags), empty patch (listing updatable fields), and `enabled` in updates (pointing at `pause`/`resume`); persist via `_save_state` + `_save_scheduler_toml` on success
- [x] 1.5 Thread `manual: bool = False` from `run_now()` through `_run_job`, skipping the `enabled` check when `manual=True`; overlap policy still applies; scheduled firings unchanged (still gated by `enabled`)

## 2. Tool layer (`builtin_tools/schedule.py`)

- [x] 2.1 Add `show` branch to `exec_schedule`: format `show_job` output as labeled lines (raw cron, run_at, next_run, enabled, is_running, source, notify, model, preserve_context, context_max_messages, overlap_policy, last_run, last_error, created_at); unknown tag → error listing known tags
- [x] 2.2 Add `modify` branch to `exec_schedule`: pass updatable fields to `modify_job`; map its error results to `success=false` tool outputs; forward `recoverable` error semantics
- [x] 2.3 Validate `overlap_policy` ∈ {`skip`, `parallel`} in `exec_schedule` for both `add` and `modify` with an actionable error before reaching `Scheduler`; pass `context_max_messages` (int-coerced) through for both actions
- [x] 2.4 Change the depth guard to an allowlist: block all actions except `list` and `show` at `caller_depth >= 1`; the blocked-mutation error names the blocked action and lists both `list` and `show` as available to sub-agents
- [x] 2.5 Rework `run_now` output: state the job runs as a supervised scheduled-run sub-agent through the standard scheduled-run machinery, result delivered per the job's `notify` setting, follow up via `schedule show`; note "job remains paused" when `enabled=false`; note the overlap-policy subject; append once-job removal note when `schedule_type == "once"`; preserve the unknown-tag error listing known tags
- [x] 2.6 Update the `exec_schedule` module docstring to describe the eight-action set and the read/write depth split

## 3. Schema, descriptor, and brief

- [x] 3.1 Extend the `schedule` schema in `builtin_tools/schemas.py`: action enum += `show`, `modify`; add `context_max_messages` (integer) and `overlap_policy` (enum skip|parallel) properties
- [x] 3.2 Rewrite the `schedule` descriptor in `builtin_tools/descriptors.py`: document all eight actions, the `modify` updatable-field contract (immutable tag, `enabled` rejected), the `show` follow-up, and `run_now` supervised-sub-agent semantics with do-not-emulate guidance
- [x] 3.3 Extend `_format_schedule` in `tool_formatting.py` (verified symbol, line 186) to render `modify` and `show` calls in the `action + tag` brief style (`schedule modify "daily-report"`, `schedule show "daily-report"`)

## 4. Tests

- [x] 4.1 New test file for the `schedule` tool surface: modify partial-patch preserves other fields (task, cron, created_at, last_run, model, preserve_context, notify, overlap_policy); modify cron re-registers; invalid cron rejected with job unchanged; empty patch, `enabled`-in-modify, and unknown-tag errors
- [x] 4.2 Same file: show returns full metadata incl. raw cron, `created_at`, `enabled`, `is_running`; show unknown tag error; `list` output unchanged (compact — no new fields)
- [x] 4.3 Same file: depth-guard allowlist — sub-agent (caller_depth 1) succeeds on `list`/`show`, blocked on `add`/`remove`/`pause`/`resume`/`modify`/`run_now` with the error naming both read actions; main agent (depth 0) passes all eight actions
- [x] 4.4 Same file: `run_now` output asserts (supervised sub-agent statement, notify/show follow-up, paused-job note when `enabled=false`, once-job removal note, overlap-policy-subject note) and `run_now` unknown-tag error lists known tags; `overlap_policy` validation errors for both `add` and `modify`
- [x] 4.5 `Scheduler.modify_job` unit tests against a real `Scheduler` instance: patch persists to `scheduler.toml` round-trip; once-job keeps `schedule_type`/`run_at`; hidden params (`context_max_messages`, `overlap_policy`) updatable; `run_now(manual)` executes a paused job while scheduled firing skips it
- [x] 4.6 Add `test_tool_brief_panel.py` cases: `schedule modify "daily-report"` (task not displayed) and `schedule show "daily-report"`

## 5. Verification and validation

- [x] 5.1 Run `make check` (ruff + vulture + full pytest suite); fix failures, updating `vulture_whitelist.py` for new public API symbols (`show_job`, `modify_job`) if vulture flags them; baseline is 2097 passed, 1 platform-specific skip
- [x] 5.2 Run `openspec validate upgrade-schedule-tool --type change --strict` and fix any reported delta-spec issues
- [x] 5.3 Update documentation: `builtin_tools/schedule.py` docstring covered by 2.6; verify AGENTS.md "Key Modules" needs no row changes (no new modules)