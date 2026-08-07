## Why

The proactive OAuth probe (`_probe_oauth_challenge` in `mcp_client.py`) sends a GET request to trigger a 401 that fires the SDK's OAuth handshake. Gmail's MCP server (`gmailmcp.googleapis.com/mcp/v1`) returns 200 on unauthenticated GET, 200 on unauthenticated POST `tools/list`, and 200 on unauthenticated POST `tools/call` with a dummy tool name. It only returns 401 on `tools/call` with a **real** tool name — auth is enforced at the tool execution layer, not the HTTP middleware layer. The current probe never triggers a 401, so the OAuth handshake never fires during `/mcp auth`, and the operator never sees the authorization URL in Telegram.

## What Changes

- Replace the dummy-tool POST probe (`_oauth_probe`) with a two-step discovery probe: POST `tools/list` to get real tool names, then POST `tools/call` with the first real tool name and empty arguments `{}`.
- The 401 from the real tool call fires the SDK's `async_auth_flow` via the probe's `httpx.AsyncClient` (which shares the same `OAuthClientProvider` with `redirect_handler` and `callback_handler`), posting the auth URL to Telegram and waiting for the callback.
- Remove the `_PROBE_TOOL_NAME` constant — the probe no longer uses a hardcoded dummy tool name. The existing `_PROBE_POST_HEADERS` constant is retained and reused for both `tools/list` and `tools/call` POSTs.
- If `tools/list` returns an empty tool list or fails, fall back to session connection with a WARNING (existing safety net, unchanged).
- The GET probe step remains unchanged — if GET returns 401/403, OAuth fires immediately and no POST is sent.
- No changes to the session connection path, callback server, token storage, or cancellation logic.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-oauth-flow`: The proactive OAuth probe now discovers real tool names via `tools/list` and calls `tools/call` with a real tool name to trigger the 401 that fires the OAuth handshake, instead of using a dummy tool name that servers may not challenge.

## Impact

- **Code**: `mcp_client.py` — `_probe_oauth_challenge` method. The POST retry block changes from a single dummy-tool POST to a two-step `tools/list` → `tools/call` sequence. New constants for `tools/list` and `tools/call` JSON-RPC bodies replace `_PROBE_TOOL_NAME`.
- **Tests**: `tests/test_mcp_oauth_probe.py` — existing MockTransport tests for the dummy-tool POST path need updating. New tests for the `tools/list` → real-tool `tools/call` path. New test for `tools/list` returning empty (fallback).
- **Spec**: `openspec/specs/mcp-oauth-flow/spec.md` — update the probe description and scenarios to reflect the `tools/list` discovery step.
- **Dependencies**: No new dependencies. Uses existing `httpx.AsyncClient` and `json` stdlib.
- **Risk**: Low. The change replaces a non-functional probe path with a working one. Servers that challenge on GET (401/403) are unaffected — the GET probe fires OAuth before any POST. Servers that challenge on any `tools/call` (including dummy names) still work — the real-tool `tools/call` also gets a 401. The only new code path is the `tools/list` round-trip, which is a read-only discovery request.