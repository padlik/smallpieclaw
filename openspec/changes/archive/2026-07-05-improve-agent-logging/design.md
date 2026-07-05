## Context

Today all logging is configured in `main.py._setup_logging()`. A single prose formatter (`%(asctime)s [%(levelname)s] %(name)s: %(message)s`) feeds a stdout `StreamHandler` and a custom `_NightlyRotatingFileHandler` writing `agent.log` **inside the source checkout** (`_DEFAULT_LOG = _AGENT_DIR/agent.log`). Run identity (`trace` id like `r-9f3c`, agent `label` like `sa-1a2b`) is not a structured field: it is formatted into a `log_prefix` **string** in `react_loop.py` and threaded explicitly through call arguments (`context_manager`, `memory_store` accept a `log_prefix` param). `llm_client.py` already keeps its trace in a `contextvars.ContextVar`.

The agent cannot analyze its own executions except by re-reading prose through an LLM. There is no fact-level, queryable, per-step operational record. Every module already binds a stdlib logger via `logging.getLogger(__name__)`, so any solution must interoperate with stdlib logging rather than force a rewrite of ~20 call sites.

**In-force ADRs** (supersession graph `0001 → 0002 → 0003`): only **ADR-0003 (TOML agent-scoped vault)** is live. It establishes an agent-scoped vault at `~/.local/share/<agent_name>/` resolved independently of `agent_home`. This design is coherent with it: the XDG log path mirrors that rule using the XDG *state* dir (`~/.local/state/<agent_name>/logs/`), and the redaction processor sources its known values from that vault. No in-force ADR needs revisiting.

**Constraint** (`trace_context.py`): trace identity is threaded explicitly and "no process-global mutable trace state is used for **correctness-critical** behavior." This design honors that — `structlog.contextvars` carries identity for *logging only* (observability); correctness-critical trace propagation stays explicit and unchanged.

## Goals / Non-Goals

**Goals:**
- Give the agent a structured, queryable, fact-level record of its own executions (`agent.jsonl`), primary over prose, using `structlog` rather than bespoke stdlib filters/formatters.
- Let the agent query the active structured log mid-run, scoped to the current run, cheaply and in-process.
- Move logs out of the checkout to an XDG state dir; fix rotation (date-suffixed + gzip).
- Preserve the human prose stream (`tail -f`, `grep '[sa-xxx]'`) with zero drift from the structured surface.
- Interoperate with existing stdlib `logging.getLogger(__name__)` call sites; migrate only the hot set to structured key-values.

**Non-Goals:**
- No SQLite / queryable database. In-process filtering over the active JSONL is sufficient.
- No retrofit of every `logger` call site to native `structlog` loggers — foreign stdlib records flow through unchanged.
- No mid-run querying of rotated `.gz` history. Cross-run learning stays with `ResultsMemory` / `StrategyMemory` (logs = facts, memory = meaning; one-way flow).
- No custom log framework — that is exactly what adopting `structlog` avoids.

## Decisions

### C4 Context — who reads the logs

```
                    ┌─────────────────────────────────────────┐
                    │              Agent process                │
                    │   (ReAct loop, tools, LLM client, sched)  │
                    └───────────────────┬───────────────────────┘
                                        │ emits log events
                     ┌──────────────────┴───────────────────┐
                     ▼                                       ▼
          ┌────────────────────┐                 ┌────────────────────────┐
          │  Human operator    │                 │  Agent (self-analysis)  │
          │  reads agent.log   │  ◄── secondary  │  queries agent.jsonl    │ ◄── primary
          │  tail -f / grep    │                 │  via log_query tool     │
          └────────────────────┘                 └────────────────────────┘
```

### C4 Container/Component — the structlog pipeline

