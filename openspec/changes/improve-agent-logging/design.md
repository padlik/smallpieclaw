## Context

Today all logging is configured in `main.py._setup_logging()`. A single prose formatter (`%(asctime)s [%(levelname)s] %(name)s: %(message)s`) feeds a stdout `StreamHandler` and a custom `_NightlyRotatingFileHandler` writing `agent.log` **inside the source checkout** (`_DEFAULT_LOG = _AGENT_DIR/agent.log`). Run identity (`trace` id like `r-9f3c`, agent `label` like `sa-1a2b`) is not a structured field: it is formatted into a `log_prefix` **string** in `react_loop.py` (`log_prefix` property) and threaded explicitly through call arguments (`context_manager`, `memory_store` all accept a `log_prefix` param). `llm_client.py` is the one exception — it already keeps its trace in a `contextvars.ContextVar` and weaves it into a caller tag.

The agent cannot analyze its own executions except by re-reading prose through an LLM (the `scheduler.toml.example` self-health task literally reads the last 500 lines). There is no fact-level, queryable, per-step operational record.

**In-force ADRs** (supersession graph `0001 → 0002 → 0003`): only **ADR-0003 (TOML agent-scoped vault)** is live. It establishes an agent-scoped vault at `~/.local/share/<agent_name>/` resolved independently of `agent_home`. This design is coherent with it in two ways: (1) the new XDG log path mirrors that rule using the correct XDG *state* dir (`~/.local/state/<agent_name>/logs/`), and (2) secret redaction sources its known values from that vault. No in-force ADR needs revisiting.

**Constraint** (`trace_context.py`): trace identity is threaded explicitly and "no process-global mutable trace state is used for **correctness-critical** behavior." This design honors that — the new ambient identity lives in a logging-only `contextvars` filter, which is observability, not correctness.

## Goals / Non-Goals

**Goals:**
- Give the agent a structured, queryable, fact-level record of its own executions (`agent.jsonl`), primary over prose.
- Let the agent query the active structured log mid-run, scoped to the current run, cheaply and in-process.
- Move logs out of the checkout to an XDG state dir; fix rotation (date-suffixed + gzip).
- Preserve the human prose stream (`tail -f`, `grep '[sa-xxx]'`) as a retained, secondary surface with zero format drift from the structured surface.
- Keep the change incremental: identity + rotation land independently; structured events enrich a small hot set of call sites.

**Non-Goals:**
- No SQLite / queryable database. In-process filtering over the active JSONL is sufficient for "recent events in this run."
- No retrofit of every `logger` call site to structured events — only the hot set.
- No mid-run querying of rotated `.gz` history. Cross-run learning stays with `ResultsMemory` / `StrategyMemory` (logs = facts, memory = meaning; one-way flow).
- No new external dependency.

## Decisions

### C4 Context — who reads the logs

```
                    ┌─────────────────────────────────────────┐
                    │              Agent process                │
                    │   (ReAct loop, tools, LLM client, sched)  │
                    └───────────────────┬───────────────────────┘
                                        │ emits log records
                     ┌──────────────────┴───────────────────┐
                     ▼                                       ▼
          ┌────────────────────┐                 ┌────────────────────────┐
          │  Human operator    │                 │  Agent (self-analysis)  │
          │  reads agent.log   │  ◄── secondary  │  queries agent.jsonl    │ ◄── primary
          │  tail -f / grep    │                 │  via log_query tool     │
          └────────────────────┘                 └────────────────────────┘
```

### C4 Container/Component — the logging pipeline

```
   logger.info(msg, extra={event, tool, dur_ms, exit, err})
        │
        ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  RootLogger                                                    │
   │   ├─ TraceIdentityFilter  (NEW)                                │
   │   │    reads contextvars → sets record.trace / .agent / .label │
   │   └─ SecretRedactionFilter (NEW)                               │
   │        scrubs known vault values from record.msg + extra       │
   └───────┬───────────────────────────────────┬──────────────────┘
           ▼                                    ▼
   ┌───────────────────┐              ┌────────────────────────────┐
   │ ProseHandler       │              │ JsonlHandler                │
   │  StreamHandler +   │              │  GzipRotatingFileHandler    │
   │  file (agent.log)  │              │  → agent.jsonl              │
   │  renders "[label   │              │  JsonFormatter reads        │
   │  trace] msg" from  │              │  record attrs → one JSON    │
   │  record attrs      │              │  object per line            │
   └───────────────────┘              └──────────────┬─────────────┘
                                                      ▲
                                    log_query tool ───┘ (in-process read of
                                    filters active agent.jsonl by trace/level/event
```

**D1 — Lift identity into `LogRecord` via a `contextvars` filter (not prose-parsing).**
A `TraceIdentityFilter` reads context-local `trace`/`agent`/`label` and sets them as record attributes. Both formatters render from those attributes: prose reconstructs `[label trace] msg`; JSON emits fields. *Alternatives:* (a) regex identity back out of the prose message — rejected as fragile/lossy, every call site formats differently; (b) pass a struct to every call — rejected, that's the status quo `log_prefix` threading we want to shed. The `contextvars` approach generalizes an in-tree, proven pattern (`llm_client.py`). Explicit `log_prefix` args at touched sites are retired; correctness-critical trace threading is untouched.

