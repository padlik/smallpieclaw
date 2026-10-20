# Planning review log

## proposal Round 1 — 2026-10-13

Verdict: NEEDS_FIX. No artifact is frozen.

### 🔴 Outstanding
- P-1: Mark breaking configuration and behavior changes explicitly.
- P-2: Capture cloud-only Ollama, manual context-switch rejection, admission-time model inheritance, obsolete selectors, and exact agent retry defaults.
- P-3: State optional request-parameter omission and preserve graph extraction's fixed tuning as an explicit exception.
- P-4: Remove chat-model credential/endpoint fallback from independent embedding configuration.
- P-5: Define the current-call retry boundary without prohibiting retries after earlier completed tool calls or resetting the budget.

### 🟡 Addressed
- Unknown context-limit decision clarified with the user: retain `agent.ctx_max_tokens` as an explicitly assumed local budget; do not present it as detected model capacity. Required unknown output limits still fail clearly.
- Narrowed capability list is justified; unchanged scheduling, context-ownership, prompt-file, and secret contracts need no gratuitous deltas.

## proposal Round 2 — 2026-10-13

Verdict: PASS. `proposal.md` is frozen; no serious outstanding findings.

### 🔴 Fixed
- P-1 through P-5: breaking changes, omitted decisions, graph request-parameter exception, independent embeddings, and current-call retry boundaries are explicit and consistent with the brief.

### 🟡 Addressed
- Unknown context limits retain the user-approved assumed agent budget; unknown required output limits fail clearly.
- `/models` already exists; its new contract will be specified rather than introducing a duplicate command.
- Stale discovered entries remain selectable; later artifacts must preserve that explicit brief commitment.

### 🔴 Outstanding

None.

## design Round 1 — 2026-10-13

Verdict: NEEDS_FIX. Proposal remains frozen; design is not frozen.

### 🔴 Outstanding
- D-1: Specify same-form transient retries and terminal unexpected-exception behavior, with JSON routing only for locally unsupported native calls.
- D-2: Define known optional-output reserves and the distinct combined-window versus input-only budgeting rules.
- D-3: Define graph extraction parameter compatibility and fixed-cap failure outcomes.
- D-4: Specify removal/rejection of per-call generation knobs and scoped ADR-0007 supersession.
- D-5: Bind vision requests to the run's pinned model and define true/false/unknown capability outcomes without model switching.

### 🟡 Addressed
- Changes can be contained in the current design batch; no frozen-proposal decision requires reopening.

## design Round 2 — 2026-10-13

Verdict: NEEDS_FIX. Proposal remains frozen; design is not frozen.

### 🔴 Fixed
- D-1 through D-5: native error paths, output reserves, graph exception outcomes, obsolete per-call knobs, and pinned-model vision selection are explicit.

### 🔴 Outstanding
- D-6: Retain the same attempt state through empty/parse/native/JSON/human-wait loops and define the logical request lifecycle.
- D-7: A known separate input ceiling is context metadata; do not impose an assumed agent combined window on top of it.

### 🟡 Addressed
- Both remaining fixes are mechanics within the frozen proposal, not new product decisions.

## design Round 3 — 2026-10-13

Verdict: PASS. `design.md` is frozen.

### 🔴 Fixed
- D-6: Logical request lifecycle and persisted shared attempt state cover all control-flow loops without implicit reset.
- D-7: Known separate input limits supersede assumed local context budgeting; output reserves constrain only combined windows.
- Earlier D-1 through D-5 decisions remain intact.

### 🟡 Addressed
- Carry explicit provider C/T/O field mappings, official-cloud-only Ollama, and checkpoint attempt-state persistence into the specification deltas.

### 🔴 Outstanding

None.

## specs Round 1 — 2026-10-13

Verdict: NEEDS_FIX. Proposal and design remain frozen; specs are not frozen.

