# react_loop.py — Code Analysis

Analysis of `/Users/paulpronko/Documents/develop/smallpieclaw/react_loop.py` (1467 lines) for Super-classes & Inheritance, Large Methods, Idiomatic Python, and Technical Debt & Shortcomings.

## Section A — Findings List

### Inheritance / Class Design

**INH-01 — `ReactContext` is a god-object dataclass**
- **Category:** Inheritance
- **Description:** `ReactContext` bundles ~30 fields spanning core services, config, memory layers, cancellation state, callbacks, and cached tool defs into one flat dataclass. There's no literal inheritance in the file, but this is the "superclass" antipattern in dataclass form — a single object every function threads through and mutates.
- **Location:** `react_loop.py:178-278`
- **Suggested Fix:** Decompose into composed sub-dataclasses (`ServicesBundle`, `MemoryLayers`, `RuntimeConfig`, `Callbacks`) held as fields on a slimmer `ReactContext`.

**INH-02 — Dependencies typed as bare `object`, bypassing existing Protocols**
- **Category:** Inheritance
- **Description:** `tool_index`, `memory`, `builtin_executor`, `mcp_manager`, `skill_registry`, `short_term`, `working`, `results`, `graph_memory`, `graph_memory_writer`, `strategy_memory` are all typed `object`, despite `interfaces.py` defining `Protocol` classes for exactly this purpose (per AGENTS.md). This is a known/accepted gap per memory (`pyright-lsp-noise-vs-gates`), but worth surfacing since it defeats structural typing and static analysis for the whole loop.
- **Location:** `react_loop.py:184-188, 209-211, 232-241`
- **Suggested Fix:** Type against Protocols under `TYPE_CHECKING` the way `TrustedZoneChecker` already is (line 25).

### Large Methods

**LM-01 — `react_loop()` is a ~140-line function with nested loop + multi-phase responsibility**
- **Category:** Large Method
- **Description:** Setup, the step-loop, extension negotiation, and teardown are all inline in one function with a `while True` wrapping a `while state.step < state.max_steps`.
- **Location:** `react_loop.py:955-1096`
- **Suggested Fix:** Extract `_run_step_loop(...)` and `_negotiate_extension(...)`.

**LM-02 — `_dispatch_tool()` mixes four concerns in one function**
- **Category:** Large Method
- **Description:** Vision routing, builtin dispatch with a nested shell-chunk-streaming closure, confirmation/auto-approve branching, and MCP dispatch all live in one ~90-line function.
- **Location:** `react_loop.py:1376-1466`
- **Suggested Fix:** Extract `_make_shell_chunk_callback(...)` and `_handle_confirmation_flow(...)`.

**LM-03 — `_exec_vision_query()` mixes zone-gating, confirmation building, and LLM invocation**
- **Category:** Large Method
- **Location:** `react_loop.py:439-514`
- **Suggested Fix:** Extract a `_needs_confirmation_for_read(ctx, path)` helper — likely duplicable with `file_read`'s zone gate in `builtin_tools/files.py`.

**LM-04 — Tool-action branch inside `_dispatch_action()` does six things inline**
- **Category:** Large Method
- **Description:** Trace emission, working-memory update, `send_file` handling, logging, progress, and sink-writing are all inlined in the `"tool"` branch.
- **Location:** `react_loop.py:905-938`
- **Suggested Fix:** Extract `_handle_tool_action(ctx, action_obj, sink, state, progress)`.

### Idiomatic Python

**IMP-01 — Broad `except Exception` beyond the documented daemon-resilience tolerance**
- **Category:** Idiomatic Python
- **Description:** 9+ broad catches, several around non-daemon-critical paths (prompt assembly, strategy extraction) where a narrower exception type is knowable.
- **Location:** lines 139, 512, 639, 654, 722, 989, 1029, 1259, 1368
- **Suggested Fix:** Narrow where the callee's contract is known; keep broad only at true I/O/LLM/callback boundaries.

