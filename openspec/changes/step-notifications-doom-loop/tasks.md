# Tasks — step-notifications-doom-loop

## T1 — Loop collapse + `_LoopState` init
**File:** `react_loop.py`

1. Define module-level sentinel: `_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000` (retain existing value if already present; do not rename or delete).
2. Replace the nested `while True: / while state.step < state.max_steps:` structure with a single `while True:` loop. Update `_LoopState.__init__` to always set `max_steps = _EFFECTIVELY_UNLIMITED_STEPS` regardless of agent type.
3. Delete `_handle_step_limit_reached()` (lines 1594–1615 in the current codebase) and its call site entirely.
4. Delete the existing `"⚠️ Agent reached maximum steps. Operation cancelled."` post-loop fallback — it is dead code after gate removal. The post-loop return after `break` MUST be `"⚠️ Task stopped by operator."` (the operator-cancel message, since `break` is only set by the operator-cancel `should_continue=False` path).
5. Verify at apply time: before removing the between-loop `operator_cancelled` check, confirm that every code path in `_run_single_step` that sets `state.operator_cancelled = True` also yields `result.early_return` (non-None) or `result.should_continue = False`. If any cancel path relies solely on the state flag, move the check inside the single loop rather than deleting it.

**Acceptance:**
- `grep -n "while state.step" react_loop.py` returns 0 matches.
- `grep -n "_EFFECTIVELY_UNLIMITED_STEPS" react_loop.py` returns ≥ 1 match (definition retained).
- `grep -n "_handle_step_limit_reached" react_loop.py` returns 0 matches.
- `grep -n "reached maximum steps" react_loop.py` returns 0 matches.
- Operator-cancel path verified to terminate the loop correctly (via test or code inspection).

---

## T2 — Doom-loop detection in `_dispatch_action`
**File:** `react_loop.py`

