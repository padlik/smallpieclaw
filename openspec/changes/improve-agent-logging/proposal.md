## Why

Agent logs are written as freeform prose to `agent.log` inside the source checkout directory, with a hand-rolled numbered-shift rotation. This location is wrong (logs pollute the code checkout), the rotation is primitive (no compression, renames on every rollover), and — most importantly — the prose format makes the agent unable to analyze its own executions without re-parsing text through an LLM. There is no fact-level, per-step operational record the agent can query mid-run to self-correct. Rather than hand-roll structured logging with custom stdlib filters and formatters, we adopt `structlog`, a mature library that provides the processor pipeline, contextvars binding, and JSON rendering this change needs.

## What Changes

- **Adopt the `structlog` library** as the logging backbone, integrated with stdlib `logging` via `structlog.stdlib.ProcessorFormatter` so existing `logging.getLogger(__name__)` call sites keep working while gaining structured output.
- **Relocate logs to an XDG state directory** at `~/.local/state/<agent_name>/logs/`, independent of `agent_home` (mirrors the existing vault path rule). **BREAKING**: existing deployments' log location moves; operators tailing the old path must update it.
- **Add a structured JSONL event sink** (`agent.jsonl`) as the primary machine-readable log surface, rendered by a `structlog.processors.JSONRenderer` on a rotating stdlib handler, written alongside the prose `agent.log` (retained for `tail -f`/`grep`, now secondary via a console/plain renderer on a second handler). Both sinks share one processor chain, so their content cannot drift.
- **Carry run identity (`trace`, `agent`, `label`) via `structlog.contextvars`**, merged into every event by the `merge_contextvars` processor and bound at run/thread entry points — replacing the prose `log_prefix` string threading. This generalizes the ambient-trace pattern already used in `llm_client.py` onto a supported library primitive.
- **Introduce a small closed `LogEvent` taxonomy** emitted as a structured `event_type` key, giving the agent a discoverable, stable vocabulary to query against instead of prose substrings.
- **Emit rich structured key-values** (`event_type`, `tool`, `dur_ms`, `exit`, `err`) at a small hot set of call sites (tool dispatch, LLM calls, ReAct step/run boundaries, error paths). Other call sites flow through unchanged as `foreign_pre_chain` stdlib records and gain identity for free.
- **Add a `log_query` built-in tool** the agent invokes mid-run — an in-process filter over the active `agent.jsonl` only, scoped by the current `trace_id`, returning structured operational facts for self-correction.
- **Add a `structlog` secret-redaction processor** that scrubs known vault values from the event dict before either renderer serializes it.
- **Replace the numbered-shift rotation** with date-suffixed, gzip-compressed daily backups (30-day retention preserved), applied by the stdlib rotating handlers `structlog` renders into.

Out of scope (explicitly deferred): a SQLite/queryable store, retrofitting every call site to structured `structlog` loggers, mid-run querying of rotated `.gz` history (cross-run learning remains the memory subsystems' job).

## Capabilities

### New Capabilities
- `structured-event-logging`: A JSONL event sink (rendered by `structlog`) with contextvars-carried identity (`trace`/`agent`/`label`), a closed `LogEvent` taxonomy emitted as structured keys, rich key-values at hot call sites, and secret redaction — written alongside the retained prose sink from the same processor chain.
- `runtime-log-introspection`: A `log_query` built-in tool that lets the agent read and filter its own active structured log mid-run, scoped by the current run's trace ID, to support self-correction.

### Modified Capabilities
- `agent-scoped-directories`: Log files relocate to an XDG state directory (`~/.local/state/<agent_name>/logs/`), resolved independently of `agent_home` — extending the existing rule that agent state paths derive from `agent_name`.

## Impact

- **Code**: `main.py` (replace `_setup_logging`/`_NightlyRotatingFileHandler` with `structlog` configuration wiring stdlib rotating handlers + `ProcessorFormatter`, XDG path resolution), a new logging config module (processor chain, redaction processor, `LogEvent` enum), `react_loop.py` + `agent_controller.py` (`bind_contextvars` at run entry, step/run boundary events, retire explicit `log_prefix` threading at touched sites), `llm_client.py` (LLM lifecycle events; reconcile its existing contextvars trace with `structlog.contextvars`), `tool_executor.py` + `builtin_executor.py` (tool lifecycle events + register `log_query`), `config_schema.py` (log directory/path fields).
- **Config**: `[paths] log_file` semantics change from a bare filename in the checkout to an XDG-resolved directory; `config.toml.example` and README updated.
- **Behaviour**: The scheduled self-health task (`scheduler.toml.example`) switches from reading 500 prose lines to querying structured events.
- **Dependencies**: **Adds `structlog`** to `requirements.txt` (new runtime dependency). No other new dependencies — `gzip`, `contextvars`, and stdlib logging handlers only.
- **Tests**: New coverage for the processor chain (identity merge, redaction, JSON render shape), `LogEvent` emission, `log_query` filtering/scoping, and XDG path resolution; existing `agent.log` path assertions updated. `vulture_whitelist.py` updated for any new public symbols.
- **Docs**: README logging section, `AGENTS.md` log-format note, `ARCHITECTURE.md` logging line.
