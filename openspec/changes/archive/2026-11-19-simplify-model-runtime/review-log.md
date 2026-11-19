# Simplify model runtime: review log

## proposal Round 1 — 2026-11-01

Current batch: `proposal.md` and its glossary reference. No artifacts of this new change are frozen yet. The predecessor artifacts and accepted ADRs remain frozen and unchanged. Review baseline: `explore-brief.md` and `planning-route.md`.

### 🔴 Fixed
- Removed permission for embeddings to inherit matching chat-provider credentials or endpoints; both embeddings and extraction retain independent configuration.
- Added canonical `context-monitoring` and pending predecessor `native-tool-calling` capability changes, preserving native payload, pairing, and retry contracts.

### 🟡 Addressed
- Distinguished deliberate-selection preferences from disposable catalog-cache persistence consistently.
- Stated that ordinary explicit requests need no active Main or inventory membership, and that empty/all-failed discovery leaves commands and valid explicit requests available.
- Explicitly removed discovered capability metadata as an image/tool request gate while preserving real adapter/protocol limitations and provider errors.
- Restricted the glossary companion to terms actually used and restored its required table format.
- Made the proposal's publication route explicit: reviewed planning must be committed and merged to `main`, not merely present on a planning branch, before apply.

### 🔴 Outstanding
- The two serious findings have been corrected but await Round 2 review. The proposal batch is not frozen and no later stage is authorized.

## proposal Round 2 — 2026-11-01

### 🔴 Fixed
- The reviewer verified both Round 1 blockers resolved: independent configurations and the complete affected-capability inventory.

### 🟡 Addressed
- The reviewer verified separate persistence, explicit-request/bootstrap behavior, advisory capability metadata, glossary fidelity, and publication-to-main gates. No new findings remain.

### 🔴 Outstanding

None. **PASS: `proposal.md` and `proposal-glossary-reference.md` are frozen.** No further review round is required. Numerical policy defaults and storage details remain design work. Pause here; no design, specs, ADR, tasks, implementation, commits, or merges are authorized by this review.

## design Round 1 — 2026-11-01

Current batch: `design.md` and `design-glossary-reference.md`. Frozen baseline: proposal and its companion (proposal Round 2 PASS), predecessor artifacts, accepted ADRs. Review baseline: `explore-brief.md`, `planning-route.md`.

### 🔴 Fixed
- Unified Decision 1 and Decision 5 on the old combined `models-config.toml`: the runtime's only contact is an existence check plus one actionable notice naming the path with deliberate `/models` selection guidance; never read, converted, or written; bytes preserved.

### 🟡 Addressed
- Clarified compaction recurrence: later eligible sends may re-attempt after a successful compaction that remains over threshold (unless a failure disabled attempts for the run); an already-published summary is ordinary history; "no recursive summary compaction" prohibits only same-step recursion on the just-published candidate.
- Added corrupt/unparseable `model-selection.toml` handling: bytes preserved un-overwritten, actionable sanitized report, no automatic candidate choice or bootstrap while corrupt, commands and valid explicit requests remain available.
- Legacy JSON filename corrected against source: the state-home legacy file is `scheduler_jobs.json` (`xdg.py` `paths.scheduler_jobs`, wired at `scheduler.py:889`); `scheduled_jobs.json` is only the pre-XDG `data/` source copied by `migrate.py`. Parent source verification reversed the reviewer's suggested direction; the design now notes the ADR-0037 prose discrepancy for the later ADR stage.
- Glossary companion reduced to the one term actually used (`Scheduled run`) with context drawn from the design, and the required heading restored.

### 🔴 Outstanding
- Corrections await design Round 2 review. The design batch is not frozen and no later stage is authorized.

## design Round 2 — 2026-11-01

### 🔴 Fixed
- The reviewer verified all five Round 1 corrections resolved with evidence: unified old-combined-file treatment (existence check plus one actionable notice), compaction recurrence semantics, corrupt-preference handling, glossary companion fidelity, and the state-home legacy filename `scheduler_jobs.json` — the reviewer confirmed the parent's source-verified reversal against the actual constants (`xdg.py`, `scheduler.py:889`, `test_xdg.py:39`, README, ADR-0019).

### 🟡 Addressed
- Folded the reviewer's two non-blocking minors without a further round, per their suggested fixes: the test-plan parenthetical now scopes "no repeat" to the failing/empty/cancelled family and states the successful-case re-trigger rule; a deliberate `/models select` is defined to atomically replace corrupt preference bytes through the standing deliberate-write path (byte preservation binds automatic/runtime writes only), with manual repair and restart as the alternative.
- Aligned the preference risk row with Decision 1's startup notice (optional drift).

### 🔴 Outstanding

