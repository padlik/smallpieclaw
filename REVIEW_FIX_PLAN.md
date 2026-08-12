# Code Review Fix Plan — smallpieclaw

Staged implementation plan derived from the code review findings.
Each stage is self-contained and can be implemented, tested, and committed independently.

**Branch:** `feature/review-implementation-plan`
**Baseline:** `make check` → 1627 passed, 1 skipped (platform-specific).

---

## Verification Legend

- ✅ **FIXED** — verified in codebase, no action needed
- 🔲 **TODO** — not yet implemented, scheduled in a stage below
- ⏸️ **DEFERRED** — gated on an upstream decision, not actionable now

---

## Already Fixed (verified in codebase — no further action)

| ID | Status | Evidence |
|---|---|---|
| OS-02 | ✅ | `config_schema.py:690` allows `"sse"`; `:696` requires url for sse; `:480` comment updated |
| OS-03 | ✅ | `config_schema.py:463` `OAuthConfig.timeout: int = 300` |
| OS-04 | ✅ | `builtin_executor.py:84` `_grant_tracker_var` ContextVar; `:336` `use_grant_tracker()` context manager; per-sub-agent isolation via contextvars |
| OS-06 | ✅ | `agent_runtime.py:313` imports from `builtin_tools.context_io` |
| OS-07 | ✅ | `openspec/changes/archive/2026-07-followups.md:76-77` both marked **RESOLVED** |
| TD-01 | ✅ | `agent_controller.py:118` `self.strategy_memory` declared |
| TD-03 | ✅ | `execution_plan.py:332` `self._active_lock = threading.Lock()` |
| TD-05 | ✅ | `react_loop.py:730,736` non-JSON args wrapped as `{"_raw": ...}` |
| IDIOM-06 | ✅ | `memory_store.py` — no `utcnow` references remain |
| IDIOM-07 | ✅ | `builtin_tools/shell.py:570` parenthesized correctly |
| INH-01 | ✅ | `outcome_utils.fail_outcome()` helper; used across `execution_plan.py` (10+ sites) |
| LM-01 | ✅ | `execution_plan.py:579 _run_batch`, `:617 _drain_pending`, `:655 _classify_incomplete` extracted |
| TD-02 | ✅ | `execution_plan.py:655` `_classify_incomplete` used at `:930,:940` |
| OS-01 | ✅ (archived) | `add-prompt-search` change exists but 0/27 tasks — treat as deferred, not active work |

---

## Stage 1 — Idiomatic Python Cleanup (Low Risk, High Breadth)

All small, isolated, no behavioral change. Safe to batch in one PR.

### 1A. IDIOM-01 — Remove dead `pfx = ""` noise
- **Files:** `react_loop.py` (9 occurrences + format args)
- **Action:** Delete `pfx = ""` assignments and the leading `%s`/`pfx` from `logger.info` calls. Run identity now comes from structlog contextvars.
- **Verify:** `ruff check .` + `make test`

### 1B. IDIOM-02 — Remove defensive `getattr` on ReactContext dataclass fields
- **Files:** `react_loop.py` (9 occurrences), `agent_runtime.py:243-252`
- **Action:** Replace `getattr(ctx, "field", default)` with direct `ctx.field` for declared dataclass fields. Reserve `getattr` for genuinely optional/duck-typed objects.
- **Verify:** `make test` — if any test relied on the default, it indicates a real wiring gap to fix.

### 1C. IDIOM-03 — Extract `_coerce_args` helper
- **Files:** `react_loop.py:845,1264`
- **Action:** Add `def _coerce_args(args): return {str(i): v for i, v in enumerate(args)} if isinstance(args, list) else args`; replace both inline copies.
- **Verify:** `make test`

### 1D. IDIOM-04 — Move local stdlib imports to module top
- **Files:** `agent_controller.py:499` (`import uuid`), `:588` (`import time`), `memory_store.py:130` (`import re`)
- **Action:** Move to module-top imports. Keep genuinely cycle-breaking local imports (`react_loop.py:1119`, `agent_runtime.py:313-315`) as-is.
- **Verify:** `ruff check .` + `make test`

