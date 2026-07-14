## Context

`builtin_executor.py` (2713 lines) is a single `BuiltinExecutor` class that implements 15
built-in tools, holds their `BUILTIN_TOOLS` descriptor dict, and dispatches them via two
`if/elif` chains (`_dispatch` for phase 1, `_run` for the phase-2 confirmed path). It also
owns the cross-cutting confirmation machinery (interactive token staging + a headless
sub-agent → Telegram operator bridge) and eight collaborators wired *after* construction
(`main.py:256` builds it; 294/295/437/438/446/474/475/505 patch the settables on).

The archived `introduce-agent-runtime` change (in force via ADR-0007) built the
construction seam that makes this split safe; this design consumes that seam rather than
changing it.

In-force ADRs that constrain this design: **ADR-0007** (AgentRuntime owns sub-agent
construction — the `_sub_agent_factory` the executor reads is runtime-produced), **ADR-0005**
(SubAgentSupervisor is the supervision boundary — the agents handler must keep delegating to
`_supervisor`), **ADR-0004** (structlog lifecycle events `TOOL_START`/`TOOL_END`/`TOOL_FAILED`
must be preserved), **ADR-0003** (TOML vault the `secret_get` handler reads). None need
revisiting.

### Target structure (C4 component level — lightweight, ASCII)

Container = the agent process. Zoom into the `builtin_tools` component group after the split:

```
                         ReAct loop / scheduler / tool_index
                                        │  execute() / confirm() / cancel()
                                        ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │  BuiltinExecutor  (façade — builtin_executor.py)                    │
   │  • public API: execute, confirm, cancel, is_builtin, all_tools,     │
   │    shutdown, signal_headless_confirm                                │
   │  • cross-cutting confirmation state: _pending, headless events,     │
   │    _subagent_confirm_prompt_fn, _requires_confirmation, bridge      │
   │  • _exec_table / _run_table  (name → bound handler method)          │
   │  • real forwarders: _exec_spawn_agent/_exec_get_agent_result        │
   │  • inline: _exec_schedule ;  re-exports: helpers + BUILTIN_TOOLS    │
   └───────┬───────────────────────────────────────────────────────────┘
           │ owner back-ref (read late-bound collaborators at call time)
   ┌───────┴───────┬───────────┬───────────┬───────────────┐
   ▼               ▼           ▼           ▼               ▼
 ShellTools     FileTools   AgentTools  MemoryTools   SecretsTools+LogQueryTools
 (shell.py)    (files.py)  (agents.py) (memory.py)   (secrets_log.py)
   │                                        
   └── stateless leaves (no owner): descriptors.py, patterns.py, text_utils.py,
       logquery_helpers.py, context_io.py

   vision_query: descriptor in descriptors.py; NO table entry; executed by the ReAct loop.
```

## Goals / Non-Goals

**Goals:**
- Reduce `builtin_executor.py` to a thin façade; move tool bodies into a `builtin_tools/`
  package grouped by concern.
- Preserve all runtime behavior, the public API, the direct reach-in methods, the
  module-level helper functions, and the `BUILTIN_TOOLS` dict exactly.
- Replace the `if/elif` dispatch with two name-keyed registry dicts, keeping the two-phase
  confirmation flow intact.
- Leave the confirmation seam clean so a future `ConfirmationCoordinator` extraction is a
  mechanical move.

**Non-Goals:**
- Extracting `ConfirmationCoordinator` (deferred follow-on change).
- Changing tool semantics, confirmation policy, model-facing schemas, or the
  construction/wiring in `main.py`.
- Removing the owner back-reference (accepted for this pass; the god-object *state* stays
  centralized on the façade — only method bodies relocate).
- Inverting the builtin ↔ scheduler / runtime / tg construction cycles.

## Decisions

1. **Hybrid façade + handler package** (over mixins / pure delegation). Mixins are
   superclasses (violates AGENTS.md "no superclasses") and only cosmetically dissolve the
   god class. Pure self-contained handlers are impossible because 8 collaborators are wired
   post-construction (ADR-0007 runtime seam), so handlers would need `None`-at-init; they
   need the owner back-ref anyway, and scheduler/test reach-ins force the façade to keep the
   method surface regardless. Hybrid gets smaller files without an inheritance tree.

2. **Handlers read late-bound collaborators via `self._owner.<attr>` at call time, never
   snapshotted.** The 8 settables (`_working`, `_results`, `_sub_agent_factory`,
   `_notify_html_fn`, `scheduler`, `_graph_memory`, `_graph_memory_writer`,
   `_subagent_confirm_prompt_fn`) are `None` at handler-construction time. Snapshotting would
   capture `None`. Constructor-time constants (`default_timeout`, `max_output`, `_data_dir`,
   `_shell_*`, `_vault_path`, `_log_jsonl_path`) may be read either way; reading via owner is
   used uniformly for simplicity.