### 🔴 Outstanding
- S-1: Strict validation fails on 11 requirement statements longer than 500 characters; shorten or split without suppressing validation.
- S-2: Replace or remove exact baseline requirement titles instead of adding renamed contradictory duplicates; merge duplicate operation sections.
- S-3: Restore unaffected baseline scenarios and concrete XDG values; correct mismatched scenario titles and add missing budget cases.
- S-4: Add observable scenarios for startup, rediscovery, obsolete selectors, graph exceptions, migration, self-awareness, retry boundaries, and callback limits.

### 🟡 Addressed
- Corrections are confined to the current specs batch; no frozen-proposal or design decisions require reopening.

## specs Round 2 — 2026-10-13

Verdict: NEEDS_FIX. Proposal and design remain frozen; specs are not frozen.

### 🔴 Fixed
- S-1 through S-4: strict validation, baseline delta operations, preserved regression detail, and missing decision coverage are resolved in substance.

### 🔴 Outstanding
- R-1: Add Migration statements to both removed context/vision requirements.
- R-2: Rename five scenarios whose titles contradict their rejection/migration behavior.
- R-3: Add concrete unknown optional-output and known required-output budget scenarios.

### 🟡 Addressed
- Remaining corrections fit the current specs batch; frozen artifacts remain unchanged.

## specs Round 3 — 2026-10-13

Verdict: NEEDS_FIX. Proposal and design remain frozen.

### 🔴 Fixed
- R-1: Both removed requirements include migration guidance.
- R-3: Optional unknown-output and required-output budget cases have concrete scenarios.

### 🔴 Outstanding
- R-4: Delete five obsolete, contradictory scenario blocks retained next to their correctly titled replacements (R-2 follow-up).

## specs Round 4 — 2026-10-13

Verdict: PASS. The eight specification deltas are frozen.

### 🔴 Fixed
- R-4: Five obsolete scenario blocks are absent; correctly titled replacement scenarios remain.
- Installed CLI scenario-preservation rules require explicit REMOVED old requirements plus ADDED distinctly named replacements. Three contract replacements use these supported operations; strict validation passes without weakening rules.

### 🟡 Addressed
- Runtime callers stop sending obsolete tuning arguments; configuration migration separately discards legacy per-model tuning.

### 🔴 Outstanding

None.

## adr Round 1 — 2026-10-13

Verdict: NEEDS_FIX. New ADR drafts are not frozen; existing ADRs remain unchanged.

### 🔴 Outstanding
- A-1: Replace ambiguous "insufficient" metadata wording with the exact neither-C-nor-T condition for assumed local budgeting before final acceptance.

### 🟡 Addressed
- Sequence, manifest, partial supersession scopes, and unchanged historical decisions pass review.

## adr Round 2 — 2026-10-13

Verdict: PASS. ADR manifest and new records 0034–0036 are frozen.

### 🔴 Fixed
- A-1: Assumed local budgeting applies only when neither combined C nor separate input T is known, matching frozen design and specs.

### 🟡 Addressed
- Manifest lifecycle wording now records passed review and acceptance; prior ADRs remain unchanged.

### 🔴 Outstanding

None.

## tasks Round 1 — 2026-10-13

Verdict: NEEDS_FIX. The stalled review attempts produced no findings and do not count as completed review rounds. The fresh task-only review returned the findings below; tasks remain unfrozen.

### 🔴 Outstanding
- T-1: Explicitly trigger complete provider discovery at startup and test startup refresh.
- T-2: Gate explicit-model spawns, plan steps, and scheduled jobs when no valid main selection is active; scheduled failures still create a record and alert.

### 🟡 Addressed
- Accepted for correction: enumerate budget cases, preserve retry eligibility after earlier completed tools, state migration and verification safeguards, and fix retry-schema/example ordering.

## tasks Round 2 — 2026-10-13

Verdict: PASS. Implementation checklist is frozen; implementation remains unstarted.

