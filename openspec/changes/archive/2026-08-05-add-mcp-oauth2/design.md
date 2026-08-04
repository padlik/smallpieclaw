## Context

The agent connects to MCP servers via `mcp_client.py` using the MCP Python SDK (v1.27.0). Currently, HTTP-transport servers are connected with optional static headers only — no OAuth support. Servers like Google's Gmail MCP (`https://gmailmcp.googleapis.com/mcp/v1`) require OAuth 2.0 authorization-code + PKCE and return 401 without a valid Bearer token.

The agent's only user UI is Telegram. There is no local browser to open an auth URL. The operator may be on mobile. The agent host is reachable via dynamic DNS + port forwarding (public IP, no VPN needed).

The MCP SDK (v1.27.0) ships `OAuthClientProvider` — an `httpx.Auth` subclass that handles auth-code + PKCE, metadata discovery (RFC 8414/9728), token refresh, and RFC 8707 resource parameters. It is injected via the `auth=` parameter on `streamablehttp_client()`. The provider requires three injectable hooks: `redirect_handler` (deliver auth URL to user), `callback_handler` (receive the OAuth redirect), and `TokenStorage` (persist tokens + client info).

Google does NOT support Dynamic Client Registration (DCR). The client_id/secret must be pre-created in Google Cloud Console and pre-seeded in `TokenStorage` so the SDK skips its DCR path.

### In-force ADRs relevant to this design

- **ADR-0003** (Accepted, supersedes ADR-0002): TOML vault for centralized secret storage. OAuth client_id/secret live in `config.toml` under `[mcp_servers.oauth]`, not in the vault — they are server configuration, not runtime secrets. OAuth tokens (access + refresh) are stored in `$XDG_STATE_HOME/<name>/mcp_tokens/<server>.json` with `0600` perms; vault integration is deferred (see Open Questions).
- **ADR-0019** (Accepted 2026-08-03): XDG Base Directory layout for agent storage. Token files go in `$XDG_STATE_HOME/<name>/mcp_tokens/` — the STATE bucket, not DATA. OAuth access/refresh tokens are secret-like runtime credentials (analogous to `secrets.toml` which also lives in STATE), not persistent agent knowledge like `memory.json` (DATA). Per ADR-0019 commitment #1, the path is resolved exclusively by `xdg.py` via a new `mcp_tokens_dir` field on `XDGPaths` — no other module computes the path.

### C4 diagram — container view

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Process                                 │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────────┐   │
│  │ Telegram     │   │ MCPManager   │   │ mcp_oauth.py (NEW)     │   │
│  │ Interface    │   │ (mcp_client) │   │                        │   │
│  │              │   │              │   │  CallbackServer        │   │
│  │ /mcp auth ───┼──▶│ start_oauth  │──▶│  (ephemeral HTTPS,     │   │
│  │ /mcp list   │   │  _flow()     │   │   one-shot, ~30s)      │   │
│  │ /mcp info   │   │              │   │                        │   │
│  └──────┬───────┘   │  _SdkClient  │   │  FileTokenStorage      │   │
│         │           │  Wrapper     │   │  (mcp_tokens/ in STATE)│   │
│         │           │              │   │                        │   │
│  ┌──────▼───────┐   │  auth=       │   │  OAuthProviderFactory  │   │
│  │ Telegram     │   │  OAuthClient │   │  (builds SDK provider) │   │
│  │ Bot API      │   │  Provider    │   │                        │   │
│  │ (inline btn) │   │              │   └────────────────────────┘   │
│  └──────────────┘   └──────┬───────┘                                 │
│                             │                                         │
└─────────────────────────────┼─────────────────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │ MCP SDK (v1.27.0) │
                    │ OAuthClientProvider│
                    │ (auth-code+PKCE)  │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ Google IdP   │  │ Gmail MCP    │  │ Other MCP    │
    │ accounts.    │  │ gmailmcp.    │  │ servers      │
    │ google.com   │  │ googleapis.  │  │ (future)     │
    └──────────────┘  └──────────────┘  └──────────────┘

         User's phone (external)
    ┌──────────────────────┐
    │ Telegram client      │
    │  → taps inline button │
    │  → browser opens      │
    │  → Google consent     │
    │  → redirect to agent  │
    └──────────────────────┘
