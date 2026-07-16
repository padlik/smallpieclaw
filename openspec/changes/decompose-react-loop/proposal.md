# Proposal: Decompose `react_loop()`

## Why

`react_loop()` is a 660-line function (lines 486–1146) that implements the same four actions — `tool`, `create_tool`, `plan`, `vision_query` — in two separate code paths: one for native tool calls (Phase E) and one for json_mode (Phase G). These paths have already diverged in observable ways:

- **plan** (json_mode): emits neither `on_tool_trace` nor `working.add_step` → plans are invisible to trace consumers and working memory when the model uses json_mode
- **create_tool** (json_mode): does not emit `on_tool_trace`
- **tool** (native): does not log success/failure; does not normalize list-form args
- **`ABSOLUTE_PLAN_CEILING = 200`**: defined twice (L746, L1058); max_steps math copy-pasted across both paths
- **`vision_query`**: dedicated native intercept (L790–807) vs json_mode routing via `_dispatch_tool`

This drift will worsen as features are added to one path and not the other. The core goal is to converge E and G into a single dispatch path so these inconsistencies are structurally impossible.

## What Changes

- Extract four pure/leaf module-level helpers from setup phases (Tier 1, no behavior change): `_assemble_system_prompt`, `_init_messages`, `_ensure_tool_defs`, `_normalize_shorthand_action`
- Extract `_finish_run` and `_emit_tool_trace`; unify the two duplicated plan blocks into `_run_plan` (Tier 2) — fixes plan trace/working divergence and single-sources `ABSOLUTE_PLAN_CEILING`
- Introduce `_LoopState` dataclass, `_result_sink`, `_dispatch_action`, and `_request_turn` to converge native and json_mode dispatch into one path (Tier 3)
- All existing module-level patch seams (`_dispatch_tool`, `_build_system_prompt`, `_encode_images`) remain module-level functions in `react_loop.py`
- No class-based refactor; no formatter/parser split to a sibling module

**Tier 2 behavior change**: After unification, json_mode plan execution will emit `on_tool_trace` and `working.add_step` — this is a bug fix (parity with native), but is an observable behavior change that requires new test coverage.

**Tier 3 pre-verify**: Confirm whether `build_tool_definitions` exposes `finish` as a native tool. If so, a native `finish` currently falls to `_dispatch_tool("finish")` with no handler; `_dispatch_action` would fix this, but the current behavior must be documented before Tier 3.

## Capabilities

### New Capabilities

- `react-loop-plan-visibility`: Plan execution emits `on_tool_trace` and a `working.add_step` entry regardless of whether the model used native tool calls or json_mode dispatch.

### Modified Capabilities

- `react-loop-execution`: `react_loop()` is restructured into module-level helpers following existing `_dispatch_tool` idiom. Behavior is preserved except for the plan-visibility fix. All existing patch seams remain intact.

## Impact

- Affected code:
  - `react_loop.py` — all structural changes; new helpers added as module-level functions; `ABSOLUTE_PLAN_CEILING` reduced to one definition
  - `tests/test_react_loop.py` — new assertions for plan trace/working emissions in json_mode; `_LoopState`/`_dispatch_action` seam tests for Tier 3
  - `tests/test_native_intercepts.py` — regression coverage for unified dispatch; confirm native `finish` behavior pre/post Tier 3
  - `tests/test_p1_non_json_failure.py` — patch site for `_dispatch_tool`; verify no breakage from Tier 1–2
- No intended changes to:
  - `agent_controller.py` call sites or `ReactContext` shape
  - `execution_plan.py` (circular import handled via existing lazy imports, preserved)
  - Any module-level public names (`fmt_*`, `parse_json`, `ReactContext`, `ToolTrace`, etc.) — all stay importable from `react_loop.py`
  - Telegram, MCP, or memory subsystems

## Constraints

- `_dispatch_tool` must remain a module-level function in `react_loop.py` (patched by 4 sites across 3 test files)
- `_build_system_prompt`, `_encode_images`, `parse_json`, `extract_json_candidates`, `format_tool_result`, `fmt_tool_result_progress`, `fmt_tool_call`, `_compact_args_repr`, `_linearize_native_turns`, `_append_native_tool_result`, `_tool_icon`, `_JSON_FAIL_LIMIT`, `ReactContext`, `ToolTrace` must remain importable from `react_loop.py`
- The `try/finally` wrapping `RUN_END` + `reset_run_context` must fire on every return path; all `return`s stay in `react_loop()`
- `messages` and `goal_idx` reassignment (by `maybe_compact` and `_linearize_native_turns`) stays owned by `react_loop()` / `_LoopState`; helpers may only append
- Circular import with `execution_plan` is handled via lazy imports inside helpers — preserve this pattern
- `_request_turn` owns the LLM call and native/json_mode classify; it returns a `_Turn` dataclass. The `json_fail_streak` protocol (increment, `_JSON_FAIL_LIMIT` abort-return, re-prompt append+continue) stays in `react_loop()` between the `_request_turn` call and `_dispatch_action` — `_request_turn` signals an early-return string, it does not own streak state
- `_dispatch_action` receives a pre-extracted `action_obj` dict in canonical shape. The caller is responsible for normalizing native `tool_calls[0].arguments` into the canonical form before passing to `_dispatch_action`; this normalization includes the `plan_data` shape difference (native: `tc.arguments` with steps at top level; json_mode: `action_obj.get("plan", {})`) 
- `_LoopState` fields: `messages: list[dict]`, `goal_idx: int`, `max_steps: int`, `step: int = 0`, `json_fail_streak: int = 0`, `operator_cancelled: bool = False`, `last_action_time: float`, `warned_inactivity: bool = False`
- Each tier ships with `make check` green before the next tier begins

## Risks

- **Tier 2**: json_mode plans will now emit `on_tool_trace` and `working.add_step` — behavior change; tests must assert new emissions
- **Tier 3**: `_result_sink` must correctly route to `_append_native_tool_result` (native) or `messages.append` (json_mode); incorrect wiring silently drops tool results with no immediate error
- **Tier 3 (verify before)**: native `finish` behavior — see pre-verify note in What Changes above
