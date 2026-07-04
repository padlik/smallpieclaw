## Why

Agent logs are written as freeform prose to `agent.log` inside the source checkout directory, with a hand-rolled numbered-shift rotation. This location is wrong (logs pollute the code checkout), the rotation is primitive (no compression, renames on every rollover), and — most importantly — the prose format makes the agent unable to analyze its own executions without re-parsing text through an LLM. There is no fact-level, per-step operational record the agent can query mid-run to self-correct.

## What Changes

- **Relocate logs to an XDG state directory** at `~/.local/state/<agent_name>/logs/`, independent of `agent_home` (mirrors the existing vault path rule). **BREAKING**: existing deployments' log location moves; operators tailing the old path must update it.
- **Add a structured JSONL event sink** (`agent.jsonl`) as the primary machine-readable log surface, written alongside the prose `agent.log` (retained for `tail -f`/`grep`, now secondary).
- **Lift run identity (`trace`, `agent`, `label`) from the prose message string into `LogRecord` attributes** via a `contextvars`-backed logging filter, generalizing the pattern already used in `llm_client.py`. Both the prose and JSON formatters render from these structured attributes, so there is no drift and no prose-parsing.
- **Introduce a small closed `LogEvent` taxonomy** (tool lifecycle, LLM lifecycle, step/run boundaries, errors) so the agent has a discoverable, stable vocabulary to query against instead of guessing prose substrings.
- **Emit rich structured fields** (`event`, `tool`, `dur_ms`, `exit`, `err`) via `extra={}` at a small hot set of call sites (tool dispatch, LLM calls, ReAct step/run boundaries, error paths). Other call sites keep freeform messages but gain structured identity for free.
- **Add a `log_query` built-in tool** the agent invokes mid-run — an in-process filter over the active `agent.jsonl` only, scoped by the current `trace_id`, returning structured operational facts for self-correction.
- **Replace the numbered-shift rotation** with date-suffixed, gzip-compressed daily backups (30-day retention preserved).
- **Redact known secret values** from structured fields before they are written to `agent.jsonl`.

Out of scope (explicitly deferred): a SQLite/queryable store, retrofitting every call site to structured events, mid-run querying of rotated `.gz` history (cross-run learning remains the memory subsystems' job).

## Capabilities

### New Capabilities
- `structured-event-logging`: A JSONL event sink with `LogRecord`-attribute identity (`trace`/`agent`/`label`), a closed `LogEvent` taxonomy, structured `extra={}` fields at hot call sites, and secret redaction — written alongside the retained prose sink.
- `runtime-log-introspection`: A `log_query` built-in tool that lets the agent read and filter its own active structured log mid-run, scoped by the current run's trace ID, to support self-correction.

### Modified Capabilities
- `agent-scoped-directories`: Log files relocate to an XDG state directory (`~/.local/state/<agent_name>/logs/`), resolved independently of `agent_home` — extending the existing rule that agent state paths derive from `agent_name`.

## Impact

- **Code**: `main.py` (log setup, rotation handler, XDG path resolution), `trace_context.py` / a new logging filter module (identity lift), `react_loop.py` + `agent_controller.py` (step/run boundary events, retire explicit `log_prefix` threading at touched sites), `llm_client.py` (LLM lifecycle events), `tool_executor.py` + `builtin_executor.py` (tool lifecycle events + register `log_query`), `config_schema.py` (log directory/path fields).
- **Config**: `[paths] log_file` semantics change from a bare filename in the checkout to an XDG-resolved directory; `config.toml.example` and README updated.
- **Behaviour**: The scheduled self-health task (`scheduler.toml.example`) switches from reading 500 prose lines to querying structured events.
- **Dependencies**: None new — `gzip`, `contextvars`, and stdlib logging handlers only.
- **Tests**: New coverage for the identity filter, JSONL formatter, `LogEvent` emission, `log_query` filtering/scoping, redaction, and XDG path resolution; existing `agent.log` path assertions in tests updated.
- **Docs**: README logging section, `AGENTS.md` log-format note, `ARCHITECTURE.md` logging line.