### 🔴 Fixed
- T-1: Startup explicitly invokes complete all-provider discovery with the same refresh semantics as `/models` and concrete startup tests.
- T-2: Explicit-model spawn, plan, and scheduled admissions require an active main selection; scheduled failures preserve record creation and alerts.

### 🟡 Addressed
- Budget matrix, retry-after-earlier-tools boundary, migration preservation, lint/test safeguards, and retry-schema/example ordering are explicit.
- All 44 implementation tasks remain unchecked, uniquely numbered, and estimated at two hours or less.

### 🔴 Outstanding

None.

## proposal Round 1 — 2026-10-19 (scheduler/graph amendment)

Verdict: NEEDS_FIX. The user authorized reopening the planning chain; proposal, design, specs, ADR and tasks remain unfrozen for this amendment. Only proposal and exploration brief have been amended; implementation remains paused.

### 🔴 Outstanding
- R1-1: Unscoped rejection of legacy job fallback settings could permit scheduler-load or startup failure. Distinguish human agent-config rejection from ignored, runtime-warning-only legacy per-job settings; preserve job files.

### 🟡 Addressed
- Required clarifications for the next proposal revision: every scheduled admission requires valid active Main, including valid explicit selectors; stale discovered entries remain resolvable, unlike absent entries.
- Carry raw malformed-selector representation, per-job parse/validation containment and reload atomicity into design. Downstream artifacts intentionally await the ordered amendment reviews.
- Independent graph resolution, its fixed-cap budget semantics, scheduler-only pre-admission selector fallback, capability mapping and unchanged commitments otherwise passed this review.

## proposal Round 2 — 2026-10-19 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Proposal remains unfrozen; no downstream artifacts have been amended.

### 🔴 Fixed
- R1-1: Agent-config rejection is distinguished from runtime-warning-only handling of legacy per-job fallback settings; jobs never cause startup/load failure.
- All-scheduled-admission Main gating and absent-versus-stale catalog semantics are now explicit.

### 🔴 Outstanding
- R2-1: Exclude absent/None models from the malformed non-string category so default inheritance does not warn. Apply the same distinction in proposal and exploration brief.

### 🟡 Addressed
- Design must distinguish add-time inheritance from modify-time no-change, preserve exact nonblank selectors, classify whitespace-only selectors and define TOML persistence without null.

## proposal Round 3 — 2026-10-19 (scheduler/graph amendment)

Verdict: PASS. Amended proposal and exploration brief are frozen. Design, specs, ADR and tasks still await ordered amendment and review; implementation remains paused.

### 🔴 Fixed
- R2-1: Missing/None model values silently inherit on add/load/execution; modify omission/None means no change. Empty/whitespace-only strings and non-None nonstrings are malformed and warn only at execution.
- R1-1 remains closed: legacy per-job fallback settings cannot cause startup/load failure or enable cross-model retries.

### 🟡 Addressed
- The reviewer read all amended proposal and brief text without truncation. Main gating, stale-entry semantics, independent graph admission/budgeting and unchanged commitments passed.
- Design must settle lossless raw-selector representation, per-job parse-error containment, TOML omission for inherited models, persistence and reload atomicity.

### 🔴 Outstanding

None.

## design Round 1 — 2026-10-19 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Proposal and exploration brief remain frozen; design remains unfrozen. Specs, ADR and tasks have not been amended.

### 🔴 Outstanding
- R1: Reconcile save locking and parsed-value preservation throughout decisions, risks and tests. No write-outside-lock or comment-preservation promises may contradict the selected atomic protocol.
- R2: Keep graph pins fixed per component/backfill invocation; revalidation belongs inside resolution, not every extraction. Owned extraction clients remain per call.
- R3: Retain contained legacy JSON read support; settle TOML precedence and explicit mutation semantics so removed jobs cannot reappear.

