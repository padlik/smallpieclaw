## Why

Two confirmation systems coexist with entangled shared state. `ConfirmationManager` (confirmation.py) handles tool confirm, step extension, and LLM error retry for the main agent (depth 0). A separate headless bridge on `BuiltinExecutor` handles sub-agent tool confirmations (depth ≥ 1). The two systems share `_pending`, share `auto_approve_tools` (via a fragile `_prompt_approval_set` reference), and use the same `threading.Event` + dict + token pattern — but with separate event stores, separate result dicts, and inconsistent signal APIs. The Telegram callback `cb_subagent_confirm` mutates `auto_approve_tools` via direct field access (`iface.agent._confirmation.auto_approve_tools.add(tool_name)`) instead of through a coordinator method, creating a race window between the set mutation and the event signal. This has been the most cross-referenced deferred refactor across three archived changes, gated on a security review (now resolved by nsjail shell isolation) and a "unified approve-all" decision (now resolved: extend the existing class).

## What Changes

- **Extend `ConfirmationManager`** with headless (sub-agent) confirmation signaling: `request_headless_confirmation()` and `signal_headless_confirmation()`. The class absorbs the headless confirmation event/result dicts and the default timeout from `BuiltinExecutor`.
- **Atomic sub-agent approve-all**: `signal_headless_confirmation(token, approved, approve_all=False, tool_name="")` adds to `auto_approve_tools` AND sets the event in one call, eliminating the race condition where `cb_subagent_confirm` mutates the set directly while a sub-agent thread reads it.
- **Remove entangled state from `BuiltinExecutor`**: `_headless_confirm_events`, `_headless_confirm_results`, `_prompt_approval_set`, `_subagent_confirm_timeout` are deleted. `_subagent_confirm_prompt_fn` stays on `BuiltinExecutor` (set by `main.py` as today); the bridge passes it to the coordinator at call time so the coordinator never stores transport-specific callbacks.
- **Simplify Telegram callbacks**: `cb_subagent_confirm` calls a single `coordinator.signal_headless_confirmation()` instead of reaching into two objects via two separate code paths.
- **Wire coordinator reference**: `agent_controller.py` sets `builtin._coordinator = self._confirmation` at run start (replacing `_prompt_approval_set`), and sets it to `None` after run (fail-closed).
- **No Telegram UI changes**: inline buttons, prompts, timeouts, and approve-all semantics are externally identical.
- **No constructor signature changes**: `BuiltinExecutor.__init__` loses the now-unused internal dicts but keeps the same parameter list. Param reduction is C-07's scope.

## Capabilities

### New Capabilities

(None — no new externally observable behavior. The coordinator is an internal restructuring of existing confirmation flows.)

### Modified Capabilities

- `builtin-tool-execution`: The two-phase confirmation gating framework absorbs the sub-agent headless confirmation flow (depth ≥ 1) into `ConfirmationManager`, which already owns the other three flows (tool confirm at depth 0, step extension, LLM error retry). Sub-agent approve-all is atomic (set mutation + event signal in one method call). The existing requirement "Headless confirm bridge checks the shared approval set" is rewritten: the bridge checks `auto_approve_tools` on the coordinator (no longer via `_prompt_approval_set` on `BuiltinExecutor`), and fail-closed is `coordinator is None` (not `_prompt_approval_set is None`).

## Impact

- **`confirmation.py`**: Add `request_headless_confirmation()` / `signal_headless_confirmation()` methods + `_headless_confirm_events` / `_headless_confirm_results` dicts + `default_headless_timeout` field.
- **`builtin_executor.py`**: Remove `_headless_confirm_events`, `_headless_confirm_results`, `_prompt_approval_set`, `_subagent_confirm_timeout`. Keep `_subagent_confirm_prompt_fn` (set by `main.py`, passed to coordinator at call time). Add `_coordinator` reference. Refactor `_headless_confirm_bridge` to delegate wait/signaling to coordinator.
- **`agent_controller.py`**: Replace `builtin._prompt_approval_set = self._confirmation.auto_approve_tools` with `builtin._coordinator = self._confirmation`. Replace `builtin._prompt_approval_set = None` with `builtin._coordinator = None`.
- **`telegram_callbacks.py`**: `cb_subagent_confirm` calls `coordinator.signal_headless_confirmation()` (atomic) instead of `builtin.signal_headless_confirm()` + direct `auto_approve_tools.add()`.
- **`main.py`**: No change — `builtin._subagent_confirm_prompt_fn = tg.send_subagent_confirmation_prompt` stays as-is. The bridge passes it to the coordinator at call time.
- **`builtin_tools/secrets_log.py`**: Calls `_headless_confirm_bridge` — no signature change, but the bridge now reads `_coordinator` instead of `_prompt_approval_set`.
- **Tests**: `test_p1_subagent_confirm.py`, `test_subagent_approve_all.py`, `test_prompt_approval_ttl.py`, `test_supervisor_prompt_wiring.py` — update to use coordinator methods instead of direct field access. New test for atomic approve-all.
- **`vulture_whitelist.py`**: Update if new public symbols are flagged.