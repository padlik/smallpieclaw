# Review Log: fix-mcp-token-expiry-restore

## proposal Round 1 — 2026-09-16

Reviewer: openspec-reviewer (ope-1). Baseline: explore-brief.md.

### Verdict: PASS (single-round pass rule — no 🔴 Outstanding)

Brief coverage confirmed for both coupled gaps (A: expiry restore, B: token-endpoint
metadata), upstream verification (PRs #1784/#2492/#2875 open; absent in 1.30.0,
2.2.0, main), final approach 1–4, migration story, and OQ1–OQ3 deferrals. Claims
grounded against `mcp_oauth.py`, SDK `oauth2.py` (`:134` validity short-circuit,
`:432-436` refresh endpoint fallback, `:475-479` bare `_initialize`), README:394
staleness confirmed.

### 🟡 Addressed
- [minor] Modified Capabilities named only "Automatic token refresh" → appended
  the "File-backed token storage" requirement (schema scenarios + storage methods)
  to the entry.
- [minor] No explicit non-goals line → added Non-goals section (401-branch refresh
  interposition out of scope).

### Watch-items carried to later batches
- [design/verify] Both provider build sites (`_prepare_oauth_provider`
  `mcp_client.py:552`, interactive `_run_oauth_flow` `:1871`) must construct via
  `OAuthProviderFactory.build` → `RefreshPreservingOAuthProvider`; the
  `_handle_token_response` override must live on that subclass so the interactive
  path persists metadata and the non-interactive path restores it. State in
  design; assert at verify.
- [specs] Confirm literal requirement headings "Automatic token refresh"
  (main spec line 248) and "File-backed token storage" (line 183); copy MODIFIED
  blocks in full.
- [design] Carry the brief's rejected alternatives into design.md.
- [design] OQ1: land the `oauth_metadata` full-block vs flat `token_endpoint`
  decision + token-file schema table; reconcile "at minimum the token endpoint".
- [design/tasks] Drift-guard companions stay executable assertions (b9b3a24
  pattern), not prose.

### 🔴 Outstanding
- (none)
## design Round 1 — 2026-09-16

Reviewer: openspec-reviewer (ope-1). Baselines: frozen proposal.md + explore-brief.md.

### Verdict: PASS (single-round pass rule — no 🔴 Outstanding)

All Round 1 proposal watch-items carried: (1) build sites + subclass ownership
stated in Goals and D3 close-out, verified TRUE in code (:552 and :1871 both
route through the factory); (2) rejected alternatives carried into D1/D3 +
Non-Goals (#2875 interposition, expires_at persistence, get_tokens restore,
discovery-time persist, standalone startup discovery, SDK upgrade); (3) OQ1
landed as D2 full OAuthMetadata block + D4 schema table; (4) drift guards are
executable inspect.getsource assertions. D1-D5 cover brief final-approach
items 1-4 with no invented scope. All code claims verified against SDK
oauth2.py (_initialize 475-479, is_token_valid 134 short-circuit, _refresh_token
432-436 fallback, proactive branch 500, OASM metadata set at :559 before
exchange :597-598, call sites :598/:620) and mcp_oauth.py (payload assembly
156-160 carries only client_info today — D4.2 is a real change; rollback
drop-key claim TRUE; client_info fallback 216-242). Coherent with ADR-0029
(extends, not contradicts), ADR-0030 (degradation untouched), ADR-0019 (path/
perms/atomic-write unchanged). New-ADR candidate (storage restore contract)
warranted, deferred to adr step.

### 🟡 Addressed
- [minor] D5 unit phrasing "malformed metadata -> context untouched"
  imprecise (get returns None, so the field IS set to None — equals fresh
  context state) → reworded to assert the value: "context.oauth_metadata is
  None (equals the fresh-context starting state — assert the value, not
  'unchanged')".
- [minor] Proposal's "at minimum the token endpoint" vs design's full block:
  resolution, not contradiction (full block superset) → noted so the specs
  batch phrases the requirement around the full-block decision without
  re-inheriting the "at minimum" hedge.

### Watch-items carried to later batches
- [tasks] E2E harness must leave the provider un-initialized so D1/D2 actually
  run (SDK async_auth_flow only calls _initialize when not self._initialized —
  oauth2.py:493-495); a pre-initialized provider makes the test pass vacuously.
- [specs] "Automatic token refresh" gains the restore contract; "File-backed
  token storage" gains the optional oauth_metadata block + get/set_oauth_metadata
  + read_issued_at + set_tokens carry-forward. Copy MODIFIED blocks in full;
  encode carry-forward as "present on disk -> preserved across set_tokens;
  wiped only by rollback to old code".
