## Why

`log_query` can only read the active `agent.jsonl` tail (1MB / 5000 lines) — the 30 days of gzip-rotated history are invisible to the agent. Any prompt analysis older than the current file (yesterday's runs, scheduled-job traces, cross-run failure patterns) is impossible through tooling. Meanwhile the bounded-tail/predicate machinery exists only to compensate for a storage format with no indexes. Replacing the structured JSONL sink with a SQLite WAL store makes the entire 30-day retention queryable via indexed lookups while deleting the window-scan machinery; the human-readable prose `agent.log` sink is kept unchanged.

## What Changes

- **BREAKING**: The structured log sink changes from `agent.jsonl` (JSONL, daily gzip rotation, 30 backups) to `agent_logs.sqlite` (SQLite WAL, indexed, 30-day time-based DELETE retention). `agent.jsonl` is no longer written.
- The human prose sink `agent.log` keeps its current daily gzip rotation and is unchanged.
- `log_query` is rewritten from bounded tail-scan to indexed SQL `SELECT`; all existing filters (trace, level, event_type, tool, since, text, prompt_id, limit) and the high-signal default view ("Option C") are preserved.
- **BREAKING**: `log_query` response contract changes — `window_saturated` and `scanned_lines` fields are removed; `total_matched` becomes exact across the full retention window.
- New one-time backfill CLI imports existing `agent.jsonl` + `agent.jsonl.*.gz` into the SQLite store. Backfill is additive only — old archive files remain on disk untouched as a forensic fallback.
- New SQLite-backed querying covers the full 30-day retention: prompt-scoped and trace-scoped queries work across day boundaries; aggregation queries (counts by tool/event/day) become single SQL statements.
- Structured write path moves from direct file append to a single-writer queue (`QueueHandler` → `QueueListener` → batched INSERT) with WAL mode; run identity binding (`bind_run_context`), the closed `LogEvent` taxonomy, and secret redaction are unchanged.
- The SQLite store lives in the same XDG logs directory as the current sinks: `~/.local/state/<agent_name>/logs/agent_logs.sqlite`.
- No new dependencies — stdlib `sqlite3`.

## Capabilities

### New Capabilities
- `sqlite-log-store`: SQLite WAL structured log store — schema, indexes, single-writer queue ingestion, time-based retention, crash/backup semantics. Storage mechanics (schema, indexes, queue ingestion, retention DELETE, crash/backup semantics) live exclusively in this capability.
- `log-store-backfill`: One-time CLI migration that imports existing JSONL archives into the SQLite store, idempotent and non-destructive to source files. This is a distinct new CLI (`backfill_log_store.py`), unrelated to the existing `backfill_graph_memory.py` referenced by the component-isolation requirement.

### Modified Capabilities
- `structured-event-logging`: The machine-readable primary sink becomes the SQLite store instead of `agent.jsonl`; daily gzip rotation of the structured sink is replaced by 30-day DELETE-based retention. This capability only asserts that the structured primary sink *is* now the SQLite store and that gzip rotation no longer applies to the structured stream — it does not re-specify storage mechanics. The prose sink, closed event taxonomy, run identity, secret redaction, and component log isolation are behaviorally unchanged, but their existing scenarios reference `agent.jsonl` and "primary sinks" rotation parity and must be re-pointed to the store (and to the prose sink for rotation references).
- `runtime-log-introspection`: `log_query` reads the full 30-day store via indexed SQL instead of the active-file tail window; trace/prompt queries now work across day boundaries; the window-saturation contract is replaced by an exact `total_matched`; result caps and default high-signal view are preserved.

## Impact

- **Code**: `agent_logging.py` (sink wiring swap, queue writer), new `sqlite_log.py` module (schema/handler/retention), `builtin_tools/secrets_log.py` + `builtin_tools/logquery_helpers.py` (SQL rewrite; tail/predicate machinery deleted), new `backfill_log_store.py` CLI, `builtin_tools/descriptors.py` (tool descriptor output-contract fields), `main.py` (writer flush-on-shutdown, retention trigger).
- **Tests**: `tests/test_log_query.py` (~518 lines) rewritten around an in-memory/temp-file SQLite store; new tests for writer, retention, backfill.
- **Compatibility**: In-repo consumer of `agent.jsonl` is `log_query` only; external `grep`/`jq` consumers are replaced by the `sqlite3` CLI / DB Browser. Backup procedure changes: copy `.sqlite` plus `-wal` file (or checkpoint first).
- **Dependencies**: none added (`sqlite3` is stdlib).
- **Docs**: README logging section, AGENTS.md logging notes, `vulture_whitelist.py` for new public symbols.