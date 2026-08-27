# Use record-exactly-once lifecycle logging with component log isolation

## Status

Accepted

## Date

2026-08-27

## Supersedes

None

## Context

ADR-0004 established the structlog dual-sink backbone (JSONL primary, prose secondary, one processor chain, closed `LogEvent` taxonomy, contextvars run identity). Its retrofit left three residues. First, hot-set modules emitted both a structured `log_event` record and an unstructured `logger.` line for the same lifecycle moment — four pure duplicates, three prose twins whose fields (model, goal, steps) the structured events lacked, and one path (`builtin_executor._emit_tool_lifecycle_error`) that emitted both `ERROR` and `TOOL_FAILED` for a single tool exception, while the react_loop tool-event lifecycle helper (`_emit_tool_lifecycle`) deliberately emitted only `TOOL_FAILED` with a double-count comment. Second, the optional background graph-memory component (daemon thread) wrote ~40 unstructured records into `agent.jsonl`/`agent.log` carrying no trace identity and no `event_type` — uncorrelatable noise in the primary machine surface. Third, nothing documented the actual two-tier contract: which records are structured lifecycle events, which are prose diagnostics, and where component diagnostics belong.

Extending structured events to background components was explored and rejected (see the change's explore brief): a worker thread has no run scope to correlate to, and `merge_contextvars` (structlog 26.1.0 uses `event_dict.setdefault(...)`) would make always-on binding pointless rather than dangerous — ADR-0004's own consequence that background-thread identity is absent unless bound makes the fire-and-forget posture the correct reading, not an oversight.

## Considered Options

- **Keep both records per lifecycle moment** (prose twin + structured event): preserves prose-only consumers, but preserves the exact drift C-32 exists to close; `log_query` sees duplicate facts in different shapes.
- **Keep the prose twin, drop the structured event**: regresses `log_query` lifecycle querying (the reason ADR-0004 exists).
- **Extend the `LogEvent` enum with subsystem events (GRAPH_BATCH_START etc.)**: rejected — the flat enum would conflate core run lifecycle with subsystem diagnostics, and the component's daemon thread has no run identity to enrich those events with; the events would be queryable but uncorrelatable.
- **Component log isolation (static logger routing) + record-exactly-once**: `graph_memory` records leave the primary sinks entirely; each hot-set lifecycle moment is recorded once by the structured event carrying its prose twin's fields; `builtin_executor` aligns with the react_loop single-`TOOL_FAILED` precedent.

## Decision Outcome

Chosen: record-exactly-once lifecycle logging with component log isolation. The structured `log_event` record is the record of truth for every hot-set lifecycle moment; duplicate prose lines are removed after their fields are merged into the structured events; `builtin_executor` emits `TOOL_FAILED` only for unexpected tool exceptions (no companion `ERROR` record — `ERROR` remains a reserved, zero-emitter enum member). Optional background components route to dedicated component logs (`graph_memory.log`, same daily-gzip rotation) via static logger configuration (`propagate=False`, dedicated handlers, stdout at WARNING+), configured regardless of component enablement. The `LogEvent` taxonomy stays closed at its ten core members.

This is a companion contract to ADR-0004, not a supersession: the backbone (structlog via `ProcessorFormatter`, dual sink, closed taxonomy, contextvars identity) is unchanged; this decision governs *how many records represent one fact* and *where component diagnostics live*.

## Consequences

- Good, because `log_query` sees each lifecycle fact exactly once, in one shape, with the operational fields (model, goal, steps) the prose twins held.
- Good, because `agent.jsonl` becomes purely agent lifecycle; component noise with no correlation value leaves the primary machine surface.
- Good, because the fire-and-forget posture is now an explicit contract: background components need no trace plumbing and no taxonomy extension.
- Bad, because prose-surface detail lines (tool result, step model, finish stats) now exist only as `_ProseRenderer` renderings of structured events — acceptable, but prose-grep habits must rely on the renderer's shape.
- Bad, because `event_type = "ERROR"` matches nothing after this change (reserved member; still advertised as a filter value) — future error paths may re-activate it.
- Neutral, because component log isolation is a pattern: future optional background components (scheduler jobs, MCP connections) should follow the same static-routing recipe instead of extending the taxonomy.
- Follow-up: a queryable store (SQLite/DuckDB, C-34) may later add a component JSONL sink; that mechanism decision supersedes the active-file query approach, not this contract.