# Explore Brief: oauth-probe-post-method

## Problem

The proactive OAuth probe in `_probe_oauth_challenge` (`mcp_client.py:948-995`) sends a GET request to the MCP server URL. Gmail's MCP server (`gmailmcp.googleapis.com/mcp/v1`) returns 405 on GET (MCP streamable-http endpoints only accept POST). Since 405 ∉ {401, 403}, `probe_saw_auth_challenge` stays False, the OAuth flow never fires, and no auth URL reaches Telegram. The session connects without a token via `initialize` (which Gmail allows unauthenticated), and OAuth only triggers on the first `tools/call` — not during `/mcp auth`.

## Approaches Rejected

- **POST-only probe (no GET)** — Loses the "server does not require OAuth" detection path for GET-accepting servers. Though those servers also accept POST and return 200, keeping GET as a first attempt preserves the existing behavior for servers that already work.
- **Subclass OAuthClientProvider + call `_perform_authorization` directly** (tero/deepagents pattern) — Reaches into SDK internals (`_perform_authorization` is a leading-underscore method). More robust but fragile across SDK versions and over-engineered for this fix.
- **Bypass gmailmcp.googleapis.com, wrap Gmail REST API** (Orkas-AI/openab pattern) — Avoids the issue but requires reimplementing the MCP tool interface. Out of scope.

## Final Approach: GET→405→POST retry

1. Send GET probe (current behavior).
2. If GET returns 405, retry with POST containing a JSON-RPC `tools/call` body.
3. The POST body uses method `tools/call`, params `{"name": "_oauth_probe", "arguments": {}}`, id = random UUID.
4. Headers match the SDK's streamable_http transport: `Accept: application/json, text/event-stream`, `Content-Type: application/json`, `MCP-Protocol-Version: 2025-11-25`.
5. Auth middleware returns 401 before MCP protocol handling → SDK's `async_auth_flow` fires → `redirect_handler` → Telegram auth URL.
6. If POST also returns 405, log WARNING (unexpected status) and proceed to session connection fallback (current behavior).

## Decision Table

| GET status | Action |
|---|---|
| 200 | No OAuth needed (current behavior, no change) |
| 401/403 | OAuth fires via event hook (current behavior, no change) |
| 405 | Retry with POST `tools/call` probe |
| Other | WARNING + session fallback (current behavior, no change) |

| POST status | Action |
|---|---|
| 401/403 | OAuth fires via event hook |
| 200 | No OAuth needed (server allowed unauthenticated tools/call) |
| 405 | WARNING + session fallback (not a valid MCP streamable_http server) |
| Other | WARNING + session fallback (current behavior) |

## Cross-Module Data Flow

```
_run_oauth_flow
  ├── _run_probe_step
  │   └── _probe_oauth_challenge
  │       ├── GET server_url  → 405? retry POST
  │       └── POST server_url (JSON-RPC tools/call _oauth_probe)
  │           └── 401 → event hook sets probe_saw_auth_challenge=True
  │                     → SDK async_auth_flow fires
  │                     → redirect_handler → Telegram auth URL
  └── session connection (unchanged)
```

## Open Questions

None. All resolved during grilling:
- Probe method: `tools/call` with `_oauth_probe`
- Headers: match SDK transport
- 405 handling: GET→405→POST retry
- POST 405: WARNING + fallback (same as current)
- JSON-RPC id: random UUID
- Scope: probe method only; delta spec for mcp-oauth-flow