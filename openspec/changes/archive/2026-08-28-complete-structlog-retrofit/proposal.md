# Proposal: Complete structlog retrofit (C-32)

## Why

The structlog retrofit (ADR-backed dual-sink logging) left the codebase with an undocumented two-tier split: 3 hot-set modules emit both structured `log_event()` events AND unstructured `logger.` records describing the *same* lifecycle moments, while `agent.jsonl` — the primary machine surface — also collects unstructured diagnostic noise from the optional `graph_memory` background component. The result is double-logged lifecycle events (one queryable, one not), an ERROR/TOOL_FAILED double-count in `builtin_executor` that the MCP path explicitly avoids, and a primary log polluted with out-of-scope records that carry no trace identity and can never be correlated. Closing C-32 now settles the two-tier logging contract deliberately instead of by accumulation.

## What Changes

- **Deduplicate lifecycle logging in hot-set modules** (react_loop.py, llm_client.py):
  - Remove 4 pure-duplicate `logger.` calls that restate what an immediately adjacent `log_event()` already records (`react_loop.py` tool-result lines, `llm_client.py` LLM error lines).
  - Enrich 3 structured events with fields currently only in their prose twins, then remove the prose twins: `STEP_BEGIN` += `model`; `RUN_BEGIN` += `model`, `goal`; `RUN_END` += `model`, `steps`.
  - Reconcile the exception-path split: `builtin_executor._emit_tool_lifecycle_error()` currently emits BOTH `ERROR` and `TOOL_FAILED` for one failure; the react_loop MCP tool-span lifecycle helper emits only `TOOL_FAILED` and documents why ("an additional ERROR event would double-count"). Align `builtin_executor` to emit `TOOL_FAILED` only.
- **Isolate the optional graph-memory component's logs**: `graph_memory` records route to a dedicated `graph_memory.log` (same daily-gzip rotation policy) instead of the primary `agent.jsonl`/`agent.log` sinks. Routing is static and enablement-independent — configured regardless of whether `[graph_memory] enabled` is true. Console visibility drops to WARNING+. No trace/agent identity is bound for these records — graph memory is a fire-and-forget enrichment layer, not run-scoped work (rationale in design.md, including the `merge_contextvars` overwrite hazard). No structured `event_type` events are added for it.
- **Document the two-tier logging contract**: `LogEvent` stays closed at its 10 core members (TOOL_*, LLM_*, STEP_*, RUN_*, ERROR); the docstring is updated to state the real contract (structured lifecycle events in `agent.jsonl`; component diagnostics in component-specific logs; plain `logger.` prose flows through the shared processor chain).
- **Out of scope**: the one-time `backfill_graph_memory.py` CLI (own `basicConfig` setup, never touches the agent sinks); third-party storage/DuckDB work (tracked separately as C-34); promoting any cold-set module to structured events.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `structured-event-logging`: (a) hot-set lifecycle moments MUST be recorded exactly once — the structured `log_event` record is the record of truth, prose duplicates are removed, and structured events carry the fields the removed prose lines held; (b) records emitted by the optional background graph-memory component MUST NOT appear in `agent.jsonl`/`agent.log` — they route to a component-specific log file, with console output limited to WARNING+.

## Impact

- **Code**: `react_loop.py` (5 log lines removed — 2 Category-A tool-result lines + 3 Category-B prose twins; 3 `log_event` calls enriched), `llm_client.py` (2 `logger.error` calls removed), `builtin_executor.py` (1 `log_event` call — the ERROR event — dropped from `_emit_tool_lifecycle_error`), `agent_logging.py` (docstring + optional component-isolation helper), `main.py` / `xdg.py` (graph-memory log path resolution), `graph_memory.py` (none or logger-name confirmation only — its records simply stop reaching the primary sinks).
- **Specs**: delta on `openspec/specs/structured-event-logging/spec.md` (dedup contract + component log isolation).
- **Tests**: `tests/test_react_loop.py`, `tests/test_agent_logging.py`, `tests/test_tool_brief_panel.py` (taxonomy guard unaffected), new tests for single-record lifecycle emission and graph-memory log routing.
- **Behavior notes**: `agent.jsonl`/`agent.log` shrink (graph-memory records disappear from them — accepted, they were uncorrelatable noise). **Disclosed behavior change (not silent):** `event_type = "ERROR"` records for tool exceptions disappear from `agent.jsonl` — any consumer filtering `event_type = "ERROR"` for tool failures loses those records; practical impact is low because `TOOL_FAILED` at ERROR level satisfies the same `log_query` filters, and a pre-removal verification task confirms no code/test depends on the ERROR `event_type`. Prose log lines for tool results / LLM errors / run start-finish now come from the structured events' `_ProseRenderer` rendering; `log_query` output is unchanged in shape (fewer duplicate-ish records in results).
- **Docs**: AGENTS.md logging section updated to describe the two-tier contract and the graph-memory log file.

## Assumptions (resolved from exploration; revisit only if wrong)

1. Graph-memory console visibility is WARNING+ (INFO stays file-only) — balances clean console against visible failures.
2. Graph-memory gets a prose log file only (`graph_memory.log`); no component JSONL sink (YAGNI — add later if DuckDB/C-34 work wants it).
3. The `LogEvent` enum gains zero new members; the "closed taxonomy" docstring is corrected rather than the taxonomy extended.