```
   structlog call:  log.info("tool failed", event_type="TOOL_FAILED",
                              tool="shell", exit=1, dur_ms=812)
   stdlib call:     logging.getLogger(__name__).info("Compacted context ...")
        │                              │
        │ (native structlog)           │ (foreign stdlib record)
        ▼                              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Shared processor chain                                        │
   │   merge_contextvars   ← trace/agent from structlog.ctxvars      │
   │   add_log_level, TimeStamper(iso)                              │
   │   redact_secrets (NEW) ← scrub known vault values              │
   │   ProcessorFormatter.wrap_for_formatter                        │
   │   (foreign stdlib records enter via foreign_pre_chain)         │
   └───────┬───────────────────────────────────┬──────────────────┘
           ▼                                    ▼
   ┌───────────────────────────┐      ┌────────────────────────────┐
   │ ProcessorFormatter         │      │ ProcessorFormatter          │
   │  processor=plain renderer  │      │  processor=JSONRenderer     │
   │  → "[label trace] message" │      │                             │
   │  → GzipRotatingFileHandler │      │  → GzipRotatingFileHandler  │
   │    + StreamHandler         │      │    → agent.jsonl            │
   │    → agent.log (prose)     │      └──────────────┬─────────────┘
   └───────────────────────────┘                     ▲
                                      log_query tool ─┘ in-process read of
                                      active agent.jsonl, filter by trace/level/event
```

**D1 — Adopt `structlog`, integrated through stdlib `ProcessorFormatter`.**
Configure `structlog` with `wrapper_class=structlog.stdlib.BoundLogger` and route rendering through `structlog.stdlib.ProcessorFormatter` set on stdlib handlers. Existing `logging.getLogger(__name__)` calls become "foreign" records processed via `foreign_pre_chain`; hot-set sites use `structlog.get_logger()` with structured kwargs. *Alternatives:* (a) hand-rolled stdlib `logging.Filter` + custom `JsonFormatter` (the prior design) — rejected: reinvents structlog's processor chain, contextvars merge, and JSON rendering; (b) `structlog` with its own non-stdlib output — rejected: would bypass existing stdlib loggers and force a full call-site rewrite. `ProcessorFormatter` is the documented bridge that lets both coexist.

**D2 — Dual sink, structured-primary, one processor chain.**
Two stdlib handlers, each with a `ProcessorFormatter` differing only in final renderer: `JSONRenderer` → `agent.jsonl` (primary), a plain key-value renderer that reproduces the current `[label trace] message` shape → `agent.log` + stdout (secondary). The shared pre-render chain guarantees identical content; only serialization differs, so no drift. *Alternative:* JSONL-only with a rendered viewer (Variant C) — rejected; `tail -f` must keep working with no new tooling.

**D3 — Closed `LogEvent` enum, emitted as `event_type`.**
A small `enum` (`TOOL_START/END/FAILED`, `LLM_CALL/FAILED`, `STEP_BEGIN/END`, `RUN_BEGIN/END`, `ERROR`) passed as the `event_type` key (structlog reserves `event` for the message). *Alternative:* ad-hoc `event_type` strings — rejected: drift defeats machine querying and the agent cannot enumerate valid values. The enum is the discoverable contract.

**D4 — Identity via `structlog.contextvars`.**
`bind_contextvars(trace=…, agent=…)` (where `agent` is the run label) at run entry (`react_loop` start — the common chokepoint for main, sub-agent, and scheduled runs); `merge_contextvars` injects them into every event; token-based reset on exit. Reconcile `llm_client.py`'s existing `ContextVar` by having it bind through `structlog.contextvars` (single source). *Alternative:* keep threading `log_prefix` strings — rejected; that is the status quo we are shedding.

**D5 — `log_query` built-in tool, in-process, active-file-only.**
Registered in `builtin_executor`. Reads only the active `agent.jsonl` (JSON-per-line from `JSONRenderer`), filters in Python by `trace` (default = current run), `level`, `event_type`, `tool`, `since`, with a result cap. The default filter (when no level is given) returns anomalies (`WARNING`+) **plus** `TOOL_START/END` and `LLM_CALL` regardless of level, while excluding high-volume `STEP_BEGIN/END` boundary chatter — so repeated-action patterns are visible without drowning the result cap in step noise. *Alternatives:* shell out to `jq` — rejected (process spawn latency, external dep); SQLite — deferred.

