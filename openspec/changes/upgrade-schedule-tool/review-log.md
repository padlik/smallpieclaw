## proposal Round 1 — 2026-09-11

Scope: reviewed the newly created `proposal.md` only. `design.md` and
`specs/schedule-tool-surface/spec.md` were read solely to confirm the
proposal→downstream direction is coherent; the verdict applies to
`proposal.md`. Nothing is frozen yet (round 1, proposal batch). No
`explore-brief.md` exists — reviewed against the agreed fallback context
(7 confirmed commitments + grounding facts).

### 🔴 Fixed
(none — no fix loop ran this round)

### 🟡 Addressed
- **`show` field enumeration drops `enabled`** — `proposal.md:10` lists the
  `show` metadata as `raw cron, run_at, next_run, is_running, source, notify,
  model, preserve_context, context_max_messages, overlap_policy, last_run,
  last_error`. The fallback commitment (item 2) and the delta spec
  (`specs/schedule-tool-surface/spec.md:83`, scenario `:89`) both include
  `enabled`. Because paused/enabled state is central to this change (run_now
  paused-override, pause/resume verbs), it belongs in the illustrative list.
  Non-blocking: the phrase "full single-job metadata" is a superset and the
  spec is authoritative and downstream, so implementation will not miss it.
  Author should add `enabled` (and optionally `enabled`/`is_running` as the
  paired state pair) to the parenthetical for accuracy.
- **`action not in ("list","show")` predicate in What Changes** —
  `proposal.md:15` states the depth-guard rule as a literal code predicate.
  The behavioral meaning (read-only `list`/`show` allowed at all depths; all
  mutating actions blocked at `caller_depth >= 1`) is present, so this is
  cosmetic HOW-leak in a what/why doc. Design D5 already carries the exact
  predicate; optional to rephrase behaviorally in the proposal.
- **Impact function name `_format_schedule`** — `proposal.md:34` names
  `tool_formatting.py — _format_schedule`. Correct module per AGENTS.md
  ("Pure presentation helpers for tool calls/results"); the exact symbol was
  not verified in this read. Confirm the function name at apply time. Not a
  proposal defect.
- Verified fine: all 7 commitments are captured with no contradictions and no
  scope creep — Option A modify + pause/resume verbs (`:9`,`:16`); show +
  compact list + sub-agent allowlist (`:10`,`:15`); modify partial-patch with
  preserved fields, immutable tag, rejected `enabled` (`:9`); run_now paused
  override Option 2 (`:11`); run_now explicit async/supervised/notify/show/
  once-removal semantics (`:12`,`:14`); exposed `context_max_messages` +
  `overlap_policy` with validation (`:13`); backward compatibility (`:16`).
- Verified fine: Capabilities lists exactly two — new `schedule-tool-surface`
  (`:22`) and modified `telegram-progress-panel` (`:26`) — matching the delta
  specs on disk. No missing or extra capability.
- Verified fine: Impact section maps to the real repo layout
  (`builtin_tools/schedule.py`, `schemas.py`, `descriptors.py`, `scheduler.py`,
  `tool_formatting.py`, tests, docstring).
- Verified fine: Why (`:5`) states the three real problems — lossy/blind
  remove+add, invisible params, misleading `run_now`.
- Verified fine: rejected alternatives cannot leak back in — `add` unchanged
  (`:16`) closes Option C upsert-on-add; `enabled` rejected in modify (`:9`)
  closes Option B; the enumerated updatable-field set (`:9`) excludes legacy
  `hours`/`minutes`/`time`; run_now positive statement (`:11`) closes Option 1.
  The detailed rejections correctly live in design.md, not the proposal.
- Verified fine: format follows the intent-driven template (Why / What Changes
  / Capabilities / Impact), concise, no leftover template comments.

### 🔴 Outstanding
(none — the proposal batch passes)

### ⚖️ Verdict
PASS. `proposal.md` is ready to freeze. Two 🟡 awareness items (add `enabled`
to the `show` enumeration; optionally rephrase the depth-guard predicate) are
declarative/cosmetic and can be folded via soft-freeze without unfreezing.

