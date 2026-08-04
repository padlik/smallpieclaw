## ADDED Requirements

### Requirement: OAuth authorization flow for MCP servers

The system SHALL support OAuth 2.0 authorization-code + PKCE authentication for HTTP-transport MCP servers, using the MCP SDK's `OAuthClientProvider`. The operator SHALL initiate the flow via `/mcp auth <name>` in Telegram, and the authorization URL SHALL be delivered as an inline button.

#### Scenario: Successful OAuth flow for a new server
- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section (client_id, client_secret, redirect_uri, scope, cert_path, key_path) and has no stored token
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent starts an ephemeral HTTPS callback server on the configured callback port, posts the authorization URL to Telegram as an inline button, and waits for the callback
- **AND** when the operator taps the button, authenticates with the OAuth provider, and the provider redirects to the callback URL
- **THEN** the agent receives the authorization code, exchanges it for an access token and refresh token, stores them in `$XDG_STATE_HOME/<agent_name>/mcp_tokens/<server>.json` with `0600` permissions, closes the callback server, connects the MCP session with the Bearer token, and reports success in Telegram

#### Scenario: OAuth flow with pre-seeded client credentials (no DCR)
- **GIVEN** an MCP server's OAuth provider does not support Dynamic Client Registration (e.g. Google)
- **WHEN** the agent creates the `OAuthClientProvider`
- **THEN** the `TokenStorage.get_client_info()` returns the pre-seeded `OAuthClientInformationFull` constructed from config (`client_id`, `client_secret`), and the SDK skips the DCR step entirely

#### Scenario: OAuth flow timeout
- **GIVEN** the operator has run `/mcp auth <name>` and the callback server is waiting
- **WHEN** no callback is received within 300 seconds
- **THEN** the callback server closes, the port is released, the flow is aborted, and the operator is notified in Telegram that authentication timed out

#### Scenario: OAuth flow cancelled by operator
- **GIVEN** the operator has run `/mcp auth <name>` and the callback server is waiting
- **WHEN** the operator taps the "Cancel" inline button (`oauth_cancel:` — a constant callback_data, since only one flow can be in progress at a time and Telegram caps callback_data at 64 bytes)
- **THEN** the callback handler resolves with an error, the callback server closes, the port is released, the single-flow lock is freed, the SDK aborts the `async_auth_flow` cleanly, and the operator is notified that authentication was cancelled

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

#### Scenario: Token storage path resolution
- **GIVEN** the agent is running with `--agent-name <name>`
- **WHEN** the `FileTokenStorage` resolves the token directory
- **THEN** the directory is `$XDG_STATE_HOME/<name>/mcp_tokens/` as resolved by `xdg_paths()`, not a hardcoded relative path

#### Scenario: Token file created with restrictive permissions
- **GIVEN** an OAuth flow completes successfully for server `gmail`
- **WHEN** the tokens are stored
- **THEN** a file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is created with `0600` permissions containing `access_token`, `refresh_token`, `expires_at`, `scope`, and `client_info`

#### Scenario: Token storage directory creation
- **GIVEN** the `mcp_tokens_dir` does not exist on first boot
- **WHEN** the agent starts and `_create_xdg_dirs` runs in `main.py`
- **THEN** the `mcp_tokens` directory is created under `$XDG_STATE_HOME/<name>/` alongside other state directories

#### Scenario: Pre-seeded client info from config
- **GIVEN** an MCP server is configured with `oauth.client_id` and `oauth.client_secret`
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** it returns an `OAuthClientInformationFull` constructed from the config values, so the SDK skips Dynamic Client Registration

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