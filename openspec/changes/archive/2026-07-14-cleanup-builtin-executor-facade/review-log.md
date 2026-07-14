# Review Log: cleanup-builtin-executor-facade

## all-artifacts Round 1 — 2026-07-14

Reviewer: @openspec-reviewer (persona from `~/.config/opencode/agents/openspec-reviewer.md`, run via a general-purpose subagent since the opencode runtime isn't available in this session). Baseline: no explore-brief.md or review-log.md existed for this change (first round); verbal exploration baseline supplied by the orchestrating agent, covering the four cleanup items' discovery/rationale and the parked `ConfirmationCoordinator` context. Scope: all five artifacts (proposal.md, design.md, specs/builtin-tool-execution/spec.md, adr.md, tasks.md) — nothing frozen.

### 🔴 Fixed
- (none — no critical blockers found)

### 🟡 Addressed
- Proposal's out-of-scope paragraph (confirmation state untouched) read as self-contradicted by tasks.md 2.1-2.4, which edit `_headless_confirm_events`'s type annotation and `_headless_confirm_bridge`'s import line. Added an explicit carve-out sentence to proposal.md distinguishing a type/import-mechanics touch from a change to the state's shape, ownership, or behavior — no dict key, value, lifecycle, or control flow changes.
- `make check` (ruff + vulture + pytest) can't verify task 2's `threading.Event` retype — no type checker in this repo. Proposal's Impact/Risk section and design.md's Migration Plan now say this item is verified by inspection, not by the stated tooling bar; added tasks.md 2.6 as an explicit manual-verification step.
- The added spec scenario ("A handler's result is unaffected by which module implements it") described a refactoring event across two code versions rather than a standing, caller-observable property — HOW leaking into a WHAT-level requirement. Reworded to "Dispatch result is independent of which internal module implements a handler," stated as a property the running system holds at any instant, with GIVEN/WHEN/THEN no longer naming the façade→`builtin_tools/` move. Proposal's Modified Capabilities description updated to match.
- design.md referred to "ADR-0008's module-layout table" (ADR-0008 is prose, no table) and missed the real staleness: ADR-0008's Decision section names `_exec_schedule` as staying "inline," which becomes factually stale after this change. Corrected the characterization in Context/Risks/Open Questions; resolved the open question as "acknowledge the drift, do not edit the immutable ADR file" and added tasks.md 5.3 to make that acknowledgment discoverable at apply time.

### 🔴 Outstanding
- (none)

Reviewer verified against source: all four items' exact line numbers and mechanics in `builtin_executor.py` (534-603, 446, 399, 138, docstring 1-27), the `__new__`-bypass test (`tests/test_scheduler_fallback.py:265-291`), `_exec_schedule` reading only `self.scheduler`, `BUILTIN_TOOLS`'s 15 tools (`builtin_tools/descriptors.py`), the ADR supersession graph (0001→0002→0003; in-force {0003,0004,0005,0006,0007,0008}), and ADR-0008's actual text (confirming the "inline" phrase and the absence of any table). Missing explore-brief.md/review-log.md flagged per reviewer's own rules; did not lower verdict confidence.

**Outcome:** No 🔴 outstanding → all artifacts FROZEN. `openspec validate --strict` passes after fixes. Change is apply-ready.

## all-artifacts Round 2 — 2026-07-14

Reviewer: @openspec-reviewer (same persona/setup as Round 1). Baseline: Round 1's entry above (validation baseline), plus the same verbal exploration baseline (no explore-brief.md exists). Scope: all five artifacts again, explicitly auditing whether each Round 1 🟡 finding was substantively resolved rather than just gestured at.

### 🔴 Fixed
- (none — no critical blockers found)

### 🟡 Addressed
- (none required — all four Round 1 findings independently re-verified as genuinely resolved, with ground truth re-checked against live source: the out-of-scope carve-out names the exact touch and scopes it correctly; tasks.md 2.6 is a concrete, executable gate (reviewer independently checked all five readers of `_headless_confirm_events` and confirmed none assume the old `object` type); the reworded spec scenario is a genuine point-in-time observable property with no relocation-event language; the ADR-0008 correction matches ADR-0008's actual text verbatim, and tasks.md 5.3 makes the drift discoverable without editing the immutable ADR)

### 🔴 Outstanding
- (none)

### 💡 Optional (addressed same pass, non-blocking)
- design.md Decision 2 overstated ADR-0008 by implying it already treated `_exec_schedule` as a pinned forwarder needing a verbatim signature, when ADR-0008 actually marks it "inline" — reworded to credit only the *other two* forwarders (`_exec_spawn_agent`/`_exec_get_agent_result`), consistent with the Context/Open Questions staleness note.
- design.md's Open Questions entry said "deferred to the tasks step" even though tasks.md 5.3 already resolves it — added an explicit "Resolved in tasks.md 5.3" note to close the loop.

Reviewer re-verified against live source (zero drift from Round 1): `builtin_executor.py` line numbers (534-603, 446, 138, 399, docstring 1-27), the `__new__`-bypass test, `BUILTIN_TOOLS`'s 15 tools, and ADR-0008's literal text (line 47: "inline `_exec_schedule`"). Independently re-ran `openspec validate cleanup-builtin-executor-facade --strict` → passes.

**Outcome:** No 🔴 or 🟡 outstanding → all artifacts FROZEN (Round 2). Two optional cosmetic tidies applied to design.md. `openspec validate --strict` passes. Change is apply-ready.