- [adr] New ADR for the storage restore contract — one decision, sibling to
  ADR-0029, honest consequence (two new drift-guard SDK couplings), sequenced
  after 0030.
- [tasks] D4.1 ordering test: set_oauth_metadata only reachable after
  set_tokens (no metadata-only file); silent refresh preserves oauth_metadata
  on disk via D4.2 (parallel to the ADR-0029 RT-survival test).
- [tasks] vulture_whitelist entries for get_oauth_metadata, set_oauth_metadata,
  read_issued_at (OQ3), following the existing mcp_oauth.py block.

### 🔴 Outstanding
- (none)

## adr+specs Round 1 — 2026-09-16

Reviewer: openspec-reviewer (ope-1). Baselines: frozen proposal.md + design.md + explore-brief.md.

### Verdict: PASS (single-round pass rule — no 🔴 Outstanding)

Full-block fidelity verified scenario-by-scenario against the main spec: both
MODIFIED requirements preserve every original scenario; "File-backed token
storage" keeps all 9 original scenarios + 4 new (metadata-persisted-after-
exchange, silent-refresh-preserves-metadata, malformed-metadata->None,
issued_at-readable); "Automatic token refresh" keeps scenarios 2/3/4 verbatim
and replaces scenario 1 — replacement justified as a correctness fix (the old
"Silent token refresh on 401" attributed refresh to the 401 trigger, but the
SDK's 401 branch runs the full authorization flow, not a refresh grant; the
real silent-refresh entry point is the proactive branch, unreachable
pre-change). Headers match the main spec exactly; all scenarios use ####.
ADR-0031 house-conformant, one decision, honest consequences (two drift-guard
couplings in Bad, one-time /mcp auth migration in Neutral). Manifest correct,
non-duplicative. IRON RULE held: git status shows only untracked additions,
no existing ADR modified.

### 🟡 Addressed (declarative additions post-PASS)
- [minor] Scenario "OAuth metadata persisted after a completed authorization"
  re-inherited the "at minimum" hedge -> reworded to "the discovered
  authorization-server metadata (the SDK OAuthMetadata model), including the
  token_endpoint" per D2's full-block decision.
- [minor] No symmetric "missing issued_at" degradation scenario -> added
  "Token file without issued_at degrades to today's behavior" mirroring the
  pre-migration scenario and closing the restore matrix.

### Watch-items carried to the tasks batch
- [tasks] Proactive-refresh test must use a fresh, un-initialized provider so
  _initialize actually runs, assert refresh fires BEFORE the request is sent
  and POSTs to the restored endpoint (not on-401, not localhost fallback).
  Keep the preserved failure-surface and no-resurrect tests intact.
- [tasks] Metadata-ordering tests: no oauth_metadata written when the exchange
  fails before completion; set_oauth_metadata only reachable after set_tokens
  (D4.1); carry-forward-across-refresh as the ADR-0029 RT-survival parallel.
- [tasks] read_issued_at matrix: present -> value; no file / no token block /
  no issued_at key -> None; synchronous.
- [tasks] Executable drift guards (inspect.getsource pins) + vulture_whitelist
  entries for get_oauth_metadata, set_oauth_metadata, read_issued_at (OQ3).

### 🔴 Outstanding
- (none)

## tasks Round 1 — 2026-09-16

Reviewer: openspec-reviewer (ope-1). Baselines: all frozen artifacts (proposal, design D1-D5, delta specs, ADR-0031).

### Verdict: FAIL (1 serious, 2 minor)

### 🔴 Fixed
- [serious] No test task covered the metadata-persist path (frozen scenario
  "OAuth metadata persisted after a completed authorization"; design D3 /
  task 2.2 implements but nothing tests it — 3.1(c) is an SDK drift guard,
  3.3/3.4 test storage in isolation, and the E2E 4.1-4.3 hand-seeds the
  oauth_metadata block, bypassing the override) -> added task 3.6: drive the
  _handle_token_response override directly — successful exchange persists the
  block (token_endpoint) after set_tokens stamped issued_at; failed exchange
  leaves no block. A persist bug would otherwise ship the exact production
  defect with the suite green.

### 🟡 Addressed
- [minor] Test-first ordering vs sibling precedent -> group-2 execution-order
  note added (write failing contract tests 3.5/3.6/4.x before implementing
  2.1/2.2, per test-driven-development skill).
- [minor] Task 5.3 pinned "the two known platform skips" -> reworded to
  compare against the pre-change baseline without pinning an absolute count.

### Watch-items carried to apply/verify
- [verify] Scenario-to-test completeness gate (all new/replaced scenarios map
  to real tests after the 3.6 fix).
