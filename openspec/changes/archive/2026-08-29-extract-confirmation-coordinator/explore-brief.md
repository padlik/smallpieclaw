# Explore Brief — extract-confirmation-coordinator

## Problem

Two confirmation systems coexist with entangled shared state:
- **ConfirmationManager** (`confirmation.py`) — 3 flow types for depth-0 (main agent): tool confirm, step extension, LLM error retry
- **Headless bridge** (`builtin_executor.py`) — 1 flow type for depth≥1 (sub-agent): tool confirm only

Shared state across the boundary:
- `_pending` dict (both systems stage tools into the same dict on BuiltinExecutor)
- `auto_approve_tools` (owned by ConfirmationManager, read by headless bridge via `_prompt_approval_set` reference)
- Telegram callback `cb_subagent_confirm` mutates `auto_approve_tools` directly (race condition)

## Rejected Alternatives

1. **New ConfirmationCoordinator class** — rejected: extends existing ConfirmationManager instead (smallest diff, no import churn, same class identity)
2. **Move _pending into coordinator** — rejected: coordinator = signaling only, executor = staging+execution (single-responsibility)
3. **Run-scoping on coordinator** — rejected: keep external lifecycle (controller sets/resets coordinator ref at run boundaries)
4. **Two signal methods for headless approve-all** — rejected: one method with flag (atomic add-to-set + set-event)
5. **Store prompt_fn on coordinator** — rejected: pass at call time (coordinator stays transport-agnostic)
6. **Move zone_paths/zone_trackers to coordinator** — rejected: tool-execution context stays on executor
7. **Also do param reduction (C-07)** — rejected: C-16 scoped to coordinator extraction only

## Design Decisions

| Decision | Choice |
|----------|--------|
| Class structure | Extend ConfirmationManager in-place, no rename |
| _pending ownership | Stays on BuiltinExecutor |
| Run lifecycle | External — controller sets builtin._coordinator at run start, None after |
| Headless approve-all API | Single method `signal_headless_confirmation(token, approved, approve_all=False, tool_name="")` |
| Prompt function | Passed at call time, not stored on coordinator |
| Zone paths/trackers | Stay on BuiltinExecutor |
| Scope | Coordinator extraction only; no param reduction (C-07's job) |

## Confirmation Flow Inventory (7 flows)

| # | Flow | Depth | Blocks in | Timeout | Approve-all |
|---|------|-------|-----------|---------|-------------|
| 1 | Tool confirm (main) | 0 | react_loop | 300s | Yes (per-tool, per-prompt) |
| 2 | Tool confirm (sub) | ≥1 | _headless_bridge | 120s | Yes (shared set) |
| 3 | Shell hard-block (sub) | ≥1 | _requires_confirm | — | Never |
| 4 | Shell adaptive skip (main) | 0 | _should_confirm | — | N/A |
| 5 | Step extension | 0 | react_loop | 120s | N/A |
| 6 | LLM error retry | 0 | react_loop | 120s | N/A |
| 7 | Zone access grant | 0 | react_loop (via 1) | 300s | Via approve-all |

## State Migration Map

### Moves to ConfirmationManager
- `_headless_confirm_events: dict[str, threading.Event]`
- `_headless_confirm_results: dict[str, bool]`
- `default_headless_timeout: int` (120s, overridable per-request)

### Removed from BuiltinExecutor
- `_headless_confirm_events` (moved)
- `_headless_confirm_results` (moved)
- `_prompt_approval_set` (coordinator owns auto_approve_tools directly)
- `_subagent_confirm_timeout` (moved as default_headless_timeout)
- `_subagent_confirm_prompt_fn` (passed at call time, not stored)

### Stays on BuiltinExecutor
- `_pending: dict[str, tuple[str, dict]]` (tool staging)
- `_zone_paths: dict[str, str]` (zone context)
- `_zone_trackers: dict[str, GrantTracker]` (grant tracker per token)
- `confirm(token)` / `cancel(token)` (tool execution)
- `_requires_confirmation(...)` (decision logic)
- `_headless_confirm_bridge(...)` (delegates wait to coordinator)

## Cross-Module Data Flows

### Foreground agent (depth 0) — tool confirm
```
react_loop → builtin.execute("shell", args)
  → _requires_confirmation("shell", ...) → returns {requires_confirmation: True, token}
  → react_loop checks coordinator.auto_approve_tools
  → coordinator.request_confirmation(token, tool, desc, progress_cb)
     → blocks on threading.Event (300s timeout)
  ← Telegram callback: agent.resume(token, confirmed)
     → coordinator.signal_confirmation(token, confirmed)
  → react_loop calls builtin.confirm(token) or builtin.cancel(token)
```

### Background agent (depth ≥1) — sub-agent file confirm
```
sub-agent react_loop → builtin.execute("file_write", args)
  → _requires_confirmation("file_write", ...) → caller_depth ≥ 1
  → _headless_confirm_bridge("file_write", ...)
     → checks coordinator.auto_approve_tools (was _prompt_approval_set)
     → if not approved: coordinator.request_headless_confirmation(
         token, tool, desc, prompt_fn=self._subagent_confirm_prompt_fn,
         timeout=120, caller_tag=caller_tag)
       → blocks on threading.Event (120s timeout)
  ← Telegram callback: coordinator.signal_headless_confirmation(
       token, approved, approve_all=..., tool_name=...)
     → atomically: add to auto_approve_tools if approve_all, set event
  → bridge calls builtin.confirm(token)
```

### Step extension
```
react_loop → coordinator.request_extension(max_steps, progress_cb)
  → blocks on threading.Event (120s timeout)
← Telegram callback: agent.resume_extend(token, response)
  → coordinator.signal_extension(token, response)
→ react_loop applies extension
```

### LLM error retry
```
react_loop → coordinator.request_retry(token, error_json, progress_cb, timeout)
  → blocks on threading.Event (timeout)
← Telegram callback: agent.resume_llm_error(token, response)
  → coordinator.signal_retry(token, response)
→ react_loop retries or cancels
```

## Open Questions (Resolved)

All 7 grill questions resolved. No outstanding open questions.

## Verification Strategy

- All existing confirmation tests must pass unchanged: `test_confirmation_retry.py`, `test_p1_subagent_confirm.py`, `test_subagent_approve_all.py`, `test_prompt_approval_ttl.py`, `test_react_loop_error_recovery.py`, `test_telegram_error_card.py`, `test_grant_tracker_isolation.py`, `test_supervisor_prompt_wiring.py`
- New test: `signal_headless_confirmation` with `approve_all=True` atomically adds to set AND signals
- New test: direct-field-mutation bug is gone (no external code writes to `auto_approve_tools` except through coordinator methods)
- Import cycle guard: `python -c "import main"` and `import builtin_executor`
- `make check` (ruff + vulture + pytest) must pass
- Update `vulture_whitelist.py` if needed