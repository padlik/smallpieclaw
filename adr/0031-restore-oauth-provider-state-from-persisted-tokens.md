# 0031-restore-oauth-provider-state-from-persisted-tokens

## Status

Accepted

## Date

2026-09-16

## Supersedes

None

## Context

Every interactive `/mcp auth` hands off to a fresh non-interactive long-lived session (ADR-0030), whose OAuth provider reconstructs from the on-disk token file. The MCP SDK (1.29.1, `client/auth/oauth2.py`) `_initialize()` loads `current_tokens` and `client_info` from storage but restores neither `token_expiry_time` nor `oauth_metadata`:

- With `token_expiry_time` unset, `is_token_valid()` short-circuits (`not self.token_expiry_time`), so a loaded token passes as perpetually valid; the proactive-refresh branch (`not is_token_valid() and can_refresh_token()`) is unreachable, and the first request with the stale token draws a 401 that goes straight to a full authorization flow — which the non-interactive session aborts into `needs_auth`. Access tokens expired ~1 hour after every authorization despite a valid refresh token on disk.
- With `oauth_metadata` unset, `_refresh_token()` falls back to `urljoin(<server-base>, "/token")` — the local MCP proxy URL, not the provider's token endpoint — so even a provider that knew its token was expired would aim the refresh grant at the wrong URL.

Upstream fix PRs are all open (#1784 expiry restore, #2492 metadata persistence, #2875 401-branch refresh); both gaps were verified present in mcp 1.30.0, 2.2.0, and `main` as of 2026-09-16, so no SDK upgrade fixes this.

## Considered Options

- **Restore in our provider/storage layer (chosen)**: subclass `_initialize()` to restore `token_expiry_time = issued_at + expires_in` and `oauth_metadata` from the token file; persist the discovered `OAuthMetadata` block after a completed token exchange; carry it forward across `set_tokens` writes. Fixes live in our layer; storage method names match PR #2492's proposed protocol extension, so an upstream landing makes our storage forward-compatible.
- **Upgrade the SDK**: rejected — gaps verified unfixed in all release lines; 2.x additionally requires an `httpx2` migration and breaks the existing `_handle_refresh_response` override surface (ADR-0029).
- **Interpose a refresh attempt inside the SDK's 401 branch (PR #2875 pattern)**: rejected — requires reimplementing the SDK's discovery/registration steps inside the 401 generator; high private-API drift risk; the proactive path plus restored expiry covers the verified defect.
- **Standalone metadata discovery at startup**: rejected — duplicates the SDK's discovery chain (PRM → OASM); one manual `/mcp auth` per server after deploy is the migration instead.
- **Persist an absolute `expires_at` field**: rejected — duplicates `issued_at + expires_in` already persisted; a single source of truth avoids the two drifting.

## Decision Outcome

A provider reconstructed from persisted tokens SHALL know the stored token's absolute expiry and the authorization server's token endpoint:

- The provider subclass restores `token_expiry_time` from the stored `issued_at + expires_in` when both are present (missing either skips the restore — pre-existing files degrade to prior behavior).
- The token file carries an optional `oauth_metadata` block (the SDK `OAuthMetadata` model, persisted via `get_oauth_metadata()` / `set_oauth_metadata()` after a completed token exchange); the provider restores it into the SDK context on initialization, so silent refreshes POST to the real token endpoint.
- `set_tokens` carries the block forward across writes (the ADR-0029 merge-preserve lesson generalized to the new key).
- The SDK pin stays `mcp>=1.29.1,<2.0`; executable drift-guard tests pin the two SDK couplings so an upgrade that changes them fails loudly.

## Consequences

- **Good**: silent refresh sustains across access-token expiries for providers reconstructed from disk (with ADR-0029's retention contract, indefinitely while the refresh token lives); the scheduled auth-check job stays green without manual re-auth.
- **Good**: fix lives in our subclass/storage layer; no SDK fork; storage method names conform to the proposed upstream #2492 protocol, so an upstream landing degrades our code to a passthrough rather than a break.
- **Bad**: two new private-API SDK couplings (`_initialize` does not restore expiry; `_refresh_token` reads `oauth_metadata.token_endpoint`) — each needs an executable drift guard that trips on SDK bumps and forces re-verification; when upstream fixes land, the guards fail and the overrides must be removed.
- **Neutral**: token files written before this change need one manual `/mcp auth` per server to gain the `oauth_metadata` block; until then the first expiry degrades exactly as today (harmless local POST, needs_auth, on-disk refresh token safe because the SDK's failure path clears only in-memory tokens).