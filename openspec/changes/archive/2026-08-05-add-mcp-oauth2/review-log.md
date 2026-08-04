## proposal Round 1 — 2026-08-04

### 🔴 Fixed
 - None (first review round; review-only, no fixes applied yet)

### 🟡 Addressed
 - Traceability check complete: every brief commitment (SDK OAuthClientProvider,
   ephemeral one-shot HTTPS callback server, 3 SDK hooks, file-backed token
   storage, pre-seeded creds / no-DCR, token lifecycle, /mcp auth|status|revoke,
   oauth config block, all 5 module changes, infrastructure) is present in the
   proposal with no contradictions.
 - mcp-transport correctly classified as Modified (not new): needs_auth changes
   the existing "Connection failure on startup → error" behavior for
   OAuth-protected servers. Verified against openspec/specs/mcp-transport/spec.md.
 - Capability names kebab-case; New vs Modified correct; Why/What Changes/
   Capabilities/Impact all present.
 - Follow-ups for the specs/design phase (not blocking):
   * Concurrent-auth rejection (brief Open Q4, resolved "reject the second")
     is not reflected in What Changes — add it; needs a scenario later.
   * Token-at-rest posture (brief Open Q2, resolved "0600 plaintext for now")
     not mentioned in the Security impact — add it.
   * needs_auth must also propagate to status-display contracts (/mcp list,
     /mcp info), not only the connection lifecycle — broaden the Modified
     Capabilities wording.

### 🔴 Outstanding
 - None. No blockers; proposal is ready to advance to design/specs.

## proposal Round 2 — 2026-08-04

### 🔴 Fixed
 - Concurrent-auth rejection now stated in What Changes (proposal.md:16):
   "Only one OAuth flow at a time: a second /mcp auth while one is in flight is
   rejected (single callback server, single port)." Matches brief Open Q4.
 - Token-at-rest posture now stated in Security impact (proposal.md:32):
   "Tokens stored as 0600 plaintext JSON for now; vault integration deferred."
   Matches brief Open Q2.
 - needs_auth display propagation now stated in Modified Capabilities
   (proposal.md:24): the state propagates to /mcp list status reporting and
   /mcp info display contracts, not just the connection lifecycle.

### 🟡 Addressed
 - Re-verified full brief→proposal traceability after edits: no regressions,
   no new contradictions, capability names still kebab-case, New vs Modified
   still correct, Why/What Changes/Capabilities/Impact all present.

### 🔴 Outstanding
 - None. Proposal batch is ready to advance to design/specs.

## design Round 1 — 2026-08-04

### 🔴 Fixed
 - None (first review of design.md).

### 🟡 Addressed
 - Strong points confirmed: all 6 decisions have rationale + alternatives;
   risks/trade-offs have mitigations; Non-Goals trace to the brief's rejected
   alternatives; migration/rollback/compatibility present and the additive
   needs_auth compatibility claim matches the mcp-transport spec; ADR-0003
   referenced accurately; Open Questions correctly note vault deferral needs no
   ADR supersession. Full brief coverage.
 - Should-fix (non-blocking):
   * oauth_cancel handler (frozen proposal Impact) is undocumented in the
     design — add how cancel closes the one-shot server and releases the
     single-flow lock (Decision 6).
   * Cert-expiry-at-auth-time failure mode not covered — add a risk bullet;
     expired/invalid TLS cert => browser handshake fails => redirect never
     arrives => opaque timeout.
   * Decision 3 relies on SDK internal `self.context.client_info` (mcp 1.27.0)
     — verify against installed SDK source before implementation.
   * C4 block labeled "container view" but is component-level (minor).

### 🔴 Outstanding
 - BLOCKER — ADR-0019 contradiction + mis-citation. design.md stored tokens at
   data/mcp_tokens/ and cited ADR-0019 as support. ADR-0019 retired
   agent_home-relative data/, mandates all storage resolve via xdg.py, and
   requires future storage use an XDG bucket. Required fix: route through
   xdg.py, explicitly choose + justify STATE vs DATA bucket.
 - Coupling: same wrong path in frozen proposal.md:9. Soft-freeze correction
   needed.

## design Round 2 — 2026-08-04

### 🔴 Fixed
 - BLOCKER resolved (ADR-0019). All `data/mcp_tokens/` references replaced with
   `$XDG_STATE_HOME/<name>/mcp_tokens/` resolved via `xdg_paths().mcp_tokens_dir`
   (design lines 13, 14, 30, 118, 164; frozen proposal.md:9; brief line 42).
   ADR-0019 citation corrected: tokens in STATE bucket (secret-like, analogous
   to secrets.toml), not DATA, with explicit justification and commitment #1
   (single resolver in xdg.py) honored. DATA-vs-STATE decision now explicit.
 - oauth_cancel handling added (Decision 6, line 141): cancel button resolves
   the future with an error, closes CallbackServer, releases the port, frees the
   single-flow lock, and aborts the SDK async_auth_flow cleanly.
 - Cert-expiry-at-auth-time risk added (line 148): CallbackServer.start()
   validates cert/key and fails fast with a descriptive error instead of an
   opaque 300s timeout.