**D2 — Dual sink, structured-primary.**
Two handlers off one logger. `agent.jsonl` is the optimized surface; `agent.log` is retained prose. One log call, one filtered record, two renders → no drift. *Alternative:* JSONL-only with a rendered `piclaw logs` viewer (Variant C) — rejected because the user wants `tail -f` to keep working with no new tooling.

**D3 — Closed `LogEvent` enum for the taxonomy.**
A small `enum` (`TOOL_START`, `TOOL_END`, `TOOL_FAILED`, `LLM_CALL`, `LLM_FAILED`, `STEP_BEGIN`, `STEP_END`, `RUN_BEGIN`, `RUN_END`, `ERROR`) in one module. *Alternative:* ad-hoc `extra={"event": "..."}` strings — rejected: drift (`tool_fail`/`tool_failed`) defeats machine querying, and the agent cannot enumerate valid events to query against. The enum is the discoverable contract.

**D4 — `log_query` as a built-in tool, in-process, active-file-only.**
Registered alongside `shell`/`file_read` in `builtin_executor`. Reads only the active `agent.jsonl`, filters in Python by `trace` (defaulting to the current run), `level`, `event`, `tool`, `since`, with a result cap. *Alternatives:* shell out to `grep`/`jq` — rejected (adds process spawn latency mid-loop, external dep on `jq`); SQLite — deferred (Non-Goal). Active-file-only keeps reads bounded by daily rotation.

**D5 — Rotation: date-suffixed + gzip, 30-day.**
Replace `_NightlyRotatingFileHandler`'s numbered shift with date-suffixed backups compressed to `.gz`, retention 30. Applies to both sinks. *Alternative:* keep numbered shift — rejected (the "primitive rotation" complaint; no compression).

**D6 — Secret redaction filter.**
A `SecretRedactionFilter` scrubs known vault values (sourced from the ADR-0003 vault) from `record.msg` and structured `extra` before either handler serializes. Structured fields (`err`, `tool` args) invite dumping stderr/args that may contain secrets, so redaction runs at the filter layer, covering both sinks uniformly.

**D7 — XDG state path resolution.**
Log directory resolves to `~/.local/state/<agent_name>/logs/` independently of `agent_home`, mirroring the vault rule. `[paths] log_file` semantics shift from a checkout-relative filename to a name within the resolved XDG dir; an explicit absolute `log_file` still overrides.

## Risks / Trade-offs

- **JSONL volume > prose** -> active-file-only queries + daily rotation + gzip backups bound size; only the hot set emits verbose structured fields.
- **`contextvars` not propagated to a new thread/executor** (identity lost on sub-agent/background threads) -> set the context vars at thread entry points (sub-agent runner, scheduler job start) exactly where `trace_id` is already forwarded.
- **Redaction misses a secret shape** -> source values from the vault (exact-match scrub) and default to redacting known keys; treat redaction as defense-in-depth, not a guarantee, and keep secrets out of `extra` by convention at emit sites.
- **`log_query` returns too much mid-loop, blowing context budget** -> mandatory result cap + default `level>=WARNING` + trace-scoped default; summarize counts when over cap.
- **Migration breaks tests/tooling asserting `agent.log` in the checkout** -> update tests; document the path move as BREAKING in proposal and README.
- **Two sinks diverge** -> mitigated structurally by D1/D2: both render from the same filtered record; no second code path builds identity.

## Migration Plan

1. Land D1 (identity filter) + D5/D7 (rotation + XDG path) first — these stand alone, fix the stated annoyance, and change no call sites' semantics beyond retiring `log_prefix` at touched sites.
2. Add D2/D3/D6 (JSONL sink, `LogEvent` enum, redaction) — additive; prose behavior unchanged.
3. Emit structured `extra={}` at the hot set (tool, LLM, step/run, error paths).
4. Add D4 (`log_query`) and switch the `scheduler.toml.example` self-health task to structured querying.
5. **Rollback:** each step is independent; reverting D2–D4 leaves the relocated prose log fully functional. No data migration — historical `agent.log(.N)` files are left in place at the old path.

## Open Questions

- **`level>=WARNING` default for `log_query`** — is warning-and-above the right default filter for self-correction, or should the agent see `INFO` step boundaries by default? (Refined in the `runtime-log-introspection` spec.)
- **Redaction scope** — scrub only exact vault values, or also apply pattern heuristics (e.g., bearer-token shapes)? Starting exact-match; heuristics deferred.
- No in-force ADR requires supersession. The XDG-state-path decision (D7) is durable and cross-cutting enough that the `adr` step should record it as a new ADR alongside the vault path rule.
