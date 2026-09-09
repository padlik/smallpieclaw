## Context

The react loop currently uses a **nested two-loop structure**:

```python
while True:                                   # outer — re-enters after extension granted
    while state.step < state.max_steps:       # inner — step gate
        result = _run_single_step(...)
        if result.early_return is not None:
            return result.early_return
        if not result.should_continue:
            break

    if state.operator_cancelled:              # between-loop check
        return "⚠️ Task stopped by operator."

    if not _handle_step_limit_reached(ctx, state, _progress):  # blocks on event.wait(120)
        break

return "⚠️ Agent reached maximum steps. Operation cancelled."
```

The outer `while True:` exists *solely* to re-enter the inner loop after `_handle_step_limit_reached()` grants an extension. Three distinct config caps (`max_iterations`, `scheduled_max_iterations`, `plan_max_iterations`) populate `state.max_steps` at `_LoopState` construction time.

`state.max_steps` has a secondary role: it is passed to `_run_plan()` as a step-budget hint; `_run_plan()` compares against it and may return a new `max_steps` clamped to `_ABSOLUTE_PLAN_CEILING`. This role must survive gate removal.

Tool outcomes are dicts with a `success: bool` field and an `error: str` field (populated when `success is False`). This is the established tool-result contract used throughout `react_loop.py` (e.g., `outcome.get("success", False)` for display, `outcome.get("success") is not False` for auth-refresh logic).

The existing `json_fail_streak` / `_JSON_FAIL_LIMIT = 3` / `_handle_non_json()` pattern is the structural idiom this change mirrors for doom-loop detection — same counter in `_LoopState`, same limit constant, same early-return abort path.

ADR-0024 lists step extension as one of four confirmation flow types owned by `ConfirmationManager`. The method `request_extension()` stays in the class after this change but the react loop stops calling it (flow type becomes dormant — no ADR supersession required).

## Goals / Non-Goals

**Goals:**
- Collapse the nested two-loop structure to a single `while True:` by removing the step gate and the extension-handling block
- Add a fire-and-forget Telegram notification at configurable step milestones (main agent only)
- Detect and self-terminate on identical repeated tool failures (all agent types)
- Silence the three step-count caps without breaking existing config files or `scheduler.toml` entries
- Remove dead code: `_handle_step_limit_reached`, `_send_extend_prompt`, `__EXTEND__:` signal path, extend-button Telegram callbacks

**Non-Goals:**
- Error-category clustering for sandbox probe loops (nsjail / EPERM patterns) — deferred
- Per-step notifications for scheduled or plan-step sub-agents
- Any changes to tool confirmation, headless confirmation, or LLM error retry flows
- Autoapprove / ghost-operator UX

## Decisions

### D1 — Nested loops collapse to a single `while True:`; `max_steps` initialized to effectively-unlimited

**Target loop structure:**

```python
state = _LoopState(messages=messages, goal_idx=goal_idx,
                   max_steps=_EFFECTIVELY_UNLIMITED_STEPS)

while True:
    result = _run_single_step(ctx, state, ...)   # increments state.step internally
    if result.early_return is not None:
        return result.early_return               # doom-loop abort, JSON-fail, operator cancel, etc.
    if not result.should_continue:
        break

    # milestone notification (D3)
    if (ctx.step_notify_interval
            and ctx.milestone_notify_fn
            and state.step > 0
            and state.step != state._last_notified_step
            and state.step % ctx.step_notify_interval == 0):
        try:
            ctx.milestone_notify_fn(state.step)
            state._last_notified_step = state.step
        except Exception:
            logger.debug("milestone_notify failed", exc_info=True)

return "⚠️ Task stopped by operator."   # only reached via break (operator-cancel path)
```

**Post-loop structure:** `break` is only set by the operator-cancel path (`should_continue = False`), so the post-loop return MUST be the operator-cancel message. The existing `"⚠️ Agent reached maximum steps. Operation cancelled."` fallback is deleted — it is dead code after gate removal. The explicit deletion of `_handle_step_limit_reached()` (lines 1594–1615 in the current codebase) is part of D1.