- [apply] 2.3 build-site assertion is design-invalidating if it fails — halt
  apply, do not work around.
- [verify] Drift guards 3.1(a)/(b) assert the SDK gaps are STILL present (fail
  when upstream #1784/#2492/#2875 land).

## tasks Round 2 — 2026-09-16

Reviewer: openspec-reviewer (ope-1). Baselines: all frozen artifacts.

### Verdict: PASS (single-round pass on the re-review — no 🔴 Outstanding; planning phase complete)

### 🔴 Fixed (verified)
- Task 3.6 closes the serious gap exactly as specified: drives the override
  directly (not a drift guard, not hand-seeded), covers both the successful-
  exchange persist-after-issued_at clause and the failed-exchange no-write
  clause, names the frozen scenario and the green-suite production risk.

### 🟡 Addressed / fixed
- [minor] Group-2 execution note satisfies test-first without disturbing the
  dependency structure. 5.3 baseline-comparison wording host-robust.
- [minor, new] Dangling cross-reference "4.1-4.5" in the group-2 note ->
  corrected to "4.1-4.4" post-PASS.

### 🔴 Outstanding
- (none — proposal, design, adr+specs, tasks all PASS; ready for /opsx-apply)

## Post-freeze validator fix — 2026-09-16

`openspec validate --strict` refused to archive-drop the scenario name
"Silent token refresh on 401" (MODIFIED requirements replace the whole block;
the tooling preserves scenario names even when rewritten). The adr+specs Round
1 PASS had blessed replacing that scenario with "Silent token refresh on
restored expiry" — the old scenario described intended-but-unrealized behavior
(the SDK's 401 branch never issues a refresh grant; the real silent-refresh
entry point is the proactive branch). Fix: restored the scenario name under
`## MODIFIED Requirements` with truthful content — an unexpired-per-restored-
clock token rejected by the server with 401 degrades via the full-flow abort
to needs_auth without attempting a refresh and without destroying the on-disk
refresh token. Declarative addition (freeze rules): fills in a boundary
condition, does not change any decision; the restored-expiry entry point and
all other scenarios are unchanged. Both scenarios now coexist: proactive
restore-path refresh (renamed scenario) + 401-branch degradation (original
name, corrected semantics).

## Oracle final review + reconciliation — 2026-09-16

Reviewer: oracle (ora-1). Scope: delta specs re-review + "100% ready to apply?"
verdict. Verdict: READY-WITH-CONDITIONS.

### 🔴 Resolved on reconciliation
- [oracle F1: post-freeze 401-scenario claim not source-confirmed] -> CLOSED by
  direct read of the installed SDK (oauth2.py, mcp 1.29.1): the 401 branch is
  headed "# Perform full OAuth flow" and its body (lines ~514-627) contains
  only _perform_authorization() call sites (x2, incl. the 403 step-up) — zero
  _refresh_token / _handle_refresh_response calls. Clause (a) confirmed: no
  refresh grant on 401. Clause (b) confirmed: clear_tokens() (oauth2.py:141-145)
  clears only in-memory context fields (current_tokens, token_expiry_time),
  never storage; the non-interactive abort raises before any exchange, so no
  set_tokens runs — on-disk RT survives. Scenario is factually correct.
- [oracle F4: possible stale test encoding old refresh-on-401 semantics] ->
  FALSE ALARM, verified: the only "on 401" text in tests is a comment inside a
  fake SDK simulation double (tests/test_mcp_oauth_probe.py:804), not an
  assertion of real SDK semantics. No existing test encodes the old scenario.

### 🟡 Addressed
- [oracle F3: post-freeze 401 scenario had no covering task] -> added task 4.5
  (declarative addition per freeze rules): no-refresh-grant assertion on the
  401 path + needs_auth degradation + on-disk file untouched. Closes the
  scenario-to-test completeness gate for the one scenario added after the
  tasks review passed.

### 🔴 Outstanding (user-owned)
- [oracle F2/git-discipline] Artifacts + ADR-0031 remain uncommitted on main.
  openspec-git-discipline requires the proposal committed to main before
  apply starts, and apply to run on a dedicated fix/* branch or worktree.
  Committing requires explicit user approval per AGENTS.md. This is the sole
  remaining gate to "100% ready to apply".

### Oracle informational findings (all clean)
- Full-block fidelity verified (9+4 storage scenarios, 4+4 refresh scenarios,
  no silent drops). Scenario-to-task mapping otherwise complete incl. 3.6.
  Implementability mechanical; deferred watch-items are proper gates
  (2.3 apply-halting build-site assertion; drift guards 3.1(a)/(b) assert the
  SDK gaps still exist).
