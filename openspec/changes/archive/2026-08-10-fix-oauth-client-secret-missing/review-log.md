## proposal Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Impact says the two mcp_client.py sites are unaffected "because they never call get_client_info()" — stronger than the brief (which only guarantees they compile unchanged). Safe regardless (default matches build()), but verify the no-call claim or soften the wording.
- Why explains the symptom/mechanism but names the two-object drift root cause only in What Changes; consider a one-clause mention in Why for self-containment.
### Brief coverage
- Two-object drift (client_metadata vs client_info): captured
- Both return paths in get_client_info(): captured
- Fill-if-None for cached path (not force-override): captured
- Defaulted constructor param / zero blast radius: captured
- Source from client_metadata, not a new config field: captured
### Verdict
- PASS — ready to freeze; both suggestions are non-blocking.

## design Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Risk 2 mitigation claimed the two mcp_client.py sites "never call get_client_info()" — unverified and likely wrong. Softened to: even if they reach get_client_info(), the default matches build()'s value, so behavior is unchanged.
- Pre-seed normalization lacked its own Decision heading. Added Decision 4 for traceability and symmetry with Decision 3.
- "Behave unchanged" in Goals was imprecise — the fix changes runtime behavior (sends client_secret). Clarified to "compile without modification; only the OAuth-flow sites gain the corrected auth header."
### Brief coverage
- Two-object drift (client_metadata vs client_info): captured
- Both return paths in get_client_info(): captured (Decision 3 + Decision 4)
- Fill-if-None for cached path (not force-override): captured
- Defaulted constructor param / zero blast radius: captured
- Source from client_metadata, not a new config field: captured
- Alternatives A & B rejected with reasons: captured
- Cross-module data flow: captured (fixed flow diagram)
- Open question (config-driven method = future scope): captured
### Verdict
- PASS — ready to freeze; all suggestions applied.

## specs Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Requirement prose leaked `model_copy(update={...})` implementation detail. Removed — the observable contract is "fill-if-None, preserve-if-set" which the scenarios already state cleanly.
### Brief coverage
- Defaulted constructor param: captured
- build() passes client_metadata.token_endpoint_auth_method: captured
- Pre-seed path adds auth method: captured (modified scenario)
- Cached path fill-if-None (not force-override): captured (two cached scenarios)
- Sourcing from client_metadata (not new config): captured
- Two-object drift addressed: captured ("prevent drift between the two objects")
- Both return paths have scenarios: captured
- Observable fix outcome (client_secret reaches token exchange): captured
### MODIFIED Requirements correctness
- Header match: exact
- Full requirement content (not partial diff): all 5 original scenarios preserved
- Pre-seeded scenario correctly modified
- New cached-path scenarios added
### Verdict
- PASS — ready to freeze; suggestion applied.

## tasks Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Task 1.4 line reference "between line 176 and 178" was fragile to line drift. Reworded to "inside the if cached.client_secret == self.client_secret: block, immediately before return cached".
- Task 3.1 vulture invocation omitted --exclude interfaces.py (drifted from make lint). Aligned with Makefile.
### Coverage check
- Decision 1 (defaulted ctor param): task 1.1 ✓
- Decision 2 (build() sources from client_metadata): task 1.2 ✓
- Decision 3 (fill-if-None on cached path): task 1.4 ✓
- Decision 4 (pre-seed includes auth method): task 1.3 ✓
- Spec scenario: pre-seed returns token_endpoint_auth_method: task 2.1 ✓
- Spec scenario: cached None repaired: task 2.2 ✓
- Spec scenario: cached non-None preserved: task 2.3 ✓
- Backward compat (default value): task 2.4 ✓
- Verification (lint, test, openspec validate): tasks 3.1-3.4 ✓
### Verdict
- PASS — ready to freeze; all suggestions applied.

## UNFREEZE — 2026-08-09 (post-implementation code review)

A workflow-backed code review at high effort (23 agents, 24 candidates verified →
18 kept → 9 deduped findings) surfaced two **decision-level** defects in frozen
artifacts. Per the unfreeze rule, the affected artifact and everything downstream
of it are unfrozen.

