## 1. Pre-implementation verification

- [x] 1.1 Grep `build_tool_definitions` to confirm whether `finish` is exposed as a native tool (OQ1 from design)
- [x] 1.2 Grep native tool call handling to confirm whether args ever arrive as a list (OQ2 from design)
- [x] 1.3 Run `make check` on main to establish a green baseline before branching

## 2. Branch setup

- [x] 2.1 Create branch `refactor/decompose-react-loop` from main
- [x] 2.2 Confirm `make check` is green on the new branch

## 3. Tier 1 — Pure/leaf extractions (zero behavior change)

- [x] 3.1 Extract `_assemble_system_prompt(ctx, user_goal) -> str` from L524–579; verify it calls module-level `_build_system_prompt` (test seam preserved)
- [x] 3.2 Extract `_init_messages(ctx, user_goal, images) -> tuple[list[dict], int]` from L581–602; returns `(messages, goal_idx)`, orchestrator retains ownership
- [x] 3.3 Extract `_ensure_tool_defs(ctx) -> None` from L607–617 only; the `None`-reset at L512 stays in `react_loop()`
- [x] 3.4 Extract `_normalize_shorthand_action(action_obj: dict) -> dict` from L911–936 as a pure transform
- [x] 3.5 Run `make check` — must be green with no behavior change

## 4. Tier 2 — Fix behavioral divergences

- [x] 4.1 Define `ABSOLUTE_PLAN_CEILING = 200` once at module level; remove the duplicate at L1058
- [x] 4.2 Extract `_emit_tool_trace(ctx, tool_name, args, *, success, duration_ms, error="") -> None` collapsing the 5 duplicated `ToolTrace` constructions + `on_tool_trace` guard
- [x] 4.3 Extract `_finish_run(ctx, action_obj, user_goal, step, run_start) -> str` from L940–997 (result coercion, summarize, save_task_outcome, strategy thread)
- [x] 4.4 Extract `_run_plan(ctx, plan_data, max_steps, progress) -> tuple[str, int, bool]` unifying L741–788 and L1049–1102; single-sources `ABSOLUTE_PLAN_CEILING`; caller normalizes `plan_data` shape before passing
- [x] 4.5 Update json_mode `plan` dispatch (formerly L1049–1102) to call `_emit_tool_trace` and `working.add_step` via `_run_plan` — this is the parity fix (behavior change: json_mode plans now emit trace + working step)
- [x] 4.6 Update json_mode `create_tool` dispatch (formerly L1042–1047) to call `_emit_tool_trace` (parity with native at L727–734)
- [x] 4.7 Add or adjust test in `test_react_loop.py` covering json_mode plan trace emission and working-memory step
- [x] 4.8 Run `make check` — must be green; new test must pass

## 5. Tier 3 — Converge native and json_mode dispatch

- [x] 5.1 Define `_LoopState` dataclass with fields: `messages`, `goal_idx`, `max_steps`, `step`, `json_fail_streak`, `operator_cancelled`, `last_action_time`, `warned_inactivity`
- [x] 5.2 Define `_Turn` dataclass with fields: `tool_calls`, `raw`, `native_attempted`, `text_from_native`, `early_return`
- [x] 5.3 Extract `_request_turn(ctx, state, system, progress) -> _Turn` from L669–864; early-return strings (cancelled at L851, error at L855) become `_Turn(early_return=…)`; `_linearize_native_turns` reassignment stays in caller or is reflected back via `_Turn`
- [x] 5.4 Implement `_result_sink(state, tc=None) -> Callable[[str], None]` — `tc` set → `_append_native_tool_result`; `tc` None → `messages.append`
- [x] 5.5 Implement `_dispatch_action(ctx, action_obj, sink, state, user_goal, run_start, progress) -> Optional[str]` handling tool, create_tool, plan, vision_query, finish, unknown; calls `_dispatch_tool` (test seam) for standard tools; returns final string only on `finish`, else None; sets `state.operator_cancelled`
- [x] 5.6 Route `operator_cancelled` signal from `_dispatch_action` back to orchestrator loop (replace direct `break` with flag check after each `_dispatch_action` call)
- [x] 5.7 Reduce `react_loop()` to the orchestrator skeleton: prologue, prompt assembly, message init, `_LoopState`, outer loop, `maybe_compact` reassignment, `_request_turn`, `json_fail_streak` protocol, `_dispatch_action`, cancel/extension-prompt control, `finally`
- [x] 5.8 Verify `react_loop()` is ~150–180 lines
- [x] 5.9 Run `make check` — must be green; `test_native_intercepts.py` and `tests/execution_harness.py`-based tests must pass
- [x] 5.10 Verify `_dispatch_tool` patch seam is unchanged (still a module-level function called by `_dispatch_action`)

## 6. Validation and merge

- [x] 6.1 Run `openspec validate decompose-react-loop --type change --strict`
- [x] 6.2 Confirm no regressions in `test_react_loop.py`, `test_native_intercepts.py`, `test_p1_non_json_failure.py`
- [x] 6.3 Open PR from `refactor/decompose-react-loop` to `main`
