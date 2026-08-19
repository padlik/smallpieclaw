# Review Log — per-model-context-window

## proposal Round 1 — 2026-08-19

Batch scope: proposal.md only. Baseline: explore-brief.md (all 4 open questions resolved) + existing specs agent-runtime-construction and native-tool-calling.

### 🔴 Fixed
- (none — no implementation blockers found)

### 🟡 Addressed
- **Scheduler.toml persisted-job migration not covered by warn-and-ignore.** `fallback_models` is also persisted per-job inside `scheduler.toml` and read back on startup (`scheduler.py:580`, `scheduler.py:741-744`). After removal, an existing `scheduler.toml` with per-job `fallback_models = [...]` entries is a second breaking surface. The breaking-change note should extend warn-and-ignore to persisted per-job meta (drop/ignore the key on load), and the Impact/Config lines should mention `scheduler.toml.example`.
- **`vulture_whitelist.py` omitted from Impact.** Adding `ModelConfig.context_window` and removing `RuntimeOptions.fallback_models`, `_fallback_indices`, etc. will move vulture flags. Add to Impact → Code list.

### 🔴 Outstanding
- (none — batch passes)

### ✅ Verdict
**Batch PASSES.** All three coupled changes and all four resolved open questions captured. Capability claims verified against live specs. Breaking change clearly marked. Two 🟡 completeness gaps to fold in before specs/design phase.

## design Round 1 — 2026-08-19

Batch scope: design.md only. proposal.md is FROZEN. Baseline: explore-brief.md.

### 🔴 Fixed
- (none — no blockers)

### 🟡 Addressed
- **C4 diagram formula parentheses.** Node `CM` labeled `threshold = eff - max_tokens * 0.85` which by precedence reads as the wrong formula. Fixed to `(eff - max_tokens) * 0.85` to match D3, the data flow, and the risks note.

### 🔴 Outstanding
- (none — batch passes)

### ✅ Verdict
**Batch PASSES.** Design fully realizes the brief, stays coherent with the frozen proposal, handles ADR-0007 supersession correctly by routing it to the adr step with rationale, and confirms method-name preservation consistent with the native-tool-calling spec. One 🟡 diagram-label fix applied.

## specs Round 1 — 2026-08-19

Batch scope: three delta specs. proposal.md + design.md FROZEN. Baseline: explore-brief.md.

### 🔴 Fixed
- **agent-runtime-construction: REMOVED block targeted a scenario, not a requirement.** "Model override and fallback trichotomy are preserved" is a `#### Scenario:` under "Runtime options preserve construction knobs" in the base spec, not a standalone `### Requirement:`. The REMOVED block would fail strict validate/archive. The scenario removal is already fully handled by the MODIFIED requirement (which replaces the trichotomy scenario with "Model override is preserved"). Fix: deleted the entire `## REMOVED Requirements` block.

### 🟡 Addressed
- **Vision error message wording vs frozen design.** Spec mandated "instruct the user to set `vision = true`" but frozen design said "preserve existing message". Reconciled: relaxed the spec THEN to "indicate that no vision-capable model is configured" — matches the design's "preserve existing message" intent without mandating new wording.

### 🔴 Outstanding
- (none — batch passes after fixes)

### ✅ Verdict
**Batch PASSES after fixes.** One 🔴 (mis-targeted REMOVED block) deleted; one 🟡 (vision error message) reconciled with frozen design. Formula correctness, full MODIFIED requirements, method-name preservation, Gherkin format, and brief coverage all solid.

## specs Round 2 — 2026-08-19

Batch scope: three delta specs, re-review after Round 1 fixes. proposal.md + design.md FROZEN.

### 🔴 Fixed
- **REMOVED block deleted — confirmed.** `agent-runtime-construction/spec.md` now has only the `## MODIFIED Requirements` section; the mis-targeted `## REMOVED Requirements` block is gone. The MODIFIED "Runtime options preserve construction knobs" requirement is complete and self-contained: all three scenarios present, all fallback-free. The trichotomy removal is now expressed solely and correctly via the MODIFIED requirement.

### 🟡 Addressed
- **Vision error message reconciled with frozen design — confirmed.** `per-model-context-window/spec.md:54` now reads "the error message SHALL indicate that no vision-capable model is configured", matching design.md's "preserve the existing `LLMPermanentError` with the same message". Consistent.

### 🔴 Outstanding
- (none — batch passes)

### ✅ Verdict
**Batch PASSES.** Both Round 1 findings correctly resolved with no regressions. Specs internally consistent, coherent with frozen proposal and design, cover the full brief. Ready to advance to adr/tasks. Two non-blocking 💡 reminders (vision progress message, `vulture_whitelist.py`) deferred to implementation/tasks phase.

## adr Round 1 — 2026-08-19

Batch scope: adr.md manifest + repo-level ADR-0020. proposal/design/specs FROZEN.

### 🔴 Fixed
- (none — no blockers)

### 🟡 Addressed
- (none)

### 🔴 Outstanding
- (none — batch passes)

### 💡 Addressed (optional)
- **Supersession scope clarified.** ADR-0020 Status and Supersedes fields now explicitly state "partially supersedes ADR-0007" — only the `RuntimeOptions.fallback_models` trichotomy is removed; the AgentRuntime construction boundary and other RuntimeOptions knobs remain in force. Prevents a header-only reader from assuming the whole construction boundary was overturned.

### ✅ Verdict
**Batch PASSES.** ADR-0020 correctly and completely supersedes ADR-0007 in madr-minimal style, the manifest is a clean summary, the prior ADR is untouched (iron rule satisfied), and the decision is coherent with every frozen artifact. Ready to advance to tasks.

## tasks Round 1 — 2026-08-19

Batch scope: tasks.md (final artifact). proposal/design/specs/adr FROZEN. Baseline: explore-brief.md.

### 🔴 Fixed
- (none — no blockers)

### 🟡 Addressed
- **Single-model error propagation test added.** Task 4.6 now explicitly tests that a transient error on the primary model propagates to the caller with NO fallback attempt — guards against partial fallback logic left in `_run_with_fallback()`.
- **Fallback-free model-override test coverage.** Task 4.2 now ensures a positive test for the "Model override is preserved" scenario remains after removing the trichotomy tests.
- **Vision no-switch case added.** Task 1.4 now includes case (d): active model already vision-capable → no switch.

### 🔴 Outstanding
- (none — batch passes)

### ✅ Verdict
**Batch PASSES.** Tasks cover all three coupled changes and all four resolved open questions, follow the design's dependency order, close every carried item, and end with proper `openspec validate --strict` + `make check` gates. Two 🟡 test-completeness items folded in. The full artifact chain is reviewed and the change is ready for /opsx-apply.