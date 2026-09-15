# Review Log: fix-mcp-oauth-refresh-token-lifecycle

## proposal Round 1 — 2026-09-14

Reviewer: openspec-reviewer (ope-1). Baseline: explore-brief.md.

### Verdict: PASS (single-round pass rule — no 🔴 Outstanding)

### 🟡 Addressed
- [minor] F1: Why omitted defect E (registration hygiene) → appended clause to
  Why covering the ~14 duplicate-registration warnings + wrapper leak as a
  co-located lifecycle defect surfaced by the auth churn.

### Watch-items carried to next batches
- F2 [minor]: react-loop detection/translation (`_is_mcp_auth_failure`) must be
  homed unambiguously in ONE capability delta (mcp-oauth-flow) so the two spec
  files leave no seam. → design.md decision + specs batch.
- F3 [minor]: the merge-preserve delta must encode "omitted → preserve /
  present → overwrite" explicitly so it reads as refining (not reversing) the
  main spec's "Re-authentication replaces existing tokens" scenario. → specs batch.

### 🔴 Outstanding
- (none)

## design Round 1 — 2026-09-14

Reviewer: openspec-reviewer (ope-1). Baselines: frozen proposal.md + explore-brief.md.

### Verdict: FAIL (2 serious, 2 minor)

### 🔴 Fixed
- F1 [serious] D2 did not gate the in-memory re-attach on refresh success — on
  the SDK failure path (`oauth2.py:456-459`) `current_tokens` is cleared, so a
  blind re-attach would resurrect a revoked RT. → D2 rewritten: capture →
  `result = super()` → re-attach ONLY when `result is True` and
  `current_tokens.refresh_token is None` → return `result` unchanged; explicit
  failure-path no-op rationale added.
- F2 [serious] D3 mischaracterized today's lifecycle ("already reconnects a
  wrapper after the flow" — it does not; `:1871-1915` registers the flow's
  wrapper directly) and omitted the teardown/`finally` restructure. → D3
  rewritten: explicit second bring-up (new behavior, cost = one session
  initialize + tools/list with the valid stored token), interactive flow
  wrapper closed, two-session `finally` distinction described.

### 🟡 Addressed
- F3 [minor] D2 implied the override re-persists via `set_tokens` the same
  cycle. → Disk-write ordering paragraph: super() persists RT-less token first
  (D1 read-merge covers disk this cycle); re-attached RT reaches disk on the
  next refresh; copies converge.
