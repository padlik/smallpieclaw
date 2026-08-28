## proposal Round 1 — 2026-08-28

### 🔴 Outstanding
- **Modified Capabilities omits the one requirement this change actually rewrites.** The existing requirement "Headless confirm bridge checks the shared approval set" (spec.md:218–240) names `_prompt_approval_set` directly in normative text and all three scenarios. This change deletes `_prompt_approval_set`. The specs batch needs an explicit MODIFIED requirement to rewrite this.
- **prompt_fn ownership is unresolved.** Impact says "passed at call time" + "keep the function for the bridge to pass" — contradictory. The bridge needs a reference to the prompt function; main.py currently supplies it. Need a design decision on the supply mechanism.

### 🟡 Addressed
- **Field-name inconsistency**: proposal uses `_headless_confirm_events` (brief) and `_headless_events` (Impact) interchangeably. Pick one.
- **"routes all four flows" overstates**: three already live on ConfirmationManager; only the headless flow moves. Reword.

### ✅ Good
- Impact file list is accurate and exhaustive — every symbol verified against code.
- `builtin-tool-execution` is the correct capability path.
- New Capabilities correctly declares none — atomic approve-all is a correctness fix, not new behavior.
- Race-condition motivation is concrete and traceable.

## proposal Round 2 — 2026-08-28

### 🔴 Outstanding
(none — all Round 1 blockers resolved)

### 🟡 Addressed
- Field-name inconsistency resolved: proposal now uses `_headless_confirm_events` / `_headless_confirm_results` uniformly (What Changes L7 abstracts to "event/result dicts"; Impact L27 names them consistently).
- "Four flows" overstatement fixed: Modified Capabilities (L23) now states only the depth≥1 headless flow is absorbed; the other three already live on ConfirmationManager.

### ✅ Good
- 🔴#1 resolved: Modified Capabilities (L23) explicitly flags the "Headless confirm bridge checks the shared approval set" requirement (spec.md:218–240) as rewritten, covering both the check path (`auto_approve_tools` on coordinator) and fail-closed semantics (`coordinator is None`). The specs batch now has an unambiguous instruction.
- 🔴#2 resolved: prompt_fn ownership is now consistent across L9/L28/L31 — `_subagent_confirm_prompt_fn` stays on BuiltinExecutor (main.py unchanged), passed to the coordinator at call time so the coordinator stores no transport-specific callbacks. Aligns with the brief's background-agent flow and preserves test_supervisor_prompt_wiring.py.
- Removal list (L9) correctly excludes `_subagent_confirm_prompt_fn`; consistent with Impact L28.
- No regressions introduced by the edits.

### ⚖️ Verdict
Proposal batch PASSES and is frozen. Both 🔴 blockers and both 🟡 items resolved; no new issues. Carry-forward for specs: the delta spec must MODIFY "Headless confirm bridge checks the shared approval set" (requirement + 3 scenarios) per L23, describing behavior rather than re-leaking a private field name.

## design+specs Round 1 — 2026-08-28

### 🔴 Outstanding
- **`_coordinator is None` fail-closed scenario is infeasible under the new ownership model.** Spec delta MODIFIED scenario "Coordinator is None after run end (fail-closed)" asserts "the operator is prompted (fail-closed)". In the new design the coordinator owns the prompt/wait machinery — when `_coordinator is None` the bridge cannot prompt or wait at all. Must define as block/deny without prompting and rewrite the scenario.
- **ADR-0011 characterization disputed.** Reviewer claims ADR-0011 is about "shell is never auto-approved" based on main spec L71. Orchestrator verified ADR-0011 file: title is "Use per-prompt scope for operator approval grants" — the main spec L71 references an *amendment* to ADR-0011, not its subject. ADR-0011 is correctly identified. This item is a false positive — no fix needed.

### 🟡 Addressed
- Migration step 7 ("Update tests to use new APIs") is generic, but Risks section names the specific tests. tasks.md should carry the full list.

### ✅ Good
- Design faithfully implements frozen proposal: Decisions 1-6 map 1:1 to What-Changes.
- Non-Goals match brief exactly.
- Method signatures match brief's cross-module data flows.
- Spec delta MODIFIED copies entire existing block and edits it correctly.
- All scenarios use 4 hashtags; every requirement has ≥1 scenario.
- C4 component diagrams present (current + target).
- Risk analysis is concrete (GIL atomicity, secrets_log.py:61 stable).

### ⚖️ Verdict
NOT ready to freeze. One real 🔴 (fail-closed scenario), one false-positive 🔴 (ADR-0011). Fix the fail-closed scenario and this batch is solid.

## design+specs Round 2 — 2026-08-28

### 🔴 Outstanding
(none — both Round 1 blockers resolved)

### 🟡 Addressed
- Fail-closed behavior for `_coordinator is None` is now fully specified in both artifacts: design Risks defines the entry guard (check `self._coordinator is None`, return fail-closed error without prompting) and the spec scenario asserts the matching observable outcome (operation blocked, fail-closed error "confirmation bridge is unavailable"). Code behavior and spec now agree.