```

## Goals / Non-Goals

**Goals:**
- Connect to OAuth-protected HTTP-transport MCP servers using the SDK's `OAuthClientProvider`
- Deliver the OAuth authorization URL to the operator via Telegram inline button
- Receive the OAuth callback via an ephemeral HTTPS server inside the agent process
- Persist tokens per-server in files with `0600` permissions
- Auto-refresh tokens on 401 (SDK handles this internally)
- Surface `needs_auth` state when no valid token exists, distinct from `error`
- Support `/mcp auth <name>`, `/mcp auth status`, `/mcp auth revoke <name>` commands
- Enforce single-flow-at-a-time (one callback server, one port)

**Non-Goals:**
- OAuth for stdio-transport servers (stdio servers handle their own auth via env vars)
- Device flow (RFC 8628) — rejected during exploration (Google doesn't support `resource=` in device flow + scope whitelist blocks Gmail scopes)
- Dynamic Client Registration — Google doesn't support it; client credentials are pre-seeded
- Token encryption at rest (plaintext `0600` JSON for now; vault integration deferred)
- Multiple concurrent OAuth flows (single port, single callback server)
- Non-Google IdP support (the design is generic via SDK, but only Google is tested)

## Decisions

### Decision 1: Use the SDK's `OAuthClientProvider` rather than implementing OAuth from scratch

**Choice:** Inject `OAuthClientProvider` via `streamablehttp_client(url, auth=provider)`.

**Rationale:** The SDK handles auth-code + PKCE, metadata discovery, token refresh, RFC 8707 resource parameters, and CSRF state validation. Implementing this from scratch would be ~500 lines of security-critical code with no benefit. The SDK's three injectable hooks (`redirect_handler`, `callback_handler`, `TokenStorage`) map cleanly to our Telegram + callback-server + file-storage architecture.

**Alternatives considered:**
- *Raw httpx with manual Bearer header*: loses auto-refresh, metadata discovery, and PKCE management. Would need to reimplement all of it.
- *Device flow + Bearer injection*: rejected — Google's device flow doesn't support `resource=` (audience binding) and restricts scopes to `openid/email/profile`, not Gmail scopes.

### Decision 2: Ephemeral callback server on the MCP event loop

**Choice:** A `CallbackServer` class that starts an `asyncio.start_server(ssl=...)` listener on the existing MCP event loop during `/mcp auth`, accepts exactly one request, validates state, resolves a future, then closes.

**Rationale:** The MCP event loop (`mcp_client.py: _loop_thread`) already runs for the lifetime of the agent. The callback server is a coroutine on that loop — no new thread, no new process. The server is open for ~30 seconds (the time between posting the auth URL and receiving the redirect). Outside that window, the port is closed. This matches the pattern used by `gh auth login`, `gcloud auth login`, and `google-auth-oauthlib`'s `run_local_server()`.

**Alternatives considered:**
- *Always-on callback server*: permanently open port with no benefit — the server is only needed during auth flows.
- *Separate process/thread*: unnecessary complexity; the MCP loop is already async and can handle the callback.

### Decision 3: Pre-seed client credentials in `FileTokenStorage`

**Choice:** `FileTokenStorage.get_client_info()` returns an `OAuthClientInformationFull` constructed from config (`client_id`, `client_secret`) on first call. The SDK checks `if not self.context.client_info:` before attempting DCR — pre-seeding skips it.

**Rationale:** Google does not implement RFC 7591 (Dynamic Client Registration). The SDK's DCR path would fail against Google. Pre-seeding the client info from config means the SDK uses the manually-created Google Cloud Console credentials directly.

**Alternatives considered:**
- *Implement DCR fallback*: pointless — Google will never support it.
- *Bypass the SDK entirely and do raw token exchange*: loses all SDK benefits (refresh, metadata, PKCE).

### Decision 4: File-backed token storage with `0600` permissions

**Choice:** Tokens stored as JSON in `$XDG_STATE_HOME/<agent_name>/mcp_tokens/<server_name>.json` with `0600` file permissions. The path is resolved via `xdg_paths(agent_name).mcp_tokens_dir` (new field on `XDGPaths`, per ADR-0019). Each file contains `access_token`, `refresh_token`, `expires_at`, `scope`, and `client_info` (client_id/secret).

**Rationale:** Simplest viable storage. Consistent with ADR-0019 (XDG Base Directory layout). The `0600` permission restricts access to the agent's user. Refresh tokens are long-lived credentials but the agent already stores API keys in config files with similar protection.

**Alternatives considered:**
- *Vault integration (ADR-0003)*: the vault is for operator-provided secrets, not agent-obtained tokens. Coupling OAuth token storage to vault availability would mean MCP auth fails if the vault is unavailable. Deferred — see Open Questions.
- *Encrypted file (e.g. age/gpg)*: adds a key management problem. Where does the decryption key live? Same chicken-and-egg as vault.

### Decision 5: `needs_auth` as a new server status state

**Choice:** Add `needs_auth` to the server status enum alongside `active`, `error`, `off`. An OAuth-protected server with no valid token is `needs_auth`, not `error` — the server is reachable, just not yet authorized.

**Rationale:** `error` implies something is broken (unreachable, subprocess died). `needs_auth` is a normal state requiring user action. This distinction matters for `/mcp list` output and for the agent's behavior — a `needs_auth` server doesn't retry on boot, it waits for `/mcp auth`.

**Alternatives considered:**
- *Reuse `error` with a descriptive message*: conflates infrastructure failure with pending authentication. The operator can't tell at a glance whether to fix the server or run `/mcp auth`.

### Decision 6: Single-flow-at-a-time enforcement

**Choice:** `MCPManager.start_oauth_flow()` rejects a second concurrent flow with an error message. Only one `CallbackServer` instance exists at a time (single port).

**Rationale:** The callback server binds to a single port. Two simultaneous flows would conflict on the port and the `state` parameter routing. The operator authenticates one server at a time, which is the natural UX anyway.

**Cancel handling:** The operator can cancel an in-flight flow via the `oauth_cancel:<state>` inline button in Telegram. The callback handler resolves the future with an error, the `CallbackServer` closes, the port is released, and the single-flow lock is freed. The SDK's `async_auth_flow` receives the error and aborts the OAuth attempt cleanly.

## Risks / Trade-offs

- **[Risk] Public port open during auth** → Mitigated by: state parameter (CSRF), PKCE (code interception), HTTPS (transport encryption), one-shot server (closes after one request), ~30s window. Acceptable for a personal assistant.
- **[Risk] Dynamic IP changes mid-flow** → Mitigated by: DNS caching on the user's resolver means the old IP is used for the ~30s window. If it fails, the user retries. Not a security or data issue.
- **[Risk] Refresh token revoked while agent is running** → Mitigated by: SDK tries refresh on 401; if refresh fails, the step fails with a clear message telling the operator to run `/mcp auth <name>`. No silent data loss.
- **[Risk] TLS cert expired or invalid when `/mcp auth` runs** → Mitigated by: the `CallbackServer.start()` call validates the cert/key files at startup and fails fast with a descriptive error if the cert is missing, unreadable, or expired. The operator sees "TLS cert error: cert expired on <date> — renew with `certbot renew`" instead of an opaque timeout after 300s.
- **[Risk] Token file read by another local user** → Mitigated by: `0600` file permissions. If the host is shared, this is the same threat model as all other agent data files.
- **[Trade-off] No token encryption at rest** → Accepted for now. Refresh tokens are sensitive but the agent already stores API keys in plaintext config. Vault integration is a future improvement (see Open Questions).
- **[Trade-off] Google-specific pre-seeding** → The design is generic (SDK works with any OAuth server), but only Google is tested. Non-Google servers that support DCR would work without pre-seeding. Non-Google servers without DCR need the same pre-seeding approach.

## Migration Plan

**Deployment:**
1. User configures `[mcp_servers.oauth]` in `config.toml` with Google Cloud Console credentials
2. User obtains a Let's Encrypt cert for their DDNS hostname
3. User sets up router port-forward for the callback port
4. User creates a Google Cloud Console OAuth client with the redirect URI
5. User restarts the agent — OAuth server shows `needs_auth` in `/mcp list`
6. User runs `/mcp auth gmail` — completes the flow — server becomes `active`

**Rollback:**
- Remove the `[mcp_servers.oauth]` section from `config.toml` — the server reverts to no-auth behavior (static headers only). Existing token files in `$XDG_STATE_HOME/<name>/mcp_tokens/` are inert and can be deleted manually.
- No database migrations, no schema changes to existing data.

**Compatibility:**
- Existing MCP servers without OAuth config are unaffected — the `oauth` field is optional, defaults to `None`.
- The `mcp-transport` spec modification is additive (new `needs_auth` state) — existing states (`active`, `error`, `off`) are unchanged.

## Open Questions

1. **Token storage + vault integration**: Should OAuth tokens eventually be stored in the vault (ADR-0003) for encryption at rest? The vault is currently for operator-provided secrets, not agent-obtained tokens. This design defers the decision — `0600` plaintext JSON is the starting point. If the vault gains an "agent secrets" namespace, tokens could migrate there. No in-force ADR needs supersession for this deferral.

2. **Cert renewal hook**: Should the agent auto-restart on cert renewal, or read cert files fresh on each `/mcp auth` flow? The design reads cert files fresh (no restart needed), but a `--deploy-hook` in certbot could also restart the agent for cleanliness. Not a design decision — operational choice.

3. **Multiple redirect URIs**: Should the config support multiple redirect URIs (e.g. for different network paths)? Currently single redirect URI per server. Sufficient for the single-operator model; revisit if multi-network access is needed.