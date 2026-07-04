# Structured-primary agent logging with contextvars identity

## Status

Accepted

## Date

2026-07-04

## Supersedes

None

## Context and Problem Statement

Agent logging emitted only human prose (`%(asctime)s [%(levelname)s] %(name)s: %(message)s`). Run identity (trace id `r-<hex>`, agent label) was formatted into a `log_prefix` string and threaded explicitly through call arguments, so it lived inside the message text rather than as structured fields. The agent could not analyze its own executions except by re-reading prose through an LLM (the self-health task read the last 500 lines), and there was no fact-level, queryable, per-step operational record.

We want the agent to be the primary consumer of its own logs — querying structured operational facts mid-run to self-correct — while preserving the human prose stream (`tail -f`, `grep`). This is an observability decision, and it must respect the existing project principle (`trace_context.py`) that no process-global mutable trace state is used for *correctness-critical* behavior.

## Considered Options

- **Keep prose-only logs; parse them for analysis** — No new format, but analysis stays lossy and LLM-dependent; identity must be regex'd out of prose, which is fragile and drifts per call site.
- **JSONL-only, render human views on demand** — One source of truth, but breaks `tail -f`/`grep` unless a bespoke viewer is built and maintained.
- **Structured-primary dual sink with contextvars identity** — A JSONL sink (primary, machine-readable) written alongside the retained prose sink (secondary), both rendered from one filtered `LogRecord`. Identity is lifted into record attributes via a `contextvars`-backed logging filter, generalizing the pattern already used in `llm_client.py`. A closed `LogEvent` enum gives the agent a discoverable query vocabulary.
- **Structured events in a SQLite/queryable store** — Richest queries, but adds a datastore, overlaps the existing `ResultsMemory`/`StrategyMemory` subsystems, and is unnecessary for "recent events in this run."

## Decision Outcome

Chosen option: "Structured-primary dual sink with contextvars identity", because it makes the machine-readable record the optimized surface without losing the human stream, eliminates format drift by rendering both sinks from one record, and reuses an in-tree ambient-context pattern. Identity in the logging filter is observability-only ambient state, leaving correctness-critical trace propagation explicit and unchanged. A closed event taxonomy is preferred over ad-hoc event strings so the agent can enumerate and query a stable contract. A SQLite store is explicitly deferred; the boundary "logs hold operational facts, memory holds learned meaning, flowing one way" keeps the log layer from reinventing the memory subsystems.

## Consequences

- Good, because the agent can query structured facts about its own run instead of re-parsing prose through an LLM.
- Good, because prose and JSONL never drift — both render from the same filtered record.
- Good, because run identity is a first-class field, not text buried in the message.
- Good, because the closed `LogEvent` enum is a discoverable, stable query contract.
- Bad, because the JSONL sink is more verbose on disk than prose (mitigated by daily rotation + gzip and by structured fields only at hot call sites).
- Bad, because `contextvars` identity must be set at thread/executor entry points or it is lost on sub-agent/background threads.
- Neutral, because a queryable datastore (SQLite) may still be revisited later; if adopted it would supersede the "active-file, in-process query" mechanism, not this record-format decision.
