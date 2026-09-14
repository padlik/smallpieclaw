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