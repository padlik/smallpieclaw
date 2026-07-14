# Review Log: split-builtin-executor-modules

## proposal Round 1 — 2026-07-14

Reviewer: @openspec-reviewer. Baseline: explore-brief.md. Scope: proposal.md (first batch, nothing frozen).

### 🔴 Fixed
- (none — no critical blockers found)

### 🟡 Addressed
- Tool count was wrong (16 → **15**). Fixed in proposal.md:3 and explore-brief.md (14 dispatched + vision_query). This count seeds a frozen routing test, so correctness matters.
- Capability `builtin-tool-execution` mixed structural + behavioral contracts. Reframed the surface-preservation invariant as observable behavior (same dispatch/confirm result; `is_builtin`/`all_tools` enumerate the same set) rather than "symbol X importable from module Y".
- Scoped the new capability as tool-agnostic (dispatch + confirmation framework + split-preservation), explicitly NOT re-specifying individual tool semantics. Added a specs-phase guardrail comment to grep openspec/specs/ for existing per-tool capabilities (secret_get, log_query, memory_graph_*) before freezing, to keep "no modified capabilities" true.
- Noted the `_load_context` re-export-vs-repoint decision is deferred to design (agent_runtime.py:313).

### 🔴 Outstanding
- (none)

Verified by reviewer against source: public API (builtin_executor.py:580–797), 8 post-init settables (main.py 294–505), 6 confirmation tools (shell 948, file_read 1456, file_write 1577, file_patch 1690, memory_graph_store 2324, secret_get 2401), vision_query intercept (react_loop.py:896–901), schedule inline + __new__ test, monkeypatch landmine.

**Outcome:** No 🔴 outstanding → proposal batch FROZEN. Proceed to design.

## design Round 1 — 2026-07-14

Reviewer: @openspec-reviewer. Baseline: frozen proposal.md + explore-brief.md + in-force ADRs (0003-0007). Scope: design.md, adr.md, adr/0008.

### 🔴 Fixed
- (none — no critical blockers)

### 🟡 Addressed
- Decision 3 referenced a "kwargs-uniform registry adapter" that was never defined, and the 14 dispatched tools do NOT share a call shape. Specified that each table value is a per-tool adapter reproducing that tool's exact current kwargs (with the full per-tool kwarg table and lambda examples); `chunk_callback` threaded for shell in both tables. Prevents a Phase-1 TypeError false start.
- Decision 4 transcribed the 3 pinned signatures with a spurious keyword-only `*,`. Corrected to verbatim positional-or-keyword-with-defaults form (`_exec_spawn_agent(self, args, caller_depth=0, caller_tag="", trace_id="", options=None)`, etc.) to avoid contradicting the signature-preservation pin.
- Open Questions reworded to past tense (ADR-0008 already recorded, adr.md completed).

### 🔴 Outstanding
- (none)

Reviewer verified against source: dispatch tables, 6 confirmation tools (_run 923-934), 8 settables construction/patch sequence (main.py 256/294/295/437/438/446/474/475/505 + None defaults 553/554/572), vision_query intercept order (react_loop 897-901), 3 pinned signatures + callers (scheduler.py:683, hasattr guard :642), monkeypatch target, ADR supersession graph (0001->0002->0003; in-force {0003,0004,0005,0006,0007}), ADR-0008 format/coherence.

**Outcome:** No 🔴 outstanding → design + adr batch FROZEN. Proceed to specs.

## specs Round 1 — 2026-07-14

Reviewer: @openspec-reviewer. Baseline: frozen proposal/design/adr/0008 + explore-brief + existing per-tool specs. Scope: specs/builtin-tool-execution/spec.md.

### 🔴 Fixed
- (none — no critical blockers)

### 🟡 Addressed
- Removed migration-temporal phrasing from durable spec text ("before the module split" / "preserved across the module split") → reframed as invariants (handler-defined result, no dispatch transformation; fixed set of 15 enumerated by is_builtin/all_tools; dispatch MUST NOT hold a vision_query handler). Equivalence-to-prior stays a verification step in tasks.
- Dropped structural "table-driven" from Requirement 1 title → "Built-in tool dispatch is total and deterministic".
- Added a framework-level scenario asserting the confirmation-capable set is fixed at exactly the 6 (shell, file_read, file_write, file_patch, memory_graph_store, secret_get) — backs the frozen routing test without re-specifying per-tool policy.
- Made the vision_query dispatch scenario observable (execute does not itself perform a vision query) rather than structural ("no handler entry").

### 🔴 Outstanding
- (none)

Reviewer verified: critical scoping clean (no duplication/contradiction with vault-runtime-lookup, runtime-log-introspection, agent-recovery, agent-runtime-construction; Req2 uses file_write example not secret_get); 15 tools (builtin_executor.py:291-472); vision_query intercept order (react_loop.py:897-901 before is_builtin :901); public method signatures. Orchestrator additionally confirmed unknown-name path returns error dict (never raises) at _dispatch:730.

**Outcome:** No 🔴 outstanding → specs batch FROZEN. Proceed to tasks.

## tasks Round 1 — 2026-07-14

Reviewer: @openspec-reviewer. Baseline: all frozen artifacts (proposal/design/specs/adr/0008) + explore-brief. Scope: tasks.md.

### 🔴 Fixed
- (none — no critical blockers)

### 🟡 Addressed
- Added unknown-name assertion to routing test 8.1 (execute unknown → success=False + error, no raise) — backs the spec's "total and deterministic" negative scenario; noted it can move to Phase 1 to guard table drift.
- Added task 8.4 verifying the three Decision-8/ADR-0008 seam constraints (no _pending/headless refs, no lifecycle logging in handler modules, _run_table sole phase-2 route) — the change's distinguishing deliverable was previously unverified.
- Split overloaded phases: 5.1 → 5.1a SecretsTools / 5.1b LogQueryTools; 6.1 → 6.1a shell log helpers / 6.1b shell core (highest-risk ~500-line task made bisectable, ≈2h granularity).
- Fixed suite-mapping hints: 6.3 names test_builtin_executor.py; 7.4 adds test_p2_longterm_consolidation.py + test_subagent_context_persistence.py; spelled out import smoke commands; added _run_table symmetry call-outs (4.2 memory_graph_store, 5.2 secret_get, 6.2 shell). Renumbered final steps to 8.5 (make check) / 8.6 (strict validate).

### 🔴 Outstanding
- (none)

Reviewer verified: full design-decision coverage, re-export list matches Decision 5, monkeypatch move + spy assertion (7.3), dependency order 0→1→{2,3,4}→5→6, ADR-0005/0007 honored, per-phase make check + import smoke, strict validate before archive, no behavior change.

**Outcome:** No 🔴 outstanding → tasks batch FROZEN. All artifacts complete; change is apply-ready.
