# Review Implementation Plan

Generated from the code review of `smallpieclaw`. This plan covers 7 work items in priority order. Each item lists scope, files, approach, validation, and effort.

> **Branch discipline (AGENTS.md):** All implementation must happen on a feature/fix branch (single agent) or workspace (parallel agents). Never code directly in `main`.

---

## Item 1 — TD-01: Strategy memory wiring audit

### Status: RECON FOUND THE ORIGINAL FINDING IS A FALSE POSITIVE — REVISED SCOPE

The original review (TD-01) claimed `AgentController.__init__` never sets `self.strategy_memory`, making the feature dead on the main path. **Recon disproves this:** `main.py:545` performs post-construction injection `agent.strategy_memory = strategy_mem` (the same pattern used for `_graph_memory` at `:688`). The feature IS wired for the main agent.

**The real issue is narrower:** `getattr(controller, "strategy_memory", None)` at `agent_runtime.py:243` is still a defensive read on a non-declared attribute, and `SubAgentRunner` does NOT propagate `strategy_memory` to child `AgentController` instances (grep of `agent_controller.py` shows zero `strategy_memory` references in `SubAgentRunner`). So sub-agents never get strategy memory — which may be intentional (sub-agents are short-lived) but is undocumented.

### Scope
- **Decision required:** wire strategy_memory into sub-agents, or document that it's main-agent-only.
- **Cleanup:** replace `getattr(controller, "strategy_memory", None)` with a typed attribute on `AgentController` (declare `self.strategy_memory: Optional[StrategyMemory] = None` in `__init__`), keeping the `main.py:545` injection working but making the wiring explicit instead of duck-typed.

