# Explore Brief: Split `builtin_executor.py` into tool modules

**Change:** split-builtin-executor-modules
**Status:** explore output — checklist for proposal creation
**Date:** 2026-07-14

## Problem & goal

`builtin_executor.py` is 2713 lines — one `BuiltinExecutor` class holding 15 built-in
tools plus a declarative `BUILTIN_TOOLS` descriptor dict (14 dispatched + `vision_query`,
which is loop-executed). This violates the AGENTS.md
rule "avoid making superclasses and large files."

Two candidate goals were distinguished:

- **Goal A — file size / readability:** relocate tool method bodies into per-group
  modules. Behavior-preserving, low risk, mechanical.
- **Goal B — decouple the god object:** break the cross-cutting confirmation machinery
  so tool handlers become independently testable.

**Decision: pursue Goal A now.** Goal A serves the stated driver (file size), carries
low risk, and — critically — does **not** foreclose Goal B. Goal B is deferred to a
separate follow-on change (see "Out of scope"). This refactor was already anticipated
and deferred by the archived `introduce-agent-runtime` change, which built the runtime
construction seam as its prerequisite.

## Rejected alternatives

- **Do nothing / leave the file.** Rejected: violates the "no large files" convention;
  the file keeps growing as tools are added.
- **Mixins** (`class BuiltinExecutor(ShellTools, FileTools, ...)`). Rejected: mixins are
  base classes in the MRO — AGENTS.md explicitly says "avoid superclasses." Dissolves the
  god class only cosmetically.
- **Pure delegation with self-contained handlers** (each handler owns its collaborators).
  Rejected: post-init wiring order (8 settables patched onto the executor after
  construction) means handlers cannot own late-bound collaborators; they need an owner
  back-reference anyway. And scheduler/test reach-ins force the façade to keep the method
  surface regardless. Pure delegation pays all the plumbing cost and still can't drop the
  façade. It is the correct *eventual* target, not this pass.
- **Coordinator-first** (extract `ConfirmationCoordinator` before splitting). Rejected as
  the *first* move: it front-loads the highest-risk extraction (deferred lifecycle span,
  headless threading, two-phase re-entry, fail-closed) onto a still-monolithic 2713-line
  file, producing a large, hard-to-bisect semantic diff. Deferring it is strictly better:
  after relocation the handlers already live in their own files, so the coordinator
  extraction becomes an isolated, independently reviewable change. Repointing handler call
  sites from `self._owner._requires_confirmation` → `self._coord.require` later is a
  mechanical rename, so Goal A does not paint us into a corner.

## Final approach

### Mechanism — hybrid façade + handlers

`BuiltinExecutor` remains a thin façade that owns the public API and **all** cross-cutting
confirmation state. Each tool group becomes a handler class constructed with a **live
back-reference to the façade** (`ShellTools(owner=self)`), reading late-bound
collaborators as `self._owner.<attr>` **at call time, never snapshotted** (snapshotting in
handler `__init__` would capture `None`). Stateless helpers move to pure leaf modules.
The if/elif dispatch becomes two registry dicts keyed on tool name.

### Module layout (full, no omissions) — new `builtin_tools/` package

| File | Contents moved from builtin_executor.py | Owner reach-ins |
|---|---|---|
| `descriptors.py` | `BuiltinTool` dataclass (283–289) + `BUILTIN_TOOLS` dict (291–514) **incl. `vision_query` descriptor (415–425)** | none |
| `patterns.py` | `_DANGEROUS_SHELL_PATTERNS`, `_SENSITIVE_PATH_PATTERNS`, `_is_dangerous_shell`, `_is_sensitive_path` (131–182) | none |
| `text_utils.py` | `_truncate_output`, `_truncate_tail` (97–124) | none |
| `logquery_helpers.py` | log-query consts, `_log_level_to_num`, `_log_query_default_keep`, `_read_tail_lines`, `_log_query_project` (188–275) | none |
| `context_io.py` | `_validate_context_key`, `_context_path`, `_save_context`, `_load_context` (72–94, 2681–2713) | none (lazy `memory_store`) |
| `shell.py` | `ShellTools`: `_exec_shell`, `_run_shell`, `_run_shell_subprocess`, `_run_shell_pty`, `_open_shell_log`, `_finalize_shell_log` (937–1444) | `default_timeout`, `max_output`, `_data_dir`, `_shell_backend`, `_shell_pty_cols`, `_shell_pty_rows`, `_shell_streaming` (all constructor constants) |
| `files.py` | `FileTools`: file_read/file_write/file_patch/file_diff/file_send exec+run (1446–1772) | `max_output` (constant) |
| `agents.py` | `AgentTools`: `_exec_spawn_agent`, `_exec_get_agent_result` (1851–2141) | `_sub_agent_factory`, `_max_subagents`, `_working`, `_results`, `_graph_memory`, `_data_dir`, `_notify_html_fn`, `_supervisor`, `_subagent_result_timeout` |
| `memory.py` | `MemoryTools`: memory_write, memory_graph_search, memory_graph_store exec+run (2144–2387) | `_memory`, `_graph_memory`, `_graph_memory_writer` |
| `secrets_log.py` | `SecretsTools` (secret_get exec+run, 2389–2498) + `LogQueryTools` (log_query, 2500–2678) | `_vault_path`, `_log_jsonl_path`, `max_output` |