### Unfrozen artifacts
- `proposal.md`, `design.md`, `specs/mcp-oauth-flow/spec.md`, `tasks.md` — the
  full chain.

### Why the chain starts at `proposal.md`, not `design.md`
Both defects are decision-level, and `proposal.md` **What Changes** commits to
both mechanisms by name — "sourced from `client_metadata.token_endpoint_auth_method`
in `OAuthProviderFactory.build()` so storage and metadata can never drift" and
"Cached path: fill-if-None via `model_copy(update={...})`" — as does **Modified
Capabilities** ("Both the pre-seed and cached return paths are normalized").
Revising either mechanism changes what an implementer writes, so the proposal
cannot stay frozen as a declarative-only edit.

### Decision-level findings driving the unfreeze
1. **Decision 2 is a no-op (CONFIRMED).** `build()` hard-codes
   `token_endpoint_auth_method="client_secret_basic"` on `client_metadata`, then
   reads it back to pass into `FileTokenStorage`, whose default is the same
   string. `oauth_cfg` has no key for it. Deleting the kwarg changes no behavior
   — verified by mutation: the whole suite stayed green. So the stated goal
   "single source of truth ... preventing future drift" is not achieved by the
   round-trip, and the auth method remains unconfigurable. Related: this forces
   `client_secret_basic` on *every* OAuth MCP server, which can regress a
   public/PKCE-registered client from working to `401 invalid_client`
   (PLAUSIBLE — depends on a provider we have not exercised).
2. **Decision 3's repair branch is unreachable in the shipped flow (CONFIRMED).**
   `get_client_info()` never returns `None` (it always falls back to the
   pre-seed), and the SDK's only `set_client_info` call sits behind
   `if not self.context.client_info:` (mcp/client/auth/oauth2.py:572). With a
   truthy pre-seed that guard is always false, so no production run can write a
   `client_info` block. A repo-wide grep confirms no other caller. The
   fill-if-None repair, and the two spec scenarios that lock in its behavior,
   describe a state the system cannot currently reach.