None. **PASS: `design.md` and `design-glossary-reference.md` are frozen.** No further review round is required. Pause here; no specs, ADR, tasks, implementation, commits, or merges are authorized by this review.

## specs Round 1 — 2026-11-01

Current batch: nine delta specs and their nine glossary companions. Frozen baseline: proposal, design, predecessor artifacts and accepted ADRs. Review baseline: `explore-brief.md` and `planning-route.md`; requirement operations checked against canonical specifications composed with the pending predecessor deltas.

### 🔴 Fixed
- None yet; corrections have been assigned to the existing spec author.

### 🟡 Addressed
- The reviewer confirmed all nine capability deltas, predecessor-first requirement names and operations, advisory discovery, independent auxiliary clients, automatic compaction, strict scheduled selectors, retained native validation, scoped legacy removal, and glossary fidelity.
- Four clarifications requested: allow diagnostic identity/liveness in count snapshots; reject malformed connections rather than every padded upstream selector; specify the frozen textual `/models select` route; supply exact final canonical Purpose wording and its later synchronization disposition.

### 🔴 Outstanding
- Manual `/compress` incorrectly acquires the automatic compactor's partitioning, prompt and run-pinned client. Preserve the existing manual successful summary shape, idle-command exclusion and owned-client lifetime; limit changes to safe successful publication and failure preservation. Correct the current batch and obtain Round 2 review before freezing.

## specs Round 2 — 2026-11-01

The interrupted launch produced no review verdict or counted round. The resumed existing reviewer session completed this round against the corrected specs and frozen baselines.

### 🔴 Fixed
- The reviewer verified manual compression preserves its existing conversation prompt, single-entry successful result, unchanged working memory, and independently admitted owned-client lifetime. Failed or empty summaries preserve history; completed-run clients are not borrowed.

### 🟡 Addressed
- Verified consistent snapshot identity/liveness fields, exact upstream selector handling, textual uncached selection and corruption recovery, and exact canonical Purpose handoff for later authorized predecessor-first synchronization.
- Requirement names and delta operations remain compatible with the effective predecessor-first baseline. No frozen decision requires unfreezing.
- Incorporated the non-blocking busy-command clarification: manual compression refuses while an interactive operation holds the command lock, without summarization or history changes. The reviewer required no further confirmation round.

### 🔴 Outstanding

None. **PASS: the nine delta specs and their glossary companions are frozen.** Pause after focused validation; no ADR, tasks, implementation, commits, merges, archive or synchronization is authorized by this review.

## adr Round 1 — 2026-11-01

Current batch: the ADR manifest, new ADR-0040–0044, and their six glossary companions. Frozen baselines: proposal, design, specs, predecessor artifacts and accepted ADR-0001–0039. Review baseline: `explore-brief.md` and `planning-route.md`.

### 🔴 Fixed
- No serious findings. The reviewer found no frozen decision requiring unfreezing.

### 🟡 Addressed
- Clarified that ADR-0041 supersedes ADR-0038's restated Main prerequisite for explicit ordinary requests as well as explicit scheduled requests; implicit requests still require an active selection.
- Explicitly retired legacy JSON reload fingerprints in ADR-0044 while retaining TOML hashes, generations, locking and freshness checks; aligned the manifest's scoped edges.
- Replaced the unsupported cross-endpoint credential-leak rationale in ADR-0042 with verified implicit inheritance and configuration-coupling concerns.
- Clarified ADR-0043's replacement of metadata-derived required-output budgeting/rejection with a configured mandatory cap, retaining optional-cap omission.
- The reviewer verified consecutive record numbers, manifest links, house sections, scoped retained constraints, glossary fidelity, and planning-publication gates. These wording corrections require no further confirmation round.

### 🔴 Outstanding

None. **PASS: the ADR manifest, ADR-0040–0044 and their glossary companions are frozen.** Pause after focused validation; no tasks, implementation, commits, merges, archive, synchronization or publication is authorized by this review.

## tasks Round 1 — 2026-11-01

Current batch: `tasks.md` and its glossary companion. Frozen baselines: proposal, design, nine capability specs, ADR manifest and ADR-0040–0044, predecessor artifacts and existing accepted ADRs. Review baseline: `explore-brief.md` and `planning-route.md`.

### 🔴 Fixed
- No serious findings. No frozen artifact requires unfreezing.

### 🟡 Addressed
- Split preference transactions/publication from disposable cache persistence/recovery into independently verifiable tasks; preserved failure, corruption and secret-isolation assertions.
- Explicitly covered complete validated pagination, malformed/incomplete-page stale preservation, and valid empty inventory replacement without changing selection or pins.
- Corrected the configuration-example path to `config.toml.example`; retained its existing test reference.
- The final checklist contains 46 unchecked tasks in 11 groups. The reviewer confirmed all nine capability areas, dependency ordering, test preservation, separate automatic/manual compression, mandatory verification and publication/apply gates. No additional confirmation round is required.

