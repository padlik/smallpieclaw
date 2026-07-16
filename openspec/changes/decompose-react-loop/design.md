## Context

`react_loop.py` contains a single 660-line function (`react_loop()`, L486–1146) that runs the full ReAct loop. The function implements two parallel dispatch paths — native tool calls (phase E, L710–834) and json_mode action dispatch (phase G, L938–1109) — for the same four actions (tool, create_tool, plan, vision_query). These paths were added at different times and have drifted into behavioral inconsistencies:

- json_mode `plan` (L1049–1102) emits neither `on_tool_trace` nor `working.add_step`; native (L778–787) does both.
- json_mode `create_tool` (L1042–1047) omits `on_tool_trace`; native (L727–734) has it.
- Native `tool` (L810–834) lacks success/failure logging and list-arg normalization; json_mode (L1005–1006, L1026–1034) has both.
- `ABSOLUTE_PLAN_CEILING = 200` is defined at two sites (L746, L1058); max_steps math is copy-pasted.

Key constraints:
- Tests patch module-level names: `_dispatch_tool` (3 test files), `_build_system_prompt`, `_encode_images`. These must remain module-level functions in `react_loop.py`.
- AGENTS.md prohibits superclasses and large files.
- `execution_plan` imports `ReactContext`/`parse_json` from `react_loop` — circular imports require lazy imports inside plan helpers (as today).
- `react_loop()` owns 6 return points and a `try/finally` (L507 / L1138–1146) wrapping all returns for `RUN_END`/`reset_run_context` guarantees.

## Goals / Non-Goals

**Goals:**
- Reduce `react_loop()` from ~660 lines to ~150–180 lines by extracting module-level helpers.
- Unify native and json_mode dispatch into a single `_dispatch_action()`, eliminating behavioral drift by construction.
- Single-source `ABSOLUTE_PLAN_CEILING` and plan execution logic.
- Fix: json_mode plan execution now emits `on_tool_trace` and `working.add_step` (parity with native path).

**Non-Goals:**
- No class or superclass wrapping `react_loop()` — 4 patch sites across 3 test files mandate module-level functions.
- No split of formatters/parsers (`fmt_*`, `parse_json`, `_linearize_native_turns`) into a sibling module — would rewrite ~20 test imports; separate lower-priority change.
- No change to `_dispatch_tool` — it is a stable test seam; `_dispatch_action` calls it.

## Decisions

### D1: Module-level helpers following the `_helper(ctx, …)` idiom

**Decision:** Extract phases as module-level functions in `react_loop.py`, not a class.

**Rationale:** 4 test files patch `react_loop._dispatch_tool` and `_build_system_prompt` directly. A class would break all patch sites. `ReactContext` already is the DI bundle a class would reinvent. AGENTS.md prohibits superclasses.

**Alternatives considered:**
- `LoopRunner` class: rejected — breaks 4 patch sites, adds a prohibited superclass, reinvents `ReactContext`.
- Pipeline/strategy object: rejected — loop control flow (multi-return, break-on-cancel, extension prompt) is inherently procedural; an object would merely relocate it.

### D2: `_LoopState` dataclass for mutable per-run locals

**Decision:** Introduce `_LoopState` dataclass bundling all mutable loop locals:

```python
@dataclass
class _LoopState:
    messages: list[dict]
    goal_idx: int
    max_steps: int
    step: int = 0
    json_fail_streak: int = 0
    operator_cancelled: bool = False
    last_action_time: float = field(default_factory=time.time)
    warned_inactivity: bool = False
```

**Rationale:** These 8 variables are currently scattered locals. A single bag enables `_dispatch_action` and `_request_turn` to read/write state without a sprawling signature. Follows the `ReactContext` pattern (dataclass DI, no class methods).

**Alternatives considered:**
- Pass each field individually: rejected — 8+ parameters per helper, fragile.
- Extend `ReactContext`: rejected — `ReactContext` is per-agent immutable config, not per-run mutable state.

### D3: `_result_sink` to unify native/json_mode result writing

**Decision:** `_result_sink(state: _LoopState, tc: Optional[ToolCall] = None) -> Callable[[str], None]` abstracts the only structural difference between dispatch paths: how tool results are written back to messages.

- `tc` set → calls `_append_native_tool_result(state.messages, tc, content)`
- `tc` None → appends `{"role": "user", "content": content}`