The between-loop `operator_cancelled` check and the `_handle_step_limit_reached()` call are removed; the operator-cancel path already produces `result.early_return` or `result.should_continue = False` inside `_run_single_step` (the outer check was only necessary because the inner loop could exit without an `early_return`). `_handle_step_limit_reached()` (lines 1594–1615) is deleted entirely.

**Apply-time verification required:** Before deleting the between-loop `operator_cancelled` check, confirm that every code path in `_run_single_step` that sets `state.operator_cancelled = True` also sets `result.early_return` to a non-None string or sets `result.should_continue = False`. If any cancel path relies solely on the state flag (without an early-return or should-continue signal), the between-loop check must be moved *inside* the single `while True:` loop rather than deleted.

`state.max_steps` is initialized to `_EFFECTIVELY_UNLIMITED_STEPS` (10M). It is retained in `_LoopState` for two reasons: (a) the checkpoint schema (`state.max_steps` appears at lines 1013/1035) requires the field, and (b) `_run_plan()` may read and mutate it (harmlessly — 10M is larger than any practical plan, so the comparison branch in `_run_plan` that raises the budget is dead, but `_run_plan` may still write `state.max_steps` down to `_ABSOLUTE_PLAN_CEILING` after a plan runs; this mutation is acceptable). `state.max_steps` is no longer a gate.

**Residual inert plumbing:** `agent_runtime.py` (`effective_max_iter` from `RuntimeOptions` / `_scheduled_max_iterations`) still threads `max_iterations` into the sub-agent `AgentConfig` at construction time. This is deliberately retained and runtime-inert: `react_loop()` initializes `max_steps = _EFFECTIVELY_UNLIMITED_STEPS` for every agent type (D1), so the construction-time value is never used as a gate. It may be pruned in a future cleanup change.

**Alternative rejected:** Per-agent-type flag to selectively remove the gate. Rejected — it forces conditional logic in `_LoopState` or `ReactContext` with no safety benefit (doom-loop applies equally to all types).

**Logging**: All format strings showing `state.step/state.max_steps` are updated to show step count only (e.g., `step {state.step}`), avoiding `X/10000000` in logs and Telegram UI. Affected locations include: logger format strings in `react_loop.py`, `Step: {step}/{max_steps}` in `telegram_interface.py:1049`, checkpoint/resume cards in `telegram_commands.py:422/429/457`, and `Completed {iteration}/{max_iterations} iterations` in `sub_agent_supervisor.py:457`.

### D2 — Doom-loop failure predicate: `outcome.get("success") is False`; key = `f"{tool_name}:{error[:200]}"`

**Failure predicate:** `tool_failed = isinstance(outcome, dict) and outcome.get("success") is False`. This matches the established tool-result contract (`success: bool`, `error: str`) and avoids false negatives (missing key treated as success, consistent with line 112). Shell non-zero exit, tool-reported errors, and exception-converted failures all produce `success=False`.

**Error source:** `error_text = (outcome.get("error", "") or "").strip()`. Truncated to 200 chars for the key.

**Key:** `key = f"{tool_name}:{error_text[:200]}"`. Composite key avoids false positives: a batch job calling `file_write` with different paths and different errors resets the counter each time.

**Placement:** The check runs inside `_dispatch_action()`, in the tool-dispatch branch (after the `vision_query`/normal-tool merge point, covering both). The tool `outcome` dict (`success`, `error`) is only in scope at this location — `_run_single_step()` never sees it. State mutations (`state._last_tool_fail_key`, `state._tool_fail_repeat`) happen here. On trigger, `_dispatch_action` returns the abort string through its existing `Optional[str]` return channel. The existing `if final is not None:` check in `_run_single_step` processes it as a terminal result; this correctly deletes the checkpoint (same path as a `finish` action, which is the right behavior for a doom-loop abort). On success, clear `state._last_tool_fail_key = ""` and `state._tool_fail_repeat = 0`.

