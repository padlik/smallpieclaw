## Context

`builtin_executor.py` was reduced to a 629-line façade by the archived
`split-builtin-executor-modules` change (ADR-0008: hybrid façade + handler-module
package). That change moved five tool groups into `builtin_tools/` as classes
(`ShellTools`, `FileTools`, `AgentTools`, `MemoryTools`, `SecretsTools`/`LogQueryTools`)
plus five stateless leaf modules (`descriptors.py`, `patterns.py`, `text_utils.py`,
`logquery_helpers.py`, `context_io.py`). `_exec_schedule` was left inline on the
façade, characterized as "tiny" — it is actually ~70 lines, the second-largest
method remaining in the file, and was kept inline only because
`tests/test_scheduler_fallback.py` builds `BuiltinExecutor.__new__(BuiltinExecutor)`
(bypassing `__init__`) and calls `_exec_schedule` directly, so no handler-instance
attribute could be assumed to exist.

Re-reading `_exec_schedule`'s body during exploration: it reads exactly one
collaborator, `self.scheduler` — no other façade state. That's the same shape as
the existing leaf modules (`context_io.py`'s `_save_context`/`_load_context` also
take their collaborators as plain parameters, not via an owner back-reference),
not the shape of the five class-based handlers (each of which reads multiple
late-bound collaborators). This design treats it as a leaf-function extraction,
not a sixth handler class.

Separately, three unrelated hygiene items surfaced in the same file: an avoidable
`# type: ignore` caused by a local `threading` import, a module docstring that
has drifted (claims 3 built-ins, there are 15; documents result fields that are
actually implemented in `builtin_tools/*`), and leftover stray blank lines from
the prior extraction.

In-force ADRs consulted: ADR-0003 (TOML vault — unrelated), ADR-0004 (structured
lifecycle logging — unrelated, not touched), ADR-0005 (sub-agent supervisor
boundary — unrelated), ADR-0006 (source categories — unrelated), ADR-0007
(AgentRuntime construction — unrelated), **ADR-0008** (façade + handler-module
package — directly relevant; this design extends its leaf-module category rather
than contradicting it). ADR-0008 is prose, not a table; its Decision section
explicitly names `_exec_schedule` as staying "**inline**" on the façade — that
phrase becomes stale after this change (see Open Questions). ADR-0001/0002 are
superseded by ADR-0003 and are not in force.

### Component view (lightweight, ASCII)

Where `schedule.py` lands relative to the existing `builtin_tools` package:

```
┌───────────────────────────────────────────────────────────────────┐
│  BuiltinExecutor  (façade — builtin_executor.py)                  │
│  • _exec_schedule(self, args) -> exec_schedule(self.scheduler, a)  │  (was: 70-line body inline)
└───────┬─────────────────────────────────────────────────────────┬─┘
        │ owner back-ref (multi-collaborator)                     │ plain args (single collaborator)
        ▼                                                          ▼
 ShellTools / FileTools / AgentTools /             descriptors.py, patterns.py, text_utils.py,
 MemoryTools / SecretsTools+LogQueryTools           logquery_helpers.py, context_io.py,
 (classes; read several owner attrs)                schedule.py  ◄── new, same shape as the others
```

## Goals / Non-Goals

**Goals:**
- Move `_exec_schedule`'s body into `builtin_tools/schedule.py` as a plain
  function `exec_schedule(scheduler, args) -> dict`, leaving a thin same-signature
  forwarder on the façade.
- Fix the avoidable `threading` typing workaround, the stale docstring, and the
  stray blank lines.
- Zero behavior change: identical dispatch, identical result dicts, identical
  test outcomes.

**Non-Goals:**
- The `ConfirmationCoordinator` extraction (`_pending`, headless-bridge state,
  `confirm`/`cancel`) — explicitly parked per the proposal; not touched here.
- Any change to the `schedule` tool's semantics, model-facing schema, or the
  scheduler's own behavior (`scheduler.py` is untouched).
- Widening the sweep beyond these four items (e.g. auditing other façade methods
  for similar leaf-vs-handler questions) — out of scope for this change.

## Decisions