The sink is constructed from the current turn context immediately before `_dispatch_action` is called. It is not reused across turns.

**Rationale:** This is the crux enabling a single `_dispatch_action` for both paths. Without it, native vs json_mode dispatch must diverge on message-write semantics.

**Risk:** Wrong `tc` → silent wrong message type. Mitigation: sink construction is co-located with turn dispatch in the orchestrator; no deferred or cached sinks.

### D4: `_request_turn` returns `_Turn`; `json_fail_streak` stays in orchestrator

**Decision:** `_request_turn` returns a `_Turn` dataclass:

```python
@dataclass
class _Turn:
    tool_calls: list[ToolCall]      # non-empty → native path
    raw: str                         # LLM text output
    native_attempted: bool
    text_from_native: Optional[str]  # prose coerced to finish
    early_return: Optional[str]      # cancelled or error signal
```

The `json_fail_streak` counter and its protocol (increment, `_JSON_FAIL_LIMIT` abort-return, re-prompt append+continue) remains in `react_loop()` between `_request_turn` and `_dispatch_action`.

**Rationale:** `json_fail_streak` drives orchestration decisions (retry or abort). It is not a concern of the LLM call itself. Keeping it in `react_loop()` preserves intent: the orchestrator decides whether to retry.

### D5: `_run_plan` receives normalized `plan_data`; caller normalizes shape

**Decision:** Callers extract `plan_data` from the correct location before passing to `_run_plan`:
- Native: `plan_data = tc.arguments`
- json_mode: `plan_data = action_obj.get("plan", {})`

`_run_plan` is agnostic to source path and receives already-extracted data. `ABSOLUTE_PLAN_CEILING` is defined once as a module-level constant.

**Rationale:** Keeps `_run_plan` pure. Normalization responsibility sits at the `_dispatch_action` call site, which already knows the dispatch path.

### D6: Staged tiers with `make check` green per tier

**Decision:** Three independently-shippable tiers:

| Tier | Extractions | Risk | Behavior change |
|------|-------------|------|-----------------|
| 1 | `_assemble_system_prompt`, `_init_messages`, `_ensure_tool_defs`, `_normalize_shorthand_action` | Zero | None |
| 2 | `_finish_run`, `_run_plan` (unifies both plan blocks), `_emit_tool_trace` | Low | json_mode plan now emits trace + working step |
| 3 | `_LoopState`, `_result_sink`, `_dispatch_action`, `_request_turn` | Medium | E/G drift eliminated by construction |

**Rationale:** Tier 2 fixes an observable behavioral bug and requires a test adjustment. Landing it separately from Tier 3 makes the fix reviewable in isolation.

## Risks / Trade-offs

**[Risk] Tier 2 behavior change for json_mode plans** → Existing tests asserting json_mode plan behavior may need adjustment.
Mitigation: add/adjust a test covering json_mode plan trace emission before landing Tier 2.

**[Risk] Native `finish` has no intercept today** → If `build_tool_definitions` exposes `finish` as a native tool, native `finish` falls to `_dispatch_tool("finish")` with no executor. Pre-Tier-3 check: grep `build_tool_definitions` and confirm whether `finish` appears. If yes, `_dispatch_action` must route native `finish` through `_finish_run`.

**[Risk] `messages` reassignment in `_LoopState`** → Both `maybe_compact` and `_linearize_native_turns` *reassign* `messages` (not append). These reassignments must remain in `react_loop()` as `state.messages = new_list`. Helpers may only `.append`.

**[Risk] `_result_sink` silent-drop on wrong `tc`** → Sink is constructed immediately from current turn; not deferred. Reviewed at Tier 3 code review.

## Migration Plan

No external API or config changes. All exported names (`ReactContext`, `ToolTrace`, `parse_json`, `extract_json_candidates`, format helpers) remain module-level with identical signatures.

Rollback: each tier is independently revertable via git. No data migrations, no schema changes.

Deployment order: Tier 1 → `make check` → Tier 2 → `make check` + new test → Tier 3 → `make check`.

## Open Questions

**OQ1:** Does `build_tool_definitions` expose `finish` as a native tool? Grep before starting Tier 3. (See risk above.)

**OQ2:** Do native tool call arguments ever arrive as a list? Determines whether list-arg normalization (currently json_mode-only, L1005–1006) moves into `_dispatch_action` or stays json_mode-only.