`builtin_executor.py` stays the **façade** and keeps: class `BuiltinExecutor`, `__init__`
(constructs the handlers), the two dispatch registries, all cross-cutting confirmation
state + methods, and re-exports for backward compat. `_exec_schedule` (schedule tool,
~74 lines) stays **inline on the façade** — it is tiny, only touches `self.scheduler`, and
a test constructs a bare instance via `BuiltinExecutor.__new__` then calls `_exec_schedule`
(must not depend on any `__init__`-set attribute).

### Dispatch registries

- `_exec_table`: tool name → phase-1 handler method, for all 14 dispatched tools (every
  built-in except `vision_query`, which is intercepted in `react_loop`).
- `_run_table`: tool name → phase-2 handler method, for the **6** confirmation-capable
  tools: `shell`, `file_read`, `file_write`, `file_patch`, `memory_graph_store`,
  `secret_get`. (`file_read` IS confirmation-capable via the sensitive-path gate — do not
  omit it.)

## Preservation contract (public + reach-in surface)

### Must remain importable/callable from `builtin_executor`

- Class `BuiltinExecutor` — imported by main.py:104 and 9 test files.
- Public methods (unchanged signatures): `execute`, `confirm`, `cancel`, `is_builtin`,
  `all_tools`, `shutdown`, `signal_headless_confirm`. Consumers: react_loop.py (901, 924,
  936, 944, 947), tool_index.py:227 (`all_tools`), telegram_commands (`signal_headless_confirm`).
- Module-level helpers via **re-export** (value imports — re-export is sufficient):
  `_is_dangerous_shell`, `_is_sensitive_path`, `_truncate_output`, `_truncate_tail`
  (test_builtin_executor.py:8, 660–679); `_validate_context_key` (test_scheduler_fallback.py:252);
  `_load_context`, `_save_context` (test_subagent_context_persistence.py:15;
  test_sub_agent_supervisor.py:434); `agent_runtime.py:313` imports `_load_context`.
- Attributes reached by tests: `_supervisor` (patched `_supervisor._pool.submit`),
  `_graph_memory`.

### Signature pins (hard constraint)

`_exec_spawn_agent`, `_exec_get_agent_result`, `_exec_schedule` must keep their **exact
current call shapes** — first-positional `args` dict plus specific kwargs — because they
are called directly and asserted on:
- scheduler.py:683 `self.builtin_executor._exec_spawn_agent(spawn_args, options=options)`
- test_scheduler_fallback / test_job_execution_log assert `.call_args[0][0]` and
  `.call_args.kwargs["options"]`
- test_spawn_agent / test_context_payload / test_subagent_context_persistence /
  test_p2_longterm_consolidation / test_sub_agent_supervisor call
  `_exec_spawn_agent({...}, caller_depth=…, trace_id=…)` positionally on arg 0
- test_scheduler_fallback.py:285 `executor._exec_schedule(args)`
- test_spawn_agent.py:369 `_exec_get_agent_result(args)`

→ These three stay as **real façade forwarders** with today's signatures; they are NOT
collapsed into the kwargs-uniform registry adapter.

### Monkeypatch homing (landmine)

test_subagent_context_persistence.py:195 does `patch("builtin_executor._save_context", …)`.
A re-export makes the *name* importable but does NOT make the patch affect a caller that
now lives in `builtin_tools/agents.py` and imports `_save_context` locally — the patch
would become a silent no-op and the test would pass while testing nothing.
**Decision:** repoint the patch target to the module where the function lives
(`builtin_tools.context_io._save_context`) AND add a task to update
test_subagent_context_persistence.py accordingly. This is decision-level; it must be in
design.md, not discovered during apply.

### `vision_query` seam — three invariants

1. Descriptor stays in `BUILTIN_TOOLS` (now in `descriptors.py`) so `is_builtin` and
   `all_tools` still list it.
2. No entry is added to `_exec_table` or `_run_table` for it.
3. react_loop.py:897–898 keeps intercepting it before the `is_builtin` branch (901),
   because execution needs LLM access the executor lacks.
The split must NOT "helpfully" add a `vision_query` handler.

### `BUILTIN_TOOLS` relocation

