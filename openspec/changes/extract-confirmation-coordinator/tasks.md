## 1. Extend ConfirmationManager with headless confirmation flow

- [x] 1.1 Add `_headless_confirm_events: dict[str, threading.Event]` and `_headless_confirm_results: dict[str, bool]` dicts to `ConfirmationManager.__init__`
- [x] 1.2 Add `default_headless_timeout: int = 120` field to `ConfirmationManager.__init__`
- [x] 1.3 Implement `request_headless_confirmation(token, tool_name, description, prompt_fn, caller_tag="") -> bool` — creates Event, stores in `_headless_confirm_events`, calls `prompt_fn(token, tool_name, description, caller_tag)`, blocks on `event.wait(self.default_headless_timeout)`, cleans up on return, returns approved bool
- [x] 1.4 Implement `signal_headless_confirmation(token, approved, approve_all=False, tool_name="") -> bool` — atomically: if `approve_all and approved and tool_name`, add `tool_name` to `auto_approve_tools`; pop event from `_headless_confirm_events`, write result to `_headless_confirm_results`, set event; return True if found, False if expired/resolved
- [x] 1.5 Verify `make check` passes with new methods (no callers yet — additive only)

## 2. Wire coordinator reference on BuiltinExecutor

- [x] 2.1 Add `_coordinator: Optional[ConfirmationManager] = None` field to `BuiltinExecutor.__init__`
- [x] 2.2 Update `agent_controller.py` run start: replace `self.builtin_executor._prompt_approval_set = self._confirmation.auto_approve_tools` with `self.builtin_executor._coordinator = self._confirmation`
- [x] 2.3 Update `agent_controller.py` run end (finally block): replace `self.builtin_executor._prompt_approval_set = None` with `self.builtin_executor._coordinator = None`
- [x] 2.4 Verify import cycle guard: `python -c "import main"` and `import builtin_executor`

## 3. Refactor _headless_confirm_bridge to delegate to coordinator

