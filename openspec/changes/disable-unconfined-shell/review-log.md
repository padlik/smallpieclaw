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

## proposal Round 4 — 2026-09-25

### 🔴 Fixed

- None. This round's changes are proposal-scope clarification only; downstream design, specs, and tasks remain frozen pending their required unfreeze/review sequence.

### 🟡 Addressed

- The `nsjail-shell-sandboxing` Modified Capabilities entry now explicitly maps config-file integrity outside jail-writable mounts and the Linux Lima proof. Proposal Impact/What Changes language records the boundary and test intent, preserving ADR-0015 and the no-host-fallback decision.

### 🟡 Outstanding

- The downstream design/specs/tasks unfreeze and review chain is still required before the implementation contract/checklist can be considered updated.
- The concrete relocation of static and per-call config files must be specified downstream, including resolving config paths currently under jail-writable `/tmp`, and the Linux regression must verify those exact paths cannot be modified by jailed commands. This has not been implemented or validated in this proposal-only round.

### 🔴 Outstanding

- None.

## proposal Round 5 — 2026-09-25

### 🔴 Fixed

- None. The reviewer reported no serious proposal finding outstanding; the proposal batch is frozen after this round.

### 🟡 Addressed

- The approved config-integrity decision is explicitly mapped under `nsjail-shell-sandboxing`: generated static and per-call configs stay outside jail-writable mounts, and Linux Lima coverage proves jailed commands cannot alter config used by later launches.

### 🟡 Outstanding

- Later batches must unfreeze and review the design/spec/tasks in sequence. The design must name a concrete host-private config location, account for current static config under the writable session `/tmp` and per-call config in default system temp, fail closed if the location is unsafe, and define Lima coverage for hostile writes plus a subsequent launch/re-probe. This design batch now records that decision, but still requires its own review; specs, tasks, and implementation remain downstream obligations and are not claimed complete here.

### 🔴 Outstanding

- None.

## design Round 2 — 2026-09-25

### 🔴 Fixed

- None. The design reviewer found no serious design finding outstanding; the design batch is frozen after this round.

### 🟡 Addressed

- The design now resolves static and per-call nsjail configs to a dedicated owner-only host-private XDG state directory, distinct from session scratch and outside writable mount sources and aliases. It specifies fail-closed behavior when that location cannot be verified and a Lima hostile-write/subsequent-launch/re-probe validation strategy.

### 🟡 Outstanding

- The spec batch must carry this as a normative integrity requirement and observable scenarios; the current spec delta records that contract and awaits its own review. The tasks batch and implementation still need their ordered unfreeze/review steps, with tests for safe-path selection, unsafe-path refusal, jail non-mounting, denied tampering, and successful later launches/re-probes. These downstream items are not claimed implemented or validated by the design review.

### 🔴 Outstanding

- None.

## specs Round 3 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious spec issue outstanding; the shell sandboxing requirement and scenarios preserve the existing backend-availability, loss, nonzero-exit, and timeout contract.

### 🟡 Addressed

- The spec now normatively requires owner-only static and per-call nsjail configs in a verified host-private path outside every jail-writable mount source and read-write alias; unsafe storage disables shell without writable-temp or host-shell fallback.
- Linux Lima observables cover config-directory non-mounting, denied jailed overwrite attempts, unchanged host config, and a successful subsequent probe and confined command.

### 🟡 Outstanding

- The tasks batch must provide separately reviewable safe-path/overlap tests, fail-closed implementation and integration work, and the dedicated Lima adversarial smoke task. The checklist now includes these as unchecked tasks; implementation and smoke validation remain outstanding. Preserve the existing timeout contract explicitly: nsjail `time_limit` returns exit 137 with a timeout message and does not mark capability lost.

### 🔴 Outstanding

- None.

## tasks Round 2 — 2026-09-25

### 🔴 Fixed

- None. The reviewer found no serious tasks issue outstanding; the task batch is ready for parent review.

### 🟡 Addressed

- Added separate unchecked tasks 1.8/1.9 for host-private nsjail config path/overlap tests and fail-closed implementation/integration, plus task 5.4 for the Linux Lima non-mounting, hostile-write, unchanged-config, and subsequent-probe/command smoke checks.
- Clarified task 5.2 to retain the existing `time_limit` contract: exit 137 with a timeout message and capability remains available.
- The reviewer's recommendation to include `tests/test_nsjail_config.py` in the task 6.1 focused regression command was addressed in this same pass. No tests were run as part of this documentation-only edit, and none are claimed to pass.

### 🟡 Outstanding

- Implementation and test execution remain unchecked downstream work; strict change validation and integrated tests are for the parent validation lane.

