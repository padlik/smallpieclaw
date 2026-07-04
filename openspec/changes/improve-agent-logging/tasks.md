## 1. XDG log path + rotation (stands alone)

- [ ] 1.1 Add XDG state-dir resolution in `config_schema.py`: derive log dir `~/.local/state/<agent_name>/logs/` from `agent_name`, independent of `agent_home`; an explicit absolute `[paths] log_file` overrides.
- [ ] 1.2 Update `main.py` to resolve the log path via the new XDG logic instead of `_AGENT_DIR/agent.log`.
- [ ] 1.3 Replace `_NightlyRotatingFileHandler`'s numbered-shift rollover with date-suffixed, gzip-compressed backups (retain 30 days).
- [ ] 1.4 Update `vulture_whitelist.py` for any removed/renamed handler symbols.
- [ ] 1.5 Add/adjust tests for XDG path resolution, `agent_home` independence, absolute-`log_file` override, and "no log in checkout"; update existing tests asserting `agent.log`.

## 2. Structured identity + dual sink

- [ ] 2.1 Add a logging module with a `TraceIdentityFilter` that reads context-local `trace`/`agent`/`label` and sets them as `LogRecord` attributes.
- [ ] 2.2 Set the identity context vars at run entry points where `trace_id` is already forwarded (`agent_controller`, `react_loop`, sub-agent runner, scheduler job start); ensure they propagate to background/executor threads.
- [ ] 2.3 Update the prose formatter to render `[label trace]` from record attributes (preserving current human output) and retire explicit `log_prefix`/`pfx` threading at the touched call sites in `react_loop.py`, `context_manager.py`, `memory_store.py`.
- [ ] 2.4 Add a `JsonFormatter` and a JSONL handler writing `agent.jsonl` (with the same rotation/gzip policy) alongside the prose sink; emit `ts`, `level`, `logger`, `msg`, plus identity fields.
- [ ] 2.5 Tests: identity appears as JSON fields and as the prose prefix; identity present without a manual prefix; graceful degradation with no run context; both sinks stay in sync.

## 3. Event taxonomy + secret redaction

- [ ] 3.1 Define a closed `LogEvent` enum (`TOOL_START`, `TOOL_END`, `TOOL_FAILED`, `LLM_CALL`, `LLM_FAILED`, `STEP_BEGIN`, `STEP_END`, `RUN_BEGIN`, `RUN_END`, `ERROR`) in one module; expose a way to enumerate members.
- [ ] 3.2 Add a `SecretRedactionFilter` that scrubs known vault values (sourced from the ADR-0003 vault) from `record.msg` and structured `extra` before serialization; wire it ahead of both handlers.
- [ ] 3.3 Tests: event values are members of the enum and enumerable; a vault secret value in `err` is scrubbed from both `agent.jsonl` and `agent.log`.

## 4. Emit structured events at hot call sites

- [ ] 4.1 Emit `TOOL_START`/`TOOL_END`/`TOOL_FAILED` with `tool`, `dur_ms`, `exit` in `tool_executor.py` and `builtin_executor.py`.
- [ ] 4.2 Emit `LLM_CALL`/`LLM_FAILED` in `llm_client.py`.
- [ ] 4.3 Emit `STEP_BEGIN`/`STEP_END` and `RUN_BEGIN`/`RUN_END` in `react_loop.py`; emit `ERROR` on exception paths.
- [ ] 4.4 Tests: a non-zero tool exit produces a `TOOL_FAILED` record with `tool`/`exit`/`dur_ms`; non-hot call sites still log with identity and no `event`.

## 5. Runtime log introspection (`log_query`)

- [ ] 5.1 Implement `log_query` in `builtin_executor.py`: in-process filter over the active `agent.jsonl` only; params `trace` (default = current run), `level` (default `WARNING`), `event`, `tool`, `since`, `last`/cap.
- [ ] 5.2 Register `log_query` as a built-in tool and add its schema/description for tool discovery.
- [ ] 5.3 Enforce a result cap; when exceeded, truncate and signal truncation in the response; return a well-formed empty result when nothing matches.
- [ ] 5.4 Tests: trace-scoped default; level+event filtering; result cap + truncation signal; rotated `.gz` records excluded; empty result well-formed.

## 6. Docs, health task, and validation

- [ ] 6.1 Update `scheduler.toml.example` self-health task to use `log_query` for structured error summaries instead of reading 500 prose lines.
- [ ] 6.2 Update README logging section, `AGENTS.md` log-format note, `ARCHITECTURE.md` logging line, and `config.toml.example` `log_file` semantics for the XDG move.
- [ ] 6.3 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` and fix findings.
- [ ] 6.4 Run `make check` (lint + full test suite) and ensure green.
- [ ] 6.5 Run `openspec validate improve-agent-logging --type change --strict` before archive.
