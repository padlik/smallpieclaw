### Requirement: OAuth authorization flow for MCP servers

The system SHALL support OAuth 2.0 authorization-code + PKCE authentication for HTTP-transport MCP servers, using the MCP SDK's `OAuthClientProvider`. The operator SHALL initiate the flow via `/mcp auth <name>` in Telegram, and the authorization URL SHALL be delivered as an inline button.

The system SHALL proactively trigger the SDK's authorization handshake by making a standalone HTTP probe request to the server URL with `auth=provider` before connecting the MCP session. This ensures the `redirect_handler` fires even for servers that allow unauthenticated MCP discovery (return 200 on `initialize`/`tools/list` without challenging with 401).

The probe SHALL send an HTTP GET request first. If the server returns 401 or 403 on the GET probe, the SDK's `async_auth_flow` fires the full OAuth handshake immediately and no further probe requests are sent.

If the GET probe returns 200 or 405 without an auth challenge, the system SHALL send a POST `tools/list` JSON-RPC request to discover real tool names, then a POST `tools/call` JSON-RPC request with the first non-mutating real tool name and empty arguments `{}`. The probe prefers a tool whose name does not start with a known mutating prefix (`send_`, `delete_`, `write_`, `update_`, `create_`, `remove_`, `set_`, `put_`, `post_`, `add_`, `insert_`, `modify_`, `edit_`, `move_`, `rename_`, `clear_`, `reset_`, `upload_`, `submit_`, `execute_`, `run_`); if all tools are mutating, the probe skips `tools/call` and falls back to session connection. The `tools/call` POST carries `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers matching the MCP streamable-http transport. This ensures the OAuth handshake fires for MCP servers that enforce auth at the tool execution layer (e.g. Gmail, which returns 200 on unauthenticated GET and `tools/list` but 401 on unauthenticated `tools/call` with a real tool name). The probe parses the `tools/list` response whether it is framed as `application/json` or `text/event-stream`. The probe SHALL reject `tools/list` responses exceeding 1 MB before parsing, logging a WARNING and falling back to session connection.

If `tools/list` returns an empty tool list or fails without an auth challenge, the system SHALL log a WARNING and proceed to session connection as a fallback. If `tools/list` itself returns 401, the event hook sets `probe_saw_auth_challenge=True` and the OAuth flow fires — the fallback is suppressed.

#### Scenario: Successful OAuth flow for a new server

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section (client_id, client_secret, redirect_uri, scope, cert_path, key_path) and has no stored token
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent starts an ephemeral HTTPS callback server on the configured callback port, makes a proactive HTTP probe to the server URL with the OAuth provider as the auth handler, and the 401 response triggers the SDK's full authorization handshake
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button, and waits for the callback
- **AND** when the operator taps the button, authenticates with the OAuth provider, and the provider redirects to the callback URL
- **THEN** the agent receives the authorization code, exchanges it for an access token and refresh token, stores them in `$XDG_STATE_HOME/<agent_name>/mcp_tokens/<server>.json` with `0600` permissions, closes the callback server, connects the MCP session with the Bearer token, and reports success in Telegram

#### Scenario: OAuth flow with pre-seeded client credentials (no DCR)

- **GIVEN** an MCP server's OAuth provider does not support Dynamic Client Registration (e.g. Google)
- **WHEN** the agent creates the `OAuthClientProvider`
- **THEN** the `TokenStorage.get_client_info()` returns the pre-seeded `OAuthClientInformationFull` constructed from config (`client_id`, `client_secret`), and the SDK skips the DCR step entirely

#### Scenario: Proactive probe triggers OAuth for server allowing unauthenticated discovery

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section, has no stored token, and returns 200 on unauthenticated `initialize` and `tools/list` (allows unauthenticated discovery)
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent makes a proactive HTTP GET probe to the server URL with `auth=provider` and the `MCP-Protocol-Version` header set to the latest known protocol version
- **AND** the server returns 401 on the probe, triggering the SDK's `async_auth_flow`
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button
- **AND** the operator completes browser authorization, the callback is received, the token is exchanged and stored, and the MCP session connects with the Bearer token

#### Scenario: Proactive probe returns 200 on GET, tools/list, and tools/call — server does not require OAuth

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section, returns 200 on the unauthenticated GET probe, returns 200 on unauthenticated POST `tools/list` with a non-empty tool list, and returns 200 on unauthenticated POST `tools/call` with a real tool name (does not challenge with 401 or 403 on any probe request)
- **WHEN** the agent makes the proactive HTTP probe (GET → tools/list → tools/call with first real tool name)
- **THEN** the SDK's `async_auth_flow` does not enter the OAuth branch, `redirect_handler` is not called, and no authorization URL is sent to Telegram
- **AND** the agent connects the MCP session normally without a token, the session becomes ready, and the agent logs at INFO that the server did not require OAuth on the probe
- **AND** no "no token file found" warning is emitted, since the probe confirmed the server did not challenge

#### Scenario: GET probe returns 200, POST tools/list returns 200, POST tools/call with real tool returns 401 — OAuth fires

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section, has no stored token, returns 200 on unauthenticated GET, returns 200 on unauthenticated POST `tools/list`, and returns 401 on unauthenticated POST `tools/call` with a real tool name (e.g. Gmail's MCP server)
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent makes a proactive HTTP GET probe to the server URL with `auth=provider` and the `MCP-Protocol-Version` header
- **AND** the server returns 200 on the GET probe
- **AND** the agent sends a POST `tools/list` JSON-RPC request to the server URL, carrying `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers
- **AND** the server returns 200 with the tool list
- **AND** the agent sends a POST `tools/call` JSON-RPC request with the first real tool name from the tool list and empty arguments `{}`, carrying `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers
- **AND** the server's auth middleware returns 401 on the unauthenticated POST, triggering the SDK's `async_auth_flow`
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button
- **AND** the operator completes browser authorization, the callback is received, the token is exchanged and stored, and the MCP session connects with the Bearer token
- **AND** the probe logs reflect the final POST `tools/call` status, not the intermediate GET 200

#### Scenario: GET probe returns 405 — POST tools/list and tools/call retry triggers OAuth for POST-only server

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section, has no stored token, and only accepts POST on its streamable-http endpoint (returns 405 Method Not Allowed on GET)
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent makes a proactive HTTP GET probe to the server URL with `auth=provider` and the `MCP-Protocol-Version` header
- **AND** the server returns 405 on the GET probe
- **AND** the agent sends a POST `tools/list` JSON-RPC request, then a POST `tools/call` with the first real tool name and empty arguments
- **AND** the server's auth middleware returns 401 on the unauthenticated POST `tools/call`, triggering the SDK's `async_auth_flow`
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button
- **AND** the operator completes browser authorization, the callback is received, the token is exchanged and stored, and the MCP session connects with the Bearer token
- **AND** the probe logs reflect the final POST `tools/call` status, not the intermediate GET 405

#### Scenario: GET probe returns 200, POST tools/list returns empty — session fallback

- **GIVEN** an HTTP-transport MCP server returns 200 on GET and returns an empty tool list on POST `tools/list` (no tools available)
- **WHEN** the agent makes the GET probe and the POST `tools/list` returns an empty tool list
- **THEN** the agent logs a WARNING that no tools were discovered and proceeds to session connection as a fallback
- **AND** the session's `initialize` may still trigger a 401 that the SDK's auth flow handles as a last resort

#### Scenario: GET probe returns 200, POST tools/list returns 401 — OAuth fires on tools/list

- **GIVEN** an HTTP-transport MCP server returns 200 on GET but 401 on unauthenticated POST `tools/list`
- **WHEN** the agent makes the GET probe and retries with POST `tools/list`
- **THEN** the event hook observes the 401 and sets `probe_saw_auth_challenge=True`
- **AND** the SDK's `async_auth_flow` fires the full OAuth handshake (discovery → redirect_handler → callback_handler → token exchange)
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button
- **AND** no POST `tools/call` is sent — the OAuth flow fires on the `tools/list` 401

#### Scenario: tools/list returns SSE-framed response — probe parses data frames and extracts tool name

- **GIVEN** an HTTP-transport MCP server returns 200 on GET and returns a `text/event-stream` framed response on POST `tools/list` (SSE `data:` frames containing the JSON-RPC tool list)
- **WHEN** the agent makes the GET probe and retries with POST `tools/list`
- **THEN** the probe concatenates the SSE `data:` frames, parses the JSON-RPC response, and extracts the first tool name from `result.tools[0].name`
- **AND** the agent sends POST `tools/call` with the extracted tool name and empty arguments `{}`

#### Scenario: OAuth flow fired but did not complete

- **GIVEN** the proactive probe triggered the SDK's authorization handshake (the server returned 401 or 403) but the token exchange did not complete (e.g. `redirect_handler` failed, callback timed out, or token endpoint rejected the code)
- **WHEN** the agent checks whether a token file was created after the session is ready
- **THEN** a warning is emitted indicating the OAuth flow was attempted but no token was stored, and the operator is advised to retry `/mcp auth <name>`

#### Scenario: OAuth flow timeout

- **GIVEN** the operator has run `/mcp auth <name>` and the callback server is waiting
- **WHEN** no callback is received within 300 seconds
- **THEN** the callback server closes, the port is released, the flow is aborted, and the operator is notified in Telegram that authentication timed out

#### Scenario: OAuth flow cancelled by operator

- **GIVEN** the operator has run `/mcp auth <name>` and the proactive probe is in progress (either waiting for the 401-triggered OAuth handshake or blocked in `callback_handler` awaiting the redirect)
- **WHEN** the operator taps the "Cancel" inline button (`oauth_cancel:` — a constant callback_data, since only one flow can be in progress at a time and Telegram caps callback_data at 64 bytes)
- **THEN** the cancel flag is set, the probe task is cancelled, the `CancelledError` propagates through `callback_handler` and the SDK's `async_auth_flow`, the `httpx.AsyncClient` context exits cleanly, the callback server closes, the port is released, the single-flow lock is freed, and the operator is notified that authentication was cancelled

#### Scenario: Concurrent OAuth flow rejected

- **GIVEN** an OAuth flow is already in progress for any server (callback server is listening)
- **WHEN** the operator runs `/mcp auth <other_name>` for a different server
- **THEN** the second flow is rejected with a message indicating an OAuth flow is already in progress, and no second callback server is started

#### Scenario: OAuth flow for unknown server

- **GIVEN** no MCP server is configured with the name `unknown`
- **WHEN** the operator runs `/mcp auth unknown`
- **THEN** the response indicates the server was not found and no flow is started

#### Scenario: OAuth flow for a server without OAuth config

- **GIVEN** an MCP server is configured without an `oauth` section
- **WHEN** the operator runs `/mcp auth <name>`
- **THEN** the response indicates the server does not have OAuth configured and no flow is started

#### Scenario: Re-authentication replaces existing tokens

- **GIVEN** a server `gmail` already has a valid stored access token and is connected
- **WHEN** the operator runs `/mcp auth gmail` and completes a new OAuth flow
- **THEN** the new tokens replace the old ones in the token file, the MCP session reconnects with the new Bearer token, and the server remains `active`

### Requirement: Ephemeral HTTPS callback server

The system SHALL run an ephemeral HTTPS callback server inside the agent process on the MCP event loop. The server SHALL be open only during an active OAuth flow and SHALL accept exactly one request before closing.

#### Scenario: Callback server lifecycle
- **GIVEN** no OAuth flow is in progress
- **WHEN** the agent is running normally (boot, tool calls, token refresh)
- **THEN** the callback port is closed and no inbound listener exists

#### Scenario: Callback server starts on auth flow
- **GIVEN** the operator runs `/mcp auth <name>`
- **WHEN** the OAuth flow begins
- **THEN** the callback server starts listening on the configured callback port with the configured TLS cert and bind address

#### Scenario: Callback server closes after receiving callback
- **GIVEN** the callback server is listening and receives a valid callback with matching state
- **WHEN** the authorization code is extracted and the callback future is resolved
- **THEN** the callback server closes the listening socket and the port is released

#### Scenario: Callback server validates state parameter
- **GIVEN** the callback server receives a request with a `state` parameter
- **WHEN** the state does not match the expected value generated for this flow
- **THEN** the request is rejected, no authorization code is extracted, and the callback server continues waiting for a valid callback

#### Scenario: Callback server cert validation at startup
- **GIVEN** the configured TLS cert file is missing, unreadable, or expired
- **WHEN** the callback server attempts to start
- **THEN** the server fails fast with a descriptive error message indicating the cert problem, and the OAuth flow is aborted before posting any link to Telegram

### Requirement: File-backed token storage

The system SHALL store OAuth tokens per-server in JSON files resolved via `xdg_paths(agent_name).mcp_tokens_dir` (the `$XDG_STATE_HOME/<agent_name>/mcp_tokens/` directory), with `0600` file permissions. The storage SHALL implement the MCP SDK's `TokenStorage` protocol.

The `FileTokenStorage` SHALL accept a `token_endpoint_auth_method` parameter (defaulting to `"client_secret_basic"`) so the pre-seeded `OAuthClientInformationFull` carries the auth method the SDK's `prepare_token_auth()` expects.

The `get_client_info()` method SHALL set `token_endpoint_auth_method` on its pre-seed return path: the constructed `OAuthClientInformationFull` SHALL include `token_endpoint_auth_method` set to the storage's configured value. When `get_client_info()` returns a persisted block — which it does only when that block's `client_secret` matches the configured one — it SHALL return it unmodified; on a `client_secret` mismatch the configured credentials take precedence and the pre-seed is returned instead, so a secret rotation takes effect.

When a persisted `client_info` block cannot be parsed, `get_client_info()` SHALL log a warning and fall back to the pre-seed rather than raising, so a corrupt or schema-drifted block does not prevent connecting to the server.

#### Scenario: Token storage path resolution
- **GIVEN** the agent is running with `--agent-name <name>`
- **WHEN** the `FileTokenStorage` resolves the token directory
- **THEN** the directory is `$XDG_STATE_HOME/<name>/mcp_tokens/` as resolved by `xdg_paths()`, not a hardcoded relative path

#### Scenario: Token file created with restrictive permissions
- **GIVEN** an OAuth flow completes successfully for server `gmail`
- **WHEN** the tokens are stored
- **THEN** a file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is created with `0600` permissions containing a `token` object carrying the fields the provider returned (`access_token` and `token_type`, plus `refresh_token`, `scope`, and `expires_in` when granted) and an `issued_at` stamp
- **AND** a `client_info` block is present only if one was already persisted, since a normal flow never writes one

#### Scenario: Token storage directory creation
- **GIVEN** the `mcp_tokens_dir` does not exist on first boot
- **WHEN** the agent starts and `_create_xdg_dirs` runs in `main.py`
- **THEN** the `mcp_tokens` directory is created under `$XDG_STATE_HOME/<name>/` alongside other state directories

#### Scenario: Pre-seeded client info from config includes token_endpoint_auth_method
- **GIVEN** an MCP server is configured with `oauth.client_id` and `oauth.client_secret` and has no stored token file
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** it returns an `OAuthClientInformationFull` constructed from the config values (`client_id`, `client_secret`) with `token_endpoint_auth_method` set to the storage's configured value (defaulting to `"client_secret_basic"`)
- **AND** the SDK skips Dynamic Client Registration
- **AND** the SDK's `prepare_token_auth()` sends `client_secret` via HTTP Basic auth header in the token exchange request

#### Scenario: Malformed cached client info falls back to the pre-seed
- **GIVEN** an MCP server has a stored token file whose `client_info` block cannot be parsed into an `OAuthClientInformationFull` (e.g. hand-edited, or written by an older schema, so a required field such as `redirect_uris` is absent)
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** a warning naming the server is logged and no exception propagates
- **AND** the returned `OAuthClientInformationFull` is the pre-seed built from config, carrying `token_endpoint_auth_method`
- **AND** the connection attempt proceeds instead of failing

#### Scenario: Token revocation
- **GIVEN** a server `gmail` has stored tokens
- **WHEN** the operator runs `/mcp auth revoke gmail`
- **THEN** the token file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is deleted, the server's status changes to `needs_auth`, and its tools are unregistered

### Requirement: Automatic token refresh

The system SHALL rely on the MCP SDK's `OAuthClientProvider` to automatically refresh expired access tokens using the stored refresh token when the MCP server returns 401.

#### Scenario: Silent token refresh on 401
- **GIVEN** a connected MCP server has an expired access token but a valid refresh token
- **WHEN** a tool call results in a 401 response
- **THEN** the SDK automatically refreshes the token via an outbound POST to the provider's token endpoint, stores the new token, and retries the original request without user interaction

#### Scenario: Token refresh failure surfaces to operator
- **GIVEN** a connected MCP server has an expired access token and the refresh token is revoked or expired
- **WHEN** a tool call results in a 401 response and the SDK's refresh attempt fails
- **THEN** the tool call fails with an error message indicating the token expired and the operator should run `/mcp auth <name>`, and the server's status changes to `needs_auth`

### Requirement: OAuth server status reporting

The system SHALL report OAuth-related status for MCP servers, including token validity and expiry.

#### Scenario: Token status for authenticated server
- **GIVEN** a server `gmail` has a valid stored access token
- **WHEN** the operator runs `/mcp auth status`
- **THEN** the response shows the server name, token expiry time, and whether a refresh token is available

#### Scenario: Token status for server needing auth
- **GIVEN** a server `gmail` has no stored token or an expired token with no refresh token
- **WHEN** the operator runs `/mcp auth status`
- **THEN** the response shows the server name with status `needs_auth` and indicates no valid token

#### Scenario: Token status for server without OAuth
- **GIVEN** a server `local-tools` is configured without an `oauth` section
- **WHEN** the operator runs `/mcp auth status`
- **THEN** the response shows the server name with status indicating OAuth is not configured

### Requirement: OAuth configuration schema

The system SHALL support an optional `oauth` section in MCP server configuration with client credentials, redirect URI, scope, callback server settings, and TLS cert paths.

#### Scenario: OAuth config parsed from TOML
- **GIVEN** a `config.toml` with a `[[mcp_servers]]` entry containing an `[mcp_servers.oauth]` subsection
- **WHEN** the config is parsed
- **THEN** the `MCPServerConfig` includes an `OAuthConfig` with `client_id`, `client_secret`, `redirect_uri`, `scope`, `callback_port` (default 8000), `callback_bind` (default `0.0.0.0`), `cert_path`, and `key_path`

#### Scenario: OAuth config is optional
- **GIVEN** a `[[mcp_servers]]` entry without an `oauth` subsection
- **WHEN** the config is parsed
- **THEN** the `MCPServerConfig.oauth` field is `None` and the server connects without OAuth (static headers only)

#### Scenario: OAuth config requires client_id
- **GIVEN** a `[[mcp_servers]]` entry with an `oauth` subsection missing `client_id`
- **WHEN** the config is parsed
- **THEN** a `ConfigError` is raised indicating `oauth.client_id` is required

#### Scenario: OAuth config requires client_secret
- **GIVEN** a `[[mcp_servers]]` entry with an `oauth` subsection missing `client_secret`
- **WHEN** the config is parsed
- **THEN** a `ConfigError` is raised indicating `oauth.client_secret` is required
