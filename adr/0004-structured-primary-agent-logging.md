# Use structlog for structured-primary agent logging

## Status

Accepted

## Date

2026-07-05

## Supersedes

None

## Context and Problem Statement

Agent logging emitted only human prose. Run identity (trace id `r-<hex>`, agent label) was formatted into a `log_prefix` string and threaded explicitly through call arguments, so it lived inside the message text rather than as structured fields. The agent could not analyze its own executions except by re-reading prose through an LLM, and there was no fact-level, queryable, per-step operational record.

We want the agent to be the primary consumer of its own logs — querying structured operational facts mid-run to self-correct — while preserving the human prose stream (`tail -f`, `grep`). Every module already binds a stdlib logger via `logging.getLogger(__name__)`, so the solution must interoperate with stdlib logging, not replace it. The decision is which logging backbone to build on. It must respect the existing project principle (`trace_context.py`) that no process-global mutable trace state is used for *correctness-critical* behavior — identity carried for logging is observability only.

## Considered Options

- **Hand-rolled stdlib logging** — Custom `logging.Filter` for contextvars identity, a custom `JsonFormatter`, and a custom redaction filter. No new dependency, but reinvents a processor pipeline, contextvars merging, JSON rendering, and foreign-record handling that a library already provides, and concentrates maintenance/correctness risk in bespoke code.
- **`structlog` bypassing stdlib** — Use `structlog`'s own logger and output path directly. Clean structured API, but bypasses the ~20 existing `logging.getLogger(__name__)` call sites and would force a full rewrite.
- **`structlog` integrated via `structlog.stdlib.ProcessorFormatter`** — A shared processor chain (contextvars merge, level, timestamp, redaction) rendered by `ProcessorFormatter` onto stdlib handlers. Existing stdlib records flow through `foreign_pre_chain`; hot-set sites use structured `structlog` loggers. Dual sink (JSON primary, prose secondary) shares one chain, so content cannot drift.
- **Structured events in a SQLite/queryable store** — Richest queries, but adds a datastore, overlaps existing `ResultsMemory`/`StrategyMemory`, and is unnecessary for "recent events in this run."

## Decision Outcome

Chosen option: "`structlog` integrated via `structlog.stdlib.ProcessorFormatter`", because it delivers structured-primary logging (a machine-readable JSONL surface plus a retained prose surface rendered from the same chain) without a bespoke framework and without rewriting existing call sites. `structlog.contextvars` carries run identity as an ambient, observability-only mechanism, leaving correctness-critical trace propagation explicit and unchanged. A closed `LogEvent` enum emitted as an `event_type` key gives the agent a stable, enumerable query contract. A SQLite store is explicitly deferred; the boundary "logs hold operational facts, memory holds learned meaning, flowing one way" keeps the log layer from reinventing the memory subsystems.

## Consequences

- Good, because the agent can query structured facts about its own run instead of re-parsing prose through an LLM.
- Good, because prose and JSONL are rendered from one processor chain and cannot drift.
- Good, because `ProcessorFormatter` + `foreign_pre_chain` let existing stdlib call sites keep working while the hot set adopts structured key-values incrementally.
- Good, because run identity, redaction, JSON rendering, and contextvars merging come from a maintained library instead of bespoke filters.
- Good, because the closed `LogEvent` enum is a discoverable, stable query contract.
- Bad, because it adds a runtime dependency (`structlog`) — mitigated: mature, pure-Python, dependency-light, version-pinned.
- Bad, because `structlog.contextvars` identity must be bound at each thread/executor entry or it is lost on sub-agent/background threads.
- Bad, because stdlib and `structlog` must be configured once and coherently to avoid duplicate/mis-rendered records.
- Neutral, because a queryable datastore (SQLite) may still be revisited later; if adopted it would supersede the "active-file, in-process query" mechanism, not this backbone decision.