1. Define module-level constant: `_DOOM_LOOP_LIMIT = 3` (mirrors `_JSON_FAIL_LIMIT = 3`).
2. Add `_last_tool_fail_key: str = ""`, `_tool_fail_repeat: int = 0`, and `_last_notified_step: int = -1` to `_LoopState`. (`_last_notified_step` is used by T4's milestone double-fire guard.)
3. Place the doom-loop check **inside `_dispatch_action()`**, in the tool-dispatch branch after the tool outcome is obtained — this is the merge point covering both `vision_query` and normal-tool paths. The `outcome` dict is only in scope here; `_run_single_step()` never sees `success`/`error`.

```python
if outcome.get("success") is False:
    error_text = (outcome.get("error", "") or "").strip()
    key = f"{tool_name}:{error_text[:200]}"
    state._tool_fail_repeat = state._tool_fail_repeat + 1 if state._last_tool_fail_key == key else 1
    state._last_tool_fail_key = key
    if state._tool_fail_repeat >= _DOOM_LOOP_LIMIT:
        # return abort string via existing Optional[str] channel
        # (processed by _run_single_step's `if final is not None:` path → deletes checkpoint)
else:
    state._tool_fail_repeat = 0
    state._last_tool_fail_key = ""
```

Mirror the existing `json_fail_streak` abort pattern for the termination message.

4. **Checkpoint non-persistence (stated decision):** `_last_tool_fail_key` and `_tool_fail_repeat` are NOT persisted in the checkpoint dict. On resume, they default to `""`/`0` (fresh streak). This is intentional — a resumed run should not carry over a pre-resume failure streak.

**Acceptance:**
- Unit test: 3× identical `(tool_name, error)` → loop aborts with doom-loop message; 3× different errors → no abort; success resets `_tool_fail_repeat` to `0` and `_last_tool_fail_key` to `""`.
- `grep -n "doom" react_loop.py` shows `_DOOM_LOOP_LIMIT` defined and `_dispatch_action` as the call site (not `_run_single_step`).

---

## T3 — Step display denominator removal
**Files:** `react_loop.py`, `telegram_interface.py:1049`, `telegram_commands.py:422/429/457`, `sub_agent_supervisor.py:457`

Remove or rewrite all `Step: X/max_steps` or `Step X of Y` displays. After gate removal the denominator is meaningless.

- `react_loop.py`: remove or simplify step-counter log lines referencing `max_steps`
- `telegram_interface.py:1049`: update step display in RunPanel
- `telegram_commands.py:422`, `:429`, `:457`: update `/status` step display
- `sub_agent_supervisor.py:457`: update sub-agent step display

**Acceptance:** `grep -rn "max_steps\|max_iterations" telegram_interface.py telegram_commands.py sub_agent_supervisor.py` returns 0 display-reference matches (internal `_LoopState.max_steps` field is retained).

---

## T4 — `milestone_notify_fn` in `ReactContext` + `RunPanel.milestone_notify`
**Files:** `agent_controller.py`, `telegram_interface.py`

1. Add to `ReactContext`:
   ```python
   step_notify_interval: int = 0
   milestone_notify_fn: Optional[Callable[[int], None]] = None
   ```

2. Add `RunPanel.milestone_notify(step: int)` method that sends a non-blocking Telegram message.

3. In `agent_controller.py`, wire `step_notify_interval` from config and pass `milestone_notify_fn = run_panel.milestone_notify` when building `ReactContext` for the main agent. Sub-agent `ReactContext` instances get `milestone_notify_fn = None`.

4. In `react_loop.py`, after each completed step:
   ```python
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
   ```
   - `state._last_notified_step` (added in T2) prevents double-fire on inactivity-warning iterations where `state.step` does not advance.
   - The `try/except` guard is mandatory: `run_coroutine_threadsafe` raises `RuntimeError` synchronously if the bot event loop is closed or the panel is torn down; an uncaught exception kills an otherwise-healthy run. Mirror the `on_step` callback guard (`react_loop.py:1511-1515`).
   - Use `asyncio.run_coroutine_threadsafe`; discard the returned `Future` (fire-and-forget).

**Acceptance:**
- Smoke test: agent reaches step 30 → Telegram receives milestone notification. Sub-agents do not send notifications.
- Unit test: `milestone_notify_fn` is not called twice for the same step number (double-fire guard).
- Unit test: if `milestone_notify_fn` raises `RuntimeError`, the run continues without error.

---

## T5 — Telegram dead code removal
**Files:** `telegram_interface.py`, `telegram_callbacks.py`

Remove:
- `_handle_extend_progress` in `telegram_interface.py`
- `_send_extend_prompt` in `telegram_interface.py`
- `__EXTEND__:` signal handling in `telegram_interface.py`
- Extend-button callback handlers in `telegram_callbacks.py`

Preserve all other dispatch entries and handlers (confirm, subagent confirm, etc.).

Update `vulture_whitelist.py`: `request_extension` and `EXTEND_PREFIX` in `confirmation.py` are deliberately retained (ADR-0024 taxonomy) but lose their only callers in this change — add them to the whitelist. Also update the `confirmation.py` docstring (lines 190–191) that still documents the step-extension flow to reflect it is now dormant.

**Acceptance:**
- `grep -rn "_handle_extend_progress\|_send_extend_prompt\|__EXTEND__\|cb_extend" telegram_interface.py telegram_callbacks.py` returns 0 matches.
- `make lint` (ruff + vulture) passes — no new unused-symbol errors from retained dormant code.

---

## T6 — `config_schema.py`: add `step_notify_interval`, deprecate 3 old fields
**File:** `config_schema.py`

1. Add `step_notify_interval: int = 30` to the appropriate config dataclass.

2. Mark `max_iterations`, `scheduled_max_iterations`, and `plan_max_iterations` as silently ignored — read and discarded at load time, no deprecation warning emitted. Retain the fields for backward compat so existing config files do not cause startup errors.

**Acceptance:** `make check` passes; existing config files with old fields do not cause startup errors.

---

## T7 — Schedule tool `max_iterations` removal
**Files:** `builtin_tools/descriptors.py`, `builtin_tools/schemas.py`, `builtin_tools/schedule.py`

Remove the `max_iterations` parameter from the `schedule` (add-action) tool:
- Remove from descriptor JSON schema in `descriptors.py`
- Remove from validation schema in `schemas.py`
- Remove plumbing in `schedule.py`

**Acceptance:** LLM tool listing no longer exposes `max_iterations` for the `schedule` tool. Existing `schedule` calls without `max_iterations` continue to work.

---

## T8 — `spawn_agent` plumbing removal + descriptor verification
**Files:** `builtin_tools/agents.py`, `builtin_tools/descriptors.py`

1. Remove `max_iterations` plumbing from `spawn_agent` in `agents.py`.

2. **Open question (verify at apply time):** Check whether `spawn_agent` descriptor in `descriptors.py` exposes `max_iterations` to the LLM. If yes, remove it.

**Acceptance:** `grep -n "max_iterations" builtin_tools/agents.py builtin_tools/descriptors.py` returns 0 matches for spawn_agent-related code.

---

## T9 — `scheduler.py` backward compat + `scheduler.toml` docs
**Files:** `scheduler.py`, `scheduler.toml` (or relevant docs)

1. In `scheduler.py`, remove the `max_iterations` override plumbed from the schedule tool call through to sub-agent construction. Scheduled agents inherit universal unlimited runtime.

2. Update `scheduler.toml` documentation (comments or README section) to remove references to `max_iterations` as a job-level parameter.

**Acceptance:**
- Scheduled jobs defined in `scheduler.toml` without `max_iterations` run without errors.
- Scheduled jobs defined in `scheduler.toml` **with** `max_iterations` still load and run without errors (field read and discarded — backward compat guarantee).

---

## T10 — Verification
**Scope:** All modified files

1. `make check` (ruff + vulture + pytest) passes clean.
2. Doom-loop unit test: 3× identical `(tool_name, error)` → loop aborts with doom-loop message; success resets `_tool_fail_repeat` to `0` and `_last_tool_fail_key` to `""`; 3× different errors do not trigger doom. Include a scheduled-agent path: confirm doom-loop fires identically when the run originates from the scheduler (same `react_loop` code path, so structural coverage is sufficient).
3. Milestone notification unit test: `milestone_notify_fn` called at step 30/60/90; NOT called at step 0; NOT called when `step_notify_interval = 0`.
4. Operator-cancel regression: confirm that operator cancellation still terminates the loop correctly after loop collapse (either via existing test or a targeted inspection check).
5. **Max-step test cleanup (S6):** Identify tests that assert the loop halts at `max_iterations` or produce the `"maximum steps"` return string — these are behavior-removal cases, not regressions to preserve. Rewrite or delete them as part of this task; do not make the code pass them artificially. Search: `grep -rn "maximum steps\|max_iterations" tests/`.
6. Regression: existing tests for confirm flow, headless confirm, `json_fail_streak`, and scheduled jobs pass unmodified.
