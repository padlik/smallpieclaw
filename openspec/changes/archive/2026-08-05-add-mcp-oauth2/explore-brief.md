# Explore Brief: add-mcp-oauth2

## Goal

Add OAuth 2.0 (authorization code + PKCE) support for HTTP-transport MCP servers, so the agent can connect to OAuth-protected MCP servers like Google's Gmail MCP (`https://gmailmcp.googleapis.com/mcp/v1`). The only user UI is Telegram.

## Alternatives Rejected

### A. Device flow (RFC 8628) — REJECTED (two independent blockers)

1. **No audience binding**: Google's device-code endpoint (`oauth2.googleapis.com/device/code`) accepts only `client_id` + `scope`. No `resource=` parameter. Tokens get a generic Google OAuth audience. The Gmail MCP server (per MCP spec RFC 8707) MUST reject mismatched audience → 401.
2. **Scope whitelist**: Google's device flow only supports `openid`, `email`, `profile`. `gmail.readonly` / `gmail.compose` are NOT issuable via device flow.

### B. Plain VPN + LAN-IP redirect URI — REJECTED

Google's redirect URI rules reject both `http://192.168.1.100:8000` (HTTP scheme for non-loopback) and `https://192.168.1.100:8000` (raw IP host). Non-loopback HTTP is dead; non-loopback requires HTTPS + hostname.

### C. SSH port-forward + loopback redirect — REJECTED for mobile

Works on desktop (`ssh -L 8000:localhost:8000 agent`), but mobile Telegram → browser → `localhost:8000` resolves to the phone, not the agent. Dead for the primary surface.

## Selected Approach: Authorization code + PKCE via MCP SDK's OAuthClientProvider

- Use the MCP Python SDK's built-in `OAuthClientProvider` (mcp 1.27.0) which handles auth-code + PKCE, metadata discovery, token refresh, and RFC 8707 resource parameter.
- Agent host is publicly reachable via dynamic DNS + port forwarding (no VPN needed).
- Let's Encrypt cert (HTTP-01 or DNS-01) provides TLS for the callback endpoint.
- Ephemeral HTTPS callback server runs inside the agent process on the existing MCP asyncio loop — open only during an active auth flow (~30s), closed otherwise.
- User triggers auth via `/mcp auth <server>` Telegram command; agent posts the auth URL as an inline button; user taps, authenticates in browser, Google redirects to the callback server.

## Key Design Commitments

### Infrastructure (user-provided, one-time)
- Dynamic DNS hostname (e.g. `agent.myddns.com`) resolving to agent's public IP
- Router port-forward 8000 → agent LAN IP
- Let's Encrypt cert for the hostname (HTTP-01 via `certbot --standalone`, or DNS-01)
- Google Cloud Console: Web app OAuth client with `redirect_uri = https://agent.myddns.com:8000/callback`
- Google does NOT support DCR — client_id/secret must be pre-seeded in token storage

### MCP SDK integration points (3 injectable hooks)
1. `redirect_handler(authorization_url)` → posts auth URL to Telegram as inline button
2. `callback_handler()` → awaits the ephemeral callback server's future for `(code, state)`
3. `TokenStorage` protocol → file-backed, per-server JSON under `$XDG_STATE_HOME/<name>/mcp_tokens/` (resolved via `xdg.py` per ADR-0019)

### Callback server lifecycle
- Port 8000 CLOSED during boot, normal tool calls, and token refresh
- Port 8000 OPEN only during `/mcp auth` flow (~30s–5min window)
- One-shot: accepts exactly one request, validates state, then closes
- HTTPS using Let's Encrypt cert (stdlib `asyncio.start_server(ssl=...)`)

### Token lifecycle
- Boot: load stored token → valid? use it. Expired + refresh_token? SDK auto-refreshes (silent, outbound only). No token? server marked `needs_auth`.
- During ReAct step: 401 → SDK tries refresh → success: retry. Failure: step fails, user told to run `/mcp auth`.
- `/mcp auth <name>`: full flow, new tokens replace old in storage.
- `/mcp auth status`: shows token expiry per server.
- `/mcp auth revoke <name>`: deletes stored tokens.

### Config schema additions
`MCPServerConfig` gains optional `oauth: OAuthConfig | None`:
- `client_id`, `client_secret` (from Google Cloud Console, pre-seeded)
- `redirect_uri` (e.g. `https://agent.myddns.com:8000/callback`)
- `scope` (e.g. `https://www.googleapis.com/auth/gmail.readonly`)
- `callback_port` (default 8000)
- `callback_bind` (default `0.0.0.0`)
- `cert_path`, `key_path` (Let's Encrypt cert files)

### Module changes
- **NEW `mcp_oauth.py`** (~170 lines): `CallbackServer`, `FileTokenStorage`, `make_redirect_handler`, `make_callback_handler`, `OAuthProviderFactory`
- **CHANGED `mcp_client.py`**: `_SdkClientWrapper._session_runner` passes `auth=` to `streamablehttp_client` when OAuth configured; handles `needs_auth` state
- **CHANGED `config_schema.py`**: `MCPServerConfig` += `oauth` field; `OAuthConfig` dataclass; parser
- **CHANGED `telegram_commands.py`**: `/mcp auth`, `/mcp auth status`, `/mcp auth revoke`
- **CHANGED `telegram_callbacks.py`**: `oauth_cancel:<state>` callback handler

### Cross-module data flow
```
/mcp auth gmail (telegram_commands)
  → MCPManager.start_oauth_flow("gmail")
    → OAuthProviderFactory.build(cfg) → OAuthClientProvider
    → CallbackServer.start(port, ssl)
    → streamablehttp_client(url, auth=provider)
      → SDK 401 → async_auth_flow:
        → redirect_handler(url) → TG inline button
        → callback_handler() → awaits CallbackServer future
        → user authenticates → Google redirects → CallbackServer resolves
        → SDK exchanges code → stores token → retries request → 200
    → CallbackServer.stop()
  → MCPManager registers tools
  → TG: "✅ authenticated"
```

## Open Questions
1. Should the callback server bind to `0.0.0.0` or only the VPN/LAN interface? (Default `0.0.0.0` since port-forward targets it; configurable)
2. Token storage encryption: plaintext JSON with `0600` perms vs. routing through the existing vault? (Start plaintext, revisit later)
3. Should `/mcp auth` support multiple servers in one command, or one at a time? (One at a time — simpler, matches the one-shot callback server model)
4. What happens if two `/mcp auth` flows run concurrently? (Reject the second — only one callback server at a time, single port)