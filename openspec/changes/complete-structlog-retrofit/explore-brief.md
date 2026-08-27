# Explore Brief — complete-structlog-retrofit (C-32)

Exploration session summary; baseline for artifact review. Commitments below must appear in proposal/design/specs/tasks with no gaps or contradictions.

## Rejected alternatives (and why)

- **Full structlog conversion** (~993 stdlib `logger.` call sites → `slog`): rejected — massive churn, diagnostics gain nothing from structure; two-tier design already works via `foreign_pre_chain`.
- **Namespaced event types (`GRAPH.BATCH_START`) + prefix matching in log_query**: rejected — changes matching semantics, adds wildcard API surface, mixed enum/string typing.
- **Two-tier taxonomy (core enum + free-form subsystem strings)**: rejected — loses call-site type safety and single source of truth to save ~4 enum lines.
- **Grow enum with subsystem events (GRAPH_BATCH_START etc.) — "Option A"**: initially recommended with auto-generated descriptor, then REJECTED after risk analysis: (1) trace identity gap — GraphMemoryWriter daemon thread never binds run context → untraceable events; (2) ~~merge_contextvars overwrite hazard~~ **corrected during design review**: structlog 26.1.0 `merge_contextvars` uses `event_dict.setdefault(...)`, so explicit fields take precedence over contextvars — no silent-overwrite hazard exists; the decisive reason is instead ADR-0004's consequence that identity on background threads is simply absent unless bound, and binding it for a run-less daemon is pointless machinery; (3) flat enum conflates core lifecycle with subsystem events.
- **SQLite/DuckDB queryable log store (C-34)**: deferred — separate change; third-party research (librarian) found sqlite-utils processor and DuckDB read_json_auto as viable paths later.
- **Custom AST duplicate-linter / fingerprinting processor**: guardrails only, not needed to fix the existing (already manually mapped) duplicates; optional future work.
- **Backfill CLI in scope**: excluded — `backfill_graph_memory.py` uses its own `logging.basicConfig`, never touches agent sinks.

## Final approach — commitment tables

### Dedup matrix (hot-set modules)

| Category | Location | Action |
|---|---|---|
| A (pure dup, remove) | react_loop.py:1342 `logger.info("Tool result: success=True")` | remove — TOOL_END covers it |
| A | react_loop.py:1344 `logger.warning("Tool result: success=False…")` | remove — TOOL_FAILED covers it |
| A | llm_client.py:391 `logger.error("LLM chat error")` | remove — LLM_FAILED covers it |
| A | llm_client.py:536 `logger.error("LLM chat (tools) error")` | remove — LLM_FAILED covers it |
| B (merge fields, then remove prose) | react_loop.py:1418 → STEP_BEGIN(1407) | add `model`; remove prose |
| B | react_loop.py:1544 → RUN_BEGIN(1531) | add `model`, `goal`; remove prose |
| B | react_loop.py:1724 → RUN_END(1598) | add `model`, `steps`; remove prose |
| C (reconcile split) | builtin_executor.py `_emit_tool_lifecycle_error` (ERROR + TOOL_FAILED) | emit TOOL_FAILED only, matching `_tool_span` (MCP path) precedent |

Line numbers are exploration-time references; apply step re-verifies.

### Component isolation (graph memory)

| Aspect | Decision |
|---|---|
| Logger name | `graph_memory` (matches `logging.getLogger(__name__)` in graph_memory.py) |
| Routing | `propagate=False`; dedicated handlers in `setup_logging()` |
| File | `graph_memory.log` under XDG logs dir; same daily-gzip rotation/backup policy |
| Console | stdout handler at WARNING+ (INFO/DEBUG file-only) — assumption confirmed |
| Identity | none bound; no trace/agent; no `event_type` events; component is fire-and-forget |
| Enablement | routing configured regardless of `[graph_memory] enabled` (static, unconditional) |
| Backfill CLI | unaffected (own basicConfig; root-logger propagation intact) |

### Taxonomy

- `LogEvent` stays closed at 10 members (TOOL_START/END/FAILED, LLM_CALL/FAILED, STEP_BEGIN/END, RUN_BEGIN/END, ERROR). Zero new members. Docstring rewritten to state the real contract.
- log_query / default view / descriptor: unchanged (no auto-generation needed at 10 members).

## Cross-module data flows

- `main.py` → `setup_logging()` (agent_logging.py) → configures root handlers (agent.jsonl/agent.log/stdout) + now the `graph_memory` logger with dedicated handlers; `xdg.py` provides the graph-memory log path.
- Emission: react_loop/llm_client/builtin_executor `log_event()` → structlog chain → JSONL+prose; `graph_memory` logger records → component handlers only.
- Consumers: `log_query` (unchanged — reads agent.jsonl only).

## Open questions / assumptions

1. Console level for graph memory = WARNING+ (assumption confirmed in conversation).
2. Prose-only component log (no graph_memory.jsonl) — YAGNI; revisit with C-34.
3. Exact mechanism for stdout WARNING+ (handler-level vs logger-level) — design decision.
4. Whether `_emit_tool_lifecycle_error`'s ERROR event has consumers (log_query default view uses level≥WARNING, so TOOL_FAILED at ERROR level still satisfies it) — verify no test/code depends on ERROR event_type before removal.