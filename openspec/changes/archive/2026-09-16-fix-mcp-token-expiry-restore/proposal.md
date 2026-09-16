## Why

Access tokens for OAuth-configured MCP servers (e.g. `google-workspace`) expire silently ~1 hour after every interactive authorization, and the next tool call then degrades to `needs_auth` with a "run `/mcp auth`" error — even though a valid refresh token is on disk. Root cause (verified in the installed mcp 1.29.1 SDK source): the SDK's `_initialize()` loads stored tokens into a reconstructed provider but never restores `token_expiry_time`, so `is_token_valid()` treats the loaded token as perpetually valid and the proactive-refresh branch is unreachable; the first 401 then goes straight to a full authorization flow, which the long-lived non-interactive session (ADR-0030) correctly aborts. A second gap makes the fix non-optional: `_refresh_token()` aims at `context.oauth_metadata.token_endpoint`, which is also not restored by `_initialize()` — without persisting the discovered token endpoint alongside the token, a restored-expiry provider would POST refresh grants to the wrong URL (`<server-base>/token`, the local MCP proxy) and fail anyway. Both gaps are unmerged upstream (mcp SDK PRs #1784, #2492, #2875 all open; verified absent in 1.30.0, 2.2.0, and `main`), so the fix belongs in our `mcp_oauth.py` storage/provider layer.

## What Changes

- Extend `RefreshPreservingOAuthProvider` with an `_initialize()` override that restores `token_expiry_time` from the stored `issued_at + expires_in` when both are present (missing either leaves today's behavior — no regression for pre-existing token files), making the SDK's proactive silent-refresh branch reachable for providers reconstructed from disk.
- Persist the discovered OAuth authorization-server metadata (at minimum the token endpoint) in the token file after a completed interactive flow, and restore it into `context.oauth_metadata` on `_initialize()`, so silent refreshes POST to the provider's real token endpoint (e.g. `https://oauth2.googleapis.com/token`) instead of the SDK's `<server-base>/token` fallback.
- Extend `FileTokenStorage` with `get_oauth_metadata()` / `set_oauth_metadata()` following the existing `client_info` conventions (atomic write, merge-carry across `set_tokens`, warn-and-fallback on malformed blocks), plus an internal read path exposing `issued_at` to the provider.
- No SDK version change: the pin stays `mcp>=1.29.1,<2.0` (newer lines verified unfixed; 2.x would additionally break the existing `_handle_refresh_response` override surface and require an `httpx2` migration). Existing drift-guard tests gain companions pinning the two new SDK couplings (`_initialize` behavior, `_refresh_token` metadata dependence).

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mcp-oauth-flow`: The "Automatic token refresh" requirement changes from relying on the SDK's in-memory-only expiry tracking to a restore contract: a provider reconstructed from persisted tokens SHALL know the stored token's absolute expiry and the provider's token endpoint, so silent refresh is attempted proactively before any request goes out with a stale access token. The "File-backed token storage" requirement's persisted-schema scenarios gain the optional `oauth_metadata` block and the new storage read/write methods.

### Non-goals

- Interposing a refresh attempt inside the SDK's 401 branch (upstream #2875 pattern) is out of scope — the proactive path plus restored expiry covers the verified defect; revisit only if upstream lands it.

## Impact

- **Code**: `mcp_oauth.py` (all changes confined to `FileTokenStorage` + `RefreshPreservingOAuthProvider`; `mcp_client.py` wiring untouched — both build sites already construct the subclass). `vulture_whitelist.py` for the new public storage methods. `tests/test_mcp_oauth.py` for unit + drift-guard + reconstructed-provider refresh tests.
- **Token file schema**: `mcp_tokens/<server>.json` gains an optional top-level `oauth_metadata` block written after interactive flows; the `token` block (including `issued_at`) is unchanged. Old files load unchanged (no `oauth_metadata` → metadata restore skipped; no `issued_at` → expiry restore skipped).
- **Dependencies**: none (pin unchanged).
- **Operator-facing behavior**: after one re-authorization following deploy, silent refresh sustains across expiries (the scheduled auth-check job stays green); until then, behavior is exactly today's. Status display (`/mcp auth status`) output is unchanged.
- **Docs**: README's MCP OAuth token-file description (currently stale re: `expires_at`) is updated to the actual `issued_at`+`expires_in` schema plus the new `oauth_metadata` block.