No external module imports `BUILTIN_TOOLS` (only used internally at 590, 593;
test_prompt_loader's `SYSTEM_BUILTIN_TOOLS` is an unrelated local list). Safe to move to
`descriptors.py` behind a re-export.

### The 8 post-init settables (main.py)

Late-bound and read via `self._owner.*` at call time: `_working` (294), `_results` (295),
`_sub_agent_factory` (437), `_notify_html_fn` (438), `scheduler` (446), `_graph_memory`
(474), `_graph_memory_writer` (475), `_subagent_confirm_prompt_fn` (505). Construction is
at main.py:256; all 8 are patched afterward. Handlers must never cache these.

## Cross-module data flows

- **Two-phase confirmation (bidirectional with handlers):**
  `execute → _dispatch → Handler._exec_*` → handler calls
  `self._owner._requires_confirmation(tool, args, desc, caller_depth, caller_tag)`.
  Depth 0 → stage `_pending[token]=(tool,args)`, return `requires_confirmation` dict.
  Depth ≥1 → `_headless_confirm_bridge` blocks on operator via
  `_subagent_confirm_prompt_fn`, then on approval calls `confirm(token, _emit_lifecycle=False)`.
  `confirm(token) → _run(tool,args) → _run_table[tool] → Handler._run_*`.
- **Lifecycle span:** `execute()` opens TOOL_START; completion emitted immediately or
  deferred to `confirm()`; headless bridge passes `_emit_lifecycle=False` to avoid
  double-close. Logging is module-level (`agent_logging.log_event`), not handler state.
- **spawn path:** react_loop / scheduler → `_exec_spawn_agent` → `_sub_agent_factory`
  (main.py closure over `AgentRuntime.create`) → `_supervisor` submits.
- **auto-approve:** entirely in `confirmation.py` (auto_approve_tools) + react_loop.py:931;
  the executor split touches none of it.

## Phasing (dependency-ordered, each independently shippable + verifiable)

0. Extract stateless leaves (`patterns`, `text_utils`, `logquery_helpers`, `context_io`,
   `descriptors`) + add re-exports. Must land first.
1. Convert `_dispatch`/`_run` if/elif → `_exec_table`/`_run_table` (no bodies move yet).
2. `files.py` (`FileTools`) — lowest coupling.
3. `memory.py` (`MemoryTools`).
4. `secrets_log.py` (`SecretsTools`, `LogQueryTools`).
5. `shell.py` (`ShellTools`) — largest, highest transcription risk.
6. `agents.py` (`AgentTools`) — highest risk, do last; keep real façade forwarders.

Hard order: 0 → 1 → {2,3,4} → 5 → 6. All phases edit `builtin_executor.py`, so they
serialize on that file (parallel branches, sequential merges).

## Out of scope (follow-on: `ConfirmationCoordinator`)

Extracting confirmation state + methods into a `ConfirmationCoordinator` is a separate
change. To keep it cheap later, bake these three seam constraints into THIS change's
design at zero extra risk:
1. Handlers touch confirmation only through `_requires_confirmation` / `confirm` / `cancel`
   — never `_pending` or the headless dicts directly.
2. Keep `_run_table` as the single phase-2 routing point (it becomes the coordinator's
   `run_dispatch` unchanged).
3. Keep lifecycle logging module-level, not woven into handler state.

## Verification strategy

- Per phase: `make check` (ruff + vulture `--min-confidence 80` + pytest).
- Group→suite map: patterns/text/files/shell → test_builtin_executor.py; log_query →
  test_log_query.py; context I/O → test_subagent_context_persistence.py; agents →
  test_spawn_agent.py, test_context_payload.py, test_sub_agent_supervisor.py,
  test_scheduler_fallback.py; memory/graph → test_graph_memory_integration.py,
  test_p2_graph_memory_admission.py, test_p2_longterm_consolidation.py; headless confirm →
  test_p1_subagent_confirm.py.
- Add a routing test: `set(BUILTIN_TOOLS)` == frozen expected set; every non-`vision_query`
  name resolves in `_exec_table`; `_run_table` keyset == exactly the 6 confirmation tools;
  `is_builtin("vision_query")` is True while absent from both tables.
- Import-cycle guard: `python -c "import main"` and `import builtin_executor` each phase.
  Keep `graph_memory`, `config_schema`, `prompt_loader`, `agent_runtime`,
  `sub_agent_registry`, `memory_store` imports function-local; keep `builtin_tools/__init__.py`
  empty/light so it does not eagerly import handler modules that import back.
- Update `vulture_whitelist.py` for newly public handler classes/methods flagged ≥80.

## Open questions

Resolved during explore:
- Reach-in inventory — fully enumerated (above). ✓
- `_exec_*`/`_run_*` signature assertions in tests — yes, three methods pinned; kept as
  real forwarders. ✓
- External `BUILTIN_TOOLS` import — none; safe to relocate. ✓
- `auto_approve_tools` boundary — untouched by the split. ✓
- Owner coupling type — pass the `BuiltinExecutor` instance as `owner`; an `ExecutorContext`
  Protocol is optional and deferred. ✓
- Schedule tool — stays inline permanently. ✓

Still to confirm in proposal/design:
- `_load_context` in agent_runtime.py:313 — re-export (minimal churn) vs repoint to
  `builtin_tools.context_io`. Lean: re-export now, repoint later.
- Exact home/decision for the `_save_context` monkeypatch target (see "Monkeypatch homing").
- Whether any additional test patches a `builtin_executor.<symbol>` by module path (only
  `_save_context` found so far) — re-grep before freezing tasks.