(Post-review note by main agent: both 🟡 items were applied as declarative
soft-freeze edits — `enabled` added to the `show` metadata list, and the
depth-guard line rephrased behaviorally. `proposal.md` is now frozen.)

## design Round 1 — 2026-09-11

Scope: reviewed the newly created `design.md` for consistency with the FROZEN
`proposal.md` and against the 7 user-approved commitments. `specs/` read only
for direction-coherence (not reviewed). Verdict applies to `design.md` only.

### 🔴 Fixed
(none — no fix loop ran this round)

### 🟡 Addressed
- **D4 show_job field parity is under-specified vs the frozen proposal** —
  `design.md:79` defines `show_job(tag)` as returning "the per-job dict
  `list_jobs()` already builds, plus `created_at`." The frozen proposal's
  `show` metadata list now explicitly names `enabled`, and the change's whole
  premise is that `list` is compact while `show` is full. If `list_jobs()`'s
  dict does not already carry `enabled`, `last_run`, and `last_error`, D4 as
  written would silently omit them from `show`, contradicting the frozen
  proposal. Grounding calls `list_jobs()` a "full meta dict," so it likely
  does carry them — but D4 should state the guaranteed `show` field set (or
  confirm list_jobs carries `enabled`/`last_run`/`last_error`) rather than
  defining `show` by whatever `list_jobs` happens to build. Non-blocking: the
  downstream spec (`specs/schedule-tool-surface/spec.md:83`) enumerates the
  full field set and will drive the implementation.
- **Missing risk: `run_now` + `overlap_policy = skip` + in-flight run =
  silent skip reported as "triggered."** — D3 (`design.md:73`) correctly keeps
  the overlap policy applied to manual triggers. But a `skip`-policy job that
  is `run_now`'d while a run is in flight will hit the overlap check
  (`scheduler.py:696-705`) and skip, while `exec_schedule` returns immediately
  reporting the job was triggered. This re-creates the exact "reports success
  but silently did nothing" failure mode the proposal's Why sets out to kill
  (the paused no-op). The Risks section notes the paused-override case but not
  this one. Recommend a Risks entry and/or `run_now` output wording that
  surfaces an overlap-skip outcome (or an explicit "accepted" rationale).
  Non-blocking — D3's decision is sound; this is a missing risk/output-clarity
  note, not a decision conflict.
- **Option C (upsert-on-`add`) not listed among rejected alternatives** —
  D7 (`design.md:95`) handles `enabled`-in-modify and implicitly reflects
  Option B via "per the Option-A decision," but Option C is absent. Since
  `add` semantics are unchanged and `modify` is a separate action, nothing
  reintroduces upsert; still, a one-line rejection (or a Non-Goal noting `add`
  stays create-only) would close the loop. Minor completeness only.
- Verified fine: every one of the 7 commitments has matching decision-level
  detail — D1/D2 (modify_job in Scheduler, partial-patch field-merge,
  `_resolve_schedule` re-validation, `schedule.clear`+`_register_job`
  re-registration, `created_at`/`last_run`/`last_error`/`source`/history
  untouched); D3 (`run_now` `manual=True` threaded past the `enabled` check,
  overlap still applies, scheduled firings keep the check, output notes
  paused); D4 (`show_job` accessor, `list` stays compact); D5 (depth-guard
  allowlist `("list","show")`, fails closed — matches the frozen proposal's
  behavioral allowlist statement); D6 (tool-layer `overlap_policy`
  validation, TOML permissive); D7 (enabled-rejection + empty-patch errors,
  both recoverable); D8 (schema/descriptor/brief updated together, do-not-
  emulate). No decision contradicts the frozen proposal.
- Verified fine: rejected alternatives carry rationale — internal remove+add
  and full-replace (D2), erroring on paused (D3), filtering `list_jobs` in the
  tool layer (D4), blocklist depth guard (D5), assembling the patch in
  `exec_schedule` (D1). Aligned with the grounding facts (resume_job uses
  `_register_job`; pause uses `schedule.clear`; `_run_job` enabled check at
  691-693; overlap at 696-705; once-job removal at 799-800/872-875).
- Verified fine: Risks are real and mitigations sensible — manual override of
  a forgotten paused job, patch during in-flight run (applies to next firing),
  stale 6-action calls, `modify` once-job+cron mixed updates guarded by
  `_resolve_schedule`, descriptor length. (Plus the one missing risk noted
  above.)
