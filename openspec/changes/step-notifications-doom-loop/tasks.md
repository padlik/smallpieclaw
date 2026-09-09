# Tasks — step-notifications-doom-loop

## T1 — Loop collapse + `_LoopState` init
**File:** `react_loop.py`

1. Define module-level sentinel: `_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000` (retain existing value if already present; do not rename or delete).
2. Replace the nested `while True: / while state.step < state.max_steps:` structure with a single `while True:` loop. Update `_LoopState.__init__` to always set `max_steps = _EFFECTIVELY_UNLIMITED_STEPS` regardless of agent type. Remove `_handle_step_limit_reached` and the step-gate `should_continue` path that returns it.
3. Verify at apply time whether the `operator_cancelled` check between the outer and inner loops remains necessary after the inner loop is removed; delete it only after confirming every operator-cancel signal inside `_run_single_step` yields `early_return` or `should_continue = False` independently.

**Acceptance:**
- `grep -n "while state.step" react_loop.py` returns 0 matches.
- `grep -n "_EFFECTIVELY_UNLIMITED_STEPS" react_loop.py` returns ≥ 1 match (definition retained).
- If the between-loop operator-cancel check is removed: confirm via test or code inspection that operator cancellation still terminates the loop correctly.

---

## T2 — Doom-loop detection in `_run_single_step`
**File:** `react_loop.py`

1. Define module-level constant: `_DOOM_LOOP_LIMIT = 3` (mirrors `_JSON_FAIL_LIMIT = 3`).
2. Add `_last_tool_fail_key: str = ""` and `_tool_fail_repeat: int = 0` to `_LoopState`.
3. After tool dispatch returns an outcome and **before** returning `_StepResult`, check:

```python
if outcome.get("success") is False:
    error_text = (outcome.get("error", "") or "").strip()
    key = f"{tool_name}:{error_text[:200]}"
    state._tool_fail_repeat = state._tool_fail_repeat + 1 if state._last_tool_fail_key == key else 1
    state._last_tool_fail_key = key
    if state._tool_fail_repeat >= _DOOM_LOOP_LIMIT:
        # self-terminate: inject abort message and return early
else:
    state._tool_fail_repeat = 0
    state._last_tool_fail_key = ""
```

Mirror the existing `json_fail_streak` abort pattern for the termination message.

**Acceptance:** Unit test: 3× identical `(tool_name, error)` → loop aborts with doom-loop message; 3× different errors → no abort; success resets `_tool_fail_repeat` to `0` and `_last_tool_fail_key` to `""`.

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
           and state.step % ctx.step_notify_interval == 0):
       ctx.milestone_notify_fn(state.step)
   ```
   Use `asyncio.run_coroutine_threadsafe`; discard the returned `Future` (fire-and-forget).

**Acceptance:** Smoke test: agent reaches step 30 → Telegram receives milestone notification. Sub-agents do not send notifications.

---

## T5 — Telegram dead code removal
**Files:** `telegram_interface.py`, `telegram_callbacks.py`

Remove:
- `_handle_extend_progress` in `telegram_interface.py`
- `_send_extend_prompt` in `telegram_interface.py`
- `__EXTEND__:` signal handling in `telegram_interface.py`
- Extend-button callback handlers in `telegram_callbacks.py`

Preserve all other dispatch entries and handlers (confirm, subagent confirm, etc.).

**Acceptance:** `grep -rn "_handle_extend_progress\|_send_extend_prompt\|__EXTEND__\|cb_extend" telegram_interface.py telegram_callbacks.py` returns 0 matches.

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
5. Regression: existing tests for confirm flow, headless confirm, `json_fail_streak`, and scheduled jobs pass unmodified.
