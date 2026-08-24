## Context

The agent's context window is consumed by three separate channels — system prompt, chat history, and tool definitions — but only the first two are measured by the compaction system. The native tool-calling path (ADR-0009) injects all 21 built-in tools plus every registered MCP tool into the LLM payload, and this cost is invisible to `maybe_compact` (which counts only `system` + `messages`). The compaction threshold formula from ADR-0020 (`max(int((effective - model.max_tokens) * 0.85), 256)`) operates on an incomplete picture of the payload size.

The operator has no visibility into which MCP servers are consuming context. The existing `/show_ctx` command dumps the full system prompt as a file, but does not break down consumption by category or surface the hidden tool-definition cost.

This change introduces a `ContextMonitor` that continuously tracks context-window consumption by category, fixes the `maybe_compact` bug, and exposes the monitor via a `/context` Telegram command and a `context_profile` built-in tool.

### Current architecture (C4 Container)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Process (Container)                     │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ Telegram     │   │ Agent        │   │ ReAct Loop               │  │
│  │ Interface    │──▶│ Controller   │──▶│                          │  │
│  │              │   │              │   │  state.messages           │  │
│  │ /show_ctx    │   │ build_       │   │  ctx._tool_defs           │  │
│  │ /status      │   │ system_      │   │                          │  │
│  │ /compress    │   │ prompt()     │   │  ┌─────────────────────┐ │  │
│  │              │   │              │   │  │ context_manager     │ │  │
│  │ (asyncio     │   │ (shared      │   │  │ maybe_compact()     │ │  │
│  │  event loop) │   │  state)      │   │  │ counts system+msgs  │ │  │
│  └──────────────┘   └──────┬───────┘   │  │ ONLY — tool defs    │ │  │
│                            │           │  │ INVISIBLE            │ │  │
│                            │           │  └─────────────────────┘ │  │
│                            │           └──────────────────────────┘  │
│                            │                    │                    │
│                            ▼                    ▼                    │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ LLM Client   │   │ Memory Store │   │ Tool Registry            │  │
│  │              │   │ (short_term, │   │ (builtin + MCP)           │  │
│  │ chat_with_   │   │  working,    │   │                           │  │
│  │  tools()     │   │  results)    │   │ build_tool_definitions()  │  │
│  └──────────────┘   └──────────────┘   └──────────────────────────┘  │
│                                                                      │
│  Threading: agent runs on worker thread; Telegram on asyncio loop   │
│  concurrent_updates(True) → commands process during agent run       │
│  Per-user asyncio.Lock gates new runs, not commands                 │
└─────────────────────────────────────────────────────────────────────┘
```

**Key constraints from in-force ADRs:**
- ADR-0007: `AgentRuntime` is the construction boundary; `ControllerDeps` bundles deps; `ReactContext` is built per-run
- ADR-0008: Built-in tools live in `builtin_tools/` subpackage; `builtin_executor.py` is the dispatcher/facade
- ADR-0009: Native tool-calling path sends all tool definitions; text path uses `top_tools` filter
- ADR-0020: Per-model context window; compaction threshold = `max(int((effective - max_tokens) * 0.85), 256)`

## Goals / Non-Goals

**Goals:**
- Introduce a `ContextMonitor` on `AgentController` that tracks context consumption by category (system prompt, chat history, tool definitions, completion reserve) via a push model from the ReAct loop
- Sub-categorise tool definitions by MCP server so "fat" servers are visible
- Compute `danger_level` and `headroom_real` (accounting for tool defs) so the operator sees the true risk
- Fix the `maybe_compact` bug: include tool-definition tokens in the compaction total
- Expose the monitor via `/context` Telegram command (summary dashboard) and `context_profile` built-in tool (JSON snapshot)
- Make the monitor the foundation for future context budgeting and automated event triggers

**Non-Goals:**
- No `compress_context` built-in tool (deferred to a future change)
- No automatic actions or event triggers based on danger level (future)
- No `top_tools` filtering on the native tool-calling path (separate change)
- No system prompt section-level breakdown (deferred — the monitor is extensible)
- No live history compaction triggered by the agent (stays automatic via `maybe_compact`)
- No system prompt injection of context stats (noise that degrades reasoning quality)

## Decisions

### Decision 1: Push model — ReAct loop publishes snapshot each turn

**Choice:** The ReAct loop pushes a `ContextSnapshot` to the `ContextMonitor` each turn.

**Alternatives considered:**
- *Pull model:* The monitor builds a snapshot on-demand when `/context` or the tool is called. Rejected — the monitor wouldn't be "always tracking," just "always computing on demand." The push model makes the monitor a continuous tracker, which is the foundation for future event triggers (the monitor can fire events when a push crosses a threshold).

**Rationale:** The ReAct loop already has all the data each turn (`state.messages`, `system`, `ctx._tool_defs`). Publishing a lightweight snapshot (token counts + metadata, not copies of messages) is cheap. The monitor becomes a simple store of the latest snapshot.

### Decision 2: Reference swap without locking

**Choice:** The snapshot is published via a reference swap (no deep copy, no lock). The `ContextSnapshot` dataclass is immutable (`frozen=True`) so published snapshots are never mutated after publication. The idle transition (live to not-live) always publishes a new snapshot with `is_live=False`, never mutates the existing one. The monitor is thread-safe for concurrent reads from the Telegram event loop while the agent thread publishes.

**Alternatives considered:**
- *Deep copy under lock:* Consistent snapshot but slower. Rejected — the snapshot is a diagnostic view, not a transactional read. A slightly stale snapshot is acceptable.
- *asyncio.run_coroutine_threadsafe:* Would marshal the publish onto the Telegram loop. Rejected — unnecessary complexity; the monitor is a simple dataclass swap.

**Rationale:** Python's GIL makes a reference swap atomic at the bytecode level. The monitor stores a `ContextSnapshot | None`; reads return the current reference. No lock needed.

### Decision 3: Monitor lives on AgentController, not ReactContext

**Choice:** `AgentController` holds the `ContextMonitor` instance. `ReactContext` gets a reference to it (like it does for `short_term`). The Telegram interface reaches it via `iface.agent.context_monitor`. The `BuiltinExecutor` receives the `ContextMonitor` injected directly at construction time (not via an agent back-reference), avoiding circular coupling between `AgentController` and `BuiltinExecutor`.

**Alternatives considered:**
- *On ReactContext (per-run):* Rejected — only exists during a run; `/context` can't read it when idle; tools can't reach it.
- *Standalone in main.py:* Same as AgentController but with explicit wiring. Rejected — follows the existing composition pattern where AgentController holds shared state.

**Rationale:** AgentController already holds `short_term`, `working`, `results`, etc. The monitor is another piece of agent state. When idle, the monitor holds the last published snapshot — `/context` still works.

### Decision 4: Tool defs grouped by MCP server via ToolRegistry

**Choice:** The snapshot cross-references tool names from `_tool_defs` back to the `ToolRegistry` to get the `server_name` and `is_mcp` fields. The per-server grouping is seeded from the full list of registered MCP servers (from the MCP manager/registry), so servers with zero discovered tools still appear with a token cost of zero. Built-in tools form a "builtin" group; each MCP server is a separate group. The total `tool_defs_tokens` is defined as the sum of all per-server group estimates — this same value is passed to `maybe_compact()` so the compaction total and the displayed total are the same number.

**Alternatives considered:**
- *Count all tool defs as one number:* Rejected — can't identify fat servers.
- *Per-tool breakdown:* Rejected — too granular for a summary; server grouping is the actionable level.

**Rationale:** The `Tool` dataclass already has `server_name` and `is_mcp` (`tool_registry.py:21-28`). The grouping is cheap (just a dict accumulation by server name). It directly answers "which MCP server is eating my context?"

### Decision 5: maybe_compact gains optional tool_defs_tokens parameter

**Choice:** `maybe_compact()` gains `tool_defs_tokens: int = 0` parameter. The ReAct loop call site computes `tool_defs_tokens` as the sum of per-server group estimates from `group_tool_defs_by_server()` — the same value used in the snapshot — and passes it to `maybe_compact()`. This ensures the compaction total and the displayed total are the same number. The compaction total becomes `estimate_messages_tokens(messages, system) + tool_defs_tokens`. A shared `resolve_compaction_threshold(llm_cfg, ctx_max_tokens)` helper is extracted so both `maybe_compact()` and `_publish_context_snapshot()` derive the effective window and threshold from the same computation.

**Alternatives considered:**
- *Change estimate_messages_tokens to accept tools:* Rejected — would change the signature of a widely-used function and affect all callers.
- *Compute tool-def tokens inside maybe_compact:* Rejected — would couple the compaction module to the tool definition format.

**Rationale:** Optional parameter with default 0 is backward-compatible. Existing callers are unaffected. The ReAct loop is the only call site that has `_tool_defs`, so it's the natural place to compute the cost.

### Decision 6: context_profile tool executed by built-in executor dispatch

**Choice:** `context_profile` is a standard built-in tool dispatched by `builtin_executor.py` to its handler. The handler reads from the `ContextMonitor` injected directly into the `BuiltinExecutor` at construction time (not via an agent back-reference), avoiding circular coupling between `AgentController` and `BuiltinExecutor`. It is not confirmation-capable.

**Alternatives considered:**
- *Executed by the ReAct loop (like vision_query):* Rejected — `context_profile` doesn't need LLM access; it just reads from the monitor. The executor dispatch is the simpler path.

**Rationale:** Follows ADR-0008's pattern: tool logic lives in `builtin_tools/` subpackage, dispatch is via `builtin_executor.py`. The handler reads the monitor and returns JSON.

### Target architecture (C4 Component)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Process (Container)                     │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ AgentController                                              │    │
│  │  ├── context_monitor: ContextMonitor  ← NEW                  │    │
│  │  ├── short_term: ShortTermMemory                             │    │
│  │  ├── working: WorkingMemory                                  │    │
│  │  ├── results: ResultsMemory                                  │    │
│  │  └── builtin_executor: BuiltinExecutor                       │    │
│  └────────┬─────────────────────────────────────┬────────────────┘    │
│           │                                     │                    │
│           │  ref                                 │ ref                │
│           ▼                                     ▼                    │
│  ┌──────────────────┐              ┌──────────────────────────┐      │
│  │ ContextMonitor   │              │ BuiltinExecutor          │      │
│  │  (NEW)           │              │  dispatch("context_      │      │
│  │                  │              │   profile") → handler     │      │
│  │  _snapshot:      │◀─read────────│  handler reads monitor   │      │
│  │   ContextSnapshot│              └──────────────────────────┘      │
│  │   | None         │                                                │
│  │                  │              ┌──────────────────────────┐      │
│  │  publish(snap)   │◀─push────────│ ReAct Loop               │      │
│  │  read() → snap   │              │  each turn:              │      │
│  └──────────────────┘              │  publish snapshot        │      │
│         ▲                          │  pass tool_defs_tokens   │      │
│         │ read                      │  to maybe_compact        │      │
│         │                           └──────────────────────────┘      │
│  ┌──────────────────┐                                                │
│  │ Telegram /context│                                                │
│  │  cmd_context()   │                                                │
│  │  reads monitor   │                                                │
│  └──────────────────┘                                                │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ ContextSnapshot (dataclass)  ← NEW                            │    │
│  │  system_prompt_tokens: int                                    │    │
│  │  chat_history_tokens: int                                     │    │
│  │  tool_defs_tokens: int                                        │    │
│  │  tool_defs_by_server: dict[str, int]                          │    │
│  │  completion_reserve: int                                      │    │
│  │  effective_window: int                                        │    │
│  │  compaction_threshold: int                                    │    │
│  │  headroom_nominal: int                                        │    │
│  │  headroom_real: int                                            │    │
│  │  danger_level: "safe"|"approaching"|"danger"                  │    │
│  │  is_live: bool                                                │    │
│  │  turn: int                                                    │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### Data flow: push snapshot each turn (C4 Dynamic)

```
ReAct Loop (worker thread)          ContextMonitor (shared)         Telegram (asyncio loop)
     │                                     │                              │
     │  turn N completes                    │                              │
     │  compute:                            │                              │
     │    system_tokens = estimate(system)  │                              │
     │    history_tokens = estimate(msgs)   │                              │
     │    tool_defs_by_server =              │                              │
     │      group_tool_defs_by_server(       │                              │
     │        _tool_defs, registry, mcp_mgr)│                              │
     │    tool_defs_tokens =                │                              │
     │      sum(per-server group estimates) │                              │
     │    danger_level = compute(total,      │                              │
     │      threshold)                      │                              │
     │    snapshot = ContextSnapshot(...)    │                              │
     │                                     │                              │
     │── publish(snapshot) ─────────────────▶│                              │
     │  (reference swap, no lock)           │  _snapshot = snapshot        │
     │                                     │                              │
     │  maybe_compact(messages, system,    │                              │
     │    ctx_max, llm,                    │                              │
     │    tool_defs_tokens=tool_defs_tokens)│                             │
     │  (bug fix: total now includes       │                              │
     │   tool defs)                        │                              │
     │                                     │                              │
     │                                     │      /context command        │
     │                                     │◀───── read() ────────────────│
     │                                     │── snapshot ─────────────────▶│
     │                                     │      (renders dashboard)    │
