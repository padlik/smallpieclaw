# Design — complete-structlog-retrofit (C-32)

## Context

ADR-0004 (in force, nothing supersedes it) established the logging backbone: structlog via `ProcessorFormatter`, dual sink (`agent.jsonl` primary JSON / `agent.log` prose) from one processor chain, `LogEvent` closed taxonomy, contextvars run identity, and explicit deferment of a queryable store. The retrofit left three residues:

1. **Duplicate lifecycle records** — hot-set modules emit a structured `log_event` AND an unstructured `logger.` line for the same moment (exploration mapped 4 pure duplicates, 3 enrich-then-remove pairs, and one ERROR/TOOL_FAILED double emission).
2. **Component noise in the primary sink** — `graph_memory` (optional, background daemon thread) writes ~40 unstructured records into `agent.jsonl`/`agent.log` with no trace identity and no `event_type`; they are uncorrelatable and invisible to the `log_query` default view.
3. **An undocumented two-tier contract** — the `LogEvent` docstring says "closed, small, stable" but nothing states the real policy for lifecycle vs. diagnostic vs. component records.

Constraints: `log_query` (structured-event-logging capability) reads only `agent.jsonl`; the `_ProseRenderer` reproduces the legacy prose line from structured fields; the backfill CLI configures its own logging and is untouched by any of this.

## Goals / Non-Goals

**Goals:**

- Each hot-set lifecycle moment is recorded by exactly one structured record that carries the operational fields its prose twin held.
- `agent.jsonl` contains zero graph-memory records; the component's diagnostics land in a dedicated, rotation-equivalent `graph_memory.log`.
- The two-tier contract is stated in code (`agent_logging.py` docstring) and project docs (AGENTS.md).
- All existing tests pass unchanged except where they assert the removed duplicate lines; `make check` green.

**Non-Goals:**

- No conversion of the ~993 cold-set `logger.` call sites to structlog.
- No new `LogEvent` members; no descriptor auto-generation; no `log_query` semantics changes.
- No graph-memory JSONL sink, no trace identity for component records, no structured events for the component.
- No SQLite/DuckDB store (C-34, separate change); no AST lint/fingerprinting guardrails (optional future work).
- No changes to `backfill_graph_memory.py` or its logging.

## Decisions

### D1 — Structured record is the record of truth; prose twins removed

For each duplicate pair, enrich the `log_event` call with the prose line's fields, then delete the prose line.

| Pair | Enrich | Remove |
|---|---|---|
| react_loop tool result (2 sites, ~1342/1344) | none needed — TOOL_END/TOOL_FAILED already carry tool/exit/dur_ms; error detail is in TOOL_FAILED's `err` | both `logger.` lines |
| STEP_BEGIN (~1407 vs ~1418) | add `model` | `logger.info("step %d/%d \| model: %s…")` |
| RUN_BEGIN (~1531 vs ~1544) | add `model`, `goal` | `logger.info("start \| model…")` |
| RUN_END (~1598 vs ~1724) | add `model`, `steps` | `logger.info("finish \| model…")` |
| llm_client chat error (~391 vs 392; ~536 vs 537) | none needed — LLM_FAILED carries model/dur_ms/err | both `logger.error` lines |

Rationale: the prose line was a second rendering of the same fact; `_ProseRenderer` renders structured events to prose anyway, so the human surface keeps equivalent lines (with identity prefix) at no duplicate cost. Alternative (keep prose, drop structure) rejected — it would break `log_query` lifecycle querying. Alternative (keep both) preserves the drift C-32 exists to close.

### D2 — builtin_executor emits TOOL_FAILED only on unexpected exceptions

`_emit_tool_lifecycle_error` (builtin_executor.py:483-509) drops its `LogEvent.ERROR` emission and keeps `TOOL_FAILED` (ERROR level, carrying tool/dur_ms/exit/err). Precedent: the react_loop tool-event lifecycle helper (~1795-1808) emits only `TOOL_FAILED` with an explicit double-count comment. Verified: `LogEvent.ERROR` / `event_type="ERROR"` has no consumer anywhere (only the one emission site; zero test references); the `log_query` default view keys on level ≥ WARNING plus the {TOOL_START, TOOL_END, LLM_CALL} include-set, all unaffected. Rejected alternative: keep both records (ERROR as severity signal + TOOL_FAILED as lifecycle) — rejected because the two records carry identical failure facts and the react_loop precedent already treats the second as double-counting. Note: after this change `LogEvent.ERROR` has zero emitters; it remains an enum member as a **reserved** value for future error paths (the descriptor/schema advertise it as a filterable `event_type`, which stays valid — it simply matches nothing until an emitter uses it).

### D3 — Component isolation via static logger routing in `setup_logging()`