### 🟡 Addressed
 - SDK internal verified: `if not self.context.client_info:` confirmed at
   mcp/client/auth/oauth2.py:572 (mcp 1.27.0). Decision 3 pre-seeding is sound.
 - Wording nit (line 120): Decision 4 rationale still said "XDG data directory
   layout" — fixed to "XDG Base Directory layout" now that tokens are in STATE.
 - Carry into tasks phase: adding mcp_tokens_dir to the frozen XDGPaths dataclass
   requires (a) the field lives in xdg.py only, and (b) per ADR-0019 commitment
   #1, directory creation happens exclusively in main.py (_create_xdg_dirs), not
   in FileTokenStorage. Capture as a task.

### 🔴 Outstanding
 - None. Design batch is ready to freeze and advance to specs.

## specs Round 1 — 2026-08-04

### 🔴 Fixed
 - None (first review of the specs batch).

### 🟡 Addressed
 - Verified mcp-transport MODIFIED requirements are full copies (not partial
   diffs) against openspec/specs/mcp-transport/spec.md: connection lifecycle
   (6 existing scenarios preserved + 2 new OAuth boot scenarios), server status
   reporting (3 preserved, needs_auth added to "List all"), status display
   contracts (4 preserved + 1 new needs_auth display). needs_auth added to all
   three required requirements; boot refresh-token boundary split correctly.
 - Verified mcp-oauth-flow covers all required behaviors: flow lifecycle,
   ephemeral callback server, token storage (ADR-0019 STATE bucket + xdg_paths()
   resolution + main.py dir creation), auto-refresh, OAuth status reporting,
   config schema w/ defaults, concurrent-flow rejection, cancel teardown, cert
   validation before posting link, pre-seeded client info (no DCR). Format valid
   (## ADDED Requirements, ### Requirement:, #### Scenario:, GIVEN/WHEN/THEN);
   every requirement has >=1 scenario. Capability names match frozen proposal
   (mcp-oauth-flow new, mcp-transport modified); no scope drift.
 - Should-fix (non-blocking):
   * Re-auth of an already-authenticated server (brief line 53: "new tokens
     replace old in storage") has no scenario — the successful-flow scenario is
     scoped to "no stored token." Added a token-replacement scenario.
   * "OAuth config requires client_id and client_secret" only tested missing
     client_id — split into two scenarios (missing client_id, missing
     client_secret).

### 🔴 Outstanding
 - None. Specs batch is ready to advance to ADR/tasks.

## tasks Round 1 — 2026-08-04

### 🔴 Fixed
 - None (first review of the tasks batch).

### 🟡 Addressed
 - Full coverage map verified: every frozen spec requirement maps to tasks;
   design Decisions 1-6 all implemented; all 8 checklist items present.
   Dependency order matches the required chain exactly (config → xdg → storage
   → callback → factory → mcp client → telegram → wiring → validation). Format
   correct (- [ ] checkboxes under ## numbered headings); granularity <2h each.
   Validation section runs openspec validate --strict plus ruff/vulture/pytest
   (AGENTS.md make check). ADR-0019 compliance explicit (2.3: dir creation in
   main.py, not FileTokenStorage).
 - Should-fix before apply:
   * #1 (coverage gap vs frozen spec) — Task 8.3 implements the operator message
     for a failed 401 refresh but NOT the required status transition to
     needs_auth. Amend 8.3 to set the server status to needs_auth.
   * #2 — Re-auth token replacement (brief line 53) only implicit via set_tokens
     overwrite (3.4); add an explicit test to 3.7/6.7.
   * #3 — state provisioning unwired: 4.3 validates state against an "expected
     value" never threaded into CallbackServer. Clarify: pass SDK-generated
     state into the server, or rely on SDK state validation and reduce 4.3.

### 🔴 Outstanding
 - None structural. Recommend closing 🟡 #1 (frozen-spec coverage gap) before
   advancing to apply; #2 and #3 are quick and best folded in now.

## tasks Round 2 — 2026-08-04

### 🔴 Fixed
 - #1 refresh-failure coverage gap closed: Task 8.3 now returns the operator
   message AND transitions the server to needs_auth, covering both THEN clauses
   of the frozen "Token refresh failure surfaces to operator" scenario.
 - #2 re-auth token replacement pinned: Task 3.7 adds a test that re-auth
   overwrites the existing token file (new tokens replace old) — brief line 53.
 - #3 state provisioning wired: Task 4.1 adds an expected_state field (set after
   the SDK generates the auth URL); 4.3 validates against it when set and notes
   the SDK's internal secrets.compare_digest check (defense-in-depth). 4.6's
   mismatched-state test now has a source of truth.

### 🟡 Addressed
 - Re-verified no regressions from the edits; coverage map still complete and
   consistent with both frozen specs. Two optional items remain (non-blocking):
   needs_auth display label in telegram_formatter; a [mcp_servers.oauth]
   config.toml example in operator docs.

### 🔴 Outstanding
 - None. Tasks batch is complete. The full proposal → design → specs → tasks
   chain has been reviewed and is consistent end-to-end; ready for /opsx-apply.