# ADR — step-notifications-doom-loop

## ADR-0025: Collapse nested loop to single `while True:` (D1)

**Status:** Accepted

**Context:** `react_loop.py` contains a nested loop structure: an outer `while True:` that handles operator-cancel checks, and an inner `while state.step < state.max_steps:` that implements the step gate. The inner loop cap was the primary mechanism for bounding agent runtime. With the step gate removed, the nested structure is unnecessary.

**Decision:** Collapse both loops into a single `while True:`. `_LoopState.max_steps` is set to `_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000` (retained for checkpoint schema compatibility and `_run_plan` compatibility — not as a runtime gate). `_handle_step_limit_reached` is deleted. The operator-cancel check between loops is audited at apply time and removed if redundant.

**Consequences:**
- Agents no longer terminate due to step count.
- Code is simpler — one loop, one exit path per condition.
- `_EFFECTIVELY_UNLIMITED_STEPS` sentinel must be preserved for checkpoint schema compat.

---

## ADR-0026: Doom-loop detection via composite `(tool_name, error[:200])` key (D2)

**Status:** Accepted

**Context:** Without a step cap, agents can get stuck repeating the same failing tool call indefinitely. A safety backstop is needed that does not terminate agents doing legitimate long-running work.

**Decision:** Track `_tool_fail_repeat: int = 0` and `_last_tool_fail_key: str = ""` in `_LoopState` (field names chosen to mirror `json_fail_streak` / `_JSON_FAIL_LIMIT = 3`). Define `_DOOM_LOOP_LIMIT = 3` as a module-level constant. After each tool failure, compute `key = f"{tool_name}:{(outcome.get('error','') or '').strip()[:200]}"`. If the same key appears `_DOOM_LOOP_LIMIT` consecutive times, the loop self-terminates with an explanatory abort message. Any success or a different failure key resets `_tool_fail_repeat` to `0` and `_last_tool_fail_key` to `""`.

**Consequences:**
- Protects against identical-error tight loops (most common stuck pattern).
- Does NOT protect against varied-error loops or slow forward progress on genuinely bad goals. Accepted risk — doom-loop is a repetition detector, not a budget cap.
- Truncating error text to 200 chars prevents key explosion on verbose errors.

---

## ADR-0027: Milestone notification via optional callback (transport-agnostic) (D3)

**Status:** Accepted

**Context:** Users need awareness when an agent has been running for many steps. The notification mechanism must not block the agent loop and must work across different transport layers (Telegram, headless, test).

**Decision:** Add `step_notify_interval: int = 0` and `milestone_notify_fn: Optional[Callable[[int], None]]` to `ReactContext`. The main agent controller wires `milestone_notify_fn = run_panel.milestone_notify` and reads `step_notify_interval` from config (default 30). Sub-agents receive `milestone_notify_fn = None`. In the loop, the callback is invoked fire-and-forget via `asyncio.run_coroutine_threadsafe`; the returned `Future` is discarded.

**Consequences:**
- Transport-agnostic: test code can pass a plain sync lambda; Telegram uses the RunPanel method.
- No blocking: agent loop never awaits the notification.
- Sub-agents never send milestone notifications (by construction, not by config).
- `step_notify_interval = 0` disables notifications (no modulo-zero risk).

---

## ADR-0028: Extension flow made dormant, not deleted (D4)

**Status:** Accepted

**Context:** `ConfirmationManager.request_extension()` and the `__EXTEND__:` signal path implement a step-extension flow that is no longer needed after gate removal. ADR-0024 defines a four-flow taxonomy (tool confirm, headless confirm, extension, LLM retry). Deleting the extension flow coordination code would violate ADR-0024's taxonomy.

**Decision:** `request_extension()` is made dormant — it remains in `ConfirmationManager` but is no longer called by the react loop. The Telegram UI dead code (`_handle_extend_progress`, `_send_extend_prompt`, extend-button callbacks in `telegram_callbacks.py`) is deleted because it is presentation-layer, not coordination-layer. ADR-0024 remains in force; the extension coordination slot is reserved.

**Consequences:**
- ADR-0024 taxonomy preserved.
- Telegram UI is clean: no extend buttons, no extend prompts.
- Future re-implementation of step-extension (e.g., for a revised `/autoapprove` or `/autoextend` feature) can reuse the dormant slot without an ADR revision.