### 🟡 Addressed
- Revision must refresh committed file hashes, prevalidate registrations, classify pre/post-attempt file failures, contain invalid overlap policies and bound/redact selector evidence.
- Settle startup/backfill catalog sources; carve graph out of ordinary Main gating; replace stale ADR follow-ups with scoped new-record requirements; keep one Open Questions section.
- The reviewer accepted the scheduled single-admission algorithm, trusted shape-only exemption, selector matrix, graph budgeting and pre-DB/dry-run boundaries.

## design Round 2 — 2026-10-19 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Design remains unfrozen; proposal and brief remain frozen.

### 🔴 Fixed
- Round-1 graph lifetime and legacy JSON read-only semantics passed, including prevention of shadowed legacy-job resurrection.
- Persistence locking, value-level preservation and round-1 follow-ups passed except for the reload hash distinction below.

### 🔴 Outstanding
- R2-1: Mutations compare disk with the stored hash; reloads compare disk with the hash of their own parsed input, not the old stored hash. Operator edits must load, and a stale reload must never overwrite a concurrent committed mutation. Add both tests.

### 🟡 Addressed
- Publish the document and committed hash together, or require successful reload before any further mutation after post-replace publication failure.
- Re-read legacy JSON during shadow-removal preflight; unreadable legacy data must not permit resurrection-prone removal. List/show should visibly mark legacy jobs read-only.
- Later ADR authors must explicitly settle the scoped ADR-0020 clause rather than leave conditional wording unresolved.

## design Round 3 — 2026-10-19 (scheduler/graph amendment)

Verdict: PASS. Design is frozen at b40d50422ba527db9bec28cc73352f2db49fda0c8a46b1ac3ee8e0e634420260. Proposal and brief remain frozen; specs, scoped new ADR records and tasks await amendment and review. Implementation remains paused.

### 🔴 Fixed
- R2-1: Reloads validate their parsed-input hash, while mutations validate the stored hash. Operator edits can load, and stale reloads cannot overwrite committed mutations.
- Post-replace incomplete publication requires successful reload before further mutation; legacy shadow removal checks current file state; list/show marks legacy jobs read-only.

### 🟡 Addressed
- Carry into specs: publication includes registrations as well as document/hash/view; incomplete registration publication sets RELOAD_REQUIRED and successful reload replaces the registration set completely, never exposing a mixed set as active.
- Include explicit pre-attempt refusal for the reload-required latch in mutation-disposition scenarios. Existing design already covers this behavior.
- Review reused the prior bounded lock-feasibility check and unchanged-section evidence; no implementation or full-suite verification was performed.

### 🔴 Outstanding

None.

## specs Round 1 — 2026-10-20 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Proposal, brief and design remain frozen; specs remain unfrozen. Strict CLI validation passed before this semantic review; ADR and tasks remain unchanged.

### 🔴 Outstanding
- R-1: Graph resolution must not gate on or mutate Main, but may copy current catalog/selection revisions read-only into RunModel provenance. Remove the contradictory prohibition on reading revision data.
- R-2: Concurrent mutations must revalidate the source document/generation under the commit locks and re-prepare stale candidates; matching disk/stored hashes alone cannot prevent a lost update. Add a two-mutation scenario.

### 🟡 Addressed
- Restore historical REMOVED requirement/scenario bodies verbatim from baseline, retaining Reason/Migration and operative ADDED replacements.
- Clarify manual-switch rejection/fit clearing, current-selection budget comparison and full R6 ticket identity; add A100k→B10k rejection→C20k plus explicit B-resume coverage without prohibiting approved preparation.
- Add legacy-fingerprint reload revalidation, conditional history-lock wording, run-ID-not-terminal-row terminology and explicit model-admission ordering. No substitution is allowed once the effective model is chosen, including credential failure before a pin is returned.
- Baseline scheduler requirements remain intact; targeted searches found no contradictions in the six unchanged deltas. No implementation verification was performed.

## specs Round 2 — 2026-10-20 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Specs remain unfrozen. Proposal, brief and design remain frozen; strict validation passed before this semantic round.

