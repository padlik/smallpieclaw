## Why

`builtin_executor.py` is 2713 lines — a single `BuiltinExecutor` class implementing 15
built-in tools plus their descriptor dict and dispatch — which violates the AGENTS.md
convention "avoid making superclasses and large files" and makes the module hard to read,
review, and extend. The archived `introduce-agent-runtime` change built the runtime
construction seam explicitly as a prerequisite for this split, so the work is now
unblocked.

## What Changes

- Extract the tool implementations from `builtin_executor.py` into a new `builtin_tools/`
  package, grouped by concern (shell, filesystem, sub-agents, memory/graph,
  secrets/logging) plus stateless leaf modules (descriptors, pattern detection, text
  truncation, log-query helpers, context I/O).
- `BuiltinExecutor` becomes a thin façade that constructs the tool-group handlers, owns all
  cross-cutting confirmation state, and routes calls through two name-keyed registry dicts
  (`_exec_table` for phase-1, `_run_table` for the six confirmation-capable tools) instead
  of the current `if/elif` chains.
- Tool-group handlers hold a live back-reference to the façade and read late-bound
  collaborators (the 8 post-init settables) at call time; they never snapshot them.
- The change is **behavior-preserving**: no runtime behavior, tool semantics, confirmation
  flow, or model-facing tool schema changes. The public executor API, the direct reach-in
  methods (`_exec_spawn_agent`, `_exec_get_agent_result`, `_exec_schedule`), the module-level
  helper functions, and the `BUILTIN_TOOLS` descriptor dict all remain importable/callable
  from `builtin_executor` via forwarders and re-exports.
- The `schedule` tool stays inline on the façade (tiny; a test builds a bare instance via
  `__new__` and calls `_exec_schedule`).
- **NOT included:** extracting confirmation into a `ConfirmationCoordinator`. That is a
  deliberate follow-on change. This change only bakes in three zero-cost seam constraints so
  the coordinator drops in cleanly later.

## Capabilities

### New Capabilities
- `builtin-tool-execution`: The behavioral contract for how the agent runtime dispatches
  built-in tools and gates dangerous/sensitive ones through confirmation. Scope is
  tool-agnostic — it covers the dispatch and confirmation *framework* and its preservation
  across the module split, **not** the semantics of any individual tool (those are
  unaffected). Invariants, expressed as observable behavior: dispatch equivalence (a caller
  invoking tool `T` gets the same result; `is_builtin`/`all_tools` enumerate the same set of
  15 tools), two-phase confirmation preservation (interactive token staging/`confirm`/`cancel`
  plus the headless sub-agent operator bridge), and the `vision_query` "declared as a built-in
  / executed in the ReAct loop" seam (still enumerated by `is_builtin`, still intercepted by
  the loop, never given a dispatch handler).

### Modified Capabilities
<!-- None. The split preserves the behavior of existing capabilities (execution-planning,
     sub-agent-supervision) exactly; their reach-ins into the executor are kept, so no
     existing spec-level behavior changes. Specs-phase author: before freezing, grep
     openspec/specs/ for any existing capability that already specifies per-tool semantics
     (secret_get, log_query, memory_graph_*); the new capability must NOT re-specify or
     contradict those — it stays scoped to the dispatch/confirmation framework. -->

## Impact

- **New:** `builtin_tools/` package (`__init__.py`, `descriptors.py`, `patterns.py`,
  `text_utils.py`, `logquery_helpers.py`, `context_io.py`, `shell.py`, `files.py`,
  `agents.py`, `memory.py`, `secrets_log.py`).
- **Modified:** `builtin_executor.py` (reduced to façade + registries + re-exports),
  `vulture_whitelist.py` (new public handler symbols).
- **Consumers unchanged (surface preserved):** `main.py`, `react_loop.py`, `scheduler.py`,
  `tool_index.py`, `agent_runtime.py`, `interfaces.py` (`ToolBackend` Protocol),
  `telegram_commands.py`. (`agent_runtime.py:313` imports `_load_context`; whether that stays
  a re-export or repoints to `builtin_tools.context_io` is a design-phase decision.)
- **Tests:** one monkeypatch target must move (`patch("builtin_executor._save_context")` in
  `test_subagent_context_persistence.py`); a new routing test locks the dispatch tables and
  the `vision_query` seam. No behavioral test assertions change.
- **Risk:** import cycles (keep collaborator imports function-local; keep
  `builtin_tools/__init__.py` light) and late-bound attribute capture (handlers must read the
  8 settables via the owner at call time).
