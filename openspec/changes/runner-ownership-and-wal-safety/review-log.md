# Review log

## proposal Round 1 — 2026-11-21

Verdict: PASS; proposal frozen after declarative clarifications.

### Yellow addressed

- Named on-demand and scheduled supervisor submissions explicitly.
- Added preserved partial-constructor cleanup and regression coverage.
- Assigned plan tracking cleanup scenarios to agent-runtime-construction without changing visibility/capacity policy.
- Clarified all graph-open failures, including former WAL repair markers, take the non-destructive unavailable path.
- Stated XDG and empty-directory migration remain unchanged.

### Red outstanding

None.

## design Round 1 — 2026-11-21

Verdict: FAIL; proposal remains frozen, design not frozen.

### Red fixed for next review

- Full cancelled-Future cleanup could run under the executor shutdown lock. Replaced callback cleanup with tracked accepted entries and post-executor-shutdown cancellation cleanup.

### Yellow addressed

- Chose an explicit submission lock/stopping flag handoff, outstanding-entry retirement and exactly-once shutdown claiming.
- Restricted graph-open failure logs to exception class plus fixed guidance, excluding raw exception text/traceback.
- Preserved timeout notification gating and omitted worker thread-local context clearing from shutdown cleanup.

### Red outstanding

- R1 resolution requires design Round 2 review.

## design Round 2 — 2026-11-21

Verdict: PASS; design frozen after declarative clarifications.

### Red resolved

- R1: no terminal Future callback. Pending entries precede submission, Future assignment precedes lock release, and cancellation-only cleanup runs after executor shutdown outside all locks. Worker cleanup and shutdown claims remain mutually exclusive.

### Yellow addressed

- Explicitly prohibited nesting submission and owner/registry locks; all rejection rollback runs after submission-lock release.
- Preserved existing completion callback/notify gating and timeout-cancelled suppression.

### Red outstanding

None.

## specs Round 1 — 2026-11-21

Verdict: PASS; specs frozen after declarative scenario additions.

### Yellow addressed

- Added diagnostic registration failure before/after insertion.
- Added direct post-shutdown submission rejection with rollback, close and no execution/finish callback.

### Red outstanding

None.

## adr Round 1 — 2026-11-21

Verdict: PASS; ADR manifest frozen.

### Yellow addressed

- Added optional explicit preservation of ADR-0006 source/visibility/capacity policy.

### Red outstanding

None. All 48 records and nine companions accounted for; existing ownership and graph-only containment decisions remain in force, with no new/superseding ADR required.

## tasks Round 1 — 2026-11-21

Verdict: PASS; tasks frozen after declarative split/reordering.

### Yellow addressed

- Moved handoff regressions ahead of handoff implementation and split edge coverage into smaller tasks. Final count is 18, below the requested 20-task ceiling.
- Already-correct baseline cases are recorded as characterization, not falsely claimed as failing regressions.

### Red outstanding

None. All brief/design/spec commitments map to tasks; test paths, existing leak/deletion assertions, strict checks and workflow gates were verified by the reviewer.

## specs Round 2 — 2026-11-21

Verdict: PASS; declarative wording compression only, no unfreeze.

### Yellow addressed

- Initial strict validation exited 1 with five length warnings and no structural errors. Shortened those five ADDED requirement paragraphs below the warning threshold without changing scenarios or decisions.
- Reviewer confirmed all normative contracts remain covered by the concise requirements and unchanged scenarios.

### Red outstanding

None.

## Final planning validation — 2026-11-21

- `openspec validate runner-ownership-and-wal-safety --type change --strict`: exit 0, `Change 'runner-ownership-and-wal-safety' is valid`.
- `openspec status --change runner-ownership-and-wal-safety`: exit 0, 5/5 artifacts complete.
- Task count: 18 unchecked implementation tasks; no split required.
- `git status --short`: only the new change directory is untracked; no tracked files modified.
- `git diff --check`: exit 0; separate scan of untracked files found no trailing whitespace.
- All 19 new files resolve inside the change root; required artifacts, metadata and companions exist; no placeholders found.
- No implementation, runtime test execution, commits, branch creation or merges performed. Proposal artifacts must be committed and reach main before apply begins; user approval is still required for git operations.

## design Round 3 — 2026-11-23

User authorized revising the existing change after the panel-warning review. Proposal intent remains frozen; design and dependent specs/ADR/tasks were reopened for the approved queue-first shutdown and shim-only stopping-refusal decisions.

Verdict: PASS; revised design frozen after declarative clarifications.

### Yellow addressed

- Defined explicit local primary ownership and terminal exception precedence, excluding unrelated ambient handlers and already-handled execution errors.
- Moved cancelled-queued terminalization before the bounded started-worker drain, with no early running-client close or callbacks under locks.
- Clarified claim-and-drain cancellation precedence and retained ordinary per-entry error containment.
- Stated only shutdown cancels accepted Futures; timeout/owner cancellation retain record signaling.
- Named `SupervisorStoppingError`; direct callers retain a RuntimeError-compatible exception after rollback, while only the spawn shim emits the fixed refusal shape.
- Preserved generic safe scheduler launch-failure presentation and exactly-once release without adding a template allowlist entry.
- Clarified that running workers and synchronous cleanup callbacks do not gain an immediate-shutdown guarantee.

### Red outstanding

None. Downstream specs/ADR assessment/tasks remain pending re-review. Revised planning must reach main before fixes that depend on the changed shutdown/rejection decisions; no commit, merge or archive is authorized by this review.

## specs Round 3 — 2026-11-23

Verdict: PASS; revised supervisor/runtime specs frozen after declarative coverage additions. The graph spec is unchanged.

### Yellow addressed

- Added explicit local-primary/handled-error distinction and independent terminal-stage precedence scenarios.
- Captured queue-before-drain ordering, direct typed stopping rejection, exact shim refusal fields and unchanged generic scheduled-failure reporting/release.
- Added the multi-queued-entry cancellation scenario: later entries and the bounded started-worker drain remain attempted before cancellation propagates.
- Made no-second-terminalization observable and retained timeout/owner record signaling instead of direct Future cancellation.

### Red outstanding

None. Frozen proposal and revised design remain consistent; task reconciliation is next.

## ADR revalidation — 2026-11-23

The specs reviewer revalidated `adr.md` against the revised design. The existing manifest is refrozen unchanged: queue-first ordering, a dedicated RuntimeError subtype and shim-only translation are tactical enforcement inside existing supervisor/ownership boundaries, with no new executor/admission framework, source-category or graph decision.

## tasks Round 2 — 2026-11-23

Verdict: PASS; revised tasks frozen after declarative clarifications.

### Yellow addressed

- Added approved task 6.0 before final verification, preserving the original 18 IDs/history and keeping the total at 19.
- Named `README.md` as the affected usage-documentation target.
- Extended the focused command with trusted scheduled-spawn and scheduler failure-classification coverage; require any additional new regression files in the final focused run.
- Kept queue/drain and typed-refusal work in small prospective red/green slices, with existing warning 1–2 evidence reconciled honestly.
- Reopened 6.2–6.4; earlier green results remain historical rather than fresh verification of later warning fixes.

### Red outstanding

None. All revised planning batches passed review; their Git checkpoint on main remains required before queue-first/refusal implementation.

## Revised planning validation clarification — 2026-11-23

The initial strict validation found one requirement-length warning in the accepted-queued-cancellation requirement. Split the queue-before-drain wording into its own requirement with its existing scenario, preserving all reviewed semantics rather than weakening validation. Strict validation must be rerun before the planning Git checkpoint.