- F4 [minor] D4.1 error text over-specified the cause ("token expired or
  revoked") for the never-authorized case. → Text now "needs authorization.
  Run /mcp auth <name>." matched by the D4.3 indicator; Risks bullet 3 also
  rewritten (removed the stale "same cost as today" claim).

### 🔴 Outstanding
- (pending re-review of the four fixes above)

## design Round 2 — 2026-09-14

Reviewer: openspec-reviewer (ope-1, same session).

### Verdict: PASS (single-round pass on the re-review — no 🔴 Outstanding)

### Verified fixed
- F1: D2 success-gate + bool contract matches `oauth2.py:454-473`; failure path
  no-op; no revoked-RT resurrection.
- F2: D3 lifecycle accurate (single bring-up today; second bring-up after fix;
  teardown + two-session `finally` distinction); D3×D5 interplay confirmed clean.
- F3: disk-write ordering paragraph accurate.
- F4: D4.1 text + D4.3 indicator complementary with D4.2; Risks bullet 3 corrected.
- Whole-document scan: no new issues.

### 🔴 Outstanding
- (none)

## adr+specs Round 1 — 2026-09-14

Reviewer: openspec-reviewer (ope-1, same session). Baselines: frozen proposal.md + design.md.

### Verdict: PASS (single-round pass — no 🔴 Outstanding)

### Verified
- MODIFIED blocks are full requirement copies with exact headers (no archive-time
  scenario loss); lane discipline held (F2: detection homed in mcp-oauth-flow;
  mcp-transport only references the fast-fail); F3 held (merge reads as refinement
  of the untouched re-auth-replaces scenario); D2 success-only gate present;
  scenario well-formedness (####, GIVEN/WHEN/THEN); no scope invention.
- ADR-0029/0030: house style conformant, sequence 0029/0030 contiguous after 0028,
  one decision each, honest consequences (SDK hook contract test; human re-auth for
  dead RT; one extra session bring-up); IRON RULE held (only two new untracked files
  under adr/, none modified).

### 🟡 Addressed
- F1 [minor] adr.md manifest framed D4 as "tactical, not ADR-worthy" while also
  attributing it to ADR-0030. → Manifest reworded: 0030 captures D3 + D4's durable
  degradation principle; D4's error-string mechanics + D5 hygiene are tactical.

### 🔴 Outstanding
- (none)

## tasks Round 1 — 2026-09-14

Reviewer: openspec-reviewer (ope-1, same session). Baselines: all frozen artifacts.

### Verdict: FAIL (1 serious, 3 minor)

### 🔴 Fixed
- F1 [serious] Group 2 inverted the test-first ordering (implement → wire → test)
  on the correctness-critical D2 group. → Reordered: 2.1 failing contract tests
  (success gate, failure no-op, bool passthrough, two-cycle successive refresh),
  2.2 subclass impl, 2.3 factory wiring.

### 🟡 Addressed
- F2 [minor] No dedicated two-cycle successive-refresh test (the defect-B
  regression). → Folded into 2.1: two consecutive silent refreshes, RT survives
  on disk AND in memory after both.
- F3 [minor] Background dead-RT path covered only in pieces. → New 4.3 end-to-end
  integration test spanning groups 3-4 (no send_oauth_prompt call, wrapper
  needs_auth/connected=False, status query, subsequent fast-fail); old 4.3
  renumbered to 4.4.
- F4 [minor] Task 3.2 is the largest unit but coherent with a clear done-state —
  reviewer accepted as-is; no change.

### 🔴 Outstanding
- (pending re-review of the group-2 reorder + the two new tests)

## tasks Round 2 — 2026-09-14

Reviewer: openspec-reviewer (ope-1, same session).

### Verdict: PASS (single-round pass on the re-review — no 🔴 Outstanding)

### Verified fixed
- F1: group 2 now test-first (2.1 failing tests → 2.2 subclass → 2.3 wiring);
  group ordering 1→2→3→4→5→6 holds.
- F2: two-cycle successive-refresh test in 2.1 asserts disk + in-memory RT
  survival across both cycles.
- F3: 4.3 end-to-end background dead-RT integration test with all four
  assertions; correctly scoped to groups 3-4, no group-5 dependency.
- F4: 3.2 accepted as-is.
- Whole-file scan: checkbox format intact, groups contiguous (4.1-4.4),
  anchors accurate, no scope creep.

### 🔴 Outstanding
- (none — planning artifact review chain complete: proposal, design, adr+specs,
  tasks all PASS)

## verify — 2026-09-15

Reviewer: oracle (ora-1) for the code-vs-artifact review; orchestrator re-ran
the suites and provenance checks. Baselines: all frozen artifacts + implemented
code on branch fix/mcp-oauth-refresh-token-lifecycle.

### Verdict: PASS — ready for archive after reconciliation below

### Verified
- D1–D5 all implemented and matching design intent, incl. the two highest-risk
  details: D2's success-only re-attach gate (failure path strict no-op; no
  revoked-RT resurrection) and D3's two-session `finally` (live non-interactive
  runner never cancelled — asserted at tests/test_mcp_client.py:1930).
- Scenario→test mapping complete across both delta specs (storage merge 3-case
  set, two-cycle disk+memory RT survival, non-interactive handoff, background
  dead-RT e2e with send_oauth_prompt.assert_not_called(), needs_auth fast-fail
  before connected check, 14-tool re-registration with zero conflict warnings,
  cross-server conflict still warns).
- ADR-0029/0030 exist, house-conformant, and match the design decisions they
  claim to record.
- Suites re-run independently: test_mcp_oauth.py 43 passed; test_mcp_client.py
  87 passed; test_react_loop.py + test_mcp_oauth_probe.py 136 passed;
  `make check` 2249 passed, 2 skipped. Skip provenance checked: both skips
  pre-date this branch (test_consent_scope_wording.py:187 from commit a694fbc
  on main; test_path_policy.py platform normcase). No new skips vs baseline —
  task 6.1 claim holds.
- `openspec validate fix-mcp-oauth-refresh-token-lifecycle --type change
  --strict` passes — task 6.2 claim holds.

### 🟡 Addressed (verify-stage reconciliation)
- W1: proposal.md scope said "no `mcp` SDK modification or pinning" /
  out-of-scope "SDK patching or version pinning", but commit b9b3a24 added
  `mcp>=1.29.1,<2.0` (requirements.txt:3) + SDK drift-guard test. Defensible
  (design.md risk note "SDK pin review at upgrade time"; no SDK source
  modified) but the frozen artifact contradicted the delivery. → proposal.md
  scope lines amended declaratively (accuracy fix recording what shipped, no
  decision content changed) and this entry records the reconciliation.

### Open suggestions (non-blocking, recorded for future work)
- S1: task 3.2's literal wording ("build the provider up front", "construct a
  fresh wrapper with the non-interactive provider") is stale vs the implemented
  mechanism (no set_oauth on the fresh wrapper; its _session_runner builds the
  provider via _prepare_oauth_provider during bring-up — cleaner, reuses the
  verified startup path, satisfies design D3). Task text left as-is since work
  is complete; note kept here to prevent confusion during archive review.
- S2: `cb_server.stop()` in the restructured two-task `finally` has no
  dedicated assertion (covered implicitly by the probe/cancel suite). Consider a
  focused teardown assertion as follow-up test hardening.

### 🔴 Outstanding
- (none)