**Mirrors:** `_handle_non_json` pattern exactly — module-level constant (`_DOOM_LOOP_LIMIT = 3`), two fields in `_LoopState` (`_last_tool_fail_key: str = ""`, `_tool_fail_repeat: int = 0`), early-return abort string.

**Alternative rejected:** Error-category clustering (EPERM pattern matching). Deferred to a separate follow-up; not part of this change.

### D3 — Milestone notification via `milestone_notify_fn` and `step_notify_interval` in `ReactContext`

Two new fields added to `ReactContext`:
- `step_notify_interval: int = 0` (dataclass default) — populated in `agent_controller.py` from `app_cfg.agent.step_notify_interval` (config default 30); same wiring location as other config-derived context fields. The dataclass default is deliberately `0`, not `30`: sub-agent contexts bypass config wiring and rely on the dataclass default, so `0` keeps them notification-free by construction.
- `milestone_notify_fn: Optional[Callable[[int], None]] = None` — wired in `agent_controller.py` to `run_panel.milestone_notify` for the main agent; remains `None` for all sub-agent types

For the main agent, `RunPanel` is always present at wiring time — no None-guard required at the wiring site, but the loop guard `if ctx.milestone_notify_fn` covers the sub-agent case.

**Disabled-path guard:** `if ctx.step_notify_interval and ctx.milestone_notify_fn and state.step > 0 and state.step % ctx.step_notify_interval == 0`. The `step_notify_interval > 0` guard prevents `ZeroDivisionError`. The `state.step > 0` guard prevents a spurious "step 0" ping before any work completes.

**Double-fire prevention (S1):** `state.step` is incremented inside `_run_single_step`, but inactivity-warning paths may return `should_continue=True` without incrementing the step, causing the same step number to appear on the next loop iteration. To prevent duplicate notifications, add `_last_notified_step: int = -1` to `_LoopState` and gate on `state.step != state._last_notified_step`; set `state._last_notified_step = state.step` immediately after dispatching. The full guard becomes: `if ctx.step_notify_interval and ctx.milestone_notify_fn and state.step > 0 and state.step != state._last_notified_step and state.step % ctx.step_notify_interval == 0`.

**Resume boundary (review finding, benign):** `_last_notified_step` is not persisted in checkpoints, so it resets to `-1` on resume. The checkpoint stores the already-incremented step, and the milestone check runs only after the *next* step completes, so a milestone landing exactly on a resume boundary is silently **skipped** — it can never double-fire. This is acceptable for liveness pings and intentional; do not mistake it for a bug.

**Fire-and-forget mechanism:** `RunPanel.milestone_notify(step)` must dispatch the Telegram send without blocking the react-loop thread. The bot runs on an asyncio event loop in a separate thread. The implementation uses `asyncio.run_coroutine_threadsafe(bot.send_message(...), loop)` and does **not** await the result. The returned `Future` is discarded. This is the same dispatch pattern used by other synchronous→async bridges in `telegram_interface.py` (e.g., the progress-callback dispatch path).

**Exception guard:** The milestone call MUST be wrapped in `try/except Exception: logger.debug(...)`. If the bot event loop is closed or the panel is torn down, `run_coroutine_threadsafe` raises `RuntimeError` synchronously on the react thread. An uncaught exception here would kill an otherwise-healthy run. Mirror the `on_step` callback guard pattern (`react_loop.py:1511-1515`).

**Alternative rejected:** Route through `ConfirmationManager` EXTEND_PREFIX path — blocks via `event.wait()`, solving nothing.
**Alternative rejected:** Direct `ctx.telegram.send_message(...)` from react_loop — violates transport agnosticism; react_loop has no Telegram dependency today.

### D4 — `request_extension()` stays in `ConfirmationManager`; `_handle_step_limit_reached()` is deleted