### 1E. IDIOM-05 — Log `on_step` callback failures instead of silent swallow
- **Files:** `react_loop.py:963-965`
- **Action:** Change `except Exception: pass` → `except Exception: logger.debug("on_step callback failed", exc_info=True)`. Keep resilient, stop hiding.
- **Verify:** `make test`

### 1F. IDIOM-08 — Collapse `_is_authorized` duplicate branches
- **Files:** `telegram_interface.py:891-896`
- **Action:** Replace two identical `if/elif` branches with: `return user_id in self.allowed_ids if self.security_mode in ("allowlist", "pairing") else False`
- **Verify:** `make test`

### 1G. IDIOM-09 — Replace deprecated `asyncio.get_event_loop()`
- **Files:** `telegram_interface.py:275`
- **Action:** Use `asyncio.new_event_loop()` + `set_event_loop()`, or capture the loop in `post_init` via `asyncio.get_running_loop()`.
- **Verify:** `make test` + manual Telegram bot smoke check if feasible

**Stage 1 Exit Criteria:** `make check` passes (lint + 1627 tests). No behavioral change.

---

## Stage 2 — Tech Debt Quick Wins (Low Risk, Small Effort)

### 2A. TD-04 — Name the "unlimited steps" magic ceiling
- **Files:** `react_loop.py:1053`
- **Action:** Define `_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000` module constant with a comment explaining it's a safety ceiling, not a real limit. Update the operator message to be honest ("effectively unlimited (capped at 10M for safety)").
- **Verify:** `make test`

### 2B. TD-06 — Cache LLM capability probe
- **Files:** `react_loop.py:757-758`
- **Action:** Compute `hasattr(ctx.llm, "chat_with_tools_fallback") and callable(...)` once at loop start (or via a cached flag on `ReactContext`), not on every turn.
- **Verify:** `make test`

### 2C. TD-09 — Stop reaching into `FileTokenStorage._read_file`
- **Files:** `mcp_client.py:599,629`
- **Action:** Add a public `FileTokenStorage.read_status() -> dict | None` (sync, no event loop) that returns parsed token/expiry info. Replace `get_token_info`'s private access + duplicated parsing with a call to it.
- **Verify:** `make test` + `pytest tests/test_mcp_client.py -v`

### 2D. TD-12 — Fix schemas.py count + add sync test
- **Files:** `builtin_tools/schemas.py:22`, new test
- **Action:** Fix comment from "17 tools" to actual count. Add a test asserting `set(BUILTIN_TOOL_SCHEMAS) == set(BUILTIN_TOOLS)` so future drift fails CI.
- **Verify:** `make test` (new test passes)

### 2E. TD-13 — Extract `_reply_jobs_list` helper
- **Files:** `telegram_commands.py:498-501,514-517,530-533,536-538`
- **Action:** Extract the repeated 3-line "list jobs + reply" tail into `_reply_jobs_list(iface, message)`; call after each mutating branch in `cmd_jobs`.
- **Verify:** `make test`

**Stage 2 Exit Criteria:** `make check` passes. No behavioral change except TD-04 message wording.

---

## Stage 3 — Inheritance / Duplication Reduction (Medium Risk)

### 3A. INH-03 — Extract `_run_with_fallback` for chat methods
- **Files:** `llm_client.py:439-511,560-622`
- **Action:** Extract `_run_with_fallback(messages, call_fn, progress_cb, *, needs_tools)` taking the per-attempt callable. `chat_with_fallback` and `chat_with_tools_fallback` become 3-line wrappers.
- **Verify:** `make test` + `pytest tests/test_llm_client.py -v` (if exists)

### 3B. INH-04 — Add `_require_cb_auth` decorator for Telegram callbacks
- **Files:** `telegram_callbacks.py` (8 handlers), `telegram_commands.py:28` (reference `_require_auth`)
- **Action:** Add `_require_cb_auth` decorator mirroring `_require_auth`; wrap each `cb_*` handler. Removes ~55 lines of boilerplate.
- **Verify:** `make test` + manual callback test if feasible