### 🔴 Outstanding

None. **PASS: `tasks.md` and its glossary companion are frozen.** Planning is complete, subject to final focused validation. No implementation, commits, merges, archive, synchronization or publication is authorized by this review; the agreed exact-planning-on-`main` gate still precedes any separately authorized apply.

## design Round 1 — 2026-11-14 — list-only selection revision

Current batch: `design.md`. Frozen baselines: unchanged proposal and `explore-brief.md`, plus the user's approved list-only Telegram selection decision. Design and downstream ADR/spec/task artifacts were unfrozen for this decision-level revision; downstream edits await their own reviewed batches.

### 🔴 Fixed
- No blocking findings. Typed selection is replaced by candidate buttons in both normal correction and corrupt-preference recovery; the discovery-dependent empty-cache trade-off is explicit.

### 🟡 Addressed
- Preserved atomic persistence before publication, failed-save preservation, no automatic fallback while preferences are corrupt, valid uncached saved selectors and independent runtime/service/scheduled selector paths.
- No glossary terms were introduced. The optional clarification of “other hidden route” was not required: the reviewer confirmed its Telegram recovery context.

### 🔴 Outstanding

None. **PASS: revised `design.md` is frozen.** ADR-0045 will partially supersede only ADR-0040's typed Telegram selection route; accepted ADR-0040 remains unchanged. Specs and tasks await their own batches. No implementation or commits authorized by this review.

## adr Round 1 — 2026-11-14 — list-only selection revision

Current batch: ADR-0045, its glossary companion and `adr.md`. Frozen baselines: unchanged proposal and explore brief, plus the revised design.

### 🔴 Fixed
- No blocking findings. ADR-0045 partially supersedes only the typed Telegram entry; ADR-0040's body and status remain unchanged.

### 🟡 Addressed
- Preserved button token scope/expiry, atomic write-before-publish, failed-save state, corrupt-preference recovery, uncached saved selectors and independent runtime/service/scheduled admission.
- Documented discovery-dependent empty-cache recovery and refresh-only guidance. No new glossary terms; existing house style retained.

### 🔴 Outstanding

None. **PASS: ADR-0045, its glossary companion and revised `adr.md` are frozen.** Specs and tasks await their own reviewed batches. No implementation or commits.

## specs Round 1 — 2026-11-14 — list-only selection revision

Current batch: `specs/telegram-command-surface/spec.md`. Frozen baselines: proposal, explore brief, revised design and ADR-0045/manifest.

### 🔴 Fixed
- None in this round.

### 🟡 Addressed
- Reviewer confirmed button-only selection, cache-only refresh, atomic corrupt-preference recovery, token rules and uncached saved-selector behavior.

### 🔴 Outstanding

- The typed-text non-selection scenario combines an automatic choice with an existing preference file. Separate saved selection with known bytes from automatic selection with no file; preserve bytes or absence in the outcome.

**FAIL: spec remains unfrozen pending this bounded correction and re-review.** Tasks remain pending; no code changes.

## specs Round 2 — 2026-11-14 — list-only selection revision

Current batch: bounded recheck of the Telegram spec against the same frozen baselines.

### 🔴 Fixed
- The typed-text non-selection scenario now distinguishes saved selection with known preference bytes from automatic selection with no preference file, and preserves bytes or absence respectively.

### 🟡 Addressed
- Renamed the existing token-rejection scenario to “Expired or unknown selection token is rejected”, without changing its behavior.

### 🔴 Outstanding

None. **PASS: revised Telegram spec is frozen.** Round 1's remaining checks still apply; the reviewer confirmed the corrected file hash. Tasks await their own batch. No code changes or commits.

## tasks Round 1 — 2026-11-14 — list-only selection revision

Current batch: task 9.2 repurposed in place and task 11.1 documentation wording. Frozen baselines: proposal, explore brief, revised design, ADR-0045/manifest and revised Telegram spec.

### 🔴 Fixed
- No blocking findings. The reviewer confirmed button-only selection coverage, corrupt-preference recovery, cache-only refresh and preservation of generic assertions when guidance tests change.

### 🟡 Addressed
- The reviewer recommended spelling out persistence before active-selection publication or success reporting in task 9.2. This is a declarative alignment with the already-frozen spec, not a new policy.
- Task count remains 46 with 37 complete. Completed 9.1 history and task 9.3 remain unchanged.

### 🔴 Outstanding

None. **PASS: revised tasks are frozen**, with the publication-order wording clarification recorded above to be applied before final validation. Planning revision review is complete; code still contains the old guidance until repurposed 9.2 is applied. Revised planning must reach `main` before apply. No commits, merges or implementation authorized by this review.
