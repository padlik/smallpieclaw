## proposal Round 1 — 2026-09-24

### 🔴 Fixed

- None. The reviewer found no serious proposal-stage issue; `proposal.md` is frozen after this round.

### 🟡 Addressed

- F5: The Impact inventory omitted `builtin_tools/descriptors.py`; added the path without changing the proposal's decisions.

### 🟡 Outstanding

- F1: Assign explicit main/scheduled/sub-agent LLM notice to a normative capability in design/specs; add a capability only if existing ones cannot own it, with the required unfreeze.
- F2: Define the ownership and exact outcome semantics for scheduled `last_error` and notifications without confusing ordinary command nonzero exit with backend loss.
- F3: Reconcile native-tool-calling's cached-definition requirement with runtime capability loss and already-active calls.
- F4: Remove or replace builtin-tool-execution's confirm-mode subprocess-fallback scenarios in the delta spec.
- F6: Define whether shell_env_* is offered when shell is unavailable, and how stale calls behave.

### 🔴 Outstanding

- None.

## proposal Round 2 — 2026-09-25

### 🔴 Fixed

- None. The proposal was unfrozen to incorporate the operator's new default-backend decision and passed this review without serious findings; it is frozen again.

### 🟡 Addressed

- The new default `shell_backend = "nsjail"` is marked BREAKING in the proposal, mapped to `nsjail-shell-sandboxing`, and reflected in the compatibility impact. Prior F5 (`builtin_tools/descriptors.py` in Impact) was confirmed resolved.

### 🟡 Outstanding

- F1: Home the explicit LLM notice under an existing normative capability in design/specs.
- F2: Define scheduled-run `last_error` ownership and distinguish backend loss from ordinary command nonzero exit.
- F3: Amend the native-tool-calling cached-definition requirement for a capability-generation transition.
- F4: Replace builtin-tool-execution's obsolete confirm-mode subprocess-fallback scenarios.
- F6: Define `shell_env_*` availability and stale-call behavior.

### 🔴 Outstanding

- None.

## proposal Round 3 — 2026-09-25

### 🔴 Fixed

- None. The proposal was unfrozen to incorporate the operator's scheduled-prompt-only scope and passed without serious findings; it is frozen again.

### 🟡 Addressed

- F2: Removed the scheduled `last_error`/notification promise and `schedule-tool-surface` capability; accepted and disclosed the existing outcome limitation, with generic status correction deferred to ARCH-03.

### 🟡 Outstanding

- F1: Home the explicit LLM shell-unavailable notice under an existing normative capability in design/specs.
- F3: Reconcile native-tool-calling's cached definitions with runtime capability loss.
- F4: Replace builtin-tool-execution's obsolete confirm-mode subprocess fallback scenarios.
- F6: Define `shell_env_*` availability and stale-call behavior.

### 🔴 Outstanding

- None.

## design Round 1 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious design-stage issue; `design.md` is frozen after this round.

### 🟡 Addressed

- F6: Defined the unavailable shell family as `shell` plus `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get`; all are hidden and stale calls are rejected.
- F3: Chose generation-keyed native tool-definition caching and refresh after capability loss.
- F4: Chose fail-closed dispatch and confirmation with no subprocess fallback, including after runtime loss.
- Identified the in-force ADR-0012 fallback decision for partial supersession in the ADR step, preserving ADR-0026/0027.

### 🟡 Outstanding

- F1: Place the runtime LLM shell-unavailable notice in a normative requirement under an existing modified capability in the specs batch.
- F3/F4: Rewrite the cached-definitions and confirm-mode fallback scenarios in the respective delta specs.
- Clarify in the builtin-tool-execution delta that “other tools” excludes the shell_env_* family, which is unavailable together with shell.

### 🔴 Outstanding

- None.

## specs Round 1 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious specs-stage issue; the three delta specs are frozen after this round.

### 🟡 Addressed