### 3C. INH-06 — Delete vestigial AgentController pass-through shims
- **Files:** `agent_controller.py:407-446` (`_format_tools`, `_fmt_tool_call`, `_fmt_tool_result_progress`, `_format_tool_result`, `_extract_json_candidates`, `_parse_json`, `_format_skills`, `_format_models`, `_format_log_section`)
- **Action:** Delete the 9 shim methods. Update any tests that reference them to import from `react_loop`/`prompt_builder` directly.
- **Verify:** `grep -rn '_format_tools\|_fmt_tool_call\|_extract_json_candidates\|_parse_json' tests/` → update references; `make test`

### 3D. INH-02 — Extract `_JsonVectorStore` composition for memory classes
- **Files:** `memory_store.py:305-412` (LongTermMemory), `:534-614` (ResultsMemory)
- **Action:** Extract a `_JsonVectorStore` composition object (holds path/_data/_lock/_load/_save) and a module-level `vector_search(data, llm, query, top_k)` helper. Both classes **contain** (not inherit) these. Aligns with AGENTS.md "no superclasses."
- **Verify:** `make test` + `pytest tests/test_memory_store.py -v` (if exists)

### 3E. INH-05 — Extract `_gate` for file-tool zone-gate logic
- **Files:** `builtin_tools/files.py:101-139,201-278,319-356,401-466,522-564`
- **Action:** Extract `_gate(path, operation, *, desc, caller_depth, caller_tag) -> dict | None` returning a confirmation dict or `None` (=proceed). Each `_exec_*` calls the gate then its `_run_*`.
- **Verify:** `make test` + `pytest tests/test_builtin_tools_files.py -v` (if exists)

**Stage 3 Exit Criteria:** `make check` passes. Reduced duplication, no behavioral change.

---

## Stage 4 — Large Method Decomposition (Medium Risk, Larger Effort)

### 4A. LM-02 — Extract helpers from `react_loop`
- **Files:** `react_loop.py:888-1070`
- **Action:** Extract `_handle_non_json(state, turn, progress) -> Optional[str]` (fail-streak block 996-1028) and `_action_from_turn(turn) -> tuple[dict, sink]` (native/parse dispatch 978-1029).
- **Verify:** `make test`

### 4B. LM-03 — Hoist shared tool-lifecycle logging
- **Files:** `react_loop.py:1243-1384` (MCP logging 1328-1376)
- **Action:** Extract a `tool_lifecycle(tool, dur_ms, outcome)` helper or `with tool_span(tool):` context manager; use for both MCP and built-in paths.
- **Verify:** `make test`

### 4C. LM-04 — Decompose `BuiltinExecutor.__init__`
- **Files:** `builtin_executor.py:111-304`
- **Action:** Extract `_build_exec_table()`, `_build_run_table()`, `_init_nsjail(...)`. Consider a config dataclass for the ~15 `shell_nsjail_*` params.
- **Verify:** `make test`

### 4D. LM-05 — Extract `_compression_report`
- **Files:** `agent_controller.py:301-391`
- **Action:** Extract `_compression_report(before, after, n_messages, *, fallback, reason="") -> str` to deduplicate the token-accounting + logging + return-string tail.
- **Verify:** `make test`

### 4E. LM-06 — Deduplicate `_exec_vision_query` confirmation construction
- **Files:** `react_loop.py:418-487`
- **Action:** Compute `(needs_confirm, reason)` once, then build the confirmation payload in a single place.
- **Verify:** `make test`

### 4F. LM-20 — Decompose `_run_shell_subprocess`
- **Files:** `builtin_tools/shell.py:280-588`
- **Action:** Extract `_spawn(command|nsjail_cmd)`, `_pump_streams(proc, deadline, appenders) -> timed_out`, `_classify_exit(returncode, output, error, nsjail)`. The select loop (:430-476) is the natural standalone unit.
- **Verify:** `make test` + `pytest tests/test_builtin_tools_shell.py -v`

### 4G. LM-22 — Decompose `_probe_oauth_challenge`
- **Files:** `mcp_client.py:1084-1260`
- **Action:** Extract `_post_discovery_probe(client, url) -> final_status`; collapse oversized-response guards (:1183-1202) into one helper.
- **Verify:** `make test` + `pytest tests/test_mcp_client.py -v`