- Verified fine: Migration rollback is genuinely safe — no persistence-format
  change; `add_job` already accepts and round-trips `context_max_messages`/
  `overlap_policy` (scheduler.py:476-529), `show_job` is read-only, the
  `manual` flag is runtime-only, so a single revert leaves all job files
  readable by pre-change code.
- Verified fine: Open Questions / ADR compliance holds — `run_now` manual
  override still flows through `SubAgentSupervisor` with `SOURCE_SCHEDULED`
  (ADR-0005/0007) and `exec_schedule` stays a stateless leaf with all new
  state/locking/validation in `Scheduler` (ADR-0008). No ADR needs revisiting.
- Verified fine: the C4 component diagram's boundaries are accurate
  (exec_schedule leaf dispatcher; Scheduler owns `_jobs_meta`, locking,
  validation, persistence; supervisor path untouched but for the override
  flag) and consistent with ADR-0008. (ASCII pipe alignment around
  `design.md:24-27` is cosmetically ragged — not reported as a defect.)
- Verified fine: template compliance — Context / Goals-Non-Goals / Decisions /
  Risks-Trade-offs / Migration Plan / Open Questions all present; HOW detail
  stays at decision level (named methods/primitives, no full code blocks); no
  leftover template comments.

### 🔴 Outstanding
(none — the design batch passes)

### ⚖️ Verdict
PASS. `design.md` is consistent with the frozen `proposal.md`, covers all 7
commitments at decision level, documents its rejected alternatives, and
violates no in-force ADR. Three 🟡 items (clarify D4's guaranteed `show` field
set incl. `enabled`; add a risk/output note for `run_now`+`skip`+in-flight
silent skip; optionally list Option C) are declarative/awareness and can be
folded via soft-freeze without unfreezing the proposal.

(Post-review note by main agent: all three 🟡 items were applied as declarative
soft-freeze edits — D4 now guarantees the full `show` field set; the
run_now+skip+in-flight risk was added with an output-wording mitigation;
Option C listed among Non-Goals/rejected alternatives. `design.md` is now
frozen.)

## specs Round 1 — 2026-09-11

Scope: reviewed the two new delta specs (`schedule-tool-surface`,
`telegram-progress-panel`) for format compliance, coverage of the 7
commitments, and consistency with the FROZEN `proposal.md` + `design.md`.
Diffed the telegram MODIFIED block against its main-spec source
(`openspec/specs/telegram-progress-panel/spec.md` lines 9-84) line by line.

### 🔴 Fixed
(none — no fix loop ran this round)

### 🟡 Addressed
- **`show` field list omits `created_at` vs frozen design D4** —
  `schedule-tool-surface/spec.md:83` enumerates the full `show` field set but
  does NOT include `created_at`; the "Show returns full metadata" scenario
  (`:85-89`) also omits it. Frozen design D4 was soft-freeze-edited to
  "guarantee the full `show` field set" and explicitly returns `list_jobs()`'s
  dict "plus `created_at`." (Note: the 7-commitment baseline also omits
  `created_at`, so design D4 and the commitment/spec disagree.) Reconcile:
  either add `created_at` to the `show` field enumeration + scenario to match
  frozen design D4, or confirm it is intentionally excluded and note D4
  over-specifies. Non-blocking, but a real cross-artifact drift.
- **Negative-path modify scenarios lack a GIVEN precondition** — "Modify empty
  patch rejected" (`:54-57`) and "Modify enabled rejected" (`:59-62`) reference
  `tag = "report"` with no `GIVEN a job report exists`. Because validation
  order matters (tag-existence vs patch-content), without the precondition the
  scenario could fail for the wrong reason (unknown-tag error instead of the
  intended error). Add `GIVEN a job report exists` to both so they test the
  intended error path. (The other GIVEN-less scenarios — unknown-tag,
  add-validation — are acceptable pure input-validation cases.)
