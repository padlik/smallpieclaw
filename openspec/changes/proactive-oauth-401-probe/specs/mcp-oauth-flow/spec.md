## MODIFIED Requirements

### Requirement: OAuth authorization flow for MCP servers

The system SHALL support OAuth 2.0 authorization-code + PKCE authentication for HTTP-transport MCP servers, using the MCP SDK's `OAuthClientProvider`. The operator SHALL initiate the flow via `/mcp auth <name>` in Telegram, and the authorization URL SHALL be delivered as an inline button.

The system SHALL proactively trigger the SDK's authorization handshake by making a standalone HTTP probe request to the server URL with `auth=provider` before connecting the MCP session. This ensures the `redirect_handler` fires even for servers that allow unauthenticated MCP discovery (return 200 on `initialize`/`tools/list` without challenging with 401).

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

#### Scenario: Proactive probe returns 200 — server does not require OAuth

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section but the server returns 200 on the unauthenticated probe (does not challenge with 401 or 403)
- **WHEN** the agent makes the proactive HTTP probe
- **THEN** the SDK's `async_auth_flow` does not enter the OAuth branch, `redirect_handler` is not called, and no authorization URL is sent to Telegram
- **AND** the agent connects the MCP session normally without a token, the session becomes ready, and the agent logs at INFO that the server did not require OAuth on the probe
- **AND** no "no token file found" warning is emitted, since the probe confirmed the server did not challenge

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