3. **Two name-keyed registry dicts** (`_exec_table`, `_run_table`) replace `if/elif`.
   `_exec_table` has 14 entries (all tools except `vision_query`); `_run_table` has exactly
   the 6 confirmation-capable tools (`shell`, `file_read`, `file_write`, `file_patch`,
   `memory_graph_store`, `secret_get`). **The 14 dispatched tools do NOT share a call shape**
   (`shell` takes `caller_depth, caller_tag, chunk_callback`; `spawn_agent` takes
   `caller_depth, caller_tag, trace_id`; `file_read/file_write/file_patch/memory_graph_store/
   secret_get/log_query` take `caller_depth, caller_tag`; `file_send/get_agent_result/
   memory_write/file_diff/memory_graph_search` take `caller_tag` only; `schedule` takes `args`
   only). Therefore each table value is a **per-tool adapter that reproduces that tool's exact
   current kwargs** (e.g. `"file_send": lambda a, ctx: self._exec_file_send(a, caller_tag=ctx.caller_tag)`,
   `"shell": lambda a, ctx: self._shell.exec(a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag, chunk_callback=ctx.chunk_callback)`).
   `_dispatch`/`_run` build a small call-context (`caller_depth`, `caller_tag`, `chunk_callback`,
   `trace_id`) and pass it to the adapter, which forwards only the kwargs the tool accepts
   today — `chunk_callback` is threaded for `shell` in both tables. A naïve uniform
   `table[name](args, caller_depth=…, caller_tag=…)` would `TypeError` on the `caller_tag`-only
   and `args`-only tools; the frozen routing test + `make check` catch that, but the adapter
   shape is the spec. Alternative (decorator auto-registration) rejected as over-engineering
   for a fixed tool set and harder to keep behavior-identical.

4. **The 3 pinned methods stay as real façade forwarders**, not table-only entries, with
   their signatures preserved **verbatim** from current `builtin_executor.py` (positional-or-
   keyword with defaults — do NOT add a keyword-only `*,`):
   `_exec_spawn_agent(self, args, caller_depth=0, caller_tag="", trace_id="", options=None)`,
   `_exec_get_agent_result(self, args, caller_tag="")`, `_exec_schedule(self, args)`.
   `scheduler.py:683`
   and ~6 test files call them directly (asserting `call_args[0][0]` / `.kwargs["options"]`),
   so their exact call shapes are contractual. `_exec_schedule` stays fully inline (a test
   builds a bare instance via `__new__` and calls it without `__init__`).

5. **Module-level helpers move to leaf modules and are re-exported from `builtin_executor`**
   (`_is_dangerous_shell`, `_is_sensitive_path`, `_truncate_output`, `_truncate_tail`,
   `_validate_context_key`, `_load_context`, `_save_context`, `BUILTIN_TOOLS`,
   `BuiltinTool`). Value-import tests (`from builtin_executor import X`) keep working
   unchanged.

6. **Monkeypatch target moves.** `test_subagent_context_persistence.py` patches
   `builtin_executor._save_context`; once the caller (`AgentTools`) resolves `_save_context`
   from `builtin_tools.context_io`, the patch on `builtin_executor` is a silent no-op. The
   test must repoint to the module the caller actually resolves (`builtin_tools.context_io._save_context`)
   and the spy must be asserted to fire.

7. **`_load_context` in `agent_runtime.py:313` stays a re-export from `builtin_executor`**
   for this change (minimal churn); repointing it to `builtin_tools.context_io` is deferred as
   later cleanup. (Resolves the brief's open question.)

8. **Confirmation stays on the façade now**, but three zero-cost seam constraints are honored
   so the future coordinator drops in cleanly: (a) handlers touch confirmation only through
   `_requires_confirmation`/`confirm`/`cancel`, never `_pending`/headless dicts directly;
   (b) `_run_table` is the single phase-2 routing point; (c) lifecycle logging stays
   module-level (`agent_logging.log_event`), coherent with ADR-0004.

## Risks / Trade-offs

- **Import cycles** (`agent_runtime` ↔ executor; graph_memory/config_schema/prompt_loader/
  sub_agent_registry/memory_store) -> keep those imports function-local exactly as today;
  keep `builtin_tools/__init__.py` empty/light so it never eagerly imports handler modules
  that import back; add `python -c "import main"` + `import builtin_executor` smoke per phase.
- **Late-bound attribute capture** -> read via owner at call time (Decision 2); routing/wiring
  test asserts handlers see post-init values.
- **Lifecycle span deferral regression** (the `confirm()` deferred `TOOL_END` + headless
  `_emit_lifecycle=False`) -> move no logging into handlers; preserve the façade's span logic
  verbatim; covered by `test_p1_subagent_confirm.py`.
- **Monkeypatch becomes a no-op** (Decision 6) -> repoint + assert the spy fires; a silent
  no-op would pass while testing nothing.
- **God object across files** (Goal-A trade-off) -> accepted; state stays centralized, only
  bodies move. The coordinator follow-on addresses it later; seam constraints keep that cheap.
- **vulture flags new public handler symbols** -> update `vulture_whitelist.py` per phase.

## Migration Plan

Phased, each phase independently shippable + `make check`-green + import-smoke:
- **Phase 0** — extract stateless leaves (`descriptors`, `patterns`, `text_utils`,
  `logquery_helpers`, `context_io`) + add re-exports. Pure relocation.
- **Phase 1** — convert `_dispatch`/`_run` `if/elif` → `_exec_table`/`_run_table` over the
  current methods (no bodies move). Isolates the dispatch change for bisectability.
- **Phases 2–4** (parallel-authorable, merge sequentially): `files.py`, `memory.py`,
  `secrets_log.py`.
- **Phase 5** — `shell.py` (largest, highest transcription risk).
- **Phase 6** — `agents.py` (highest risk; keep real forwarders; delegates to `_supervisor`
  per ADR-0005).
Hard order: 0 → 1 → {2,3,4} → 5 → 6. All phases edit `builtin_executor.py`, so they serialize
on that file. **Rollback:** each phase is behavior-preserving, so reverting a single phase
commit restores the prior green state.

## Open Questions

- The "façade + handler-module package for built-in tools" pattern was recorded as
  **ADR-0008** (`adr/0008-use-facade-handler-package-for-builtin-tools.md`), since it
  establishes how future built-in tools are added (descriptor + handler + table entry). No
  in-force ADR needed supersession.
- Re-grep for any other `patch("builtin_executor.<symbol>")` by module path before freezing
  tasks (only `_save_context` found so far); tasks will include this verification step.