### Not part of this unfreeze
Implementation-level findings from the same review (`str` vs the SDK's `Literal`
annotation; `set_client_info`'s lossy `exclude_none=True` write) are code-level
and do not require artifact changes. The test-coverage and test-sensitivity
findings were fixed directly in `tests/test_mcp_oauth.py` and are unaffected by
the unfreeze.

### Operator decisions (2026-08-09)
1. **Delete the `build()` indirection.** Keep the constructor default as the single
   hardcoded source. Record the public/PKCE regression as an accepted trade-off and
   defer a real `OAuthConfig` knob to its own proposal.
2. **Delete the fill-if-None repair**, its two spec scenarios, and its tests. The
   pre-seed change alone is the whole fix.

### Review agent
- The `@openspec-reviewer` subagent type is not registered in this session. At the
  operator's direction the round was delegated to a `general-purpose` agent carrying
  the reviewer's instructions, with `explore-brief.md` (as amended by its addendum)
  attached as the baseline. Both rounds below used that stand-in.

## post-unfreeze (proposal + design + specs + tasks) Round 1 — 2026-08-09
### 🔴 Fixed
- `specs/mcp-oauth-flow/spec.md` "Token file created with restrictive permissions"
  asserted the token file contains `client_info` and `expires_at`, contradicting this
  batch's own claim that nothing writes a `client_info` block. Independently verified
  by running `set_tokens`: a normal flow writes only `{"token": {...}}` with
  `issued_at`/`expires_in`, no `client_info` and no `expires_at`. The clause was a
  pre-existing inaccuracy carried from the main spec, but a MODIFIED requirement
  restated in full must not carry a claim the same batch disproves — corrected in
  place rather than declared out of scope, since this delta syncs into the main spec.
- `adr.md` still credited the change with "fill-if-None on cached path, sourcing from
  `client_metadata`" — both deleted. Now lists only the two shipped choices, with a
  note that neither removed mechanism reached the implementation.
- `specs/.../spec.md` prose asserted "The cached return path SHALL return the
  persisted block unmodified", which the `client_secret`-rotation branch contradicts
  (on mismatch it logs and falls through to the pre-seed). Reworded to state the
  match condition and the rotation fall-through explicitly.
### 🟡 Addressed
- "the SDK's **only** `set_client_info` call" was factually wrong — there are two
  (CIMD and DCR paths, oauth2.py:583 and :594). Both sit behind the `:572` guard, so
  the unreachability conclusion holds; wording corrected in `design.md`, `proposal.md`,
  and the brief addendum.
- The recorded trade-off overstated itself: `_require` checks key presence only and
  the SDK's basic branch needs a *truthy* secret, so `client_secret = ""` sends no
  client authentication and does not regress. Narrowed to non-empty placeholders.
- Drifted line references (`mcp_oauth.py:392`, "line 185") replaced with symbol
  anchors in `design.md` and `tasks.md`.
- An assertion added to `test_build_returns_provider` was untraceable to any task →
  recorded as task 2.5, noting it pins the constructor default arriving via `build()`,
  not a kwarg.
- `proposal.md` Impact omitted the `test_token_storage_preserves_client_info` repair.
### 🔴 Outstanding
- Three blockers above (fixed in Round 2's input).
### Verdict
- FAIL — three blocking items.

## post-unfreeze (proposal + design + specs + tasks) Round 2 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed
- Singular/plural `set_client_info` carryover in `proposal.md` (the Round-1 fix had
  landed in `design.md` and the brief but not here).
- Last hardcoded line numbers in `proposal.md` Impact replaced with symbol anchors.
- The rewritten permissions scenario over-promised optional token fields:
  `exclude_none=True` drops `refresh_token`/`scope`/`expires_in` when the provider
  omits them. Qualified to "the fields the provider returned ... when granted".
- `design.md` "No token file on disk contains a `client_info` block" was absolute
  where the spec is conditional → "The agent never writes a `client_info` block".
### Deliberately not applied (reviewer concurred both are correct as-is)
- The pre-seed scenario's `AND` about `prepare_token_auth` sending the Basic header
  rests on verified SDK behavior rather than a regression test. Pinning it would mean
  asserting on SDK internals, which costs more than the documentation value.
- `set_client_info`'s lossy `exclude_none=True` remains unfixed: pre-existing,
  orthogonal, and observable only on the path this change proves unreachable. Carried
  into the deferred `OAuthConfig` proposal, where a reachable cached path could exist.
### Brief coverage (as amended by the addendum)
- Problem statement / root cause (two-object drift): captured
- Option A rejection: VOID per addendum — correctly asserted nowhere
- Option B rejection (config field = scope creep): captured
- Step 1 (defaulted ctor param, zero blast radius): captured
- Step 2 (`build()` sources from `client_metadata`): VOID — recorded as deliberately
  not done; verified absent from the code
- Step 3a (pre-seed includes the auth method): captured
- Step 3b (cached fill-if-None): VOID — recorded as deliberately not done; no
  `model_copy` remains in code or artifacts
- Cross-module data flow: captured, diagram amended to mark `build()` UNCHANGED
- Addendum: cached path unreachable, with evidence: captured
- Addendum: `client_secret_basic` forced for all servers as an accepted trade-off:
  captured, with the empty-secret carve-out
- Addendum: `OAuthConfig` knob deferred to its own proposal: captured
### MODIFIED Requirements correctness
- Header match: exact against `openspec/specs/mcp-oauth-flow/spec.md`
- Full content preserved: all 5 original scenarios intact plus the malformed-fallback
  addition. The only removals were the two cached-path scenarios this change had
  itself added; no originally-present scenario was lost.
- Scenario accuracy: verified against code, not just against the artifacts. The
  permissions scenario matches `set_tokens` field-for-field; pre-seed and
  malformed-fallback each map to a passing test.
### Verdict
- PASS — ready to freeze. Four cosmetic suggestions from this round were applied
  afterwards; none affected a decision, a scenario's truth value, or shipped code.

### Status
- All four artifacts re-frozen per the single-round pass rule (4a).
- Gates after every edit: `ruff` clean, `vulture` clean, `openspec validate --strict`
  valid, `pytest tests/test_mcp_oauth.py` 18 passed, `make check` 1630 passed /
  1 skipped (1627 baseline + 3 net new tests).
- Production diff: exactly 3 added lines in `mcp_oauth.py`.