ADR-0024's four-flow taxonomy is preserved in the class signature. The step-extension flow type becomes dormant. No ADR supersession needed. `_handle_step_limit_reached()` in `react_loop.py` is deleted; its call site is the only place it is invoked.

### D5 — `schedule` tool (add-action) `max_iterations` parameter removed from descriptor and schema

`builtin_tools/descriptors.py` (the `schedule` add-action descriptor), `builtin_tools/schemas.py` (the add-action arg schema), and `builtin_tools/schedule.py` (the handler) all remove the `max_iterations` parameter. This is independent of the `spawn_agent` question (Open Questions) — the two tools have separate descriptors and are verified separately.

## Architecture: Component View

```
┌─────────────────────────────────────────────────────────────────┐
│  react_loop.py                                                  │
│                                                                 │
│  _LoopState                                                     │
│  ├── step: int                                                  │
│  ├── max_steps: int  (= _EFFECTIVELY_UNLIMITED_STEPS;           │
│  │                    retained for checkpoint schema +          │
│  │                    _run_plan compat; not a gate)             │
│  ├── json_fail_streak: int  (existing)                          │
│  ├── _last_tool_fail_key: str = ""   ◄── NEW                    │
│  └── _tool_fail_repeat: int = 0     ◄── NEW                     │
│                                                                 │
│  while True:                                                    │
│    1. _run_single_step()                                        │
│       ├── LLM call                                              │
│       ├── Parse action                                          │
│       ├── Dispatch tool → outcome dict {success, error, ...}   │
│       ├── doom-loop check (NEW):                               │
│       │     if success is False:                                │
│       │       key = f"{tool_name}:{error[:200]}"               │
│       │       if key == _last_tool_fail_key: repeat += 1       │
│       │       else: reset key, repeat = 1                      │
│       │       if repeat >= 3: early_return = abort msg         │
│       │     else: reset key and repeat = 0                     │
│       └── return _StepResult                                    │
│    2. if early_return → return                                  │
│    3. if not should_continue → break                            │
│    4. milestone notify (NEW):                                   │
│         if interval > 0 and fn and step > 0                    │
│            and step % interval == 0:                           │
│              fn(step)  ──────────────────────────────────────▶ RunPanel.milestone_notify
└─────────────────────────────────────────────────────────────────┘
```

## Risks / Trade-offs

- **Budget risk from unlimited runtime**: Accepted. Doom-loop catches exact-repeat failures but not varied-error or slow-progress loops. No mitigation in this change.
- **`_EFFECTIVELY_UNLIMITED_STEPS` (10M) in checkpoint schema**: Value appears in persisted checkpoints. If a checkpoint is resumed, `state.max_steps` of 10M is loaded correctly — no gate logic runs against it.
- **`request_extension()` retained but dormant**: Slight API surface creep in `ConfirmationManager`. Acceptable; the method is part of the ADR-0024 four-flow taxonomy. Can be pruned in a future cleanup change.
- **Doom-loop limit of 3**: Conservative. Agents with `agent-recovery` auto-retries (up to 2 per step) exhaust retries before a failure surfaces to react_loop level; doom-loop triggers only on the 3rd unremediated identical failure at the react-loop boundary.

## Migration Plan

No data migration or schema change. All config fields are backward-compatible. `scheduler.toml` entries with `max_iterations` continue to load without error (`scheduler.py` reads the field from job meta for backward compat but does not pass it to any runtime path).

Deployment: in-place; no external service restart required. The change takes effect at the next `main.py` start.

Rollback: revert code changes. No config or file-system state to reverse.

## Open Questions

- **`spawn_agent` descriptor exposure of `max_iterations`**: `builtin_tools/agents.py` plumbs `max_iterations` through spawn args (Impact table line 43). Whether the `spawn_agent` tool descriptor in `descriptors.py` also exposes this parameter to the LLM is unconfirmed. Verify at apply time: if present, remove it from the descriptor; if absent (internal plumbing only), the `agents.py` change stands alone.