`setup_logging()` additionally configures the `graph_memory` logger (name matches `graph_memory.py`'s `getLogger(__name__)`):

```python
gm = logging.getLogger("graph_memory")
gm.propagate = False
gm.setLevel(logging.INFO)
# dedicated handlers:
#   _GzipTimedRotatingFileHandler(<xdg logs>/graph_memory.log, same backup_count) + prose_formatter
#   StreamHandler(stream) at WARNING level + prose_formatter
```

Properties:
- **Static and enablement-independent** — routing is configured unconditionally; a disabled graph memory cannot leak its disabled-notice into `agent.jsonl`.
- **Shared chain benefits** — handlers reuse the same `_ProseRenderer`/JSON formatters, so redaction and timestamps still apply if the component ever emits structured fields.
- **Console WARNING+** — implemented via handler-level (`gm_stream.setLevel(logging.WARNING)`), NOT logger-level, so INFO/DEBUG still reach `graph_memory.log` while stdout stays clean.
- **No identity by design** — the worker thread never binds run context. This is deliberate, not an oversight: ADR-0004 already establishes that identity on background threads is simply *absent* unless bound, and a fire-and-forget daemon has no run scope to correlate to — binding would be pointless machinery, not a safety requirement. (Verified against structlog 26.1.0: `merge_contextvars` uses `event_dict.setdefault(...)`, so explicit event-dict fields would safely take precedence over contextvars if a component ever wanted to carry an originating-run hint — the mechanism is available if ever needed, which further undercuts the case for always-on binding.)
- **Path** — new `graph_memory_log` property on `XDGPaths` (xdg.py, `…/logs/graph_memory.log`; named to mirror the existing `graph_memory_db` leaf); `setup_logging()` accepts it (computed internally from the existing log path by default; `main.py` passes nothing new unless a custom path is needed).

Alternative considered: structlog processor tagging + handler filters — more machinery for one component; plain logger routing is the stdlib-idiomatic mechanism. Alternative: JSONL component sink — deferred with C-34 (YAGNI today).

### D4 — Docstring/doc contract update, zero code change to the enum

`LogEvent` docstring rewritten: core lifecycle members are emitted by the react loop and its direct collaborators; component diagnostics live in component logs; plain `logger.` records flow through the shared chain without `event_type`. AGENTS.md logging section updated to match. The enum's 10 members are untouched.

### D5 — Tool-event lifecycle helper precedent note for apply

The double-count-prevention precedent lives in the react_loop tool-event lifecycle helper (`_emit_tool_lifecycle`, ~1795 — used by the tool-span wrapper for MCP tool calls, but the precedent itself is in the helper, not the span). During apply, the implementer re-confirms the exact carrier function before editing `builtin_executor` (line numbers from exploration may have drifted).

## Component diagram (lightweight C4-inspired, ASCII; purpose: existing code, design review)

```
┌───────────────────────────── Agent process (container) ─────────────────────────────┐
│                                                                                     │
│  Hot-set components               Logging backbone (agent_logging.py)               │
│  ┌────────────────┐               ┌───────────────────────────────────────────┐     │
│  │ react_loop     │──log_event──▶ │ shared processor chain                    │     │
│  │ llm_client     │──log_event──▶ │ (contextvars→level→logger→ts→rename→      │     │
│  │ builtin_exec   │               │  redaction)                               │     │
│  └───────┬────────┘               └───────┬───────────────────────┬───────────┘     │
│          │ logger. (deduped:              │                       │                 │
│          │  lifecycle lines removed)      ▼                       ▼                 │
│          │                        ┌──────────────┐        ┌──────────────┐          │
│          │                        │ agent.jsonl  │        │ agent.log    │          │
│          │                        │ (JSON primary│        │ + stdout     │          │
│          │                        │  for log_    │        │ (prose)      │          │
│          │                        │  query)      │        └──────────────┘          │
│          │                        └──────────────┘                                  │
│  ┌───────▼────────┐               routing (NEW, D3): propagate=False + handlers     │
│  │ graph_memory   │──────────────────────┬─────────────────────────┐                │
│  │ (daemon thread)│                      ▼                         ▼                │
│  └────────────────┘              ┌──────────────┐          ┌──────────────┐         │
│   no identity, no event_type     │ graph_memory │          │ stdout       │         │
│   (fire-and-forget)              │ .log (gzip   │          │ WARNING+ only│         │
│                                  │ rotation)    │          └──────────────┘         │
│                                  └──────────────┘                                   │
│                                                                                     │
│  backfill CLI (separate process): own basicConfig — untouched                       │
└─────────────────────────────────────────────────────────────────────────────────────┘
   Consumer: log_query built-in reads agent.jsonl only (unchanged)
```

- Boundaries: hot-set modules vs. optional background component; primary sinks vs. component sink.
- Responsibilities: the chain renders; routing decides *where*; the component decides nothing.
- Key relationships: `graph_memory` logger detaches from root propagation; hot-set `log_event` output unchanged in shape.
- Assumptions: console WARNING+ via handler level; prose-only component file.
- Open questions: none blocking (see below).

## Risks / Trade-offs

- [Prose-surface loses standalone detail lines (tool result/step model/finish stats)] → Mitigated: `_ProseRenderer` renders the enriched structured events to prose with identity prefix; verify rendered lines carry equivalent info during apply.
- [Dropped `ERROR` event_type breaks an unknown consumer] → Mitigated: verified zero consumers (single emission site, no test references); disclosed in proposal behavior notes; `log_query` default view unaffected (level-based).
- [Graph-memory failures become invisible to operators watching stdout] → Mitigated: WARNING+ console handler keeps failures visible; file retains full detail.
- [Routing code runs even when graph memory is disabled] → Cost is two idle handlers; acceptable for deterministic isolation (prevents disabled-notice leaks into `agent.jsonl`).
- [Enriched `goal` field could carry long user text into JSONL] → Mitigation in tasks: truncate `goal` to 80 chars, mirroring the removed prose line.
- [Line-number drift between exploration references and current code] → Mitigation in tasks: apply re-locates each call by content, not line number.

## Migration Plan

Single deployable unit (daemon restart picks up all changes). No data migration: `agent.jsonl`/`agent.log` simply stop receiving graph-memory records going forward; existing files are untouched and remain readable by existing tooling. Rollback = revert commit (routing is additive configuration; dedup is line removal). No schema/format changes to either sink.

## Open Questions

- None blocking. Two recorded for the ADR step: (1) whether the two-tier contract + component isolation should be recorded as a superseding or companion ADR to ADR-0004 (recommendation: companion ADR-0023 documenting record-exactly-once + component isolation, explicitly *not* superseding the backbone decision); (2) whether a component JSONL sink should accompany the future C-34 store work (deferred, not decided here).