### 4H. LM-24 — Table-dispatch for `cmd_mcp`
- **Files:** `telegram_commands.py:756-946`
- **Action:** Replace sequential `if sub ==` blocks with a dispatch dict `{"on": _mcp_on, "off": _mcp_off, "info": _mcp_info, "auth": _mcp_auth}`. Auth sub-helpers already extracted — finish the pattern.
- **Verify:** `make test`

### 4I. LM-25 — Decompose `graph_memory.search`
- **Files:** `graph_memory.py:636-765`
- **Action:** Extract `_seed_entities(vec, k)`, `_expand_facts(seed_ids, k)`, `_search_episodes(vec, k)` returning lists that `search` ranks. Each becomes independently testable.
- **Verify:** `make test` + `pytest tests/test_graph_memory_e2e.py -v`

### 4J. LM-26 — Decompose `_exec_spawn_agent`
- **Files:** `builtin_tools/agents.py:44-260`
- **Action:** Extract `_validate_spawn_args(args)`, `_build_context_payload(task, args)`, `_coerce_overrides(args)`. Fold the three try/except override blocks (:198-209) into one helper.
- **Verify:** `make test`

### 4K. LM-23 — Extract `_ProgressPanel` from `_run_agent_task_locked`
- **Files:** `telegram_interface.py:531-708`
- **Action:** Extract a `_ProgressPanel` helper object (owns `_steps`, timing, classify/build/flush) and a `_dispatch_progress(msg)` method. Removes the list-cell mutation hack (`_last_edit_ts=[0.0]`, `_step_n=[0]`).
- **Verify:** `make test` + manual Telegram streaming test if feasible

### 4L. LM-21 — Decompose `_run_oauth_flow`
- **Files:** `mcp_client.py:1372-1547`
- **Action:** Split into `_await_session_ready(wrapper)`, `_verify_token_persisted(name, probe_saw_challenge)`, `_register_wrapper(name, wrapper)`. Share the tool-registration block with `_connect_server:762-773`.
- **Verify:** `make test` + `pytest tests/test_mcp_client.py -v`

**Stage 4 Exit Criteria:** `make check` passes. Each decomposed method <60 lines where feasible. No behavioral change.

---

## Stage 5 — Tech Debt: Encapsulation & Robustness (Medium Risk)

### 5A. TD-07 — Reduce `build_react_context` desync risk
- **Files:** `agent_runtime.py:214-259`, `react_loop.py:155-244`, `agent_controller.py:88-124`
- **Action:** Have `AgentController` hold a small typed deps object it can hand to `ReactContext` directly, or generate the copy from a shared field list. Eliminates the 3-place desync that caused TD-01.
- **Verify:** `make test`

### 5B. TD-08 — Give `_SdkClientWrapper` explicit methods
- **Files:** `mcp_client.py:707,759-760,781-784,1418-1420,1507`
- **Action:** Add `configure(...)` method and `clear_tools()`/`drain_task()` methods to `_SdkClientWrapper`. Replace direct private field mutation with these methods. Keeps ownership inside the wrapper.
- **Verify:** `make test` + `pytest tests/test_mcp_client.py -v`

### 5C. TD-10 — Centralize LadybugDB error-string matching
- **Files:** `graph_memory.py:302-307` (WAL), `:437-443` (migration), `:350` (extension install)
- **Action:** Centralize the version-specific string tokens in named constants with a comment pinning the ladybug version. Catch specific exception types where the library exposes them.
- **Verify:** `make test` + `pytest tests/test_graph_memory_e2e.py -v`

### 5D. TD-11 — Batch-embed entity names in graph write loop
- **Files:** `graph_memory.py:481` (`upsert_entity`), `:1039` (`_process_batch`), `:1064-1071` (loop)
- **Action:** Batch-embed all entity names once per batch (most embedding APIs accept a list), then pass vectors into `upsert_entity`. Reduces N serial network round-trips to 1.
- **Verify:** `make test` + `pytest tests/test_graph_memory_e2e.py -v`

