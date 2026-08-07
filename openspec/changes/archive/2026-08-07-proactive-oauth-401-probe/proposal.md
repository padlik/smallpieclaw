## Why

The interactive OAuth flow (`_run_oauth_flow` in `mcp_client.py`) relies on the MCP SDK's `OAuthClientProvider` — a lazy, 401-triggered `httpx.Auth` flow. The SDK only fires the OAuth handshake (discovery → `redirect_handler` → `callback_handler` → token exchange) when an HTTP request receives a 401 response. Some MCP servers (notably Google's gmail bridge) allow unauthenticated `initialize` and `tools/list` — they return 200 without challenging. Because the SDK's `async_auth_flow` only enters the OAuth branch on 401, `redirect_handler` is never called, no authorization URL is produced, and the Telegram inline button is never sent. The operator sees a warning: "OAuth flow returned success but no token file found — redirect_handler may not have fired." The flow appears to succeed but no token is ever acquired.

## What Changes

- Add a **proactive 401-probe step** to `_run_oauth_flow` in `mcp_client.py`: before connecting the MCP session, make a standalone `httpx.AsyncClient(auth=provider)` GET request to the server URL. The 401 response triggers the SDK's full `async_auth_flow` — discovery, registration, `redirect_handler` (sends link to TG), `callback_handler` (waits for redirect), token exchange, `storage.set_tokens()`. Then connect the session normally with the now-valid token.
- The probe request carries the `MCP-Protocol-Version` header so the SDK can correctly decide whether to include the RFC 8707 `resource` parameter.
- The probe step is cancellable: if the operator taps Cancel while waiting for the redirect, the probe's `httpx.AsyncClient` is closed and the flow aborts.
- When the probe returns 200 (server doesn't require OAuth on this endpoint), the flow proceeds to connect the session normally. The existing "no token file found" warning is suppressed in this case since the probe confirmed the server didn't challenge.
- No changes to token storage, callback server, redirect handler, or config schema. The probe reuses the existing `OAuthClientProvider` and all its machinery.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mcp-oauth-flow`: The interactive OAuth flow now proactively triggers the SDK's authorization handshake via a standalone HTTP probe, rather than relying on a 401 during `session.initialize()`. This ensures `redirect_handler` fires for servers that allow unauthenticated discovery.

## Impact

- **`mcp_client.py`**: `_run_oauth_flow` gains a probe step between provider construction and session connection. New logging at the probe boundary. The existing "no token file" warning is conditioned on whether the probe triggered an authorization challenge (401 or 403).
- **`mcp_oauth.py`**: No changes — the existing `OAuthProviderFactory`, `make_redirect_handler`, `make_callback_handler`, and `FileTokenStorage` are reused as-is.
- **`config_schema.py`**: No changes — no new config fields.
- **`telegram_commands.py` / `telegram_callbacks.py`**: No changes — the cancel mechanism already exists and works with the probe.
- **Tests**: New tests for the probe step in `tests/test_mcp_client.py` or `tests/test_mcp_oauth.py` — probe triggers 401 → redirect_handler called; probe returns 200 → no redirect_handler, session connects; probe cancellation. Existing `tests/test_mcp_oauth_logging.py` warning assertions (which currently expect the "no token file found" warning unconditionally) must be updated to reflect the probe-conditioned warning behavior.