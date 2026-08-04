## Why

HTTP-transport MCP servers like Google's Gmail MCP (`https://gmailmcp.googleapis.com/mcp/v1`) require OAuth 2.0 authentication. The agent currently connects to MCP servers with no auth support — it passes static headers only. There is no way for the operator to complete an OAuth authorization flow through the agent's only UI surface (Telegram), and no token storage or refresh mechanism. Without this, the agent cannot use any OAuth-protected MCP server.

## What Changes

- Add OAuth 2.0 authorization-code + PKCE support for HTTP-transport MCP servers, using the MCP Python SDK's built-in `OAuthClientProvider` (mcp 1.27.0)
- Add an ephemeral HTTPS callback server inside the agent process that listens only during an active auth flow (~30s), then closes — no permanently open port
- Add file-backed per-server token storage (`$XDG_STATE_HOME/<name>/mcp_tokens/<server>.json`, resolved via `xdg.py` per ADR-0019) implementing the SDK's `TokenStorage` protocol, with pre-seeded client credentials (Google does not support Dynamic Client Registration)
- Add `/mcp auth <name>` Telegram command that triggers the OAuth flow, posts the authorization URL as an inline button, and waits for the callback
- Add `/mcp auth status` to show token expiry per server and `/mcp auth revoke <name>` to delete stored tokens
- Add `needs_auth` server status state for servers with no valid token (distinct from `error` — the server is reachable but not yet authorized)
- Add `oauth` section to `MCPServerConfig` with client credentials, redirect URI, scope, callback port, bind address, and TLS cert/key paths
- On boot: load stored tokens; if valid, connect silently; if expired with refresh token, SDK auto-refreshes (outbound only, no callback server); if no token, mark `needs_auth`
- During ReAct step: 401 → SDK auto-refreshes; if refresh fails, step fails with a message telling the operator to run `/mcp auth <name>`
- Only one OAuth flow at a time: a second `/mcp auth` while one is in flight is rejected (single callback server, single port)

## Capabilities

### New Capabilities
- `mcp-oauth-flow`: OAuth 2.0 authorization-code + PKCE flow for HTTP-transport MCP servers, including ephemeral HTTPS callback server, Telegram-delivered auth URL, file-backed token storage with pre-seeded client credentials, automatic token refresh, and `/mcp auth` command surface

### Modified Capabilities
- `mcp-transport`: Server connection lifecycle gains a `needs_auth` state for OAuth-protected servers without valid tokens; boot-time connection skips OAuth-protected servers that have no stored token instead of failing with `error`. The `needs_auth` state propagates to server status reporting (`/mcp list`) and server info (`/mcp info`) display contracts.

## Impact

- **New module**: `mcp_oauth.py` (~170 lines) — `CallbackServer`, `FileTokenStorage`, redirect/callback handler factories, `OAuthProviderFactory`
- **Changed modules**: `mcp_client.py` (pass `auth=` to SDK, handle `needs_auth` state), `config_schema.py` (`OAuthConfig` dataclass + parser), `telegram_commands.py` (`/mcp auth` commands), `telegram_callbacks.py` (`oauth_cancel` callback)
- **Dependencies**: MCP SDK 1.27.0 already installed; no new Python dependencies (callback server uses stdlib `asyncio.start_server` + `ssl`)
- **Infrastructure (user-provided)**: Dynamic DNS hostname, router port-forward for callback port, Let's Encrypt cert for the hostname, Google Cloud Console OAuth client with redirect URI configured
- **Security**: Callback server open only during auth flow (~30s); protected by state parameter (CSRF), PKCE (code interception), HTTPS (transport encryption), one-shot server (closes after one request). Tokens stored as `0600` plaintext JSON for now; vault integration deferred.