**Stage 5 Exit Criteria:** `make check` passes. Improved encapsulation and robustness.

---

## Stage 6 — Coverage Gap Review (Research Only — COMPLETED)

### 6A. TD-14 — Review `scheduler.py` and `sub_agent_supervisor.py`
- **Files:** `scheduler.py` (1176 LOC), `sub_agent_supervisor.py` (359 LOC)
- **Action:** Focused oracle review of both files. 19 findings produced.
- **Verify:** N/A (research task; produces findings, not code changes)

**Stage 6 Exit Criteria:** ✅ Review document produced with findings for scheduler.py + sub_agent_supervisor.py.

---

## Stage 6 Findings

### `scheduler.py` (1176 LOC)

| ID | Category | Severity | File:line | Description | Fix Sketch |
|---|---|---|---|---|---|
| SCHED-01 | large-method | high | `scheduler.py:598-774` | `_run_job` is 177 lines with two full execution strategies (spawn_agent + legacy agent_fn) | Extract `_handle_empty_task`, `_run_via_spawn_agent`, `_run_via_agent_fn`; keep `_run_job` as dispatcher |
| SCHED-02 | risk | high | `scheduler.py:636,702,769,827,839,932` | `_jobs_meta` mutated across threads without a consistent lock; concurrent once-job pop during `_save_state_locked` iteration can raise `RuntimeError: dictionary changed size` | Designate one lock as `_jobs_meta` guard for all mutation/iteration, or snapshot (`list(...)`) inside `_save_state_locked` |
| SCHED-03 | encapsulation | medium | `scheduler.py:644,685` | Scheduler calls `builtin_executor._exec_spawn_agent` (private) across class boundary; `hasattr` probe on private name | Expose public `spawn_agent(args, options)` on `BuiltinExecutor` |
| SCHED-04 | tech-debt | medium | `scheduler.py:139,209,210,425,622,1016,1073` | `datetime.utcnow()` deprecated in 3.12+; inconsistent with rest of codebase using `datetime.now(timezone.utc)` | Replace with `datetime.now(timezone.utc)` |
| SCHED-05 | duplication | medium | `scheduler.py:53,344-347,352` | Tag-normalization logic (`.replace` chain + underscore collapse) duplicated 3 times | Extract single `_normalize_tag(s) -> str` helper |
| SCHED-06 | duplication | medium | `scheduler.py:634-638,700-704,766-771` | Once-job removal block (`schedule.clear` + `_jobs_meta.pop` + `_save_state` + `_save_scheduler_toml`) copy-pasted 3 times | Extract `_remove_once_job(tag)` |
| SCHED-07 | tech-debt | low | `scheduler.py:69` | `_legacy_to_cron` has dead `run_at` parameter; `time_str: str = None` should be `Optional[str]` | Drop `run_at`; add `Optional[...]` hints |
| SCHED-08 | large-method | medium | `scheduler.py:1025-1094,357-448` | `_load_config_jobs` (70 lines) and `add_job` (92 lines) exceed 40 lines | Extract `_build_job_meta` and `_resolve_schedule` helpers |
| SCHED-09 | tech-debt | medium | `scheduler.py:960-1023` | Hand-rolled TOML serializer (64 lines) is fragile | Serialize with `tomli_w`/`tomlkit`, or extract `_serialize_job(tag, meta) -> list[str]` |
| SCHED-10 | tech-debt | medium | `scheduler.py:715` | Error detection via `result.startswith("❌")` emoji sentinel — brittle protocol coupling | Have `agent_fn` return structured `{success, result}` or raise |
| SCHED-11 | idiom | low | `scheduler.py:493` | Redundant `import threading as _threading` inside `run_now` while `threading` is module-level | Use module-level `threading.Thread(...)` |
| SCHED-12 | risk | low | `scheduler.py:327,455,468,556,562,1133` | `schedule` library (not thread-safe) mutated from main thread and read from loop thread | Serialize all `schedule.*` calls under one lock, or migrate once-jobs to croniter |

### `sub_agent_supervisor.py` (359 LOC)