### 🔴 Fixed
- R-1/R-2 and Y1/Y3–Y7 from round 1 passed: read-only graph provenance, concurrent-mutation freshness, verbatim historical removals, reload fingerprint, conditional locks, lifecycle terminology and no credential-error substitution.
- Full R6 ticket identity, rejection-conditioned guard persistence, A→B→C comparison and B-resume isolation are covered.

### 🔴 Outstanding
- R-3: Smaller/larger switches must compare effective hard input budgets from the existing C/T/O cases, including labelled local fallback, not raw combined C alone. Align the manual-switch scenarios; no new budget algorithm.

### 🟡 Addressed
- A harmless reload race reports a reload-again instruction without newly setting RELOAD_REQUIRED; preserve any already-latched state.
- Ordinary compaction remains available after a guarded candidate is evaluated read-only and fits, and for unguarded requests, not before the guarded fit check.
- The reviewer read all three amended deltas in full. Baseline requirements and previously accepted scheduler/graph commitments passed; no source implementation was reviewed.

## specs Round 3 — 2026-10-20 (scheduler/graph amendment)

Verdict: PASS. All nine specification deltas are frozen. Proposal, brief and design remain frozen; scoped new ADR records and tasks await amendment/review. Implementation remains paused.

### 🔴 Fixed
- R-3: Switch size compares effective hard input budgets from the existing C/T/O cases. Assumed local C feeds the calculation, including reserve; it is not substituted directly for B. Input-only cases and preceding-selection comparison are explicit.
- Reload races preserve the existing latch state and report reload-again; guarded compaction cannot precede a successful read-only fit check.

### 🟡 Addressed
- Optional notation cleanup and an explicit invalid-budget comparison note are nonblocking; existing budget errors still reject the request. No further specification edits were made.
- Latest strict validation passed with zero errors/warnings (CLI 1.14.3). Frozen amended hashes: catalog 6c9b58843089541d0ce7c0f46470e7e2c08c77e617d6c6716cc088b6265f6d4b; context 40a70999f9ff721f156384aeafb34c32687a9ed73e00d424331f63a4961a006e; scheduler 6fd022f27bd3c4f4a6edc3a309dd78cfd7a0a0b310d50e41e01276247ef6bd49.
- Unchanged catalog and earlier accepted clauses reused valid review evidence. No implementation or full-suite verification was performed.

### 🔴 Outstanding

None.

## adr Round 1 — 2026-10-20 (scheduler/graph amendment)

Verdict: NEEDS_FIX. The three new record bodies passed content review; the manifest remains unfrozen pending a narrow link/wording correction. New records remain Proposed until review passes and parent acceptance is recorded.

### 🔴 Outstanding
- Manifest links at lines 54–56 must resolve from the manifest's own directory to CORE/adr, using ../../../adr rather than ../../adr. The earlier command-runner link-resolution claim was incorrect.

### 🟡 Addressed
- Replace the unsupported manifest reference to ADR-0021 recovery with the R6 _on_attempt disposition actually documented by ADR-0037.
- One-decision-per-record, scoped 0034/0036 supersessions, explicit no-new-0020 edge, preserved historical records and proposed API/status wording passed.
- Round 2 may be limited to corrected manifest links and wording. After content passes, status-only promotion and manifest completion require a metadata check, not another semantic round.

## adr Round 2 — 2026-10-20 (scheduler/graph amendment)

Verdict: PASS. Parent accepts the reviewed ADR content. ADR-0037–0039 decision bodies and the corrected manifest are frozen; status-only promotion to Accepted and manifest completion now proceed, followed by a mechanical metadata check. Implementation remains paused.

### 🔴 Fixed
- The three manifest links resolve from the manifest directory to existing CORE/adr records using ../../../adr. The unsupported ADR-0021 claim was replaced with the documented R6 _on_attempt disposition.

