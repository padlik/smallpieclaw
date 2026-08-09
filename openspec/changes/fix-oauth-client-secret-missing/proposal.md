## Why

OAuth token exchange with Google (Gmail MCP server) fails with `400 client_secret is missing`. The root cause is a two-object drift: `OAuthProviderFactory.build()` sets `token_endpoint_auth_method="client_secret_basic"` on `client_metadata`, but the MCP SDK's `prepare_token_auth()` reads from `client_info.token_endpoint_auth_method` — a different object. The pre-seeded `OAuthClientInformationFull` returned by `FileTokenStorage.get_client_info()` omits this field, so `prepare_token_auth()` sees `None` and sends no `client_secret` in the token exchange request — neither via HTTP Basic auth nor in the form body. This blocks all OAuth-protected MCP servers that require `client_secret_basic` or `client_secret_post` authentication at the token endpoint (Google being the primary use case).

## What Changes

- Add a defaulted constructor parameter `token_endpoint_auth_method: str = "client_secret_basic"` to `FileTokenStorage.__init__`. The default is the sole source of the value; it is also the seam a future config knob plugs into.
- Include `token_endpoint_auth_method` in the pre-seeded `OAuthClientInformationFull` returned by `get_client_info()`. This is the entire behavioral fix.
- Add regression tests for the pre-seed path, the constructor default, and the pre-existing malformed-`client_info` fallback.

**Deliberately not changed** (both were in an earlier draft of this proposal and were
removed after a code review proved them inert — see `review-log.md` and the
`explore-brief.md` addendum):

- `OAuthProviderFactory.build()` does **not** pass the value into `FileTokenStorage`. `build()` hardcodes `token_endpoint_auth_method="client_secret_basic"` on `client_metadata` and `oauth_cfg` has no key for it, so reading it back to pass into storage — whose default is the same string — is a tautology that cannot prevent drift. Verified by mutation: deleting the kwarg left the whole suite green.
- The cached return path of `get_client_info()` is **not** normalized. It is unreachable: `get_client_info()` never returns `None`, so both of the SDK's `set_client_info` calls (each guarded by `if not self.context.client_info:`) never run and no `client_info` block is ever written.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mcp-oauth-flow`: The pre-seeded client info returned by `FileTokenStorage.get_client_info()` SHALL include `token_endpoint_auth_method` so the SDK's `prepare_token_auth()` sends `client_secret` in the token exchange. Only the pre-seed return path is normalized; the cached path is left unchanged.

## Impact

- **`mcp_oauth.py`**: three added lines — `FileTokenStorage.__init__` gains a defaulted param and stores it; the `get_client_info()` pre-seed passes it to `OAuthClientInformationFull`. `OAuthProviderFactory.build()` and the cached return path are untouched.
- **`mcp_client.py`**: Two non-test `FileTokenStorage` construction sites (`_prepare_oauth_provider` and `get_token_info`) are unaffected — the new param is defaulted, so they compile and behave unchanged.
- **Tests**: Regression tests in `tests/test_mcp_oauth.py` for the pre-seed value, the constructor default, and the pre-existing malformed-block fallback, plus a `build()`-level assertion that the value reaches the provider's storage. Also repairs `test_token_storage_preserves_client_info`, which asserted only facts that were equally true of the pre-seed fallback and so never exercised the cached path it names. Existing constructions compile unchanged due to the defaulted param.
- **No config schema changes.** The value is hardcoded via the constructor default.
- **Accepted trade-off**: `client_secret_basic` is now asserted for *every* OAuth-protected MCP server, not just Google. A provider whose client is registered as public/PKCE-only could regress from working (no client authentication sent) to `401 invalid_client`. No such provider is configured or exercised today. Making the method configurable via `OAuthConfig` is deferred to its own proposal.