**IMP-02 — Stringly-typed control-flow sentinels**
- **Category:** Idiomatic Python
- **Description:** `_RE_PROMPT = "__re_prompt__"` compared with `==` for loop control; `"__FILE__:"` / `"__SHELL_CHUNK__:"` prefix-encoded progress messages act as an ad hoc wire protocol embedded in plain strings.
- **Location:** lines 59, 751-780, 928, 1419
- **Suggested Fix:** Use an `Enum`/tagged dataclass for internal sentinels; a `ProgressEvent` dataclass for progress messages.

**IMP-03 — Untyped `dict` for tool outcomes and action objects**
- **Category:** Idiomatic Python
- **Description:** Contradicts the project's "type hints for parameters and return types" rule. `outcome: dict` and `action_obj: dict` are passed and `.get()`-accessed throughout with no schema (`success`, `output`, `error`, `exit_code`, `error_type`, `recoverable`, `suggestion`, `requires_confirmation`, `token`, `send_file`, ...).
- **Location:** pervasive, e.g. lines 887-899, 1138-1143
- **Suggested Fix:** Define `ToolOutcome`/`ActionObj` as `TypedDict(total=False)` in a shared schema module.

**IMP-04 — Incomplete `Callable` type hint despite documented signature**
- **Category:** Idiomatic Python
- **Location:** `react_loop.py:226`
- **Suggested Fix:** `Optional[Callable[[ToolTrace], None]]`.

**IMP-05 — Scattered function-local imports to dodge circular imports**
- **Category:** Idiomatic Python
- **Location:** lines 441-442, 659, 1145, 1240
- **Suggested Fix:** Either accept and document as a repo convention, or break the cycle between `react_loop.py` and `execution_plan.py`/`strategy_memory.py` via an interface module.

**IMP-06 — Exception used for expected (non-exceptional) control flow**
- **Category:** Idiomatic Python
- **Description:** `_action_from_turn` raises bare `ValueError("non-json")` purely so the caller can route to `_handle_non_json`; the message payload is never inspected.
- **Location:** lines 800-812, 1048-1055
- **Suggested Fix:** Return `Optional[dict]` and branch on `None` instead of raising.

### Technical Debt & Shortcomings

**TD-01 — File violates the project's own "avoid large files" rule**
- **Category:** Tech Debt
- **Description:** 1467 lines mixing context/state dataclasses, prompt assembly, JSON extraction, tool dispatch, plan-execution glue, lifecycle tracing, and presentation formatting.
- **Location:** whole file
- **Suggested Fix:** Split into `react_loop.py` (core loop), `react_actions.py` (dispatch/plan/finish), `react_formatting.py` (icons/`fmt_*`), `react_json.py` (`extract_json_candidates`/`parse_json`).

