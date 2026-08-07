# Explore Brief: proactive-oauth-401-probe

## Problem

The interactive OAuth flow (`_run_oauth_flow` in `mcp_client.py`) relies on the MCP SDK's `OAuthClientProvider` — a lazy, 401-triggered `httpx.Auth` flow. The SDK only fires the OAuth handshake (discovery → `redirect_handler` → `callback_handler` → token exchange) when an HTTP request receives a 401 response.

Some MCP servers (notably Google's gmail bridge) allow unauthenticated `initialize` and `tools/list` — they return 200 without challenging. Because the SDK's `async_auth_flow` only enters the OAuth branch on 401, `redirect_handler` is never called, no authorization URL is produced, and the Telegram inline button is never sent. The session comes up "ready" with no token file, and the operator sees a warning: "OAuth flow returned success but no token file found — redirect_handler may not have fired."

## Alternatives Rejected

1. **Probe-triggered 401 via a raw tool call** — Make a `tools/call` to a minimal tool to trigger 401. Rejected: depends on knowing a tool name, which requires `list_tools` (which may itself be unauthenticated and return an empty or partial list). Fragile and server-specific.

2. **Replicate the SDK's discovery chain manually** (Shape B from exploration) — Call `_initialize()`, manually fetch PRM + OASM metadata, manually register client, then `_perform_authorization()`. Rejected: reaches deep into SDK internals (`_`-prefixed methods), duplicates discovery logic, fragile across SDK versions, ~100+ lines of security-critical code with no benefit over letting the SDK do it.

3. **Runtime OAuth detection for plain HTTP servers** — Detect at runtime whether an HTTP MCP requires OAuth by probing for 401, rather than relying on config. Rejected as not relevant: the config-driven distinction (`oauth` block present vs absent) is already enforced at the right boundaries (`/mcp auth` gated on `server_has_oauth`, `_run_oauth_flow` only entered for OAuth servers). No runtime detection needed.

## Selected Approach: Standalone httpx Probe (Shape A)

Make a standalone `httpx.AsyncClient(auth=provider)` GET request to the server URL **before** connecting the MCP session. The 401 response triggers the SDK's full `async_auth_flow` — discovery, registration, `redirect_handler` (sends link to TG), `callback_handler` (waits for redirect), token exchange, `storage.set_tokens()`. Then connect the session normally; the provider now has a valid token and adds the Bearer header, so `initialize` succeeds without a second 401.

### Key data flow

```
_run_oauth_flow:
  1. Build provider + callback server (as now)
  2. NEW: probe step
     async with httpx.AsyncClient(auth=provider) as client:
         await client.get(server_url, headers={MCP-Protocol-Version: ...})
     # 401 → SDK fires async_auth_flow → redirect_handler → callback → token exchange
     # tokens now persisted via storage.set_tokens()
  3. Connect session (streamablehttp_client with auth=provider)
     → provider adds Bearer header → initialize succeeds
  4. list_tools → ready_future.set_result
```

### Protocol version header

The SDK reads `MCP-Protocol-Version` from the request header (oauth2.py:498) to decide whether to include the RFC 8707 `resource` parameter (`should_include_resource_param`). The probe request must carry this header. Use the latest protocol version (`2025-11-25`, matching what the server negotiated in the logs).

### Server returns 200 on probe (no OAuth needed)

If the server returns 200 on the probe, the SDK's `async_auth_flow` skips OAuth entirely — no `redirect_handler`, no token. This is the correct behavior for a server that doesn't actually require OAuth. The session then connects normally. The existing warning ("no token file found") should be suppressed or reworded in this case, since the probe confirmed the server doesn't require auth.

### Cancellation

The existing `_watch_cancel` coroutine polls `_oauth_cancel_requested`. The probe step must also be cancellable — if the operator taps Cancel while waiting for the redirect, the probe's `httpx.AsyncClient` should be closed and the flow should abort.

## Cross-module data flows

- `mcp_client.py::_run_oauth_flow` → `mcp_oauth.OAuthProviderFactory.build()` (as now)
- `mcp_client.py::_run_oauth_flow` → `httpx.AsyncClient(auth=provider)` (NEW)
- SDK `OAuthClientProvider.async_auth_flow` → `redirect_handler` (in `mcp_oauth.py`) → Telegram inline button
- SDK `OAuthClientProvider.async_auth_flow` → `callback_handler` (in `mcp_oauth.py`) → `cb_server.wait_for_callback()`
- SDK `OAuthClientProvider.async_auth_flow` → `storage.set_tokens()` (in `mcp_oauth.py`) → token file written
- `mcp_client.py::_run_oauth_flow` → `streamablehttp_client(auth=provider)` (as now, but now with valid token)

## Open questions

1. **Protocol version value**: Should we hardcode `2025-11-25`, read it from config, or try to negotiate it first? The logs show the server negotiated `2025-11-25`, but the probe happens before any MCP negotiation. Hardcoding the latest known version is the pragmatic choice — the `resource` param inclusion is a best-effort enhancement, not a hard requirement.

2. **Probe request method**: GET vs POST vs HEAD. The MCP streamable_http transport uses POST for initialize. A GET is simpler and sufficient to trigger a 401. Some servers might behave differently for GET vs POST on the MCP endpoint. GET is the safer default; if a server doesn't 401 on GET but does on POST, we'd need to revisit.

3. **Probe timeout**: The probe request itself should be quick (it's just triggering a 401). But the overall flow timeout is `oauth_cfg.get("timeout", 300)` — the operator has 5 minutes to complete the browser authorization. The probe's HTTP timeout should be short (e.g. 10s), but the `async_auth_flow` generator internally makes additional requests (discovery, registration) that also need time. Using the overall flow timeout for the httpx client is simplest.

4. **Warning suppression**: The existing warning at mcp_client.py:984 fires when `token_file.exists()` is False after the session is ready. With the probe, if the server returned 200 (no OAuth needed), the token file won't exist — but this is now expected, not a warning condition. Should we downgrade to INFO or suppress entirely when the probe returned 200?