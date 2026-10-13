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