```

## Risks / Trade-offs

- **[Snapshot staleness]** → The snapshot is a reference swap without locking. A `/context` call during a turn may see the previous turn's snapshot. *Mitigation:* acceptable for a diagnostic view; the snapshot includes `turn` and `is_live` so the operator knows how fresh it is.

- **[Tool-def token estimation cost]** → `estimate_tokens(json.dumps(tool_defs))` runs each turn. For 35+ tools, the JSON serialization + token estimation adds a small overhead. *Mitigation:* the estimate is heuristic (not tiktoken for every call); the cost is negligible compared to the LLM call itself. Can be cached if tool defs don't change (they're built once per run).

- **[ToolRegistry cross-reference]** → Grouping tool defs by server requires cross-referencing tool names from `_tool_defs` back to the `ToolRegistry`. If a tool name exists in `_tool_defs` but not in the registry, it falls into an "unknown" group. *Mitigation:* this shouldn't happen in practice (tool defs are built from the registry), but the "unknown" group handles it gracefully.

- **[maybe_compact backward compatibility]** → Adding `tool_defs_tokens` with default 0 means existing callers (tests, other code paths) are unaffected. *Mitigation:* the default-0 design ensures no behavior change for callers that don't pass the parameter.

- **[Monitor lifecycle]** → The monitor persists between runs (holds last snapshot). If the agent restarts, the monitor is empty until the first run. *Mitigation:* `/context` reports "no snapshot available" in this case, which is correct.

## Migration Plan

1. **Add `ContextMonitor` and `ContextSnapshot`** — new module `context_monitor.py`, no existing code changes.
2. **Wire monitor into `AgentController`** — add `context_monitor` field; wire in `main.py` construction.
3. **Add `context_monitor` to `ControllerDeps` and `ReactContext`** — follows ADR-0007 pattern.
4. **Publish snapshot from `react_loop`** — add publish call after each turn; compute tool-def tokens.
5. **Fix `maybe_compact`** — add `tool_defs_tokens` parameter; update call site in `react_loop`.
6. **Add `context_profile` built-in tool** — new handler in `builtin_tools/`, new descriptor in `descriptors.py`, new schema in `schemas.py`.
7. **Add `/context` Telegram command** — new `cmd_context` handler; register in `telegram_interface.py`; add to BotFather menu and help text.
8. **Update tests** — new tests for monitor, updated tests for `maybe_compact`, new tests for command and tool.
9. **Update docs** — README and config example with `/context` command and `context_profile` tool.

**Rollback:** The change is additive (new module, new command, new tool) except for the `maybe_compact` parameter (backward-compatible default). Rolling back means removing the new code and reverting the `maybe_compact` signature — the default-0 parameter means existing callers don't break.

## Open Questions

- **Should the tool-def token estimation be cached per run?** Tool definitions are built once per run (`_ensure_tool_defs` in `react_loop.py:960-969`), so the token cost is constant within a run. Caching avoids re-computing `estimate_tokens(json.dumps(tool_defs))` every turn. Low priority — the cost is small — but worth noting for the implementation.

- **Should the `context_profile` tool accept arguments?** E.g., `context_profile --detailed` for a richer view. Currently scoped as summary-only (no arguments). If future section-level tracking is added, the tool could gain a `detail` argument. Deferred.

- **No in-force ADRs need supersession for this change.** ADR-0020 (per-model context window) is amended by the `maybe_compact` fix but does not need supersession — the threshold formula is extended, not replaced. The ADR step should record a new ADR documenting the tool-def visibility fix as an amendment to ADR-0020's compaction model.