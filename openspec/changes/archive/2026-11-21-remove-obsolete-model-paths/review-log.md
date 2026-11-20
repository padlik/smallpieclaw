# Review log

## proposal Round 1 — 2026-11-19

### 🔴 Outstanding

- Replace the claim that no behavior relies on removed paths: ReAct actively uses the chat aliases.
- Map capability changes to actual canonical requirements, separating internal cleanup from contract changes.
- Define complete retirement of `agent.diagnose_empty_responses`, without removing context monitoring or empty-response classification/retries.
- Record narrow partial supersessions of ADR-0040's old-file notice and ADR-0009's alias compatibility; preserve accepted ADR history.
- Keep explicit graph configuration unchanged: ADR-0042 already superseded the catalog-dependent graph chain.

### 🟡 Addressed

- None yet; corrections requested before the next review.

## proposal Round 1 — correction status

### 🔴 Fixed

- C1: replaced the unused-behavior claim and explicitly migrated live ReAct calls and capability probing to canonical chat methods.
- C2: mapped changes to real catalog, native, XDG and narrowly scoped Telegram contracts; implementation-only removals no longer claim nonexistent requirements.
- C3: specified rejection of the retired diagnostic key through the existing removed-field validator, with remove-key guidance.
- C4: specified new, narrow ADR supersessions; accepted ADRs remain untouched.
- C5: identified graph cleanup as already superseded by ADR-0042; explicit graph configuration remains unchanged. The accepted API removal is intentionally marked breaking.
- C6: limited diagnostics removal to empty-response curl diagnostics; context monitoring and empty-response classification/retries remain unchanged.

### 🟡 Addressed

- Named the retry stubs and retained cancellation, retry policy, revisions, validators, preference quoting and actual-send gates.
- Clarified registry-only Telegram construction and authenticated stale legacy callback refusal.

### 🔴 Outstanding

- Awaiting the corrected proposal's second review; batch is not frozen.

## proposal Round 2 — 2026-11-19

### 🔴 Fixed

- Reviewer confirmed all six first-round critical issues are resolved.

### 🟡 Addressed

- Carry into design/specs/tasks: use a MODIFIED full XDG requirement, retain concrete live cancellation boundaries, update stale retry-helper comments, and add the legacy callback scenario under the existing models requirement.

### 🔴 Outstanding

None. Proposal batch frozen.

## design Round 1 — 2026-11-19

### 🔴 Fixed

- D1: capability probing now checks callable `chat_with_tools` without invoking it.
- D2: diagram distinguishes the client's two public methods from provider modules' `chat` function.
- D3: removed instructions to preserve a dead stub's cancellation check; existing live cancellation boundaries remain unchanged.

### 🔴 Outstanding

- Corrected design awaits a second review; batch is not frozen.

## design Round 2 — 2026-11-19

### 🔴 Fixed

- Reviewer confirmed D1–D3 are resolved; diagram, callable-only probe and existing cancellation boundaries agree with the proposal.

### 🔴 Outstanding

None. Design batch frozen.

## specs Round 1 — 2026-11-19

### 🔴 Fixed

- S1: scoped the two-method interface to chat only; native and embedding methods remain unchanged.

### 🟡 Addressed

- Reviewer confirmed complete preservation of original XDG, Telegram and obsolete-key scenarios outside the scoped changes.
- Carry into tasks: embedding-method preservation, no-send capability probing, diagnostic-key rejection with unchanged unrelated unknown-key handling, and authorized/unauthorized legacy callback regressions.

### 🔴 Outstanding

- Corrected native requirement awaits second review; specs are not frozen.

## specs Round 2 — 2026-11-19

### 🔴 Fixed

- Reviewer confirmed S1 is resolved and embedding methods remain unchanged.

### 🔴 Outstanding

None. Specs batch frozen.

## adr Round 1 — 2026-11-19

### 🔴 Fixed

- A1: distinguished pending combined-API removal from existing runtime behavior and removed the unsupported claim that ADR-0044 relies on the old-file notice.

### 🟡 Addressed

- Manifest now identifies the highest prior ADR ID and relevant context without claiming an unverifiable full-record review.
- Carry into tasks: explicit upgrade documentation for the retired file and `/models`.

### 🔴 Outstanding

- Corrected ADR context awaits second review; ADR batch is not frozen.

## adr Round 2 — 2026-11-19

### 🔴 Fixed

- Reviewer confirmed A1 and the manifest wording are resolved; narrow supersessions remain intact and prior accepted ADRs are unchanged.

### 🔴 Outstanding

None. ADR batch frozen.

## tasks Round 1 — 2026-11-19

### 🔴 Fixed

- T1: migrate ReAct calls, probe and affected test doubles before deleting aliases.
- T2: split settings-test migration into fixture/model, runtime/controller, and graph/startup/Telegram groups before deleting the combined API.

### 🟡 Addressed

- Retry-stub cleanup explicitly removes test-only seams without adding cancellation checks.
- Legacy-file regression explicitly asserts no startup notice.

### 🔴 Outstanding

- Corrected checklist awaits second review; tasks are not frozen.

## Strict validation — 2026-11-19

- Initial strict validation rejected a renamed historical native scenario identifier even though its canonical-method steps preserved the guarantee.
- Restored the original scenario title for delta matching; canonical-method steps and alias removal remain unchanged. This is a declarative identifier correction, not a behavior change or unfreeze.
- Strict validation will be rerun; no rule was weakened.

## tasks Round 2 — 2026-11-19

### 🔴 Fixed

- Reviewer confirmed T1–T2: callers migrate before alias deletion, and settings-test migration is split into three bounded groups before combined-API deletion.
- Reviewer accepted the historical scenario-title restoration as declarative, with no behavior change or unfreeze.

### 🔴 Outstanding

None. Tasks batch frozen. All planning batches passed review.

## Final planning validation — 2026-11-19

- `openspec validate remove-obsolete-model-paths --type change --strict`: exit 0, valid, no warnings.
- `openspec status --change remove-obsolete-model-paths --json`: all five required artifacts done; planning complete.
- `git diff --check`: exit 0. An additional scan covered untracked planning files: no whitespace or conflict-marker problems.
- Work is confined to new planning/report files; no tracked production files changed, no staging or commits, and all 18 implementation tasks remain unchecked.
- Python tests/static checks were not run for this documentation-only phase; they are required by the implementation checklist.
- Before apply, commit and merge the proposal state to `main` with explicit user authorization. No implementation or archive is authorized by this proposal workflow.