- **`run_now` do-not-emulate guidance is descriptor-only, not specced** —
  commitment 5 lists do-not-emulate guidance; per design D8 it lives in the
  descriptor. The `run_now output semantics` requirement (`:116-118`) covers
  async/supervised/notify/show/once-removal but not do-not-emulate. Acceptable
  if intentionally descriptor-only (the `add`-surface requirement `:140-142`
  does spec "descriptor SHALL document…"); consider one assertion so it is
  enforceable. Minor.
- **`run_now` + `overlap_policy=skip` in-flight case (frozen design risk) has
  no spec scenario** — design now carries this risk (a `skip` job run_now'd
  during an in-flight run skips yet reports "triggered"). The `run_now` output
  requirement has no scenario for it. Optional: add a scenario or accept it as
  descriptor-only guidance. Minor.
- Verified fine: `schedule-tool-surface` covers all 7 commitments as testable
  requirements+scenarios — 8-action set with depth-guard allowlist and blocked
  -mutation error naming both read actions (`:5-29`); modify partial-patch
  (preserved fields, immutable tag, enabled-rejection, empty-patch, unknown-
  tag, once-job preservation, hidden params) (`:31-79`); show full metadata +
  unknown-tag + list-stays-compact (`:81-94`); run_now paused override +
  scheduled-firing skip (`:96-114`); run_now output semantics + once-removal +
  unknown-tag (`:116-138`); add param surface + invalid-overlap rejection
  (`:140-152`). No requirement is missing a scenario.
- Verified fine: no `schedule-tool-surface` requirement contradicts frozen
  proposal/design (partial-patch, allowlist, tool-layer overlap validation,
  supervised SOURCE_SCHEDULED run_now all match).
- Verified fine: format — `### Requirement:` with SHALL prose; `#### Scenario:`
  (4 hashtags); every requirement has ≥1 scenario; all under
  `## ADDED Requirements`.
- Verified fine: telegram-progress-panel requirement prose correctly updated
  (`:7`) to add the `schedule` `modify`/`show` `action + tag` rendering, and
  the two new scenarios (`:64-73`) are concrete and match the existing brief
  style (`schedule modify "daily-report"`, `schedule show "daily-report"`).
- Verified fine: scenarios are testable pytest cases against
  exec_schedule/scheduler/tool_formatting.

### 🔴 Outstanding
- **telegram-progress-panel MODIFIED block is STILL not a faithful complete
  copy — the pre-review fix was partial; 3 whole scenarios + 1 assertion from
  the main requirement are dropped and will be LOST at archive time.** The main
  requirement `Tool-call brief on Running line` (main spec lines 9-84) has 12
  scenarios; the delta MODIFIED block reproduces only 9 of them + the 2 new
  ones. The fix restored `shell_env_set`/`shell_env_list` but MISSED, in their
  original position (between `secret_get` and `schedule`):
    1. `#### Scenario: spawn_agent shows truncated task description`
       (main `:53-57`)
    2. `#### Scenario: wait_for_any_agent shows count when more than two`
       (main `:59-63`)
    3. `#### Scenario: wait_for_any_agent shows IDs when two or fewer`
       (main `:65-68`)
  AND the delta's `secret_get` scenario (`telegram delta :43-46`) dropped its
  final assertion `- **AND** the line does not show the secret value`
  (main `:51`) — a security-relevant assertion.
  Because a MODIFIED block replaces the whole requirement at archive, these
  must be restored verbatim. Required delta scenario order after `secret_get`
  (with its restored AND-line): spawn_agent → wait_for_any_agent(>2) →
  wait_for_any_agent(≤2) → schedule → shell_env_set → shell_env_list →
  schedule modify (new) → schedule show (new). This is exactly the archive-time
  data-loss pitfall flagged in the batch NOTE.

### ⚖️ Verdict
FAIL. `schedule-tool-surface` passes (only 🟡 items). `telegram-progress-panel`
has a 🔴: the MODIFIED requirement block still omits `spawn_agent` and both
`wait_for_any_agent` scenarios plus the `secret_get` "does not show the secret
value" assertion, all present in the main spec (lines 9-84). Restore them in
their original positions before the specs batch freezes, then re-verify against
the main spec. The 🟡 items (add `created_at` to `show` per design D4; add
GIVENs to the two modify negative-path scenarios; optional do-not-emulate /
overlap-skip scenarios) can be folded alongside.