**TD-02 — `cancel_event.clear()` at run start races across concurrent runs sharing one owner** — **FIXED** (branch `fix/react-loop-cancel-event-race`)
- **Category:** Tech Debt
- **Description:** `ctx._tool_defs = None` and `ctx.cancel_event.clear()` mutate `ctx` at the top of every `react_loop()` call. Follow-up investigation confirmed `_tool_defs` is not actually racy — `AgentRuntime.build_react_context` builds a fresh `ReactContext` per `run()` call, so no two runs ever share the same `ctx` instance. `cancel_event`, however, *is* a real shared object: `main.py` constructs one MAIN `AgentController` for the whole process, and `telegram_interface.py`'s per-user `asyncio.Lock`s only serialize messages from the *same* user — two different authorized users can hit `agent.run()` concurrently, both sharing `AgentController._cancel_event` (confirmed by reading `agent_controller.py:94-95, 161-162` and `telegram_interface.py:360-670`). If User A's task is cancelled via `/stop` (`agent_controller.py:264`, deliberately global — see `cmd_stop` in `telegram_commands.py:250-258`) while User B's message starts a new run in the same window, `react_loop()`'s unconditional `clear()` silently erases A's pending cancellation before A's blocked LLM/tool call returns to check `is_set()` — `/stop` fails silently for A.
- **Location:** `react_loop.py:972-974` (now `~1000-1004`)
- **Fix applied (v1, superseded):** A first attempt added a `CancelRunCoordinator` in `react_loop.py` that refcount-guarded `clear()` on a *shared* event (only cleared it on the 0→1 concurrent-run transition). An independent `/code-review` pass caught a serious regression in that approach: a brand-new, unrelated run starting while *any* sibling run was still active would inherit a stale cancellation and die at step 0 — and in a busy multi-user bot where `active_runs` never drops to 0, every subsequent run could keep dying this way, indefinitely. Reproduced directly: `ev=threading.Event(); coord=CancelRunCoordinator(); coord.begin_run(ev); ev.set(); coord.begin_run(ev); ev.is_set()` → `True` for the second, unrelated run. That version was reverted.
- **Fix applied (v2, current):** Stopped trying to guard a shared event and removed the sharing instead. Added `CancelEventRegistry` in `agent_controller.py`: `AgentController.run()` now mints a fresh, private `threading.Event()` per run via the registry (only when the controller owns its cancellation — no external event was supplied) and passes it into `AgentRuntime.build_react_context(..., cancel_event=...)` (new optional override param, `agent_runtime.py`); `run()`'s `finally` releases it afterward. `AgentController.cancel()` now fans `set()` out to every event currently registered — i.e. every run actually in flight right now — instead of setting one shared flag. Callers that bypass `run()` (direct `build_react_context()` calls, existing tests, the externally-supplied-event path used by sub-agent cascade-cancel) are unaffected — verified no other code depends on the MAIN `AgentController`'s cancel event being one fixed object (`main.py`'s `LLMClient` isn't wired to it; nothing external reads `agent._cancel_event`). `react_loop.py` itself is back to its original, unmodified form. Regression tests in `tests/test_react_loop.py` (`TestCancelEventRegistry`, `TestAgentControllerCancelRegistryWiring`) cover: distinct per-run events, `cancel_all()` reaching only active runs, a released run being unaffected by a later cancel, and the specific "unrelated new run must not inherit a stale cancellation" case the v1 fix got wrong. One characterization test (`test_cancel_ownership_owned_by_default` in `tests/test_agent_runtime_characterization.py`) was updated to document the new, intentional identity change. `make check`: 1761 passed, 1 skipped, ruff/vulture clean.
- **Known related gap, not fixed here (out of scope):** the same code review flagged that other mutable state on the shared MAIN `AgentController` — `self.llm._active_idx`, the LLM trace id, `builtin_executor._prompt_approval_set`/`_current_prompt_id`, and `confirmation.auto_approve_tools` — is *also* unguarded against concurrent runs from different users, and is pre-existing (not introduced by this fix). Two concurrent runs can currently cross-contaminate each other's trace tagging, active-model index, and tool-confirmation auto-approvals. This is a separate, larger architectural gap (the whole `AgentController` isn't safe for concurrent multi-user execution beyond cancellation) and needs its own scoped fix/decision, not a drive-by change.

**TD-03 — Duplicated, divergence-prone max-steps clamping logic**
- **Category:** Tech Debt
- **Description:** `_dispatch_action` and `_run_plan` each independently clamp against `_ABSOLUTE_PLAN_CEILING` with slightly different formulas.
- **Location:** lines 940-944, 1147-1153, 1179-1183
- **Suggested Fix:** Centralize into one `_clamp_plan_steps(current, requested)` helper.

**TD-04 — Brittle text-heuristic MCP auth-failure detection**
- **Category:** Tech Debt
- **Description:** `_is_mcp_auth_failure` substring-matches error text (`"401"`, `"unauthorized"`, ...); a server changing its wording silently breaks the retry/needs-auth transition.
- **Location:** lines 81-99
- **Suggested Fix:** Prefer a structured error code/exception from `mcp_manager.call_tool` over text sniffing.

**TD-05 — Magic-number "unlimited" step ceiling has no regression test signal**
- **Category:** Tech Debt
- **Location:** lines 55-58, 1074-1077
- **Suggested Fix:** Add a test asserting the loop actually halts at `_EFFECTIVELY_UNLIMITED_STEPS` — currently the only thing preventing a true infinite loop if this constant is ever "cleaned up."

