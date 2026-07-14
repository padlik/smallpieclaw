## 1. Extract `_exec_schedule` into a leaf function

- [x] 1.1 Create `builtin_tools/schedule.py` with `exec_schedule(scheduler, args: dict) -> dict`, containing the body currently at `builtin_executor.py:534-603` verbatim (only the `if not self.scheduler` guard becomes `if not scheduler`, and `self.scheduler.*` calls become `scheduler.*`).
- [x] 1.2 Replace `BuiltinExecutor._exec_schedule`'s body with `return exec_schedule(self.scheduler, args)`, keeping its exact current signature (`def _exec_schedule(self, args: dict) -> dict:`).
- [x] 1.3 Add the import: `from builtin_tools.schedule import exec_schedule`.
- [x] 1.4 Run `pytest tests/test_scheduler_fallback.py -k schedule` and confirm `TestBuiltinExecutorScheduleTool::test_exec_schedule_passes_fallback_to_add_job` passes unchanged (it calls `BuiltinExecutor.__new__(BuiltinExecutor)` then `executor._exec_schedule(args)` directly — no test edits should be needed).

## 2. Fix `threading` import and typing in the headless confirmation bridge

- [x] 2.1 Add a top-level `import threading` to `builtin_executor.py`.
- [x] 2.2 Remove the local `import threading as _threading` inside `_headless_confirm_bridge` and update its one use (`_threading.Event()` → `threading.Event()`).
- [x] 2.3 Retype `_headless_confirm_events` from `dict[str, object]` to `dict[str, threading.Event]` (the `__init__` annotation).
- [x] 2.4 Remove the now-unnecessary `# type: ignore[attr-defined]` on `event.set()` in `signal_headless_confirm`.
- [x] 2.5 Run `pytest tests/test_p1_subagent_confirm.py` and confirm all headless-bridge tests still pass.
- [x] 2.6 There is no type checker in `make check` (ruff + vulture + pytest don't validate annotations) — manually confirm `event.set()` at the call site in `signal_headless_confirm` matches `threading.Event`'s real interface, and grep for any other reader of `_headless_confirm_events` that assumed the old `dict[str, object]` type before treating 2.3 as verified.

## 3. Fix the stale module docstring

- [x] 3.1 Replace the built-ins list claim ("shell, file_read, file_write") with a pointer to `BUILTIN_TOOLS` in `builtin_tools/descriptors.py` as the source of truth, per the agreed wording: "Always-available built-in tools, injected into every agent run regardless of what is in tools/ or tools_generated/. See BUILTIN_TOOLS in builtin_tools/descriptors.py for the full current list."
- [x] 3.2 Reword the "Error classification" / result-dict-fields section so it describes the contract implemented by `builtin_tools/*` handlers (it currently reads as if implemented in this file) — keep the substantive content (error_type/recoverable/suggestion descriptions) unchanged, only correct the ownership framing.

## 4. Remove stray leftover blank lines

- [x] 4.1 Collapse the extraction-artifact double/triple blank lines around `builtin_executor.py:36-37` (between the `typing` import and `import agent_logging`), `68-69` (around the `logger = logging.getLogger(__name__)` line), `401-402` (after `signal_headless_confirm`, before the "Internals" section comment), and `606-609` (before `_exec_spawn_agent`) down to single blank lines, matching the spacing convention used elsewhere in the file. (Line numbers shifted after tasks 1-3's edits; matched by content. Left two other double-blanks untouched — they're legitimate PEP8 two-blank-line separators before top-level class definitions, `_CallContext` and `BuiltinExecutor`, not extraction artifacts.)

## 5. Verify

- [x] 5.1 Run `make check` (ruff + vulture --min-confidence 80 + full pytest suite) and confirm it's green. (1074 passed, 1 skipped; ruff and vulture both clean.)
- [x] 5.2 Run `python -c "import main"` and `python -c "import builtin_executor"` as an import-cycle smoke check (matching the verification convention from the prior `split-builtin-executor-modules` change). Both succeed.
- [x] 5.3 `adr/0008-use-facade-handler-package-for-builtin-tools.md`'s Decision section says `_exec_schedule` stays "inline" on the façade — that phrase is now stale, but ADRs are immutable once accepted, so do not edit the ADR file. Acknowledged here; ADR file untouched.
- [x] 5.4 Run `openspec validate cleanup-builtin-executor-facade --type change --strict` before archiving. Passes.
