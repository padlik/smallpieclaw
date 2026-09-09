# Review Log — step-notifications-doom-loop

## proposal Round 2 — 2026-09-08

### 🔴 Fixed
- All three caps (max_iterations, scheduled_max_iterations, plan_max_iterations) now named with explicit fate
- Risk statement added for unattended scheduled/plan agents
- schedule tool descriptor/schema removal added to Impact
- step_notify_interval=0 semantics stated
- Sub-agents NOT getting milestone notifications stated as explicit requirement
- Misleading step displays addressed in Impact
- README update added to Impact
- Full dead-code set (_handle_extend_progress, _send_extend_prompt, __EXTEND__: signal, telegram_callbacks.py handlers) listed

### 🟡 Addressed
- Tool name `schedule_task` → `schedule` (add-action) corrected in What Changes and Impact table

### 🔴 Outstanding
*(none — proposal frozen)*

## design Round 2 — 2026-09-08

### 🔴 Fixed
- D1: actual nested loop structure now described (outer while True / inner while step < max_steps)
- D2: failure predicate (outcome.get("success") is False) and error source (outcome.get("error","")) explicitly defined

### 🟡 Addressed (soft-freeze additions)
- D3: both ReactContext fields declared (step_notify_interval + milestone_notify_fn) with wiring location
- D3: fire-and-forget mechanism specified (run_coroutine_threadsafe, Future discarded)
- D1: apply-time verification note added for operator_cancelled between-loop check
- D1: sub_agent_supervisor.py:457 added to denominator locations

### 🔴 Outstanding
*(none — design frozen)*

---

## design Round 1 — 2026-09-08

### 🔴 Outstanding (resolved in Round 2)
- D1 misdescribed the loop — actual nested structure not shown, single-loop collapse unspecified
- D2 did not define the tool failure predicate or error source

### 🟡 Addressed in Round 2
- 10M max_steps rationale corrected (checkpoint schema, not plan hint)
- state.step > 0 guard added to milestone check
- UI denominator locations enumerated
- D5 added for schedule tool max_iterations removal
- RunPanel always-present claim documented

---

## tasks + adr Round 1 — 2026-09-09

### 🔴 Fixed
- T2/ADR-0026: field names aligned to frozen design/specs (`_tool_fail_repeat`, `_last_tool_fail_key`); reset value corrected to `""` (not `None`); `_DOOM_LOOP_LIMIT = 3` constant added
- T6: "add deprecation warnings on load" option removed; silently-ignored-only per frozen proposal/explore-brief

### 🟡 Addressed
- T1: `_EFFECTIVELY_UNLIMITED_STEPS` definition step added; operator-cancel apply-time verification added to acceptance
- T9: backward-compat acceptance added for `scheduler.toml` entries **with** `max_iterations`
- T10: scheduled-agent doom-loop structural coverage note added; operator-cancel regression check added
- ADR-0028: "unreachable code path" → "no longer called by the react loop" (consistent with explore-brief "other callers may exist")

### 🔴 Outstanding
*(none — tasks and adr frozen)*

---

## proposal Round 1 — 2026-09-08

### 🔴 Outstanding (blockers — not yet fixed)

1. **Three caps feed `max_steps`, not one.** The gate removal in `react_loop.py` is universal (`while state.step < state.max_steps`). But `max_steps` is populated from three separate sources: `max_iterations` (main agent, default 8), `scheduled_max_iterations` (default 100, in `agent_runtime.py` / `main.py`), and `plan_max_iterations` (in `execution_plan.py` / `agent_controller.py`). Proposal names only `max_iterations`. Fate of the other two is unresolved.

2. **Removing scheduled cap eliminates the only runtime bound on unattended background agents.** Doom-loop only catches identical repeated `(tool_name, error[:200])` — it does not backstop varied-error loops or slow forward progress. Proposal says "scheduled jobs inherit unlimited runtime" without a risk statement. This is a real budget-safety regression.

3. **`schedule_task` tool `max_iterations` override becomes a silent no-op.** `builtin_tools/descriptors.py:74-75` documents this parameter to the LLM; it's plumbed through schemas, agents.py, scheduler.py. After gate removal it does nothing. Descriptor/schema must be updated or the decision to accept-but-ignore must be stated.

### 🟡 Should Fix

4. `step_notify_interval = 0` semantics (disabled-path guard against `%0` division) not stated.
5. Sub-agents explicitly receiving NO milestone notifications not stated as a requirement.
6. Misleading step displays (`Step: X/max_iterations` in `telegram_commands.py:983`, `sub_agent_supervisor.py:457`) — fate after deprecation not addressed.
7. README / AGENTS.md: Extend buttons and `max_iterations` docs need updating (user-visible behavior change).
8. Dead-code retirement scope: `_handle_extend_progress`, `_send_extend_prompt`, `__EXTEND__:` signal path, extend-button callbacks in `telegram_callbacks.py` — all orphaned, not listed in Impact.