- [x] 3.1 Add entry guard at top of `_headless_confirm_bridge`: if `self._coordinator is None`, return fail-closed error immediately (operation blocked, confirmation bridge unavailable) — matches design Risks behavior-change note
- [x] 3.2 Replace `self._prompt_approval_set` check with `self._coordinator.auto_approve_tools` check (same logic, different reference)
- [x] 3.3 Replace the manual Event creation + `_headless_confirm_events` storage + `_subagent_confirm_prompt_fn` call + `event.wait(timeout)` block with a single call to `self._coordinator.request_headless_confirmation(token, tool_name, description, prompt_fn=self._subagent_confirm_prompt_fn, caller_tag=caller_tag)` (uses coordinator's `default_headless_timeout`; tests override via `coordinator.default_headless_timeout = 0`) — updated: `timeout` param removed from signature per oracle review
- [x] 3.4 Keep `_subagent_confirm_prompt_fn` on `BuiltinExecutor` (set by `main.py`, passed to coordinator at call time)
- [x] 3.5 Verify `secrets_log.py:61` call to `_headless_confirm_bridge` still works (signature unchanged)

## 4. Update Telegram callbacks to use coordinator

- [x] 4.1 Update `cb_subagent_confirm` in `telegram_callbacks.py`: replace `builtin.signal_headless_confirm(token, approved)` with `coordinator.signal_headless_confirmation(token, approved, approve_all=is_approve_all, tool_name=tool_name)` — single atomic call
- [x] 4.2 Remove the direct field mutation `iface.agent._confirmation.auto_approve_tools.add(tool_name)` — now handled atomically by `signal_headless_confirmation` with `approve_all=True`
- [x] 4.3 Update the deny path for non-allowed approve-all tools: call `coordinator.signal_headless_confirmation(token, False)` instead of `builtin.signal_headless_confirm(token, False)`
- [x] 4.4 Access coordinator via `iface.agent._confirmation` (the ConfirmationManager instance) — no new wiring needed, the coordinator IS the confirmation manager

## 5. Update tests (migrate all removed-symbol references before section 6 removes them)

- [x] 5.1 `test_p1_subagent_confirm.py`: replace `executor.signal_headless_confirm(token, approved)` with `coordinator.signal_headless_confirmation(token, approved)`; replace `executor._headless_confirm_events` assertions with `coordinator._headless_confirm_events`; replace `exe._subagent_confirm_timeout = 1` with `coordinator.default_headless_timeout = 1`; update module docstring (line 3) to reference `coordinator.signal_headless_confirmation`
- [x] 5.2 `test_subagent_approve_all.py`: replace direct `auto_approve_tools.add()` mutations with `coordinator.signal_headless_confirmation(token, approved, approve_all=True, tool_name=...)`; replace `executor._headless_confirm_events` with `coordinator._headless_confirm_events`; replace `patch.object(executor, "_subagent_confirm_timeout", 0)` with `patch.object(coordinator, "default_headless_timeout", 0)`; replace `executor._prompt_approval_set = ... or set()` / `.add("file_read")` (lines 155-156) with `executor._coordinator = coordinator` where `coordinator.auto_approve_tools = {"file_read"}`; replace `executor._prompt_approval_set = {"shell"}` (line 166) with `executor._coordinator = coordinator` where `coordinator.auto_approve_tools = {"shell"}`; update stale comment at line 188 referencing `signal_headless_confirm`
- [x] 5.3 `test_prompt_approval_ttl.py`: replace all `executor._prompt_approval_set = {"file_read"}` (lines 31, 74, 98) with `executor._coordinator = coordinator` where `coordinator.auto_approve_tools = {"file_read"}`; replace `executor._prompt_approval_set = set()` (line 43) with `executor._coordinator = coordinator` where `coordinator.auto_approve_tools = set()`; replace `executor._prompt_approval_set = None` (line 55, fail-closed case) with `executor._coordinator = None`; replace `patch.object(executor, "_subagent_confirm_timeout", 0)` with `patch.object(coordinator, "default_headless_timeout", 0)`
- [x] 5.4 `test_supervisor_prompt_wiring.py`: replace `executor._prompt_approval_set = sentinel_set` with `executor._coordinator = sentinel_coordinator` where sentinel has `auto_approve_tools` attribute; update comment (line 87) to reference `_coordinator` instead of `_prompt_approval_set`
- [x] 5.5 `test_p2_graph_memory_admission.py`: replace `b.signal_headless_confirm(tokens_seen[0], True/False)` at lines 294 and 321 with `coordinator.signal_headless_confirmation(tokens_seen[0], True/False)` — access coordinator via the executor's `_coordinator` reference or a shared `ConfirmationManager` instance
- [x] 5.6 Add new test: `signal_headless_confirmation` with `approve_all=True` atomically adds to `auto_approve_tools` AND sets the event — no concurrent reader can see the set without the event being set
- [x] 5.7 Add new test: `_headless_confirm_bridge` returns fail-closed error when `_coordinator is None` (orphaned sub-agent after run end)
- [x] 5.8 Verify `test_confirmation_retry.py` passes unchanged (ConfirmationManager's existing 3 flows untouched)
- [x] 5.9 Verify `test_react_loop_error_recovery.py` passes unchanged (retry flow untouched)
- [x] 5.10 Verify `test_telegram_error_card.py` passes unchanged (LLM error card untouched)
- [x] 5.11 Verify `test_grant_tracker_isolation.py` passes unchanged (zone paths/trackers untouched)

## 6. Remove old state and cleanup

- [x] 6.1 Remove `signal_headless_confirm` method from `BuiltinExecutor` (moved to coordinator as `signal_headless_confirmation`) — after all callers migrated in sections 4-5
- [x] 6.2 Remove `_headless_confirm_events`, `_headless_confirm_results`, `_subagent_confirm_timeout`, `_prompt_approval_set` fields from `BuiltinExecutor.__init__` — after all references migrated in sections 4-5
- [x] 6.3 Update `vulture_whitelist.py` if new public symbols on `ConfirmationManager` are flagged
- [x] 6.4 Run `ruff check .` and fix any lint errors
- [x] 6.5 Run `vulture . vulture_whitelist.py --min-confidence 80` and fix any dead code findings
- [x] 6.6 Run `make check` (full lint + test suite) — must pass with zero new failures
- [x] 6.7 Run `openspec validate extract-confirmation-coordinator --type change --strict` before archive