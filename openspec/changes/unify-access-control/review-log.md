# Review Log — unify-access-control

## proposal Round 1 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1) | Baseline: explore-brief.md

### 🔴 Fixed
- (none — no critical issues found)

### 🟡 Addressed (all applied per user decision "apply all findings and re-review")
- Approve-all deletion (`auto_approve_tools` + `_ALLOWED_APPROVE_ALL_TOOLS`, main-agent
  bulk-approve) missing from BREAKING deletions bullet → added, marked BREAKING.
- Shell/`secret_get` prompts not pinned to 2 buttons → added explicit clause: their
  prompts render Confirm/Deny only, no "Till /reset" affordance (no dead buttons).
- Prompt-cycle grant clearing attributed to `agent_controller.py` → corrected:
  `react_loop.py` (loop entry) is the clearing site; controller keeps run-end clearing
  and sub-agent scoping.
- Carry-forward notes (Tier 0 must be a superset of live `_BLOCKED_SYSTEM_PREFIXES`
  incl. /root /run /lib64; xdg/vault delta question deferred to design) → appended
  to explore-brief.

### 🔴 Outstanding
- (none)

## proposal Round 2 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1, re-review) | Baseline: explore-brief.md + round-1 fixes

### 🔴 Fixed
- (none — no critical issues found)

### 🟢 Confirmed
- Fix 1 (approve-all BREAKING deletion) landed; consistent with brief deletion table.
- Fix 2 (shell/secret_get render Confirm/Deny only, no dead affordance) landed; aligns
  UI with sink-side veto.
- Fix 3 (clearing sites: react_loop.py prompt-cycle vs agent_controller.py run-end)
  landed; matches brief boundary map.
- Fix 4 (carry-forward notes in brief) landed at explore-brief.md:190.
- No regressions: edits additive; three-tier semantics, capability set, scope lock intact.

### 🔴 Outstanding
- (none)

Verdict: **PASS — proposal.md frozen** per single-round pass rule.

## design Round 1 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1) | Baseline: frozen proposal.md + explore-brief.md (incl. carry-forward notes)

### 🔴 Fixed
- (none — no critical issues found)

### 🟡 Addressed
- Ledger-owner naming: D3 says GrantLedger is "owned by depth-0 ConfirmationManager" but D6(a)
  says "lives in the coordinator" — ambiguous vs the distinct `_coordinator` headless bridge.
  Reviewer deemed addable without unfreeze; will be pinned in specs phase (ledger owner is
  ConfirmationManager everywhere).

### 🟢 Confirmed
- D1–D8 cover every proposal commitment (hard-error semantics, grant immunity by ordering,
  once/session build, no partial mounts, session-logs mount removed, results manual-clean).
- Carry-forward (a) landed: Tier 0 superset of live _BLOCKED_SYSTEM_PREFIXES incl. /root,
  /run, /lib64 (verified vs nsjail_config.py:25-26) + containment test in Risks.
- Carry-forward (b) landed: no xdg/vault delta specs; rationale coherent.
- ADR spot-checks pass: 0017 reversal, 0011 partial (invariant preserved/strengthened),
  0012 untouched (execution plane), 0018 mount-source half. Supersession routing → adr step.
- Diagrams satisfy c4 rule and match decisions (classification-before-ledger, sink veto).
- D1/D6 carry rejected alternatives; Risks format correct; migration commit-sized, rollback-safe.

### 🔴 Outstanding
- (none)

Verdict: **PASS — design.md frozen** per single-round pass rule. 🟡 (ledger-owner naming)
and 💡 (nonexistent allowed_dir behavior) pinned for the specs phase — declarative additions,
no unfreeze needed.

## specs Round 1 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1) | Baseline: frozen proposal.md + design.md + baseline main specs

### 🔴 Fixed
- Orphaned baseline requirement "file_write and file_patch inside trusted zones do not
  require confirmation" (file-access-zones) — unlisted in the delta, would survive archive
  referencing deleted mechanisms (request-granted zones, sensitive-pattern gate) → added
  REMOVED block (subsumed by MODIFIED classification requirement + approval-grants).