### 🔴 Outstanding

- None.

## proposal Round 6 — 2026-09-26

### 🔴 Fixed

- None. Proposal Round 6 passed review and is frozen; this design amendment records the approved N1 session-scratch hardening and stderr/loss-classification clarifications without reopening or editing the proposal.

### 🟡 Addressed

- The proposal commits to a host-private session `/tmp` source outside other jail-writable mounts, an adversarial Linux Lima rename-plus-symlink-swap regression, and preserving the existing config-integrity and no-host-fallback boundaries.
- The proposal maps `main.py` session-source placement, the `nsjail_config.py` mount invariant, and Lima proof under `nsjail-shell-sandboxing`; ordinary child stderr is not backend-loss evidence.

### 🟡 Outstanding

- The design now carries the approved invariant and diagnostic/loss distinction and must receive its own review before downstream specs/tasks are unfrozen. Implementation and adversarial Lima validation remain later obligations; this proposal review does not claim them complete.

### 🔴 Outstanding

- None.

## design Round 3 — 2026-09-26

### 🔴 Fixed

- The reviewer identified should-fix gaps in treating nsjail supervisor diagnostics as trusted evidence, relying on an unsupported minimum-version claim for `--log`, and proving child isolation from log artifacts.

### 🟡 Addressed

- Decision 2 now includes a third host-private artifact class: per-call supervisor diagnostic logs, mode `0600`, co-located in the verified agent-scoped `state_home/nsjail-config` directory and subject to the same ownership, non-jail-mount, and alias checks.
- Decision 8 now uses the installed binary's actual `--log FILE` flag and real startup probe rather than claiming a minimum nsjail version; it distinguishes nsjail diagnostics from untrusted child stdout/stderr and specifies log-open/fallback probe behavior and loss classification tests.
- Risks and validation plans now require Lima log-write/forge denial with unchanged host logs, log-open-failure/fallback probing, and the separate child-output versus backend-loss cases. The proposal received only a declarative artifact-set note; its frozen decisions were not changed.

### 🟡 Outstanding

- Design Round 3 awaits reviewer re-review. Frozen specs, tasks, ADRs, and implementation remain downstream work; no implementation or Lima result is claimed complete.

### 🔴 Outstanding

- None.

## specs Round 4 — 2026-09-27

### 🔴 Fixed

- None. Specs Round 4 passed review and is frozen.

### 🟡 Addressed

- The nsjail-shell-sandboxing delta now normatively covers isolated session scratch placement and per-build rejection of unsafe/changed sources, including the adversarial rename-plus-symlink swap and subsequent-launch Lima proof.
- Added the separate private per-call supervisor-log contract and probe failure on unavailable log separation/stderr fallback; specified child stderr (including `[E][`-like text), nonzero exits, and timeout as non-loss, while retaining trusted setup/launch/re-probe loss handling and timeout exit 137.

### 🟡 Outstanding

- The task batch must provide test-first implementation work and separate Lima adversarial tasks for scratch-source swapping and supervisor-log forging/fallback. Implementation and Linux Lima results remain outstanding; none are claimed complete.

### 🔴 Outstanding

- None.

## design Round 4 — 2026-09-27

### 🔴 Fixed

- None. Design Round 4 passed review and is frozen.

### 🟡 Addressed

- The approved design contract keeps session scratch separate from other writable mount sources and adds per-call supervisor diagnostics as a third host-private artifact class; installed nsjail support is established by the real probe rather than a version-floor assumption.
- This specs batch carries those decisions into normative requirements and testable scenarios for unsafe/changed scratch sources, rename-plus-symlink swap denial and subsequent launch, private `0600` supervisor logs, log-open/fallback probe failure, child stderr/nonzero/timeout as non-loss, and trusted runtime loss classification.

### 🟡 Outstanding

- The spec batch awaits its own review. Tasks, implementation, and Linux Lima adversarial validation remain downstream work; none are claimed complete by this documentation update.

### 🔴 Outstanding

- None.

## tasks Round 3 — 2026-09-27

### 🔴 Fixed

- None. The reviewer passed the tasks batch with no serious outstanding findings.

### 🟡 Addressed

- Tasks 1.10–1.13 and 5.5–5.6 cover test-first work for session-source isolation and private supervisor logs, including separate Lima adversarial tests; the original 5.2/5.4 tasks remain intact.
- Task 6.1 includes `tests/test_main.py` in the focused regression command.

### 🟡 Outstanding

- Optional granularity note: task 1.13 may be split during implementation if its scope proves too broad. Implementation and Lima validation remain pending.

### 🔴 Outstanding

- None.