### ✅ Good
- 🔴#1 resolved: MODIFIED scenario "Coordinator is None after run end (fail-closed)" now says the operation is blocked WITHOUT prompting (spec L25–26), consistent with the new ownership model. The intentional behavior change (orphaned sub-agent: prompt → block) is documented in design Risks with rationale.
- 🔴#2 resolved: design clarifies the ADR-0011 reference — main spec L71 is an amendment (nsjail change), not ADR-0011's subject; ADR-0011 = "Use per-prompt scope for operator approval grants." Confirmed against the actual ADR file.
- No regressions: the entry guard precedes the auto_approve_tools check without conflict; in-run prompt scenario and orphaned block scenario coexist; all prior ✅ items intact.

### ⚖️ Verdict
design+specs batch PASSES and is frozen. Both 🔴 blockers resolved, no regressions, no new issues. Ready to proceed to the tasks batch. Carry-forward for tasks.md: implement the `_coordinator is None` entry guard as an explicit task; enumerate the full test-migration list from the brief's verification strategy.

## adr Round 1 — 2026-08-28

### 🔴 Outstanding
(none — ADR batch is correct and complete)

### 🟡 Addressed
- ADR-0024 L44 restated "shell is never auto-approved for the main agent" but that invariant was amended by ADR-0012 (configurable confirm mode). Fixed: narrowed to "sub-agent shell always-blocked; main-agent shell gate configurable via nsjail (ADR-0012 amendment)."

### ✅ Good
- Manifest correctly marks review completed, lists in-force ADRs reviewed and the one new ADR, does not duplicate repo-level ADR body.
- ADR-0024 supersession correct in both fields (Status + Supersedes).
- ADR-0024 preserves all four ADR-0011 decisions while changing only the mechanism.
- ADR-0011 file unmodified (immutable rule honored).
- Sequencing correct: 0024 follows 0023.
- No contradictions with frozen proposal/design.

### ⚖️ Verdict
adr batch PASSES and is frozen. All 8 checks green; 🟡 fixed.

## tasks Round 1 — 2026-08-28

### 🔴 Outstanding
- **Missing test-migration task: `test_p2_graph_memory_admission.py`.** Lines 294 and 321 call `b.signal_headless_confirm(token, approved)`, which task 3.4 removes from BuiltinExecutor. No section-5 task migrates this file.
- **Missing `_subagent_confirm_timeout` migration across three test files.** Task 3.5 removes `_subagent_confirm_timeout` from BuiltinExecutor, but no task migrates references in test_subagent_approve_all.py (93,109,123), test_p1_subagent_confirm.py (209,226), test_prompt_approval_ttl.py (47,58,108). No mechanism defined for tests to force a short headless timeout.

### 🟡 Addressed
- Remove-before-migrate ordering inversions: task 2.4 removed `_prompt_approval_set` before 3.2 migrated its consumer; task 3.4 removed `signal_headless_confirm` before callers migrated.
- Each test task should enumerate ALL removed symbols it must migrate.

### ✅ Good
- Format correct: `## N.` numbered headings, `- [ ] N.M` checkboxes throughout.
- Production-change coverage matches frozen artifacts.
- main.py correctly has NO task.
- New tests added for atomic approve-all (5.5) and `_coordinator is None` fail-closed (5.6).
- Cleanup section complete: vulture, ruff, make check, openspec validate.

### ⚖️ Verdict
NOT ready to freeze. Two 🔴 test-migration gaps guarantee `make check` failure during apply.

## tasks Round 2 — 2026-08-28

### 🔴 Outstanding
- **Task 5.2 omits `_prompt_approval_set` migration for test_subagent_approve_all.py.** Lines 155, 156, 166 use `executor._prompt_approval_set`. Task 6.2 removes it. Line 155 reads the attribute → AttributeError.

### 🟡 Addressed
- Task 5.3's single example only showed `= {"file_read"}` — needed to also cover `= set()` (line 43) and `= None` (line 55, fail-closed).

### ✅ Good
- 🔴#1 resolved: test_p2_graph_memory_admission.py is now task 5.5.
- 🔴#2 resolved: `_subagent_confirm_timeout` migration covered for all three files; mechanism defined (coordinator.default_headless_timeout).
- 🟡#1 (ordering) resolved: all removals moved to section 6, gated "after callers migrated in sections 4-5."
- No regressions.

### ⚖️ Verdict
NOT ready to freeze — one residual 🔴 in task 5.2.

## tasks Round 3 — 2026-08-28

### 🔴 Outstanding
(none — the Round 2 blocker is resolved and grep confirms complete migration coverage)

### 🟡 Addressed
- Task 5.2 now explicitly migrates test_subagent_approve_all.py's `_prompt_approval_set`: lines 155-156 and 166. Stale comment at line 188 also flagged. Round 2 🔴 resolved.
- Task 5.3 now covers all three `_prompt_approval_set` variants: `= {"file_read"}` (31, 74, 98), `= set()` (43), `= None` (55, fail-closed → `_coordinator = None`). Round 2 🟡 resolved.

### ✅ Good
- Grep verification confirms the migration list is exhaustive: all `_prompt_approval_set`, `signal_headless_confirm`, and `_subagent_confirm_timeout` references in tests/ map to tasks 5.1–5.5. No orphaned references remain.
- Ordering is correct: every consumer migrated in sections 4-5 before symbol removed in section 6.
- Production coverage, main.py untouched, new tests, format, cleanup all intact.

### ⚖️ Verdict
tasks batch PASSES and is frozen. All Round 2 items resolved; grep confirms complete removed-symbol migration coverage with no ordering hazards. This was the final batch — the full change (proposal + design+specs + adr + tasks) is now reviewed and ready for /opsx-apply.