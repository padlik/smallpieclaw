## Why

The proactive OAuth probe (`_probe_oauth_challenge` in `mcp_client.py`) sends a GET request to the MCP server URL to trigger a 401 that fires the SDK's OAuth handshake. Gmail's MCP server (`gmailmcp.googleapis.com/mcp/v1`) only accepts POST on its streamable-http endpoint and returns 405 on GET. Since 405 is not an auth challenge (401/403), the probe reports "server did not require OAuth," no auth URL reaches Telegram, and the session connects without a token. OAuth only fires on the first `tools/call` — not during `/mcp auth` — leaving the operator without the inline authorization button the feature was built to deliver.

## What Changes

- Change the probe from GET-only to GET→405→POST retry: if the GET probe returns 405, retry with a POST containing a JSON-RPC `tools/call` body to a dummy tool name (`_oauth_probe`).
- The POST probe carries MCP transport headers matching the SDK's `streamable_http` transport: `Accept: application/json, text/event-stream`, `Content-Type: application/json`, and the existing `MCP-Protocol-Version: 2025-11-25`.
- The POST body is a valid JSON-RPC 2.0 request: `jsonrpc: "2.0"`, `method: "tools/call"`, `params: {"name": "_oauth_probe", "arguments": {}}`, `id: <random UUID>`.
- The existing httpx response event hook (`_on_response`) continues to detect 401/403 on the POST probe, setting `probe_saw_auth_challenge = True` so the SDK's `async_auth_flow` fires the full handshake (discovery → redirect_handler → callback → token exchange).
- If the POST probe also returns 405 (or any other unexpected status), the existing WARNING + session-connection fallback applies unchanged.
- If the POST probe returns 200, the server allows unauthenticated `tools/call`; no OAuth is triggered and the session connects normally (same as GET 200).
- No changes to the session connection path, callback server, token storage, or cancellation logic.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-oauth-flow`: The proactive OAuth probe now retries with a POST `tools/call` request when the initial GET probe returns 405, enabling OAuth handshake triggering for MCP servers that only accept POST (e.g. Gmail).

## Impact

- **Code**: `mcp_client.py` — `_probe_oauth_challenge` method (lines ~948-995). The GET `client.get()` call becomes a GET-then-POST-on-405 sequence. The POST request body and headers are new constants.
- **Tests**: `tests/test_mcp_oauth_probe.py` — existing tests that patch `_probe_oauth_challenge` are unaffected (they bypass the real method). New tests needed for the POST retry path: GET 405 → POST 401 → OAuth fires; GET 405 → POST 200 → no OAuth; GET 405 → POST 405 → WARNING + fallback. The `TestProbeInternalLogging` tests that drive the real method via `MockTransport` need new handlers returning 405 on GET and 401/200 on POST.
- **Spec**: `openspec/specs/mcp-oauth-flow/spec.md` — delta spec adds the 405→POST retry scenario.
- **Dependencies**: No new dependencies. Uses existing `httpx.AsyncClient` and `uuid` stdlib.
- **Risk**: Low. The change is additive (retry on 405 only). Servers that accept GET are unaffected — the GET probe succeeds and no POST is sent. The POST body is a valid JSON-RPC request that auth middleware rejects with 401 before the protocol layer processes it.