(Post-review note by main agent: the 🔴 was fixed — spawn_agent and both
wait_for_any_agent scenarios restored verbatim in their original positions, the
secret_get security assertion restored. The 🟡s were folded: `created_at` added
to the show enumeration + scenario (matching frozen design D4); GIVEN
preconditions added to the two modify negative-path scenarios; the run_now
output requirement now notes the overlap policy (matching frozen design D3)
with a new in-flight scenario; do-not-emulate guidance intentionally remains
descriptor-only per frozen design D8. Round 2 review follows.)

## specs Round 2 — 2026-09-11

Scope: re-verified the specs batch after the fix loop. Re-diffed the telegram
MODIFIED block against the main requirement `Tool-call brief on Running line`
(`openspec/specs/telegram-progress-panel/spec.md` lines 9-84) scenario by
scenario, and checked the four `schedule-tool-surface` 🟡 fixes against frozen
proposal.md/design.md.

### 🔴 Fixed
- **telegram-progress-panel MODIFIED block is now a faithful complete copy** —
  verified all 12 original scenarios are present verbatim, in original order,
  before the two appended new ones:
    - `secret_get` (`delta :43-47`) — security assertion
      `AND the line does not show the secret value` RESTORED (`:47`).
    - `spawn_agent shows truncated task description` (`:49-53`) RESTORED,
      matches main `:53-57` verbatim.
    - `wait_for_any_agent shows count when more than two` (`:55-59`) RESTORED,
      matches main `:59-63` verbatim.
    - `wait_for_any_agent shows IDs when two or fewer` (`:61-64`) RESTORED,
      matches main `:65-68` verbatim.
    - `schedule` / `shell_env_set` / `shell_env_list` (`:66-80`) intact and in
      original order.
  Requirement prose (`:7`) equals main `:11` plus the single added sentence
  ("The `schedule` formatter SHALL render `modify` and `show` calls in the same
  `action + tag` style…"). Feature/Rule lines unchanged. The two new scenarios
  (`schedule modify` `:82-86`, `schedule show` `:88-91`) remain appended at the
  end. No dropped line, altered assertion, or reordering beyond the appended
  new scenarios. Round 1 🔴 RESOLVED.

### 🟡 Addressed
- **`created_at` fix applied** — `schedule-tool-surface/spec.md:85` show field
  enumeration now ends `…, `is_running`, and `created_at``; the "Show returns
  full metadata" scenario THEN (`:91`) now asserts `created_at`. Matches frozen
  design D4. Consistent with the frozen proposal's "full single-job metadata"
  (its parenthetical was illustrative; `created_at` as a superset member does
  not contradict it). RESOLVED.
- **GIVEN preconditions added** — "Modify empty patch rejected" (`:56`) and
  "Modify enabled rejected" (`:62`) now carry `GIVEN a job report exists`, so
  each tests the intended error path rather than potentially hitting unknown-
  tag. RESOLVED.
- **run_now overlap-subject fix applied and consistent with frozen D3** — the
  run_now output-semantics requirement prose (`:120`) now states the trigger
  is "subject to the job's overlap policy (a `skip`-policy job with a run in
  flight will skip the trigger — visible via `show`/`is_running`)", and a new
  scenario "run_now on skip-policy job with in-flight run notes overlap
  subject" (`:137-142`) was added. This matches design D3's accepted-risk
  framing (manual override bypasses the `enabled` gate but overlap still
  applies) and does not contradict the paused-override requirement (`enabled`
  and overlap are orthogonal gates). RESOLVED.
- **do-not-emulate remains descriptor-only** — recorded as a deliberate
  decision per frozen design D8, not a spec gap. Acknowledged; no change
  expected. RESOLVED.
- New minor observation (non-blocking): the added overlap scenario's final
  line (`:142`) says "the in-flight run's outcome governs whether the manual
  trigger executes." Mechanically it is the *presence* of an in-flight run at
  trigger time (not its outcome) that causes a `skip`-policy skip; the
  requirement prose (`:120`) is already precise ("will skip the trigger"), so
  this is a wording nit only. Optional: reword to "the in-flight run governs…".
- Verified fine: format still holds across both specs — `### Requirement:` with
  SHALL prose, `#### Scenario:` (4 hashtags), every requirement has ≥1
  scenario; telegram delta under `## MODIFIED Requirements`, schedule delta
  under `## ADDED Requirements`.