- F1: The builtin-tool-execution delta requires an explicit runtime notice for main, scheduled, and sub-agent prompts.
- F3: The native-tool-calling delta updates complete tool-definition assembly semantics for generation-keyed cache refresh and reserved MCP names.
- F4: The builtin-tool-execution delta replaces confirmation-mode subprocess fallback with fail-closed unavailability.
- F6: The builtin-tool-execution and native-tool-calling deltas gate the complete shell family while keeping its registry membership; other tools remain available.

### 🟡 Outstanding

- None required for this batch. Optional clarity: distinguish confirmation-capable membership from availability and add an explicit-nsjail happy-path example. Carried canonical streaming and network claims were not worsened.

### 🔴 Outstanding

- None.

## adr Round 1 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious ADR-stage issue; ADR-0033 and the decision content are frozen after this round.

### 🟡 Addressed

- ADR-0033 correctly partially supersedes ADR-0012's default and unconfined fallback without rewriting it or displacing its valid jail isolation and confirmation decisions.
- The sequence, change-local manifest link, and frozen-artifact scope passed review.

### 🟡 Outstanding

- Correct the manifest's in-force ADR inventory: reconcile all 32 predecessor records, including relevant ADR-0024 and any still-in-force ADR-0001/0002. This is a declarative manifest correction only; ADR-0033 is unchanged.

### 🔴 Outstanding

- None.

## adr Round 2 — 2026-09-25

### 🔴 Fixed

- None. The durable ADR decision remained frozen; this round checked only the declarative manifest inventory correction.

### 🟡 Addressed

- The manifest accounts for all 32 predecessor ADRs: 25 remain in force and 7 are fully superseded. ADR-0024 is historical context fully superseded by ADR-0027, correcting the Round 1 reviewer's optional claim that it was in force.

### 🟡 Outstanding

- None required. Optionally note ADR-0016's earlier partial supersession in ADR-0012's in-force manifest entry; this does not affect the decision or inventory.

### 🔴 Outstanding

- None.

## tasks Round 1 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious tasks-stage issue; the numbered checklist is frozen after this round.

### 🟡 Addressed

- All three delta capabilities have test-first, dependency-ordered tasks; platform, documentation, focused/static/full-suite checks, and strict OpenSpec validation are included.
- Correcting two declarative clarity items in the checklist: use the project's `make lint` gate and avoid testing the deferred scheduler outcome by negative assertion.

### 🟡 Outstanding

- Some cross-module plumbing tasks may need smaller execution increments if they exceed two hours; `vulture_whitelist.py` requires attention only if the new public symbols are flagged. Avoid unrelated `agent_controller.py` edits unless the prompt-assembly path requires them.

### 🔴 Outstanding

- None.

## specs Round 2 — 2026-09-25

### 🔴 Fixed

- Post-review strict validation found that MODIFIED requirements dropped three legacy scenario headings; the Round 1 specs review had missed OpenSpec's archive scenario-loss check. Replaced the affected requirements with REMOVED old headings and ADDED distinct, truthful replacements. The current CLI forbids same-name REMOVED+ADDED pairs.

### 🟡 Addressed

- The new delta requirements retain the intended shell, confirmation, and native-tool behavior without obsolete fallback-titled scenarios. `openspec validate disable-unconfined-shell --type change --strict --json` reported `valid: true` with exit 0; the reviewer found no serious issue and the specs are frozen again.

### 🟡 Outstanding

- **Separate pre-archive prerequisite:** the pre-existing `openspec/specs/native-tool-calling/spec.md` is delta-shaped rather than a valid main spec (no `## Requirements`). Strict change validation reports this as INFO but OpenSpec would refuse archive. Normalize it without changing requirement/scenario bodies in a separately reviewed change, merged before archiving `disable-unconfined-shell`. Do not edit that canonical spec during this planning-only change.
- Optional formatting: consolidate the two accepted `## ADDED Requirements` sections in the builtin delta; strict validation parses both, so this is not a blocking issue.

### 🔴 Outstanding

- None.