**D6 — Secret redaction as a `structlog` processor.**
A `redact_secrets` processor early in the chain scrubs known vault values (from the ADR-0003 vault) from the event dict — message and all key-values — before either renderer runs, covering both sinks uniformly.

**D7 — Rotation + XDG path via stdlib handlers.**
`structlog` renders into stdlib rotating handlers; use date-suffixed + gzip daily backups (retention 30) for both sinks. Log directory resolves to `~/.local/state/<agent_name>/logs/` independent of `agent_home`; an explicit absolute `log_file` overrides.

## Risks / Trade-offs

- **New dependency (`structlog`)** -> it is mature, widely used, pure-Python, dependency-light, and stdlib-compatible; pin a minimum version in `requirements.txt`.
- **`contextvars` not propagated to a new thread/executor** (identity lost on sub-agent/background threads) -> bind at each thread entry point exactly where `trace_id` is already forwarded; cover with a sub-agent test.
- **Double-configuration of stdlib + structlog** (foreign records mis-rendered or duplicated) -> single `configure()` in one logging module; `foreign_pre_chain` handles third-party (`httpx`, `telegram`) records; assert no duplicate handlers in a test.
- **JSONL volume > prose** -> active-file-only queries + daily rotation + gzip; verbose key-values only at the hot set.
- **`log_query` floods context mid-loop** -> mandatory result cap + trace-scoped default + a default filter of anomalies plus tool/LLM lifecycle minus step-boundary chatter (Option C); summarize counts when over cap.
- **Migration breaks tests asserting `agent.log` in the checkout** -> update tests; document the path move as BREAKING.
- **`llm_client` trace divergence** -> route its existing `ContextVar` through `structlog.contextvars` so there is one identity source.

## Migration Plan

1. Add `structlog` to `requirements.txt`; create the logging config module (processor chain, renderers, `LogEvent` enum, redaction processor) and swap `main.py._setup_logging` to `structlog.configure` + stdlib handlers. Land XDG path + gzip rotation here (D1/D2/D6/D7). Existing stdlib call sites keep working via `foreign_pre_chain`.
2. Bind identity via `structlog.contextvars` at run/thread entry; retire `log_prefix` threading at touched sites; reconcile `llm_client` (D4).
3. Emit `LogEvent` structured key-values at the hot set — tool, LLM, step/run, error paths (D3).
4. Add `log_query` (D5) and switch the `scheduler.toml.example` self-health task to structured querying.
5. **Rollback:** steps are independent; reverting D3–D5 leaves the relocated dual-sink logs working. Removing `structlog` entirely means reverting step 1. No data migration — historical `agent.log(.N)` files are left at the old path.

## Open Questions

- **`log_query` default filter** — RESOLVED (Option C): the no-level default returns anomalies (`WARNING`+) plus `TOOL_START/END` and `LLM_CALL` regardless of level, excluding `STEP_BEGIN/END` boundary chatter — surfacing repeated-action patterns without step noise. Captured in the `runtime-log-introspection` spec.
- **Prose renderer choice** — RESOLVED: use a plain key-value renderer that reproduces the current `[label trace] message` shape, preserving existing `grep`/`tail -f` habits, rather than `structlog.dev.ConsoleRenderer`.
- **Redaction scope** — RESOLVED (Option A): exact vault-value match only, paired with an emit-site convention to keep secrets out of `extra`. Deterministic, no false positives, no corruption of legitimate structured fields. A narrow high-confidence heuristic set (Option B) is a possible data-driven fast-follow once real tool-output leakage is observed; broad entropy heuristics are rejected.
- No in-force ADR requires supersession. Adopting `structlog` as the logging backbone is durable and cross-cutting; the `adr` step records it as a new ADR.
