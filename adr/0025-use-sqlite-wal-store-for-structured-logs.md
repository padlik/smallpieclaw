# Use SQLite WAL store for structured logs

## Status

Accepted, supersedes ADR-0004 (partial — structured sink storage mechanism and active-file query approach only; the structlog backbone decision remains in force)

## Date

2026-08-31

## Supersedes

ADR-0004 (partial — storage mechanism of the structured primary sink and the active-file, in-process query mechanism; NOT the structlog/ProcessorFormatter backbone, contextvars identity, closed `LogEvent` taxonomy, redaction layer, or dual-sink rendering-from-one-chain contract, all of which remain in force)

## Context and Problem Statement

ADR-0004 established the structlog dual-sink backbone and explicitly deferred a queryable store as "unnecessary for recent events in this run." ADR-0023's follow-up note anticipated this moment: a queryable store "supersedes the active-file query approach, not this contract."

The deferral has expired. `log_query` reads only the active `agent.jsonl` through a bounded tail window (1MB / 5000 lines); everything in the 30 days of gzip-rotated backups is invisible to the agent. Prompt-scoped analysis for any prompt older than the active file (scheduled jobs, yesterday's runs, cross-run failure aggregation) is impossible through tooling, and the bounded-tail/predicate/window-saturation machinery exists solely to compensate for an unindexed storage format.

The exploration phase surveyed external solutions (`agentic-logger`, `sessionlog`, `atomic_agents` SQLite backend, `loglite`) and converged on wide-columns + `extra` JSON + partial indexes as the field-tested schema pattern.

## Considered Options

- **SQLite shadow index** (JSONL stays primary, background tailer builds the DB): dual storage, eventual-consistency lag, an extra tailer process, and the window machinery survives. More moving parts than the replacement itself — rejected.
- **Archive-scan patch** (log_query decompresses `.gz` on window saturation): slow, keeps the window machinery, only patches one failure mode — rejected.
- **Keep JSONL, add FTS5 side index**: full-text search is not the gap; indexed field lookups over full retention are — rejected (FTS5 recorded as a future option).
- **SQLite WAL store as the structured primary sink, prose `agent.log` untouched**: hard swap, single-writer queue ingestion, 30-day DELETE retention, `log_query` rewritten as indexed SQL, one-time idempotent backfill of existing archives (additive only, sources untouched) — chosen.

## Decision Outcome

Chosen: the structured log sink becomes a SQLite WAL database (`~/.local/state/<agent_name>/logs/agent_logs.sqlite`). The structlog processor chain still renders every record, but the structured sink inserts into the store via a bounded `QueueHandler` → `QueueListener` single-writer thread with batched INSERTs; the prose sink keeps daily gzip rotation unchanged. Records map to wide columns (`ts`, `level`, `logger`, `agent`, `trace`, `prompt_id`, `event_type`, `msg`) plus an `extra` JSON column for all other operational fields, plus a `search_text` column holding the casefolded compact-JSON of the full record to preserve the existing full-record, full-Unicode text-search semantics. Retention is a 30-day time-cutoff DELETE (replacing gzip rotation for the structured stream only). `log_query` compiles its unchanged filter surface to indexed SQL and now covers the full retention window. ADR-0004's backbone and ADR-0023's exactly-once contract are untouched; only the storage and query mechanism changed — the exact boundary both ADRs pre-authorized.

## Consequences

- Good, because the entire 30-day retention becomes queryable: prompt/trace lookups across day boundaries and cross-run aggregation are now single indexed SQL statements.
- Good, because the tail-window/predicate/window-saturation machinery is deleted rather than patched around.
- Good, because the hot path gets cheaper (queue enqueue vs file append) and multi-writer contention is structurally impossible.
- Good, because no new dependencies: stdlib `sqlite3`.
- Bad, because `grep`/`jq` over `agent.jsonl` is replaced by `sqlite3` CLI workflows — documented migration examples required.
- Bad, because backup procedure changes: copy `.sqlite` together with `-wal`, or checkpoint first.
- Neutral, because the store is a single failure domain for write and query together; mitigated by graceful degradation to prose-only operation (the react loop never depends on the store).
- Neutral, because `search_text` roughly doubles stored bytes; accepted at this scale (tens of MB per 30 days).
- Follow-up: FTS5 over `msg`/`extra` remains a future option if text-search profiling ever justifies it.