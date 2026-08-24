## MODIFIED Requirements

### Requirement: OAuth authorization flow for MCP servers

The system SHALL support OAuth 2.0 authorization-code + PKCE authentication for HTTP-transport MCP servers, using the MCP SDK's `OAuthClientProvider`. The operator SHALL initiate the flow via `/mcp auth <name>` in Telegram, and the authorization URL SHALL be delivered as an inline button.

The system SHALL proactively trigger the SDK's authorization handshake by making a standalone HTTP probe request to the server URL with `auth=provider` before connecting the MCP session. This ensures the `redirect_handler` fires even for servers that allow unauthenticated MCP discovery (return 200 on `initialize`/`tools/list` without challenging with 401).

The probe SHALL send an HTTP GET request first. If the server returns 401 or 403 on the GET probe, the SDK's `async_auth_flow` fires the full OAuth handshake immediately and no further probe requests are sent.

If the GET probe returns 200 or 405 without an auth challenge, the system SHALL send a POST `tools/list` JSON-RPC request to discover real tool names, then a POST `tools/call` JSON-RPC request with the first non-mutating real tool name and empty arguments `{}`. The probe prefers a tool whose name does not start with a known mutating prefix (`send_`, `delete_`, `write_`, `update_`, `create_`, `remove_`, `set_`, `put_`, `post_`, `add_`, `insert_`, `modify_`, `edit_`, `move_`, `rename_`, `clear_`, `reset_`, `upload_`, `submit_`, `execute_`, `run_`); if all tools are mutating, the probe skips `tools/call` and falls back to session connection. The `tools/call` POST carries `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers matching the MCP streamable-http transport. This ensures the OAuth handshake fires for MCP servers that enforce auth at the tool execution layer (e.g. Gmail, which returns 200 on unauthenticated GET and `tools/list` but 401 on unauthenticated `tools/call` with a real tool name). The probe parses the `tools/list` response whether it is framed as `application/json` or `text/event-stream`. The probe SHALL reject `tools/list` responses exceeding 1 MB before parsing, logging a WARNING and falling back to session connection.

If `tools/list` returns an empty tool list or fails without an auth challenge, the system SHALL log a WARNING and proceed to session connection as a fallback. If `tools/list` itself returns 401, the event hook sets `probe_saw_auth_challenge=True` and the OAuth flow fires — the fallback is suppressed.

After a successful OAuth flow, the system SHALL register the newly discovered tools in the `ToolRegistry` via `register_mcp_tools`, so that the tools are attributable by server name in the context profile. This registration SHALL occur in the Telegram command handler after `start_oauth_flow` returns success, mirroring the behaviour of `/mcp on`.

#### Scenario: Successful OAuth flow for a new server

- **GIVEN** an HTTP-transport MCP server is configured with an `oauth` section (client_id, client_secret, redirect_uri, scope, cert_path, key_path) and has no stored token
- **WHEN** the operator runs `/mcp auth <name>` in Telegram
- **THEN** the agent starts an ephemeral HTTPS callback server on the configured callback port, makes a proactive HTTP probe to the server URL with the OAuth provider as the auth handler, and the 401 response triggers the SDK's full authorization handshake
- **AND** the SDK calls `redirect_handler`, which posts the authorization URL to Telegram as an inline button, and waits for the callback
- **AND** when the operator taps the button, authenticates with the OAuth provider, and the provider redirects to the callback URL
- **THEN** the agent receives the authorization code, exchanges it for an access token and refresh token, stores them in `$XDG_STATE_HOME/<agent_name>/mcp_tokens/<server>.json` with `0600` permissions, closes the callback server, connects the MCP session with the Bearer token, and reports success in Telegram
- **AND** the discovered tools SHALL be registered in the `ToolRegistry` via `register_mcp_tools(name, tools)` so they are attributable by server in the context profile

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
- **AND** the discovered tools SHALL be re-registered in the `ToolRegistry` via `register_mcp_tools(name, tools)`

#### Scenario: Tools registered in ToolRegistry after OAuth success

- **GIVEN** an OAuth flow for server `google-workspace` completes successfully and the MCP session is connected
- **WHEN** the Telegram command handler processes the successful result
- **THEN** the handler SHALL call `tool_registry.register_mcp_tools("google-workspace", tools)` with the server's discovered tools
- **AND** the tools SHALL be attributable to `google-workspace` by `group_tool_defs_by_server`
- **AND** the context monitor snapshot SHALL be refreshed with the updated tool-defs grouping