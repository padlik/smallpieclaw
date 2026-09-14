## MODIFIED Requirements

### Requirement: MCP server connection lifecycle

The system SHALL connect to configured MCP servers on startup and maintain connections for the agent's lifetime. Each server's connection state SHALL be independently tracked and queryable. For OAuth-protected servers, the system SHALL skip connection on boot when no valid token is available and report `needs_auth` instead of `error`. When a connected OAuth-protected server's session ends due to an authentication or authorization failure (expired/revoked token that cannot be refreshed, or a non-interactive authorization abort), the system SHALL leave the wrapper marked `needs_auth` (not connected, no generic error state), so status queries report the actionable auth state instead of a silent connection error.

Re-registering a server's tools (after a re-authentication flow or a runtime reconnect) SHALL atomically replace the server's previous registration: the manager's tool-name-to-server map entries for that server are purged before the new tools are inserted, and the wrapper being replaced is closed. A tool name claimed by a *different* server continues to warn on conflict as before.

#### Scenario: Successful connection on startup

- **GIVEN** a valid MCP server is configured with `enabled: true` and a reachable transport endpoint
- **WHEN** the agent starts and initializes MCP servers
- **THEN** the server connects successfully, its tools are discovered and registered, and its status is reported as `active`

#### Scenario: Connection failure on startup

- **GIVEN** a configured MCP server is unreachable (subprocess fails to start, or HTTP endpoint returns an error)
- **WHEN** the agent starts and attempts to connect
- **THEN** the server's status is reported as `error` with a descriptive `last_error` message, and the agent starts without that server's tools

#### Scenario: OAuth-protected server without token on startup

- **GIVEN** a configured MCP server has an `oauth` section and no stored token (or an expired token with no refresh token)
- **WHEN** the agent starts and initializes MCP servers
- **THEN** the server is not connected, its tools are not registered, and its status is reported as `needs_auth` (not `error`), and the agent starts without blocking

#### Scenario: OAuth-protected server with valid token on startup

- **GIVEN** a configured MCP server has an `oauth` section and a stored valid access token (or an expired token with a valid refresh token)
- **WHEN** the agent starts and initializes MCP servers
- **THEN** the server connects using the stored token (refreshing silently if needed), its tools are discovered and registered, and its status is reported as `active`

#### Scenario: Disabled server skipped on startup

- **GIVEN** a configured MCP server has `enabled: false`
- **WHEN** the agent starts and initializes MCP servers
- **THEN** the server is not connected, its tools are not registered, and its status is reported as `off`

#### Scenario: Runtime enable of a disabled server

- **GIVEN** a configured MCP server is currently disabled (`enabled: false`)
- **WHEN** an operator enables it via `/mcp on <name>`
- **THEN** the server connects, its tools are discovered and registered, and its status changes to `active`

#### Scenario: Runtime disable of an active server

- **GIVEN** a configured MCP server is currently connected and active
- **WHEN** an operator disables it via `/mcp off <name>`
- **THEN** the server disconnects, its tools are unregistered, and its status changes to `off`

#### Scenario: Graceful shutdown

- **GIVEN** one or more MCP servers are connected
- **WHEN** the agent shuts down
- **THEN** all connected servers are disconnected cleanly and their resources are released

#### Scenario: Re-registration after re-auth replaces the previous registration atomically

- **GIVEN** server `google-workspace` is registered with 14 tools and its entries exist in the tool-name-to-server map
- **WHEN** a successful `/mcp auth google-workspace` flow re-registers the server's tools
- **THEN** the server's previous map entries are purged under the lock before the new tools are inserted, and no "tool name conflict" warnings are logged for tools claimed by the same server on both sides
- **AND** the replaced wrapper (if any) is closed, releasing its session task and provider

#### Scenario: Reconnect of an already-registered server replaces the previous registration

- **GIVEN** server `google-workspace` is already registered with a live wrapper
- **WHEN** the manager reconnects the server (e.g. `/mcp on` on a connected server or a re-auth flow completing)
- **THEN** the previous wrapper is closed before the new one takes its place, and no duplicate map entries or duplicate-registration warnings are produced for the same server name
- **AND** a tool name claimed by a different server still logs a conflict warning as before

#### Scenario: Auth-related session death leaves the server needs_auth

- **GIVEN** a connected OAuth-protected server's session ends because authorization failed (refresh token dead, non-interactive authorization abort)
- **WHEN** the session runner exits on that auth failure
- **THEN** the wrapper is left marked `needs_auth` with `connected=False`, and status queries report `needs_auth` with the auth-recovery hint rather than a generic error or a silent `not connected` state
- **AND** subsequent tool calls fast-fail with the needs-authorization error per the `mcp-oauth-flow` background-degradation requirement