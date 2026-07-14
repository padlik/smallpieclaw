## Why

`builtin_executor.py` was reduced to a 629-line façade by the archived
`split-builtin-executor-modules` change, but that change deliberately scoped out
everything related to confirmation state (a separate, still-parked follow-on) and
didn't sweep the rest of the file for smaller leftovers. Four independently
low-risk items surfaced while exploring what's left: one genuine extraction
(`_exec_schedule` is the last tool body still living on the façade instead of in
`builtin_tools/`) and three hygiene fixes (an avoidable typing workaround, a stale
docstring, and leftover whitespace from the extraction). None of them touch
confirmation state, tool semantics, or any model-facing behavior.

## What Changes

- Extract `_exec_schedule` (`builtin_executor.py:534-603`, ~70 lines) into a new
  stateless leaf function `exec_schedule(scheduler, args)` in
  `builtin_tools/schedule.py`, matching the existing leaf-module pattern
  (`context_io.py`, `patterns.py`, `text_utils.py`) rather than the class-based
  handler pattern (`ShellTools`, `FileTools`, ...) — it reads exactly one
  collaborator (`self.scheduler`), so it doesn't need an owner back-reference.
  `BuiltinExecutor._exec_schedule` stays as a thin forwarder with its exact
  current signature (`def _exec_schedule(self, args): return exec_schedule(self.scheduler, args)`),
  so the one direct call site (`tests/test_scheduler_fallback.py`, which builds
  `BuiltinExecutor.__new__(BuiltinExecutor)` bypassing `__init__`) needs no changes.
- Hoist the local `import threading as _threading` inside `_headless_confirm_bridge`
  (`builtin_executor.py:446`) to a top-level `import threading`; retype
  `_headless_confirm_events` from `dict[str, object]` to `dict[str, threading.Event]`;
  drop the resulting `# type: ignore[attr-defined]` on `event.set()` (line 399).
  No import-cycle risk — `threading` is stdlib and nothing else in this file avoids
  top-level stdlib imports.
- Fix the stale module docstring (`builtin_executor.py:1-27`): it currently claims
  the built-ins are "shell, file_read, file_write" (there are 15 today) and
  documents `error_type`/`recoverable`/`suggestion` result fields as if they were
  implemented in this file. Repoint the tool-list claim to `BUILTIN_TOOLS` in
  `builtin_tools/descriptors.py` as the source of truth (so it can't drift again),
  and reword the error-classification section to describe it as the contract the
  `builtin_tools/*` handlers implement, not something this file does.
- Remove leftover stray double/triple blank lines from the prior extraction
  (around lines 36-37, 68-69, 401-402, 606-609) — pure whitespace, no behavior
  change.

**Explicitly out of scope:** the `ConfirmationCoordinator` extraction
(`_pending`, `_headless_confirm_events`/`_headless_confirm_results`,
`_subagent_confirm_prompt_fn`, `confirm()`/`cancel()`/`_headless_confirm_bridge`
state) is deliberately not touched. It remains parked pending two unresolved
upstream decisions: whether a planned "unified" approve-all replacement needs to
merge with `confirmation.py`'s separate `ConfirmationManager`, and the outcome of
a security review of shell's confirmation-gating logic. This change does not
reshape, rename, or relocate any of that state. The one exception: the
`threading` import hoist above touches `_headless_confirm_events`'s type
annotation and `_headless_confirm_bridge`'s import line — a type/import-mechanics
edit, not a change to the state's shape, ownership, or behavior. No dict key,
value, lifecycle, or control flow in the confirmation bridge changes.

## Capabilities

### New Capabilities
None. This is a pure internal refactor/hygiene change — no new or altered
model-facing or user-facing behavior.

### Modified Capabilities
- `builtin-tool-execution`: no behavior changes. The existing "dispatch is total
  and deterministic" requirement already implies that dispatch results don't
  depend on which internal module implements a handler; this change adds one
  scenario making that caller-observable invariant explicit as a standing
  property of the system (not a claim about the `_exec_schedule` relocation
  itself, which is an implementation detail this invariant happens to cover).
  No existing scenario's meaning changes.

## Impact

- **New:** `builtin_tools/schedule.py` (single function `exec_schedule`).
- **Modified:** `builtin_executor.py` (façade forwarder for `_exec_schedule`,
  top-level `threading` import + retyped `_headless_confirm_events`, docstring,
  whitespace cleanup).
- **Tests:** none require changes — `tests/test_scheduler_fallback.py`'s direct
  call to `executor._exec_schedule(args)` keeps working unchanged because the
  façade forwarder preserves the exact signature and only reads `self.scheduler`
  at call time.
- **Risk:** minimal. No public API, tool schema, dispatch table, or confirmation
  behavior changes. `make check` (ruff + vulture + pytest) is the verification
  bar for behavior; there is no type checker in this repo, so the
  `threading.Event` retype (item 2) is verified by inspection, not by tooling.