- Verified fine: no new contradiction with frozen proposal.md/design.md
  introduced by any fix.

### 🔴 Outstanding
(none — the specs batch passes)

### ⚖️ Verdict
PASS. The Round 1 🔴 is fully resolved — the telegram MODIFIED block now
reproduces all 12 original scenarios verbatim (including the restored
`secret_get` security assertion) plus the 2 appended new ones, with matching
requirement prose. All four Round 1 🟡 fixes are correctly folded and
consistent with the frozen artifacts. One optional wording nit
(`schedule-tool-surface:142`) remains, non-blocking. The specs batch is ready
to freeze.

(Post-review note by main agent: the wording nit was applied — "the in-flight
run's outcome governs…" reworded to "the presence of an in-flight run at
trigger time governs…". The specs batch is now frozen.)

## adr+tasks Round 1 — 2026-09-11

Scope: reviewed the two final artifacts (`adr.md` review manifest, `tasks.md`
implementation checklist) for consistency with the FROZEN proposal.md,
design.md, and both delta specs. Spot-verified 4 repo ADRs for the manifest's
supersession/in-force claims.

### 🔴 Fixed
(none — first round for this batch)

### 🟡 Addressed
- **`run_now` unknown-tag scenario has no explicit task** —
  `schedule-tool-surface/spec.md:144-147` requires `run_now` on an unknown tag
  to return `success=false` with an error listing known tags. Tasks map the
  analogous `show` (2.1 + test 4.2) and `modify` (1.4 + test 4.1) unknown-tag
  errors, but no task ensures `run_now`'s unknown-tag error lists known tags,
  and 4.4 does not assert it. Add the behavior to 2.2/2.5 and an assertion to
  4.4. Non-blocking (frozen spec + `openspec validate` at 5.2 will surface it),
  but a genuine coverage gap.
- **`run_now` overlap-subject note not asserted in tests** — task 2.5
  implements the overlap-policy-subject note in `run_now` output (matching
  frozen `schedule-tool-surface:120` + scenario `:137-142`), but test task 4.4
  enumerates run_now output assertions (supervised statement, notify/show
  follow-up, paused note, once-removal note) and omits the overlap-subject
  note. Add that assertion to 4.4 so the frozen scenario is covered.
- **Docstrings for new public methods not explicitly tasked** — AGENTS.md
  requires docstrings for public functions; task 2.6 covers only the
  `exec_schedule` module docstring. The new public `Scheduler.show_job` /
  `Scheduler.modify_job` (1.1/1.2) should carry docstrings. Minor — fold into
  1.1/1.2 or note under 5.1.
- **`_format_schedule` symbol + vulture whitelist (verify at apply)** — task
  3.3 targets `tool_formatting.py` `_format_schedule`; the exact per-tool
  formatter name was flagged unverified in the design round — confirm at apply
  time (existing `schedule add` brief already renders, so the module is
  correct). Also, new public symbols (`show_job`/`modify_job`) may trip
  vulture; 5.1's "fix failures" implicitly covers updating
  `vulture_whitelist.py` per AGENTS.md. Awareness only.
- Verified fine (ADR manifest): spot-checked all four relevant ADRs against the
  manifest's claims —
    - `adr/0003-use-toml-vault-format.md`: Status "Accepted, supersedes
      ADR-0002", Supersedes ADR-0002 → confirms the 0002→0003 chain and that
      0003 is in force. ✓
    - `adr/0005-use-sub-agent-supervisor-boundary.md`: Status "Accepted",
      Supersedes None → in force; its rule that "scheduler/internal control
      values must flow through supervisor-owned per-submission options" is
      exactly what D3's `manual` flag does. ✓
    - `adr/0007-use-agent-runtime-for-construction.md`: Status "Accepted",
      Supersedes None → in force; `SCHEDULED_AGENT` profile claim consistent. ✓
    - `adr/0008-use-facade-handler-package-for-builtin-tools.md`: Status
      "Accepted", Supersedes None → in force; new-tool pattern (descriptor +
      handler + table entry; state stays in the owner) matches D1. ✓
  No ADR after 0003 supersedes any of the three relevant records. Count
  arithmetic holds (0003-0028 = 26 in force; 28 total). (Only the 4 required
  ADRs were spot-checked; the full 28-record graph claim is asserted, not
  exhaustively re-verified — acceptable per scope.)
- Verified fine (ADR conclusion): "no new durable ADR" is sound. D5 (allowlist
  depth guard) is per-tool access policy captured by the frozen action-set
  requirement, not a cross-cutting architectural commitment; D3 (`run_now`
  manual override) applies ADR-0005/0007 without changing the supervisor
  boundary (internal control via per-submission options) and is captured by the
  frozen run_now requirements. D1-D8 either apply in-force ADRs or are
  feature-level behavior in frozen specs. None meet the durability bar.
- Verified fine (ADR format): manifest follows the template (Status/Review
  date, Review Summary, In-Force ADRs Reviewed, New Durable ADRs Created) and
  references repo ADRs by number + one-line relevance without duplicating their
  Context/Decision/Consequences.
- Verified fine (tasks format): `- [ ] X.Y` checkboxes throughout, grouped
  under `## N.` numbered headings, ordered by dependency (scheduler → tool →
  schema/descriptor/brief → tests → verification).
- Verified fine (D1-D8 → tasks): D1/D2→1.2/1.3, D3→1.5, D4→1.1, D5→2.4,
  D6→2.3, D7→1.4, D8→3.1/3.2/3.3. All mapped.
- Verified fine (spec → tasks) beyond the two gaps above: 8-action set
  (2.1/2.2/3.1, test 4.3); depth-guard allowlist naming both read actions (2.4,
  test 4.3); modify partial-patch incl. preserved fields, immutable tag
  (updatable set excludes tag; descriptor 3.2), enabled-rejection, empty-patch,
  unknown-tag, once-job preservation, hidden params, invalid-cron-unchanged
  (1.2/1.3/1.4, tests 4.1/4.5); show full metadata incl. `created_at` + raw
  cron + unknown-tag + list-stays-compact (1.1/2.1, test 4.2); run_now paused
  override + scheduled-firing skip + once-removal + supervised output
  (1.5/2.5, tests 4.4/4.5); add param surface + invalid-overlap rejection
  (2.3/3.1, test 4.4); telegram modify/show briefs (3.3, test 4.6).
- Verified fine: `openspec validate upgrade-schedule-tool --type change
  --strict` present (5.2); `make check` present (5.1); real symbols/files
  referenced (scheduler.py primitives, builtin_tools/schedule.py,
  schemas.py, descriptors.py, tool_formatting.py, test_tool_brief_panel.py).
- Verified fine: no scope creep and no rejected alternative reintroduced —
  Option B blocked (1.4 rejects `enabled` in modify), Option C absent (no task
  makes `add` an upsert), legacy `hours`/`minutes`/`time` excluded from the
  modify updatable set (1.2).

### 🔴 Outstanding
(none — the adr+tasks batch passes)

### ⚖️ Verdict
PASS. The ADR manifest's in-force/supersession claims hold for all four
spot-checked ADRs (0003/0005/0007/0008), its "no new durable ADR" conclusion is
sound, and its format is compliant. tasks.md is well-formed, dependency-ordered,
covers all of D1-D8 and (with two minor exceptions) every frozen spec
requirement, includes the mandatory `openspec validate --strict` (5.2) and
`make check` (5.1), and reintroduces no rejected alternative. Four 🟡 items
(add `run_now` unknown-tag task+test; assert the overlap-subject note in 4.4;
docstrings for `show_job`/`modify_job`; verify `_format_schedule`/vulture at
apply) are declarative and can be folded without blocking. Ready to freeze and
proceed to `/opsx-apply`.

(Post-review note by main agent: all four 🟡 items were folded as declarative
edits — 2.5 now covers the `run_now` unknown-tag error, 4.4 asserts the
overlap-subject note and the unknown-tag error, 1.1/1.2 call for docstrings on
the new public methods, 3.3 records the verified symbol (`_format_schedule`,
`tool_formatting.py:186`), and 5.1 names `vulture_whitelist.py`. The adr+tasks
batch is now frozen; all artifacts are complete.)