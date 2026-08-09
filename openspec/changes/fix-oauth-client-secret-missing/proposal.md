## Why

OAuth token exchange with Google (Gmail MCP server) fails with `400 client_secret is missing`. The root cause is a two-object drift: `OAuthProviderFactory.build()` sets `token_endpoint_auth_method="client_secret_basic"` on `client_metadata`, but the MCP SDK's `prepare_token_auth()` reads from `client_info.token_endpoint_auth_method` — a different object. The pre-seeded `OAuthClientInformationFull` returned by `FileTokenStorage.get_client_info()` omits this field, so `prepare_token_auth()` sees `None` and sends no `client_secret` in the token exchange request — neither via HTTP Basic auth nor in the form body. This blocks all OAuth-protected MCP servers that require `client_secret_basic` or `client_secret_post` authentication at the token endpoint (Google being the primary use case).

## What Changes

- Add a defaulted constructor parameter `token_endpoint_auth_method: str = "client_secret_basic"` to `FileTokenStorage.__init__`, sourced from `client_metadata.token_endpoint_auth_method` in `OAuthProviderFactory.build()` so storage and metadata can never drift.
- Normalize **both** return paths in `get_client_info()`:
  - Pre-seed path: include `token_endpoint_auth_method` in the constructed `OAuthClientInformationFull`.
  - Cached path: fill-if-None via `model_copy(update={...})` so a stale cached block (persisted with `exclude_none=True`, reloading the field as `None`) is repaired without clobbering a legitimate DCR-provided method for non-Google providers.
- Add regression tests covering both return paths.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mcp-oauth-flow`: The pre-seeded client info returned by `FileTokenStorage.get_client_info()` SHALL include `token_endpoint_auth_method` so the SDK's `prepare_token_auth()` sends `client_secret` in the token exchange. Both the pre-seed and cached return paths are normalized.

## Impact

- **`mcp_oauth.py`**: `FileTokenStorage.__init__` gains a defaulted param; `get_client_info()` normalizes both return paths; `OAuthProviderFactory.build()` passes the method through from `client_metadata`.
- **`mcp_client.py`**: Two non-test `FileTokenStorage` construction sites (`_prepare_oauth_provider` line 411, `get_token_info` line 613) are unaffected: the new param defaults to `client_secret_basic`, matching `build()`'s value, so they compile and behave unchanged.
- **Tests**: New regression tests in `tests/test_mcp_oauth.py` asserting `token_endpoint_auth_method` is set for both pre-seed and cached paths. Existing test constructions of `FileTokenStorage` compile unchanged due to the defaulted param.
- **No config schema changes**: The auth method is sourced from `client_metadata` (already set to `"client_secret_basic"` in `build()`), not from a new config field.