1. **Leaf function, not a sixth handler class.** `exec_schedule(scheduler, args)`
   takes `scheduler` as a plain parameter rather than wrapping it in a
   `ScheduleTools(owner)` class with an owner back-reference. Rationale: the
   owner-back-reference pattern (ADR-0008) exists specifically because the five
   handler classes each read *multiple* late-bound collaborators off the façade;
   `_exec_schedule` reads exactly one. Forcing a class here would add
   construction boilerplate with no corresponding benefit, and — concretely —
   would resurrect the `__new__`-bypass test problem the prior change deferred
   (a handler-instance attribute wouldn't exist on a bypassed instance). A plain
   function sidesteps that entirely: the façade forwarder still only touches
   `self.scheduler` at call time, exactly as today.
   - Alternative considered: mirror the class pattern for consistency with the
     other five groups. Rejected — consistency with a pattern that solves a
     problem this method doesn't have isn't a real benefit, and it reopens a
     test-compatibility question that's otherwise moot.

2. **Façade keeps `_exec_schedule(self, args)` as a real forwarder with its exact
   current signature**, mirroring the "pinned forwarder" treatment ADR-0008 gave
   the *other two* direct reach-ins, `_exec_spawn_agent`/`_exec_get_agent_result`
   (ADR-0008 itself marks `_exec_schedule` as staying inline, not as a pinned
   forwarder — this decision is what changes that). The only change is
   the body: `return exec_schedule(self.scheduler, args)` instead of the inline
   70 lines. `tests/test_scheduler_fallback.py:265-285` (the `__new__`-bypass
   test) needs no changes — it calls `executor._exec_schedule(args)` after
   setting `executor.scheduler` directly, which still works because the
   forwarder reads `self.scheduler` at call time, not at construction.

3. **`threading` import hoists to module level**, and `_headless_confirm_events`
   is retyped `dict[str, threading.Event]`. No import-cycle risk: `threading` is
   stdlib, and the local-import pattern used elsewhere in this file exists to
   dodge real cycles with project modules (`graph_memory`, `sub_agent_registry`,
   etc.) — not stdlib. Dropping the `# type: ignore[attr-defined]` on
   `event.set()` is a consequence, not a separate change.

4. **Docstring repoints to `BUILTIN_TOOLS` instead of enumerating tool names**
   (per explore-session decision), so it can't drift again the way the current
   "shell, file_read, file_write" claim already has. The error-classification
   section is reworded to describe the contract `builtin_tools/*` handlers
   implement, rather than implying this file implements it — content is kept
   (it's the only place this contract is documented), only the framing changes.

## Risks / Trade-offs

- **Docstring reframing could under- or over-state the contract** -> keep the
  substantive content (error_type/recoverable/suggestion field descriptions)
  verbatim; only change the sentence that claims local implementation.
- **Touching the same file as the parked confirmation state invites scope creep
  during review** -> the four edits are in non-overlapping regions (module
  docstring, `_exec_schedule`, `_headless_confirm_bridge`'s import line, stray
  blanks); none touch `_pending`/`confirm`/`cancel`/`_headless_confirm_*` state.
  Proposal and this design both call out the exclusion explicitly.
- **ADR-0008's Decision text now factually contradicts implementation**: it
  says `_exec_schedule` stays "inline," which is no longer true -> flagged as an
  open question below; low stakes, and doesn't need supersession (moving the
  body to `builtin_tools/` is squarely within ADR-0008's own pattern, not a
  divergence from it) — only the prose is stale.
- **`threading.Event` retype (item 2) isn't covered by any tool in `make check`**
  (no mypy/pyright in this repo; ruff + vulture + pytest don't validate type
  annotations) -> verify by inspection: confirm `event.set()` still type-checks
  informally against `threading.Event`'s actual interface, and that no other
  reader of `_headless_confirm_events` assumed the old `object` type.

## Migration Plan

No deployment/data migration — this is a same-process code move. Four
independent edits, each verifiable in isolation with `make check`
(ruff + vulture + pytest):
1. Extract `exec_schedule` into `builtin_tools/schedule.py`; repoint the façade
   forwarder. Verify: `pytest tests/test_scheduler_fallback.py` unchanged, plus
   full `make check`.
2. Hoist `threading` import + retype `_headless_confirm_events`. Verify:
   `pytest tests/test_p1_subagent_confirm.py` (the headless-bridge suite)
   unchanged.
3. Docstring rewrite. No test impact; verify by reading.
4. Blank-line cleanup. No test impact; `ruff check` should stay green (already
   is).
Rollback: each edit is independently revertable; no ordering dependency between
them (schedule extraction is unrelated to the other three).

## Open Questions

- ADR-0008's Decision section says `_exec_schedule` stays "inline" on the
  façade — after this change that's factually stale. ADRs are immutable once
  accepted (per this repo's ADR review convention), so the options are: leave
  the stale phrase as a known historical-accuracy gap (ADR-0008's actual
  *pattern* — façade + leaf/handler modules — is unaffected and needs no
  supersession), or note the drift somewhere discoverable (e.g. a tasks.md
  verification item) without editing the ADR file itself. **Resolved in
  tasks.md 5.3**: acknowledge the drift there at apply time; the ADR file
  itself stays untouched.