### 🟡 Addressed
- Stale cross-references in nsjail "Agent's default temp directory" requirement ("like the
  session_logs_dir mount", "not validated against the trusted-directory blocklist") →
  added MODIFIED block dropping the dead references; reframed as Tier 1 system mount derived
  from PathPolicy (normative content unchanged).

### 🟢 Confirmed
- All 8 capabilities present and exactly named per frozen proposal.
- Delta format compliant: MODIFIED blocks complete/self-contained; REMOVED carry Reason+Migration;
  headers match baseline names; 4-hashtag scenarios; GIVEN/WHEN/THEN; ≥1 scenario per requirement.
- Carry-forward pins landed: ledger owner = ConfirmationManager (approval-grants:11, with
  _coordinator disambiguation); nonexistent allowed_dirs entry warns, never fails (path-policy).
- D1–D8 fully covered across the 8 files; cross-file consistency holds; no execution-plane creep.

### 🔴 Outstanding
- (none)

Verdict: FAIL round 1 (one 🔴 delta-completeness gap) → fixes applied per user decision
("fix all issues"), round 2 dispatched.

## specs Round 2 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1, re-review) | Scope: verify round-1 fixes

### 🟢 Confirmed
- Fix 1: REMOVED block header matches baseline exactly; Reason/Migration accurate;
  delta now accounts for all 7 baseline requirements — no orphan survives archive.
- Fix 2: MODIFIED block header matches baseline; dead references dropped; reframed as
  Tier 1 system mount from PathPolicy tiers; all four original scenarios preserved.
- No new inconsistencies; ADDED once-per-session and MODIFIED mount contracts complementary.
- All round-1 🟢 findings still hold (format, pins, D1–D8, consistency, scope).

### 🔴 Outstanding
- (none)

Verdict: **PASS — specs/ batch frozen (all 8 files)** per single-round pass rule.

## adr Round 1 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1) | Baseline: design.md Open Questions routing + house style

### 🔴 Fixed
- (none)

### 🟢 Confirmed
- IRON RULE: all 5 superseded ADRs (0010, 0011, 0017, 0018, 0024) byte-for-byte untouched;
  supersession recorded only in new files.
- Sequence clean: 0026–0028 contiguous above prior highest (0025), no collisions.
- Supersession routing matches design exactly: 0010 full; 0011 partial with shell invariant
  explicitly preserved/strengthened (0027); 0017 reversed (0028); 0018 mount-source (0026);
  0024 partial with signaling preserved; 0012 NOT superseded; 0019 in force; 0015 note only.
- One decision per ADR; honest Bad bullets in Consequences; full house style sections.
- Manifest lists reviewed in-force ADRs + 3 new files; no content duplication.

### 🔴 Outstanding
- (none)

Verdict: **PASS — adr batch frozen (adr/0026–0028 + change-local manifest)** per
single-round pass rule.

## tasks Round 1 — 2026-09-03
Reviewer: @openspec-reviewer (ope-1) | Baseline: all frozen artifacts (proposal, design,
8 specs, adr batch)

### 🔴 Fixed
- (none)

### 🟢 Confirmed
- Coverage complete: every spec requirement, D1–D8, and Migration Plan step maps to ≥1 task
  (spot-verified: superset containment test, inode defense, ledger veto/lifetimes/scoping,
  PendingConfirmations, confirm-before-read, once-per-session build, no-partial-mount,
  skills copy, results contract, 3-button/2-button UX, /dir removal, deletions, vulture,
  docs, 9.2 strict validate).
- Dependency ordering sane: additive (1–2) → config (3) → rewires (4–7) → deletions (8) →
  verification (9); ledger before Telegram UX; schema before startup wiring.
- Format compliant: checkbox format, numbered groups, session-sized verifiable tasks,
  strict-validate gate present (9.2).
- No implementation in planning; no execution-plane creep; all files within frozen Impact.

### 💡 Optional (non-blocking, may ride along during apply)
- 2.5 could add an explicit "grants never persisted across restart" test (holds by
  construction — in-memory only).
- 3.3 README could mention the operator-visible ~/.<agent>/results exchange surface.

### 🔴 Outstanding
- (none)

Verdict: **PASS — tasks.md frozen** per single-round pass rule. Planning complete.