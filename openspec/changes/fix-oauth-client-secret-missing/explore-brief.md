# Explore Brief — fix-oauth-client-secret-missing

## Problem

OAuth token exchange with Google (Gmail MCP server) fails with `400 client_secret is missing`.

## Root Cause

`FileTokenStorage.get_client_info()` in `mcp_oauth.py` pre-seeds an `OAuthClientInformationFull` with `client_id`, `client_secret`, and `redirect_uris=None`, but does NOT set `token_endpoint_auth_method`. The MCP SDK's `prepare_token_auth()` reads `self.client_info.token_endpoint_auth_method` (which is `None`), so neither `client_secret_basic` nor `client_secret_post` branch fires — no `client_secret` is sent in the token exchange request.

Meanwhile, `OAuthProviderFactory.build()` sets `token_endpoint_auth_method="client_secret_basic"` on `client_metadata` (a different object). The two-object drift means the metadata value never reaches `prepare_token_auth`.

## Alternatives Rejected

- **Option A (hardcode in pre-seed only):** Fixes the reproduced case but leaves the cached-client_info return path (lines 164-183) latent-broken. A cached block persisted with `exclude_none=True` reloads `token_endpoint_auth_method` as `None`.
- **Option B (config field on OAuthConfig):** Scope creep — `OAuthConfig` has no such field today, and the value is already declared on `client_metadata`. Config plumbing is unnecessary for a hotfix.

## Final Approach (Option C)

1. Add a defaulted constructor param `token_endpoint_auth_method: str = "client_secret_basic"` to `FileTokenStorage.__init__`. Default keeps all 3 non-test construction sites + 8 test sites compiling unchanged — zero blast radius.
2. In `OAuthProviderFactory.build()`, pass `token_endpoint_auth_method=client_metadata.token_endpoint_auth_method` so storage and metadata can never drift.
3. In `get_client_info()`, normalize BOTH return paths:
   - Pre-seed (line 185): add `token_endpoint_auth_method=self.token_endpoint_auth_method`.
   - Cached (line 178): fill-if-None via `model_copy(update={...})` — preserves a legitimate DCR-provided method for non-Google providers while guaranteeing the header for Google.

## Cross-Module Data Flow

```
config_schema.py  →  mcp_oauth.OAuthProviderFactory.build()
  OAuthConfig         client_metadata = OAuthClientMetadata(
  client_id             token_endpoint_auth_method="client_secret_basic"
  client_secret         scope=...
                      )
                      storage = FileTokenStorage(
                        client_id=oauth_cfg["client_id"],
                        client_secret=oauth_cfg["client_secret"],
                        token_endpoint_auth_method=client_metadata.token_endpoint_auth_method  ← NEW
                      )
                      → OAuthClientProvider(storage=storage)

MCP SDK (oauth2.py):
  _initialize() → storage.get_client_info() → context.client_info
  async_auth_flow() Step 4: if not client_info → skip DCR (pre-seed is truthy)
  _exchange_token_authorization_code() → prepare_token_auth()
    reads context.client_info.token_endpoint_auth_method  ← was None, now "client_secret_basic"
    → sends Basic auth header with client_id:client_secret
```

## Open Questions

- None blocking. Future enhancement: make `token_endpoint_auth_method` configurable via `OAuthConfig` for `client_secret_post`-only providers. Not in scope for this fix.