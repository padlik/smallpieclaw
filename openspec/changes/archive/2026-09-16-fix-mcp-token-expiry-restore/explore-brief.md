# Explore Brief: fix-mcp-token-expiry-restore

Investigation completed 2026-09-16. Root cause verified from the installed SDK
source (mcp 1.29.1 in `.venv`), the production log store (`/tmp/agent_logs.sqlite`),
and the upstream repository state (fetched `v1.x` branch + `main` of
modelcontextprotocol/python-sdk, PR merge states via `gh`).

## Verified defect — one root cause, two coupled gaps

| ID | Gap | Evidence |
|---|---|---|
| A | SDK `_initialize()` loads `current_tokens` from storage but never restores `token_expiry_time` → `is_token_valid()` returns True forever (`not self.token_expiry_time` short-circuit) → the proactive-refresh branch (`if not is_token_valid() and can_refresh_token()`) is unreachable for ANY provider reconstructed from disk | SDK `oauth2.py:484-492` (init), `:129-134` (validity), `:500-505` (unreachable branch); prod log: token issued 20:10:41 (`has_refresh=True`, `expires_in=3600`) died at 21:10:41, first call 21:26:47 → 401 → full-auth abort |
| B | `_refresh_token()` requires `context.oauth_metadata.token_endpoint`; after `_initialize()` the metadata is None → URL falls back to `urljoin(base_url(server_url), "/token")` = `http://localhost:8000/token` (the local MCP proxy, NOT Google's token endpoint) → even with expiry restored, the first silent refresh would POST to the wrong URL and fail | SDK `oauth2.py:433-439`; upstream issue #1318 "Bug 2" |

Proof that no refresh grant ever reached Google: the SDK logs
`"Token refresh failed: {status}"` on every rejected refresh; the store contains
zero such rows. The trigger is the D3 handoff from the archived
`fix-mcp-oauth-refresh-token-lifecycle` change: it closes the interactive
provider (which held expiry in memory) and registers a fresh non-interactive
wrapper whose provider reconstructs from disk — arming both gaps on every auth.

## Upstream verification (2026-09-16)

- mcp **1.30.0** (final 1.x release, 2026-09-07) and **main / 2.2.0** (latest):
  same `_initialize` gap; the 401 branch still goes straight to full
  authorization and never attempts a refresh (verified by reading both
  branches' `oauth2.py`).
- Fix PRs all **OPEN**: #1784 (expiry restore, open since January), #2875
  (refresh-on-401), #2492 (metadata persistence). #2946 (refresh-token
  preservation) merged into v2 only — it duplicates our existing
  `RefreshPreservingOAuthProvider` override (ADR-0029).
- Conclusion: no upgrade fixes this. Pin stays `mcp>=1.29.1,<2.0`.

## Rejected alternatives

- **Upgrade mcp (1.30.0 / 2.x)** — verified unfixed in all release lines; 2.x
  additionally costs an `httpx2` migration and breaks our
  `_handle_refresh_response` override surface.
- **401-branch refresh fallback (upstream #2875 pattern)** — requires interposing
  inside the SDK's 401 generator (copying discovery/registration steps); high
  drift risk. The proactive path + restored expiry covers the actual defect.
  Possible future enhancement if upstream lands it.
- **Patch/fork the SDK** — keep fixes in our subclass/storage layer
  (established pattern from the previous change).
- **Standalone metadata discovery at startup** — duplicates the SDK discovery
  chain (PRM → issuer selection → OASM); a one-time `/mcp auth` re-run after
  deploy is the documented migration instead.
- **Persist absolute `expires_at`** — equivalent to restoring from the already
  spec'd `issued_at + expires_in`; rejected to keep a single source of truth.

## Final approach

| ID | Change | Files |
|---|---|---|
| 1 | `RefreshPreservingOAuthProvider._initialize()` override: after `super()`, restore `token_expiry_time = issued_at + expires_in` when both are present (otherwise leave None = today's behavior); restore `context.oauth_metadata` from the persisted block when present | `mcp_oauth.py` |
| 2 | `FileTokenStorage`: new `get_oauth_metadata()` / `set_oauth_metadata()` (warn + None on malformed, mirroring the `client_info` fallback pattern); `set_tokens` carries `oauth_metadata` forward alongside `client_info`; an internal read path exposes `issued_at` to the provider | `mcp_oauth.py` |
| 3 | Provider persists metadata at full-auth completion: override `_handle_token_response` → after `super()`, persist `context.oauth_metadata` when set | `mcp_oauth.py` |
| 4 | Drift-guard tests extending the b9b3a24 pattern: pin that SDK `_initialize` still lacks expiry restore; pin that `_refresh_token` still reads `oauth_metadata.token_endpoint`; unit tests for the restore matrix; end-to-end reconstructed-provider silent refresh via a scripted generator harness | `tests/test_mcp_oauth.py` |

Token-file JSON after the change (schema table lands in design.md):
`{ "token": {..., "issued_at"}, "client_info"?, "oauth_metadata"? }`.

## Cross-module data flows

- Interactive auth: `/mcp auth` → `_run_oauth_flow` (`mcp_client.py:1845`) →
  probe 401 → SDK discovery sets `context.oauth_metadata` →
  `_handle_token_response` (our override persists the metadata) →
  `set_tokens` (writes `issued_at`) → D3 handoff (`:1939-1975`) → fresh
  non-interactive provider (`_prepare_oauth_provider`, `:552`) →
  `_initialize` (our override: expiry + metadata restored) → session ready.
- Long-lived expiry: tool call → `async_auth_flow` → `is_token_valid()` False
  (restored expiry) + `can_refresh_token()` True (ADR-0029 contract) →
  `_refresh_token()` POSTs to the restored token endpoint
  (`https://oauth2.googleapis.com/token`) → `_handle_refresh_response` (RT
  re-attach — existing) → `set_tokens` merge-preserve (existing) → retry
  succeeds silently. `needs_auth` path untouched.
- Status display (`/mcp auth status`, `read_status`) unchanged — reads only the
  `token` block.

## Migration

Existing token files lack `oauth_metadata` → after deploy, the first expiry
degrades exactly like today (401 → needs_auth); one manual `/mcp auth` captures
the metadata; silent refresh sustains thereafter. No file rewrite, no config
change, no new dependency.

## Open questions

- OQ1: restore `oauth_metadata` as a full block vs a flat `token_endpoint`
  string → decided in design.md (leaning: full block; round-trips through the
  SDK model, keeps refresh + future flows fed).
- OQ2: observability of the restore path (debug log lines only; no
  `read_status` surface change) → design.md.
- OQ3: `vulture_whitelist.py` entries for new public storage API → tasks.md.