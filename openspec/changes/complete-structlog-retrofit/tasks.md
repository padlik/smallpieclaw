## 1. Pre-flight verification

- [x] 1.1 Verify the react_loop tool-event lifecycle helper (design D5) — locate `_emit_tool_lifecycle` by content (~1795) and confirm it emits only `TOOL_FAILED` with the double-count comment; re-confirm all design D1 line references by content, not line number (react_loop ~1342/1344/1407/1418/1531/1544/1598/1724, llm_client ~391/392/536/537)
- [x] 1.2 Verify no code or test depends on `event_type = "ERROR"` records (grep for `LogEvent.ERROR` and `event_type.*ERROR` consumers; expected: only the builtin_executor.py:491 emission site) — this is the proposal's disclosed pre-removal verification

## 2. Deduplicate hot-set lifecycle logging (design D1)

- [x] 2.1 react_loop.py Category A: remove the two tool-result prose lines (`logger.info("Tool '%s' result: success=True")` and the `logger.warning("Tool '%s' result: success=False …")` twin) — TOOL_END/TOOL_FAILED from builtin_executor already carry tool/exit/dur_ms/err
- [x] 2.2 react_loop.py Category B — enrich `STEP_BEGIN` log_event with `model` field, remove the `logger.info("step %d/%d | model: %s…")` twin
- [x] 2.3 react_loop.py Category B — enrich `RUN_BEGIN` log_event with `model` and `goal` (goal truncated to 80 chars, mirroring the removed prose line), remove the `logger.info("start | model…")` twin
- [x] 2.4 react_loop.py Category B — enrich `RUN_END` log_event with `model` and `steps`, remove the `logger.info("finish | model…")` twin
- [x] 2.5 llm_client.py Category A: remove the two `logger.error("LLM chat error…")` / `logger.error("LLM chat (tools) error…")` twins — `LLM_FAILED` events already carry model/dur_ms/err
- [x] 2.6 Confirm prose parity: for each removed line, verify the enriched structured event's `_ProseRenderer` output carries equivalent information (identity prefix + fields) in `agent.log`

## 3. Reconcile ERROR/TOOL_FAILED split (design D2)

- [x] 3.1 builtin_executor.py `_emit_tool_lifecycle_error`: remove the `LogEvent.ERROR` emission, keep the single `TOOL_FAILED` (ERROR level, tool/dur_ms/exit/err fields) — covers both `execute()` and `confirm()` paths
- [x] 3.2 Leave `LogEvent.ERROR` enum member in place (reserved, zero-emitter); add a one-line comment on the member noting it is reserved per ADR-0023

## 4. Component log isolation for graph memory (design D3)

- [x] 4.1 xdg.py: add `graph_memory_log` property to `XDGPaths` returning `<logs_dir>/graph_memory.log` (mirrors `graph_memory_db` naming pattern)
- [x] 4.2 agent_logging.py `setup_logging()`: configure the `graph_memory` logger unconditionally (enablement-independent) — `propagate=False`, logger level INFO, file handler `_GzipTimedRotatingFileHandler(graph_memory_log, backup_count=backup_count)` with the existing prose formatter, stream handler with the existing prose formatter at handler-level WARNING
- [x] 4.3 Wire the path: `main.py` passes `XDGPaths.graph_memory_log` (or confirm `setup_logging()` derives it from `log_file` when omitted) — no behavior change when graph memory is disabled
- [x] 4.4 Verify backfill CLI unaffected: `backfill_graph_memory.py` still uses its own `basicConfig`; `graph_memory` logger propagates to root when `setup_logging()` was never called

## 5. Documentation contract (design D4)

- [x] 5.1 agent_logging.py: rewrite `LogEvent` docstring — closed ten-member set; core lifecycle emitted by react loop and direct collaborators; component diagnostics in component logs; plain `logger.` records flow through the shared chain without `event_type`; ERROR reserved
- [x] 5.2 AGENTS.md: update the Logging bullet to describe the two-tier contract and `graph_memory.log` (XDG path, rotation, stdout WARNING+)
- [x] 5.3 README/config docs: if log-file inventory is documented anywhere user-visible, add `graph_memory.log`

## 6. Tests

- [x] 6.1 tests/test_react_loop.py: update tests asserting the removed prose lines; add single-record assertions — one `TOOL_END` per successful tool completion (no paired `logger.info` record), one `TOOL_FAILED` per failure, and one `LLM_FAILED` per LLM failure (no paired `logger.error` record)
- [x] 6.2 tests/test_react_loop.py: assert `STEP_BEGIN` record carries `model`; `RUN_BEGIN` carries `model`+`goal`; `RUN_END` carries `model`+`steps`+`dur_ms`
- [x] 6.3 tests/test_agent_logging.py: graph-memory routing tests — `graph_memory` logger record reaches `graph_memory.log` and NOT `agent.jsonl`/`agent.log`; INFO file-only vs WARNING+ on stdout; rotation config matches primary sinks; routing present even when `[graph_memory] enabled` is false; component records carry no `trace`/`agent` identity fields
- [x] 6.4 tests/test_agent_logging.py: add test that `builtin_executor` unexpected-exception path emits exactly one TOOL_FAILED and zero ERROR-type records (extend the existing lifecycle test class)
- [x] 6.5 Update `vulture_whitelist.py` if the new `XDGPaths.graph_memory_log` property or any new public symbol is flagged

## 7. Validation

- [x] 7.1 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py` — both clean
- [x] 7.2 Run full `make check` (lint + test) — green; investigate any failure to root cause before proceeding
- [x] 7.3 Run `openspec validate complete-structlog-retrofit --type change --strict` — passes before archive