### 🟡 Addressed
- All three decision bodies passed round 1 and remain unchanged. Scoped partial edges and the explicit no-new-ADR-0020-edge determination remain accepted.
- Reviewer explicitly permits status/completion-only metadata changes without another semantic round. New record dates remain 2026-10-20; prior ADRs 0001–0036 stay immutable.
- Corrected command evidence supersedes the earlier incorrect link-resolution claim; this PASS authorizes no commit, merge, apply or implementation.

### 🔴 Outstanding

None.

## tasks Round 1 — 2026-10-20 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Tasks remain unfrozen; proposal, brief, design, all nine specs and accepted ADRs 0037–0039 remain frozen. No checkbox completion or implementation progress was authorized.

### 🔴 Outstanding
- R1: Tasks 6.3/12.19 must distinguish selections A/B/C from effective budgets 100000/10000/20000, preserve B-ticket resume isolation from current Main exemptions, and keep embedding accounting out of scope.
- R2: Add explicit implementation/test work for raw add/modify/load selector semantics and the shared bounded/redacted evidence representation, including canonical post-pin records.
- R3: Remove or correct the claimed parallel topological ordering that conflicts with actual prerequisite edges.

### 🟡 Addressed
- State the global 12.1 gate for every unchecked task except itself; final coverage/gates must follow every implementation/migration/documentation task without depending cyclically on later final gates.
- Clarify old parent-task closure versus new unit work, explicitly name per-job runtime containment, and split overly broad startup/profile proof units.
- Preserve all existing IDs/order/statuses and all checked-row bytes. Only append new unchecked tasks; distinguish R6 guard-ticket from attempt-disposition terminology.
- The reviewer found the main table acyclic and accepted the C2-injected versus F-production boundary; no source implementation was reviewed.

## tasks Round 2 — 2026-10-20 (scheduler/graph amendment)

Verdict: NEEDS_FIX. Tasks remain unfrozen; higher planning artifacts stay frozen. No implementation or checkbox completion was authorized.

### 🔴 Fixed
- Round-1 R1–R3 passed: exact guard selection/budget/resume semantics, raw-selector/evidence ownership, and authoritative acyclic dependency ordering. Global governance/final gates, parent-versus-unit ownership and proof sizing were clarified.

### 🔴 Outstanding
- R4: Explicitly assign legacy per-job fallback_models retention without load warning/drop to 12.2, and execution-time warning/ignore/no factory forwarding/no cross-model retry to 12.23, with named tests and both specification references.

### 🟡 Addressed
- Make 12.22 consolidate 12.9 WARN formatting into the shared helper and rerun those tests; do not retain duplicate formatters.
- Name missing-file first mutation and one safe operator-visible file-failure notice in 12.2; derive the complete omitted-table task list rather than preserving an inaccurate enumeration.
- Add 12.24 to final production-root proof references. Existing IDs/order/statuses remain unchanged; no new task IDs are needed for this revision.

## tasks Round 3 — 2026-10-20 (scheduler/graph amendment)

Verdict: PASS. Tasks are frozen at 6a5474cc9cd1d3fc5d83c4f8bbdc4634a1b53a7b8afc29fad391e3fbc2908f28. The full amended planning chain has passed review; implementation remains paused pending task 12.1 and separate user authorization.

### 🔴 Fixed
- R4: Task 12.2 retains legacy per-job fallback_models silently on load; 12.23 warns and ignores it only at execution, never forwards it to the factory and enables no alternate-model call. Both spec requirements and named tests are covered.
- Shared WARN formatter consolidation, missing-file/operator-notice ownership, the omitted-table task list and complete production-root proof references passed.

### 🟡 Addressed
- The reviewer permits splitting dense 12.2 work during later apply if necessary and suggests a primary test-file choice for 12.23. These are nonblocking execution refinements, not new policy or completion evidence.
- All 68 IDs/order/statuses are preserved; 19 checked rows retain their prior evidence and 49 tasks remain unchecked. No new implementation completion, commit, merge or apply permission is claimed.

### 🔴 Outstanding

None.