| ID | Category | Severity | File:line | Description | Fix Sketch |
|---|---|---|---|---|---|
| SUBSUP-01 | large-method | high | `sub_agent_supervisor.py:152-333` | `_run_and_notify` is 182 lines with 3 near-duplicate terminal branches (cancelled/done/failed) | Extract `_finalize_terminal(record, status, result, ...)`; keep only branch-specific notify text |
| SUBSUP-02 | duplication | medium | `sub_agent_supervisor.py:233-244,266-277,300-311` | `result_log_cb` invocation + try/except duplicated 3 times | Extract `_safe_result_log(cb, *, tag, task, result, success, elapsed, model)` |
| SUBSUP-03 | duplication | low | `sub_agent_supervisor.py:279-285,313-319` | Notification header HTML duplicated (done/failed differ only in emoji/verb) | Extract `_result_header(icon, verb, runner, label, task, elapsed) -> str` |
| SUBSUP-04 | tech-debt | medium | `sub_agent_supervisor.py:226,228,238` | `"[Cancelled]"` magic-string protocol between runner and supervisor | Define shared `CANCELLED_SENTINEL` constant or signal via `record.status` |
| SUBSUP-05 | encapsulation | medium | `sub_agent_supervisor.py:130,138,195,205,230,241,247,264,283,298,308,317` | Cross-class private-attribute access on runner/record (`_model_id`, `_short_term`, `_agent._trace_id`, `_result_event`, `_timeout_cancelled`) | Add read properties (`runner.model_id`, `runner.short_term`, `runner.trace_id`) and `record.signal_result()` / `record.timeout_cancelled` public surface |
| SUBSUP-06 | tech-debt | low | `sub_agent_supervisor.py:152-153` | Missing type hints on `runner`/`record` params in `_run_and_notify` and `submit` | Import `SubAgentRunner`/`SubAgentRecord` under `TYPE_CHECKING` and annotate |
| SUBSUP-07 | idiom | low | `sub_agent_supervisor.py:175` | Mutable-dict closure flag (`context_save_attempted = {"done": False}`) instead of clearer idiom | Use `nonlocal` after extracting terminal path (SUBSUP-01), or a guard object |

### Summary

| File | High | Medium | Low |
|---|---|---|---|
| `scheduler.py` | SCHED-01, SCHED-02 | SCHED-03/04/05/06/08/09/10 | SCHED-07/11/12 |
| `sub_agent_supervisor.py` | SUBSUP-01 | SUBSUP-02/04/05 | SUBSUP-03/06/07 |

**Priority recommendation:** SCHED-02 (locking crash risk) is the only finding with genuine correctness risk. The two large-method items (SCHED-01, SUBSUP-01) carry the most duplication and are the natural next refactor targets. Encapsulation findings (SCHED-03, SUBSUP-05) are pervasive but low-risk — address opportunistically when surrounding methods are refactored.

---

## Deferred (Not Actionable Now)

| ID | Reason |
|---|---|
| OS-05 | `ConfirmationCoordinator` extraction gated on (1) security review of shell confirmation gating and (2) "unified approve-all" decision. No action until upstream decisions are made. |
| OS-01 | `add-prompt-search` change is 0/27 tasks, 18d stale. Either begin implementation (spec is detailed) or formally archive as deferred. **Decision needed from user.** |

---

## Implementation Order Summary

| Stage | Theme | Items | Risk | Effort |
|---|---|---|---|---|
| 1 | Idiomatic Python cleanup | IDIOM-01,02,03,04,05,08,09 | Low | S |
| 2 | Tech debt quick wins | TD-04,06,09,12,13 | Low | S |
| 3 | Duplication reduction | INH-02,03,04,05,06 | Medium | S–M |
| 4 | Large method decomposition | LM-02,03,04,05,06,20,21,22,23,24,25,26 | Medium | L |
| 5 | Encapsulation & robustness | TD-07,08,10,11 | Medium | M |
| 6 | Coverage gap review | TD-14 | N/A | M |

**Recommended approach:** Implement stages in order. Each stage is a separate PR. Run `make check` after every stage. If a stage reveals new issues, add them to this plan before proceeding.