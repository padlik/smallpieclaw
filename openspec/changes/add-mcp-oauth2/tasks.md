## 1. Config Schema

- [x] 1.1 Add `OAuthConfig` frozen dataclass to `config_schema.py` with fields: `client_id: str`, `client_secret: str`, `redirect_uri: str`, `scope: str`, `callback_port: int = 8000`, `callback_bind: str = "0.0.0.0"`, `cert_path: str`, `key_path: str`
- [x] 1.2 Add `oauth: OAuthConfig | None = None` field to `MCPServerConfig`
- [x] 1.3 Add `_parse_oauth(entry: dict, server_name: str) -> OAuthConfig | None` parser in `config_schema.py` — returns `None` if no `oauth` subsection; raises `ConfigError` if `oauth` present but missing `client_id` or `client_secret`
- [x] 1.4 Wire `_parse_oauth` into `_parse_mcp_server` so `MCPServerConfig` gets the `oauth` field populated
- [x] 1.5 Add tests in `tests/test_config_schema.py`: OAuth config parsed with all fields, OAuth config optional (None when absent), missing client_id raises ConfigError, missing client_secret raises ConfigError, defaults applied for callback_port and callback_bind

## 2. XDG Path Resolution

- [x] 2.1 Add `mcp_tokens_dir: Path` field to `XDGPaths` frozen dataclass in `xdg.py`
- [x] 2.2 Set `mcp_tokens_dir = state_home / "mcp_tokens"` in `xdg_paths()` return value
- [x] 2.3 Add `mcp_tokens_dir` creation to `_create_xdg_dirs()` in `main.py` (per ADR-0019 commitment #1 — directory creation happens exclusively in `main.py`, not in `FileTokenStorage`)
- [x] 2.4 Add test in `tests/test_xdg.py`: `xdg_paths("agent").mcp_tokens_dir` resolves to `$XDG_STATE_HOME/<agent>/mcp_tokens`

## 3. Token Storage

- [x] 3.1 Create `mcp_oauth.py` with `FileTokenStorage` class implementing the MCP SDK's `TokenStorage` protocol (`get_tokens`, `set_tokens`, `get_client_info`, `set_client_info`)
- [x] 3.2 `FileTokenStorage.__init__(server_name, mcp_tokens_dir, client_id, client_secret)` — stores server name, token dir path, and pre-seed credentials from config
- [x] 3.3 `get_tokens()` — reads `<mcp_tokens_dir>/<server_name>.json`, returns `OAuthToken` or `None` if file doesn't exist
- [x] 3.4 `set_tokens(tokens)` — writes `OAuthToken` to `<mcp_tokens_dir>/<server_name>.json` with `0600` permissions
- [x] 3.5 `get_client_info()` — returns `OAuthClientInformationFull` constructed from pre-seeded `client_id`/`client_secret` (so SDK skips DCR); reads from file if previously stored
- [x] 3.6 `set_client_info(info)` — writes `OAuthClientInformationFull` to the token file (merged with tokens)
- [x] 3.7 Add tests in `tests/test_mcp_oauth.py`: store/load tokens round-trip, `0600` file permissions verified, `get_client_info` returns pre-seeded credentials, missing file returns `None`, re-auth overwrites existing token file (new tokens replace old)

## 4. Callback Server

- [x] 4.1 Add `CallbackServer` class to `mcp_oauth.py` — `__init__(port, bind, ssl_context, loop)`, stores an `asyncio.Future` for the callback result and an optional `expected_state` field (set after the SDK generates the auth URL, before the callback arrives)
- [x] 4.2 `async start()` — calls `asyncio.start_server(self._handle, host=bind, port=port, ssl=ssl_context)` on the MCP event loop; validates cert/key files exist and are readable before starting (fail fast with descriptive error)
- [x] 4.3 `async _handle(reader, writer)` — parses HTTP GET query string for `code` and `state`; if `expected_state` is set, validates state matches (rejects mismatch, keeps waiting); extracts `code` and resolves the future with `(code, state)`; writes HTTP 200 "Auth complete, close this tab" to the browser; closes the connection. Note: the SDK's `OAuthClientProvider` also validates state internally via `secrets.compare_digest` — the callback server's validation is a defense-in-depth early-reject to avoid resolving the future with a bad code
- [x] 4.4 `async wait_for_callback(timeout=300) -> tuple[str, str | None]` — awaits the future with timeout
- [x] 4.5 `async stop()` — closes the server socket and waits for cleanup
- [x] 4.6 Add tests in `tests/test_mcp_oauth.py`: callback server starts and stops, receives valid callback and resolves future, rejects mismatched state, timeout raises `asyncio.TimeoutError`, cert validation fails fast when cert file missing

## 5. OAuth Provider Factory

- [x] 5.1 Add `OAuthProviderFactory` to `mcp_oauth.py` — `build(server_cfg, mcp_tokens_dir, tg_iface) -> OAuthClientProvider`
- [x] 5.2 Construct `OAuthClientMetadata` with `client_name="smallpieclaw"`, `redirect_uris=[cfg.redirect_uri]`, `grant_types=["authorization_code", "refresh_token"]`, `token_endpoint_auth_method="client_secret_basic"`, `scope=cfg.scope`
- [x] 5.3 Construct `FileTokenStorage` with server name, token dir, and pre-seeded client_id/secret
- [x] 5.4 Create `make_redirect_handler(tg_iface, server_name) -> Callable` — async function that posts the auth URL to Telegram as an `InlineKeyboardButton` with `url=` (opens browser) plus a Cancel button (`oauth_cancel:<state>`)
- [x] 5.5 Create `make_callback_handler(cb_server) -> Callable` — async function that delegates to `cb_server.wait_for_callback()`
- [x] 5.6 Construct and return `OAuthClientProvider(server_url, client_metadata, storage, redirect_handler, callback_handler, timeout=300)`

## 6. MCP Client Integration

- [x] 6.1 In `mcp_client.py` `_SdkClientWrapper`, add `needs_auth` flag alongside `connected`; add `_oauth_provider` field
- [x] 6.2 In `_session_runner`, when `cfg` has `oauth`: build `OAuthClientProvider` via factory, pass `auth=provider` to `streamablehttp_client()` (or `sse_client()`)
- [x] 6.3 In `_session_runner`, when `cfg` has `oauth` and no stored token: set `needs_auth=True`, `connected=False`, resolve `_ready_future` with empty tools (don't attempt connection)
- [x] 6.4 In `_session_runner`, when `cfg` has `oauth` and stored token is valid or refreshable: proceed with connection (SDK handles refresh on 401 internally)
- [x] 6.5 Add `start_oauth_flow(name)` method to `MCPManager` — rejects if a flow is already in progress (single-flow lock), starts `CallbackServer`, triggers connection with `OAuthClientProvider`, returns when flow completes or times out
- [x] 6.6 Add `needs_auth` to `list_servers()` and `get_server_info()` status output (alongside `active`, `error`, `off`)
- [x] 6.7 Add tests in `tests/test_mcp_client.py`: OAuth server without token reports `needs_auth`, OAuth server with valid token connects as `active`, concurrent `start_oauth_flow` rejected, `needs_auth` appears in `list_servers` output

## 7. Telegram Commands

- [x] 7.1 Add `/mcp auth <name>` handler in `telegram_commands.py` — calls `MCPManager.start_oauth_flow(name)`, handles errors (unknown server, no OAuth config, flow in progress)
- [x] 7.2 Add `/mcp auth status` handler — iterates servers, shows token expiry and refresh availability per server (or "no OAuth" / "needs_auth")
- [x] 7.3 Add `/mcp auth revoke <name>` handler — deletes token file via `FileTokenStorage`, updates server status to `needs_auth`, unregisters tools
- [x] 7.4 Add `oauth_cancel:<state>` callback handler in `telegram_callbacks.py` — resolves the callback future with an error to abort the flow, closes the callback server, frees the single-flow lock
- [x] 7.5 Add tests in `tests/test_telegram_commands.py`: `/mcp auth` for unknown server, `/mcp auth` for server without OAuth, `/mcp auth status` output format, `/mcp auth revoke` deletes token file

## 8. Integration & Wiring

- [x] 8.1 In `main.py`, pass `mcp_tokens_dir` from `xdg_paths()` to `MCPManager` (or to `OAuthProviderFactory`) so token storage resolves paths via `xdg.py`
- [x] 8.2 In `main.py`, ensure `mcp_tokens_dir` is created in `_create_xdg_dirs()` alongside other state directories
- [x] 8.3 In `agent_controller.py` / `react_loop.py`, handle 401 from MCP tool calls where refresh fails: return error result with "token expired, run /mcp auth <name>" message AND transition the server status to `needs_auth` (per frozen spec: "Token refresh failure surfaces to operator" THEN clause b)
- [x] 8.4 Update `vulture_whitelist.py` with any new public API symbols (`OAuthConfig`, `FileTokenStorage`, `CallbackServer`, `OAuthProviderFactory`, `XDGPaths.mcp_tokens_dir`)

## 9. Validation & Cleanup

- [x] 9.1 Run `ruff check .` and fix any lint errors
- [x] 9.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update whitelist if needed
- [x] 9.3 Run `make test` and ensure all tests pass
- [x] 9.4 Run `openspec validate add-mcp-oauth2 --type change --strict` to validate the change artifacts