**TD-06 — Suppressed type-checker warning masking a real signature gap**
- **Category:** Tech Debt
- **Location:** line 1301 (`# type: ignore[assignment]`)
- **Suggested Fix:** Type `_emit_tool_lifecycle`'s second param as `Union[dict, BaseException]` and branch via `isinstance` before assignment.

**TD-07 — Presentation/formatting logic co-located with dispatch logic**
- **Category:** Tech Debt
- **Description:** `_TOOL_ICONS`, `_tool_icon`, `fmt_tool_call`, `fmt_tool_result_progress`, `format_tool_result` are Telegram-markdown-flavored formatting helpers living in the core loop module, inconsistent with the existing `telegram_formatter.py` "pure formatting" module pattern.
- **Location:** lines 40-53, 364-436
- **Suggested Fix:** Move to a dedicated formatting module.

**TD-08 — `_truncate_context_payload`'s per-key floor can exceed its own `max_chars` budget**
- **Category:** Tech Debt
- **Description:** `per_key = max(30, budget // max(1, len(keys)))` guarantees each key at least 30 chars regardless of key count, so total truncated output can exceed `max_chars` when there are many keys.
- **Location:** lines 143-159
- **Suggested Fix:** Either recompute the budget iteratively as keys are filled, or document `max_chars` as a soft target rather than a hard ceiling.

## Section B — Risk & Priority Table

| ID | Description | Category | Risk | Priority | Effort |
|----|-------------|----------|------|----------|--------|
| TD-02 | `cancel_event.clear()` races across concurrent runs sharing one owner — **FIXED** | Tech Debt | High | P1 | M |
| TD-01 | 1467-line module violates project's own large-file rule | Tech Debt | Medium | P2 | L |
| INH-01 | `ReactContext` god-object dataclass (~30 fields) | Inheritance | Medium | P2 | L |
| IMP-03 | Untyped `dict` for tool outcomes / action objects | Idiomatic Python | Medium | P2 | L |
| LM-01 | `react_loop()` ~140-line multi-phase function | Large Method | Medium | P2 | M |
| LM-02 | `_dispatch_tool()` mixes 4 concerns in one function | Large Method | Medium | P2 | M |
| IMP-01 | Broad `except Exception` beyond documented tolerance | Idiomatic Python | Medium | P2 | M |
| TD-03 | Duplicated max-steps clamping logic (drift risk) | Tech Debt | Medium | P2 | S |
| TD-04 | Brittle substring heuristic for MCP auth-failure detection | Tech Debt | Medium | P2 | M |
| INH-02 | Dependencies typed `object`, bypassing existing Protocols | Inheritance | Low-Medium | P3 | M |
| TD-07 | Presentation formatting co-located with dispatch logic | Tech Debt | Low | P3 | M |
| IMP-02 | Stringly-typed control-flow sentinels (`_RE_PROMPT`, `__FILE__:`) | Idiomatic Python | Low | P3 | S |
| IMP-05 | Scattered function-local imports (circular-import workaround) | Idiomatic Python | Low | P3 | S |
| IMP-06 | Exception raised for expected non-JSON case (control flow) | Idiomatic Python | Low | P3 | S |
| LM-03 | `_exec_vision_query()` mixes zone-gate + confirmation + LLM call | Large Method | Low | P3 | S |
| LM-04 | Tool-action branch in `_dispatch_action()` does 6 things inline | Large Method | Low | P3 | S |
| TD-05 | "Unlimited" step ceiling has no regression test | Tech Debt | Low | P3 | S |
| TD-06 | `# type: ignore[assignment]` masking a real signature gap | Tech Debt | Low | P3 | S |
| TD-08 | `_truncate_context_payload` per-key floor can exceed `max_chars` | Tech Debt | Low | P3 | S |
| IMP-04 | `on_tool_trace: Optional[Callable]` missing signature | Idiomatic Python | Low | P3 | S |

**Top pick if you only fix one thing:** TD-02 — it's the only finding with real correctness/concurrency blast radius; everything else is maintainability/debt.