### Files
- `agent_controller.py:118-120` (add `self.strategy_memory = None` next to `_graph_memory`)
- `agent_runtime.py:243` (drop `getattr`, use `controller.strategy_memory`)
- `react_loop.py:632, 1215` (drop `getattr`, use `ctx.strategy_memory` directly — it's a declared dataclass field at `:218`)
- `agent_controller.py:459-540` (`SubAgentRunner.__init__` — decide whether to accept + forward `strategy_memory`)
- `main.py:545` (keep, but the `# type: ignore[attr-defined]` becomes unnecessary once the attribute is declared)

### Approach
1. Declare `self.strategy_memory: Optional[object] = None` in `AgentController.__init__` (next to `self._graph_memory = None` at `:118`).
2. Replace `getattr(controller, "strategy_memory", None)` → `controller.strategy_memory` in `agent_runtime.py:243`.
3. Replace `getattr(ctx, "strategy_memory", None)` → `ctx.strategy_memory` in `react_loop.py:632, 1215` (field is declared at `:218`).
4. Decide on sub-agent propagation: if main-agent-only is intentional, add a comment at `SubAgentRunner.__init__` noting the omission; if sub-agents should have it, add `strategy_memory=None` param to `SubAgentRunner.__init__` and forward to the child `AgentController`.
5. Remove `# type: ignore[attr-defined]` from `main.py:545`.

### Validation
- `ruff check .`
- `vulture . vulture_whitelist.py --min-confidence 80`
- `pytest tests/test_strategy_memory.py tests/test_agent_runtime_characterization.py -v`
- `make check` (full suite: 1627 passed, 1 skipped baseline)

### Effort: S (<2h)

---

## Item 2 — OS-04: Per-sub-agent GrantTracker isolation

### Problem
`BuiltinExecutor` creates a single `GrantTracker` at `builtin_executor.py:242`. `SubAgentRunner` reuses the shared `BuiltinExecutor` (`agent_controller.py:466` — `builtin_executor` is shared). File tools read it via `getattr(self._owner, "grant_tracker", None)` at `builtin_tools/files.py:81`. Concurrent sub-agents share grant sets and can clear each other's per-request grants.

### Scope
Isolate `GrantTracker` per agent/sub-agent run, without breaking the shared-executor design.

### Files
- `builtin_executor.py:242` (GrantTracker creation)
- `builtin_tools/files.py:79-81` (`_request_grants` reads `self._owner.grant_tracker`)
- `agent_controller.py:189-205` (the `_prompt_approval_set` / `_current_prompt_id` pattern — this is the existing per-run state injection on the shared executor; GrantTracker isolation should follow the same pattern)
- `sub_agent_supervisor.py` (sub-agent lifecycle — where per-run state is set/cleared)

### Approach
The codebase already has a pattern for per-run state on the shared executor: `AgentController.run()` sets `self.builtin_executor._prompt_approval_set` at `:190` and clears it at `:204`. GrantTracker should follow the same push/pop pattern:

1. Add `self._grant_tracker_stack: list[GrantTracker] = []` to `BuiltinExecutor.__init__`.
2. Add methods `push_grant_tracker(self, gt: GrantTracker) -> None` and `pop_grant_tracker(self) -> None` that push/pop onto the stack; `self.grant_tracker` becomes a property returning `self._grant_tracker_stack[-1]` if non-empty, else a default instance.
3. In `AgentController.run()` (depth 0, alongside the `_prompt_approval_set` set at `:190`): `self.builtin_executor.push_grant_tracker(GrantTracker())`; in the finally block (`:204`): `self.builtin_executor.pop_grant_tracker()`.
4. In `SubAgentRunner.run()` (or wherever the child `AgentController.run()` is invoked): the child controller's `run()` will push its own tracker — so each sub-agent run gets isolation automatically via the stack.
5. `builtin_tools/files.py:81` unchanged — `getattr(self._owner, "grant_tracker", None)` now reads the property, which returns the top of the stack (the current run's tracker).

### Validation
- `ruff check . && vulture . vulture_whitelist.py --min-confidence 80`
- `pytest tests/ -v -k "grant or access_control or files"` — verify existing access-control tests still pass
- Add a test: two concurrent sub-agents, each grants a different path, verify they can't see each other's grants
- `make check`

### Effort: M (2–8h) — the push/pop pattern is proven, but concurrency testing adds time

---

## Item 3 — TD-03 + TD-05: Thread-safety + edge-case crash

### TD-03: `_active_runners` race in `PlanExecutor`

**Problem:** `self._active_runners: dict[str, Any] = {}` (`execution_plan.py:335`) is mutated from step threads (`__setitem__` at `:543`, `pop` at `:554`) while the driver thread iterates (`list(self._active_runners.items())` at `:758, :873`) and pops (`:770, :799`). Racy under `max_concurrent > 1`.

**Files:** `execution_plan.py:335, 541-554, 758, 770, 799, 873-879`

**Approach:**
1. Add `self._active_lock = threading.Lock()` to `PlanExecutor.__init__` (next to `:335`).
2. Wrap the `__setitem__` at `:543` and `pop` at `:554` in `with self._active_lock:`.
3. Wrap the iteration at `:758` and `:873` in `with self._active_lock: items = list(self._active_runners.items())` then iterate the snapshot outside the lock.
4. Wrap the pops at `:770` and `:799` in `with self._active_lock:`.
5. Keep the lock critical sections short — only the dict access, not the cancel/await logic.

**Validation:** `pytest tests/test_execution_plan.py -v`; add a test with `max_concurrent=4` and a plan that triggers cancel-during-iteration.

### TD-05: Non-JSON string `args` from shorthand action crashes `_dispatch_tool`

**Problem:** `_normalize_shorthand_action` (`react_loop.py:706-732`) keeps a non-JSON string `args` as a raw `str`. `_dispatch_tool` at `:1255-1256` only coerces `list`→dict; a bare `str` hits `.items()`/`.get()` and raises.

**Files:** `react_loop.py:725-732` (shorthand normalization), `:1254-1256` (dispatch coercion)

**Approach:**
1. In `_normalize_shorthand_action`, when `args` is a string that isn't JSON-parseable, wrap it: `args = {"_raw": args}` (or return a clear protocol error: `{"error": "shorthand action args must be JSON object or JSON string, got raw string"}`).
2. Alternatively/additionally, in `_dispatch_tool` at `:1255`, extend the coercion: `if isinstance(args, str): args = {"_raw": args}` before the `list` check. This is a defense-in-depth fix.
3. Add a test: shorthand action with `args: "some raw text"` → should not raise, should pass `{"_raw": "some raw text"}` to the tool (or return a protocol error, depending on chosen approach).

**Validation:** `pytest tests/test_react_loop.py -v -k "shorthand or dispatch or normalize"`; add test for the string-args edge case.

### Combined effort: S (<2h) — both are small, localized fixes

---

## Item 4 — OS-02 + OS-03 + IDIOM-06 + IDIOM-07: Config/deprecation/precedence sweep

### OS-02: Config rejects `sse` transport but client supports it

**Problem:** `_parse_mcp_server` at `config_schema.py:686` only allows `("stdio", "http")`, but `mcp_client.py:69` imports `sse_client` and `:368-374` has a live `transport == "sse"` branch.

**Files:** `config_schema.py:686-693`; `config_schema.py:477` (comment on `MCPServerConfig.transport`)

**Approach:**
1. Change `:686`: `if transport not in ("stdio", "http", "sse"):`
2. Add `sse` to the `url` requirement: `if transport in ("http", "sse") and not entry.get("url"):`
3. Update the comment at `:477`: `transport: str  # "stdio" | "http" | "sse"`
4. Update the error message at `:688` to include `sse`.

### OS-03: `OAuthConfig` omits `timeout` field

**Problem:** `OAuthConfig` (`config_schema.py:452-465`) has no `timeout` field, but runtime reads `oauth_cfg.get("timeout", 300)` at `mcp_client.py:975,1135` and `mcp_oauth.py:429`.

**Files:** `config_schema.py:452-465` (`OAuthConfig` dataclass); `config_schema.py:638-665` (`_parse_oauth`); `mcp_client.py:975,1135`; `mcp_oauth.py:429`

**Approach:**
1. Add `timeout: int = 300` to `OAuthConfig` dataclass (after `callback_bind` at `:460`).
2. In `_parse_oauth`, parse it: `timeout=_parse_int(entry.get("timeout"), 300, f"mcp_servers.{server_name}.oauth.timeout")`.
3. **Note:** The runtime consumers read from a raw dict (`oauth_cfg.get("timeout", 300)`), not from the `OAuthConfig` dataclass. The longer-term fix is to feed `MCPManager` from typed `AppConfig.mcp_servers`. For now, the dataclass field at least documents the contract and validates if/when the migration happens. Add a comment noting the raw-dict consumers.
4. Update `vulture_whitelist.py` if the new field is flagged.

### IDIOM-06: `datetime.utcnow()` deprecated on Python 3.14

**Problem:** `memory_store.py` uses `datetime.utcnow()` at `:169, 261, 267, 344, 558`. On Python 3.14 this emits `DeprecationWarning` and returns naive datetimes. `graph_memory.py` already uses the correct `datetime.now(timezone.utc)`.

**Files:** `memory_store.py:169, 261, 267, 344, 558`

**Approach:**
1. Ensure `from datetime import datetime, timezone` is imported at the top of `memory_store.py`.
2. Replace all 5 occurrences of `datetime.utcnow()` with `datetime.now(timezone.utc)`.
3. Check whether downstream consumers expect naive datetimes (ISO strings from `.isoformat()` will now include `+00:00` offset). If any comparison or parsing assumes naive, adjust. Grep for `.isoformat()` consumers of these fields.

### IDIOM-07: Unparenthesized `or`/`and` precedence in shell exit classification

**Problem:** `builtin_tools/shell.py:570` — `elif "command not found" in combined or "not found" in error_lower and "file" not in error_lower:` — `and` binds tighter than `or`, parsing as `A or (B and C)`. Probably the intent, but fragile.

**Files:** `builtin_tools/shell.py:570`

**Approach:** Add explicit parentheses: `elif "command not found" in combined or ("not found" in error_lower and "file" not in error_lower):`

### Combined validation
- `ruff check . && vulture . vulture_whitelist.py --min-confidence 80`
- `pytest tests/test_config_schema.py tests/test_memory_store.py tests/test_shell.py tests/test_mcp_client.py -v`
- `make check`

### Combined effort: S (<2h) — four small, independent fixes

---

## Item 5 — INH-01: `fail_outcome()` helper

### Problem
The "standard failure outcome" dict — `{"success": False, "output": "", "error": ..., "exit_code": -1, "error_type": "", "recoverable": False, "suggestion": ""}` — is hand-constructed at ~12 sites and already drifting (some copies omit `error_type/recoverable/suggestion`).

### Files
- `execution_plan.py:383, 425, 518, 598, 772, 784, 801, 822, 845, 892` (10 sites)
- `react_loop.py:1312, 1379` (2 sites)

### Approach
1. Create a module-level helper in `execution_plan.py` (or a shared `outcome_utils.py` if both files need it — but since `react_loop.py` already imports from `execution_plan` indirectly, check the import graph):
   ```python
   def fail_outcome(
       error: str,
       *,
       error_type: str = "",
       recoverable: bool = False,
       suggestion: str = "",
       exit_code: int = -1,
       output: str = "",
   ) -> dict:
       """Standard failure outcome dict — single-sourced to prevent drift."""
       return {
           "success": False,
           "output": output,
           "error": error,
           "exit_code": exit_code,
           "error_type": error_type,
           "recoverable": recoverable,
           "suggestion": suggestion,
       }
   ```
2. Replace each of the ~12 hand-constructed dicts with a `fail_outcome(...)` call, preserving the exact field values at each site (some omit fields — the helper's defaults handle that).
3. If `react_loop.py` can't import from `execution_plan.py` (cycle risk), place the helper in a neutral module (e.g. `outcome_utils.py` or add to an existing shared utils module). Check `react_loop.py` imports first.
4. Update `vulture_whitelist.py` if needed.

### Validation
- `ruff check . && vulture . vulture_whitelist.py --min-confidence 80`
- `pytest tests/test_execution_plan.py tests/test_react_loop.py -v`
- `make check`
- Manually diff each replacement to confirm no field values changed (the helper must produce identical dicts).

### Effort: S (<2h) — mechanical replacement, but careful diffing required

---

## Item 6 — LM-01 + TD-02: Decompose `PlanExecutor.execute`

### Problem
`PlanExecutor.execute` (`execution_plan.py:608-935`) is ~327 lines mixing batch scheduling, timeout math, cancel-bridge thread lifecycle, future-wait polling, four near-identical result-classification blocks, and final summary assembly. The four cancellation-classification blocks (`:768-837`) differ only in `cancelled_by_parent` and "completed during grace" vs "still running" (~70 lines of near-duplicate).

### Files
- `execution_plan.py:608-935` (`execute` method)
- `execution_plan.py:768-837` (four duplicated classification blocks — TD-02)

### Approach
Decompose into focused helpers. Target: `execute` drops to <60 lines.

1. **`_run_batch(self, batch, ctx, factory, cancel_event) -> list[future]`** — schedules a batch of step threads, returns futures. Extracts the batch-scheduling + thread-spawn logic.
2. **`_drain_pending(self, pending, cancel_event, backoff) -> list[completed]`** — the future-wait polling loop. Extracts the `cancel_event.wait(backoff)` + future-completion checking.
3. **`_classify_incomplete(self, sid, *, cancelled_by_parent: bool, completed_in_grace: bool) -> tuple[dict, str]`** — the four duplicated classification blocks collapse into one helper returning `(outcome_dict, error_line)`. The four call sites become two-line calls.
4. **`_build_summary(self, results) -> dict`** — final summary assembly.
5. `execute` becomes: setup → loop (run batch → drain → classify → check cancel) → summary.

### Validation
- `ruff check . && vulture . vulture_whitelist.py --min-confidence 80`
- `pytest tests/test_execution_plan.py -v` — all existing tests must pass unchanged (behavior-preserving refactor)
- Add tests for `_classify_incomplete` directly (the four cases: cancelled-by-parent, cancelled-by-plan, completed-in-grace, still-running)
- `make check`

### Effort: L (>1 day) — this is the biggest method in the biggest file; careful behavior-preserving decomposition with test coverage at each step

### Delegation note
This is a bounded refactor with clear scope. Delegate to @fixer with a precise spec: "Extract these 4 helpers, preserve exact behavior, all existing tests must pass." The @fixer should work in small increments (one helper at a time), running tests after each extraction.

---

## Item 7 — TD-14: Re-scope review of `scheduler.py` + `sub_agent_supervisor.py`

### Problem
The oracle review lane hit a tool loop and never returned content for `scheduler.py` (1176 LOC, 5th-largest file) and `sub_agent_supervisor.py` (359 LOC). Both are unreviewed — a coverage gap.

### Scope
Read-only review (no implementation). Same categories as the original review: inheritance/duplication, large methods, idiomatic Python, tech debt.

### Files to review
- `scheduler.py` (1176 LOC) — cron jobs via `scheduler.toml`, uses `croniter`
- `sub_agent_supervisor.py` (359 LOC) — sub-agent lifecycle (admission, execution, cleanup, callbacks)

### Approach
Delegate to @oracle (read-only, `read_files` permission). Reuse `ora-1` session (`ses_00d499e05ffemHpuOBomk3ZjwC`) — it already has the core-runtime context loaded and these two files were in its original scope (the tool loop prevented reading them, but the session context is warm).

Prompt: "Complete the review of `scheduler.py` and `sub_agent_supervisor.py` that were missed in your earlier pass due to a tool loop. Same categories and output format as before: INH-xx, LM-xx, PY-xx, TD-xx with file:line locations and suggested fixes. Then a Section B rows table."

### Validation
- Review is read-only — no code validation needed
- Reconcile findings into the master findings list and Section B table

### Effort: M (2–8h) — review only, but 1176+359 LOC of careful reading

---

## Execution Order & Dependencies

```
Item 1 (TD-01)        ── S ── no deps ── do first (cheapest, clarifies wiring)
Item 2 (OS-04)        ── M ── no deps ── do second (security)
Item 3 (TD-03+TD-05)  ── S ── no deps ── parallel with Item 2 if different files
Item 4 (sweep)        ── S ── no deps ── parallel with Item 2/3
Item 5 (INH-01)      ── S ── no deps ── parallel
Item 6 (LM-01+TD-02) ── L ── no deps ── after Items 1-5 (biggest, benefits from clean baseline)
Item 7 (TD-14)       ── M ── no deps ── can run in parallel with everything (read-only)
```

**Parallelization opportunity:** Items 1, 3, 4, 5, 7 touch disjoint files and can run in parallel (different fixer lanes or workspaces). Item 2 touches `builtin_executor.py` + `agent_controller.py` which overlap with Item 1 — sequence 2 after 1. Item 6 touches `execution_plan.py` + `react_loop.py` which overlap with Items 3 and 5 — sequence 6 after 3 and 5.

**Recommended sequence:**
1. Items 1 + 7 in parallel (Item 1 is S-effort fix; Item 7 is read-only review)
2. Items 2 + 3 + 4 + 5 in parallel (disjoint files after Item 1 lands)
3. Item 6 last (biggest refactor, benefits from the clean baseline)

---

## Branch / Workspace Strategy

Per AGENTS.md dev discipline:
- **Single agent sequential:** one `feature/review-fixes` branch for Items 1-6, committing after each item.
- **Parallel agents:** use worktrees — e.g. `fix/strategy-memory-wiring`, `fix/grant-tracker-isolation`, `fix/thread-safety-args-crash`, `fix/config-deprecation-sweep`, `fix/fail-outcome-helper`, `refactor/plan-executor-decompose`. Merge in dependency order.

---

## Validation Gate (after all items)

```bash
make check    # lint + test — must yield 1627 passed, 1 skipped (baseline)
```

If any item adds new public API symbols, update `vulture_whitelist.py`.
If any item changes behavior, add or update tests.