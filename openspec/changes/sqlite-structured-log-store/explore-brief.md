# Explore Brief — sqlite-structured-log-store

## Final Approach

Replace the structured `agent.jsonl` sink with a SQLite WAL database while keeping the human-readable prose `agent.log` sink unchanged. Rewrite `log_query` from bounded tail-scan to indexed SQL. Backfill existing JSONL archives once. No new dependencies (stdlib `sqlite3`).

## Rejected Alternatives

| Alternative | Why rejected |
|---|---|
| **SQLite shadow index** (JSONL stays primary, background tailer builds DB) | Dual storage, eventual-consistency lag, extra tailer process; keeps the very window machinery the change deletes. More moving parts than the replacement itself. |
| **Archive-scan patch** (log_query decompresses `.gz` when window saturates) | Slow (decompress every archive), keeps window/predicate machinery, only patches one failure mode; retention stays invisible to tooling except on demand. |
| **Keep JSONL, add FTS5 side index** | Full-text search is not the gap; the gap is indexed field lookups over full 30-day retention. FTS5 is optional polish at this scale (msg/extra LIKE is milliseconds). |

## Schema (complete mapping)

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,           -- ISO 8601, lexicographically sortable
    level TEXT NOT NULL,
    logger TEXT,
    agent TEXT,                -- 'main' | 'sa-<id>' | job tag
    trace TEXT,                -- r-<8 hex>
    prompt_id TEXT,
    event_type TEXT,           -- LogEvent value or NULL
    msg TEXT,
    extra TEXT                 -- JSON: tool, exit, dur_ms, err, ...
);
CREATE INDEX idx_ts         ON events(ts);
CREATE INDEX idx_trace      ON events(trace);
CREATE INDEX idx_prompt_id  ON events(prompt_id);
CREATE INDEX idx_agent      ON events(agent);
CREATE INDEX idx_event_type ON events(event_type) WHERE event_type IS NOT NULL;
```

**Field mapping from current JSONL record:** `ts`→ts, `level`→level, `logger`→logger, `agent`→agent, `trace`→trace, `prompt_id`→prompt_id, `event_type`→event_type, `msg`→msg, all remaining fields (tool, exit, dur_ms, err, model, tokens, …)→`extra` JSON.

**LogEvent taxonomy (closed set, carried over verbatim):** TOOL_START, TOOL_END, TOOL_FAILED, LLM_CALL, LLM_FAILED, STEP_BEGIN, STEP_END, RUN_BEGIN, RUN_END, ERROR.

**Levels (closed set):** DEBUG, INFO, WARNING, ERROR, CRITICAL.

## log_query Contract

- Filters preserved: trace (run-scoped default; auto-widen on text/prompt_id), level (min, WARNING+), event_type (exact, enum values), tool (exact; matched via extra JSON), since (ISO timestamp prefix), text/prompt_id (case-insensitive substring), prompt_id (exact), limit (most-recent-N).
- "Option C" default view preserved: no explicit filter → high-signal events (TOOL_START, TOOL_END, TOOL_FAILED, LLM_CALL, LLM_FAILED, ERROR) + WARNING+ records; INFO STEP_* bookkeeping dropped.
- Field projection preserved: per-field 500-char truncation, output size cap against max_output budget.
- Output contract change: `window_saturated` and `scanned_lines` removed; `total_matched` becomes exact (full retention); `truncated` retains meaning (limit/size-cap hit).
- New capability: all queries now cover the full 30-day retention, not just the active file.

## Cross-Module Data Flows

1. **Write path:** `react_loop.py` / `agent_controller.py` / scheduler / sub-agents / Telegram handlers → `log_event()` / plain `logger.*` (unchanged) → structlog processor chain → `QueueHandler` → `QueueListener` single writer thread → `sqlite3` INSERT (WAL, batched commit).
2. **Read path:** `builtin_tools/secrets_log.py` `LogQueryTools._exec_log_query()` → SQL SELECT with WHERE clauses built from `LogQueryFilters` → projection/truncation → JSON payload to agent.
3. **Context binding:** `bind_run_context()` (trace/agent/prompt_id) unchanged; merged via structlog contextvars at write time.
4. **Retention:** startup or periodic `DELETE FROM events WHERE ts < cutoff` (30 days) replaces gzip rotation + 30-backup prune.
5. **Backfill:** one-time CLI (pattern: `backfill_graph_memory.py`) reads existing `agent.jsonl` + `agent.jsonl.*.gz` → INSERT.

## Decision Points (recommended defaults adopted)

1. **Hard swap vs config toggle** → hard swap (toggle doubles test matrix, keeps dead window machinery as rollback path).
2. **Backfill old archives** → yes (makes prompt-history queries useful day one; ~S effort).
3. **Retention semantics** → keep 30 days, as time-based DELETE cutoff.
4. **DB location** → same logs dir, e.g. `~/.local/state/<agent_name>/logs/agent_logs.sqlite`; backup must include `-wal` file or checkpoint first.
5. **Gone:** gzip rotation machinery applies only to prose sink going forward.

## Known Open Questions

- Is FTS5 on `msg`/`extra` worth adding now? (Leaning no — record as future option.)
- Does anything besides `log_query` consume `agent.jsonl`? (Explorer found no other in-repo consumer; verify during apply.)
- Should the writer thread expose a flush-on-shutdown hook so a graceful stop guarantees queue drain? (Assume yes — daemon stop path must drain or explicitly accept loss.)
- `estimate_tokens`/context budget interplay: output size cap logic reuses existing `max_output` budget constants — confirm no hardcoded JSONL assumptions.