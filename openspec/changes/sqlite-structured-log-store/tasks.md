## 1. SQLite store module (`sqlite_log.py`)

- [ ] 1.1 Create `sqlite_log.py` with schema DDL (wide columns `ts`,`level`,`logger`,`agent`,`trace`,`prompt_id`,`event_type`,`msg`, `extra` JSON, `search_text` NOT NULL) and indexes (`idx_ts`, `idx_trace`, `idx_prompt_id`, `idx_agent`, partial `idx_event_type` WHERE event_type IS NOT NULL), per design D2
- [ ] 1.2 Implement the record-mapping layer: JSONL record dict → row (wide columns, remaining fields → `extra` compact JSON, `search_text` = casefolded compact-JSON of the full record) — shared by live ingestion and backfill
- [ ] 1.3 Implement the single-writer: bounded `queue.Queue` + writer thread owning the only connection, batched INSERTs in short transactions, WAL pragmas (`journal_mode=WAL`, `synchronous=NORMAL`), per design D3
- [ ] 1.4 Implement overflow policy: drop-oldest with a dropped counter and WARNING to the prose sink; never block emitters
- [ ] 1.5 Implement writer failure recovery: catch exceptions per batch, retry once, re-open connection; route startup connect/open failures (e.g. read-only logs dir) into the same path; on persistent failure disable the structured sink with WARNING and keep the queue draining (graceful degradation per design Risks)
- [ ] 1.6 Implement graceful-shutdown drain hook (`QueueListener.stop()` semantics) and wire a startup `PRAGMA quick_check` fallback path
- [ ] 1.7 Implement retention: 30-day cutoff `DELETE FROM events WHERE ts < cutoff` + `PRAGMA wal_checkpoint(TRUNCATE)`, triggered at startup and daily, per design D4
- [ ] 1.8 Add new public symbols to `vulture_whitelist.py`

## 2. Logging wiring (`agent_logging.py`, `main.py`)

- [ ] 2.1 Replace the structured JSONL handler in `setup_logging()` with the `sqlite_log` queue handler (`QueueHandler` feeding the writer); keep the processor chain, prose sink, and component isolation untouched
- [ ] 2.2 Delete the JSONL structured sink path (JSON renderer handler wiring for the structured stream); prose sink keeps daily gzip rotation
- [ ] 2.3 Wire the shutdown drain in `main.py` daemon stop path; verify store lives at `~/.local/state/<agent_name>/logs/agent_logs.sqlite` (XDG layout, ADR-0019)
- [ ] 2.4 Verify structlog chain output contract unchanged: closed `LogEvent` taxonomy, `bind_run_context` identity, secret redaction all flow into rows exactly as they flowed into JSONL lines (ADR-0004 backbone, ADR-0023 exactly-once)

## 3. `log_query` SQL rewrite (`builtin_tools/`)

- [ ] 3.1 Rewrite `_exec_log_query` record-fetching to SQL SELECT against the store; preserve trace-scope resolution order (explicit trace > auto-widen on text/prompt_id > current-run contextvars) ahead of SQL compilation
- [ ] 3.2 Implement the filter→SQL compilation per design D5 table: trace, min-level over lowercase stored names, event_type exact, `json_extract(extra,'$.tool')`, since prefix→range, text = `instr(search_text, ?)` with casefolded needle (literal substring, no wildcards), prompt_id exact, `ORDER BY ts DESC, id DESC LIMIT ?` re-ascended
- [ ] 3.3 Implement the Option C default view SQL exactly as specified (six-event authoritative set OR warning+); do not alter which records surface vs today
- [ ] 3.4 Update the response contract: exact `total_matched` via COUNT(*) with same WHERE; remove `window_saturated`/`scanned_lines`; keep per-field 500-char projection and `max_output` size cap
- [ ] 3.5 Delete the tail/predicate machinery from `logquery_helpers.py` (`_read_tail_lines`, `filter_log_lines`, predicate builder, tail constants); retain `LogQueryFilters` normalization
- [ ] 3.6 Update the `log_query` descriptor in `descriptors.py` — output-contract fields AND the prose description text (replace bounded-tail/active-JSONL wording with full-retention SQL querying); handle store-unavailable with a well-formed error/empty result

## 4. Backfill CLI (`backfill_log_store.py`)

- [ ] 4.1 Create `backfill_log_store.py` (pattern: `backfill_graph_memory.py`): read `agent.jsonl` + all `agent.jsonl.*.gz`, parse lines, map via the shared record-mapping layer (computes `search_text`), batch INSERT
- [ ] 4.2 Implement idempotency bookkeeping (source filename + line offset or content hash) so re-runs import nothing new; report malformed/blank-line skip counts without failing
- [ ] 4.3 Enforce additive-only: never modify, rename, or delete source files (user-confirmed decision)

## 5. Tests

- [ ] 5.1 New `tests/test_sqlite_log.py`: row mapping (wide columns, extra JSON, casefolded search_text), WAL writes survive simulated kill, queue drop-oldest + WARNING, writer failure → structured sink disabled + prose continues, startup open failure in read-only dir → prose-only degradation, concurrent emitters (react loop + sub-agent thread + scheduler) each stored exactly once, graceful drain
- [ ] 5.2 New retention tests: 31-day-old rows deleted, recent rows kept, checkpoint runs
- [ ] 5.3 New backfill tests: import from `.jsonl` and `.gz`, malformed-line skip counting, idempotent re-run, source files untouched, imported rows searchable via text filter identically to live rows
- [ ] 5.4 Rewrite `tests/test_log_query.py` against a temp-file store: all filters (trace default + auto-widen, min level, event_type, tool, since, text incl. extra-field and Unicode-fold matches, prompt_id), Option C default view parity, limit/truncation, exact total_matched, empty result, store-unavailable error
- [ ] 5.5 Regression check: component isolation (graph_memory records never reach the store), prose sink unaffected, run-identity scenarios
- [ ] 5.6 Run `make check` (ruff + vulture + full suite) and fix failures until green

## 6. Docs and validation

- [ ] 6.1 Update README logging section: SQLite store location, `sqlite3` CLI query examples replacing grep/jq examples, backup procedure (copy `.sqlite` + `-wal` or checkpoint first), backfill usage
- [ ] 6.2 Update AGENTS.md logging notes (agent_logging.py row, log_query description, retention mechanism)
- [ ] 6.3 Run `openspec validate sqlite-structured-log-store --type change --strict` and fix any reported issues before archive