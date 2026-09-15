## Purpose

Defines the transport-level lifecycle for configured MCP servers — startup connection, runtime enable/disable via `/mcp on` and `/mcp off`, and graceful shutdown — plus operator-facing status reporting (`/mcp list`, `/mcp info`) and the display contracts that map transport modes and connection states to status labels, including the `needs_auth` state for OAuth-protected servers.

## Requirements

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

### Requirement: Server status reporting

The system SHALL report the status of all configured MCP servers, including connection state, tool count, and last error. The `needs_auth` state SHALL be reported for OAuth-protected servers that are reachable but lack valid credentials.

#### Scenario: List all servers
- **GIVEN** multiple MCP servers are configured with mixed states (active, error, off, needs_auth)
- **WHEN** an operator requests the server list via `/mcp list`
- **THEN** each server is listed with its name, transport type, status, tool count, and last error (if any)

#### Scenario: Detailed server info
- **GIVEN** a specific MCP server is configured
- **WHEN** an operator requests server info via `/mcp info <name>`
- **THEN** the response includes the server's name, transport, status, URL or command, timeout, headers, environment variables, tool list, and last error

#### Scenario: Unknown server info request
- **GIVEN** no MCP server is configured with the name `unknown_server`
- **WHEN** an operator requests server info via `/mcp info unknown_server`
- **THEN** the response indicates the server was not found

### Requirement: MCP server status display contracts

The system SHALL consistently map transport modes and connection states to display labels in server status responses. The `needs_auth` state SHALL be displayed for OAuth-protected servers awaiting authentication.

#### Scenario: HTTP and SSE transports displayed as web
- **GIVEN** MCP servers configured with `transport: http` and `transport: sse` respectively
- **WHEN** their status is listed via server list or server info
- **THEN** both are shown with transport label `web`

#### Scenario: Stdio transport displayed as stdio
- **GIVEN** an MCP server configured with `transport: stdio`
- **WHEN** its status is listed via server list or server info
- **THEN** it is shown with transport label `stdio`

#### Scenario: Server info for a disabled server
- **GIVEN** a configured MCP server with `enabled: false`
- **WHEN** an operator requests detailed server info for that server
- **THEN** the response includes `status: off` and does not include active connection details

#### Scenario: Server info for a server in error state
- **GIVEN** a configured MCP server that failed to connect or lost its connection
- **WHEN** an operator requests detailed server info for that server
- **THEN** the response includes `status: error` and the `last_error` message describing the failure

#### Scenario: Server info for a server needing authentication
- **GIVEN** a configured MCP server with an `oauth` section that has no valid stored token
- **WHEN** an operator requests detailed server info or lists servers
- **THEN** the response includes `status: needs_auth` indicating the server is reachable but requires OAuth authentication via `/mcp auth <name>`
