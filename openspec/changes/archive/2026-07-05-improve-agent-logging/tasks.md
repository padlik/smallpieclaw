## 1. Dependency and logging config module

- [x] 1.1 Add `structlog` (version-pinned minimum) to `requirements.txt`; run `make install-dev`.
- [x] 1.2 Create a logging config module defining the shared processor chain (`merge_contextvars`, `add_log_level`, `TimeStamper(fmt="iso")`, `redact_secrets`, `ProcessorFormatter.wrap_for_formatter`) and a `foreign_pre_chain` for stdlib records.
- [x] 1.3 Define the closed `LogEvent` enum (`TOOL_START/END/FAILED`, `LLM_CALL/FAILED`, `STEP_BEGIN/END`, `RUN_BEGIN/END`, `ERROR`) in the logging config module; add new public symbols to `vulture_whitelist.py`.
- [x] 1.4 Implement the `redact_secrets` processor sourcing known values from the agent-scoped vault (ADR-0003); scrub message and all key-values.

## 2. XDG path, rotation, and dual sink

- [x] 2.1 Add log directory/path fields to `config_schema.py`; resolve logs to `~/.local/state/<agent_name>/logs/` independent of `agent_home`, with an explicit absolute `log_file` overriding.
- [x] 2.2 Implement a gzip, date-suffixed daily rotating stdlib handler (retention 30) to replace `_NightlyRotatingFileHandler`.
- [x] 2.3 In `main.py`, replace `_setup_logging` with `structlog.configure` wiring two stdlib handlers: JSON sink (`JSONRenderer` → `agent.jsonl`) primary, prose sink (plain renderer reproducing `[label trace] message` → `agent.log` + stdout) secondary; keep `httpx`/`telegram` at WARNING.
- [x] 2.4 Verify no duplicate/mis-rendered records: foreign stdlib records render once via `foreign_pre_chain`, native structlog events render once.

## 3. Contextvars identity

- [x] 3.1 Bind `trace`/`agent`/`label` via `structlog.contextvars.bind_contextvars` at ReAct run entry (`react_loop`), sub-agent runner start, and scheduler job start; clear/reset on exit.
- [x] 3.2 Reconcile `llm_client.py`'s existing trace `ContextVar` to bind through `structlog.contextvars` so identity has one source.
- [x] 3.3 Retire explicit `log_prefix` string threading at touched sites (`react_loop`, `context_manager`, `memory_store`), relying on `merge_contextvars`.
- [x] 3.4 Verify identity survives into sub-agent/background threads (bound at each thread entry).

## 4. Structured events at the hot set

- [x] 4.1 Emit `TOOL_START`/`TOOL_END`/`TOOL_FAILED` with `tool`, `exit`, `dur_ms` in `tool_executor.py` / `builtin_executor.py`.
- [x] 4.2 Emit `LLM_CALL`/`LLM_FAILED` in `llm_client.py`.
- [x] 4.3 Emit `STEP_BEGIN`/`STEP_END` and `RUN_BEGIN`/`RUN_END` in `react_loop.py`.
- [x] 4.4 Emit `ERROR` on exception paths carrying a structured `err` field.

## 5. Runtime log introspection

- [x] 5.1 Implement and register the `log_query` built-in tool in `builtin_executor.py`: in-process filter over the active `agent.jsonl` only, trace-scoped by default, filters for `level`/`event_type`/`tool`/`since`, mandatory result cap with truncation indicator.
- [x] 5.2 Update `scheduler.toml.example` self-health task to query structured events via `log_query` instead of reading 500 prose lines.

## 6. Tests

- [x] 6.1 Test the processor chain: contextvars identity merge, redaction scrubs vault values, JSON render shape includes `ts`/`level`/`logger`/`msg`/`trace`/`agent`.
- [x] 6.2 Test `LogEvent` emission produces structured `event_type`/`tool`/`exit`/`dur_ms` fields.
- [x] 6.3 Test `log_query` filtering, current-run default scoping, the default filter (anomalies + `TOOL_START/END` + `LLM_CALL`, excluding `STEP_BEGIN/END`), result cap/truncation, active-file-only, and well-formed empty result.
- [x] 6.4 Test XDG path resolution (defaults, custom `agent_name`, `agent_home` independence, explicit override, no checkout log).
- [x] 6.5 Update existing tests asserting `agent.log` in the checkout to the new path/config.

## 7. Docs and validation

- [x] 7.1 Update README logging section, `AGENTS.md` log-format note, `ARCHITECTURE.md` logging line, and `config.toml.example` `[paths]` guidance.
- [x] 7.2 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`, then `make check`.
- [x] 7.3 Run `openspec validate improve-agent-logging --type change --strict` before archive.
