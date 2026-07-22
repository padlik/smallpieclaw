## ADDED Requirements

### Requirement: MCP server connection lifecycle

The system SHALL connect to configured MCP servers on startup and maintain connections for the agent's lifetime. Each server's connection state SHALL be independently tracked and queryable.

#### Scenario: Successful connection on startup
- **GIVEN** a valid MCP server is configured with `enabled: true` and a reachable transport endpoint
- **WHEN** the agent starts and initializes MCP servers
- **THEN** the server connects successfully, its tools are discovered and registered, and its status is reported as `active`

#### Scenario: Connection failure on startup
- **GIVEN** a configured MCP server is unreachable (subprocess fails to start, or HTTP endpoint returns an error)
- **WHEN** the agent starts and attempts to connect
- **THEN** the server's status is reported as `error` with a descriptive `last_error` message, and the agent starts without that server's tools

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

### Requirement: MCP tool discovery

The system SHALL discover all tools from connected MCP servers, including servers that paginate their tool lists across multiple responses.

#### Scenario: Tool discovery from a single-page server
- **GIVEN** an MCP server is connected and returns all tools in a single `tools/list` response
- **WHEN** the server's tools are listed
- **THEN** all tools are registered with their names, descriptions, and input schemas intact

#### Scenario: Tool discovery from a paginated server
- **GIVEN** an MCP server returns tools across multiple pages with `nextCursor` tokens
- **WHEN** the server's tools are listed
- **THEN** all tools from all pages are collected and registered, and listing stops when `nextCursor` is absent

#### Scenario: Tool name conflict between servers
- **GIVEN** two connected MCP servers both provide a tool with the same name
- **WHEN** tools are registered
- **THEN** the tool from the first-connected server is registered, the conflicting tool from the second server is skipped, and a warning is logged

#### Scenario: Tool with empty name is skipped
- **GIVEN** an MCP server returns a tool entry with an empty or missing `name` field
- **WHEN** tools are registered
- **THEN** that tool entry is silently skipped and does not appear in the tool registry

### Requirement: MCP tool invocation

The system SHALL invoke tools on connected MCP servers and return results in a consistent format regardless of the underlying transport.

#### Scenario: Successful tool call with text output
- **GIVEN** a connected MCP server provides a tool named `example_tool`
- **WHEN** the agent calls `example_tool` with valid arguments
- **THEN** the result is `{"success": true, "output": "<tool output text>", "error": "", "exit_code": 0}`

#### Scenario: Tool call returns an error from the server
- **GIVEN** a connected MCP server provides a tool named `example_tool`
- **WHEN** the agent calls `example_tool` and the server responds with `isError: true`
- **THEN** the result is `{"success": false, "output": "", "error": "<error text>", "exit_code": 1}`

#### Scenario: Tool call to an unknown tool
- **GIVEN** no connected MCP server provides a tool named `nonexistent_tool`
- **WHEN** the agent calls `nonexistent_tool`
- **THEN** the result is `{"success": false, "output": "", "error": "MCP tool 'nonexistent_tool' not found", "exit_code": 1}`

#### Scenario: Tool call to a disconnected server
- **GIVEN** a server that was previously connected is now disconnected
- **WHEN** the agent calls a tool from that server
- **THEN** the result is `{"success": false, "output": "", "error": "MCP server '<name>' not connected", "exit_code": 1}`

#### Scenario: Tool call with image content in result
- **GIVEN** a tool call returns content containing an image item
- **WHEN** the result is processed
- **THEN** the output includes `[image: <mime_type>]` for each image item

#### Scenario: Tool call with embedded resource in result
- **GIVEN** a tool call returns content containing an embedded resource item
- **WHEN** the result is processed
- **THEN** the output includes `[resource: <uri>]` for each resource item

#### Scenario: Tool call with audio content in result
- **GIVEN** a tool call returns content containing an audio item
- **WHEN** the result is processed
- **THEN** the output includes `[audio: <mime_type>]` for each audio item

#### Scenario: Tool call with resource link in result
- **GIVEN** a tool call returns content containing a resource link item
- **WHEN** the result is processed
- **THEN** the output includes `[resource_link: <uri>]` for each resource link item

#### Scenario: Tool call with mixed content types
- **GIVEN** a tool call returns content containing text, image, and resource items
- **WHEN** the result is processed
- **THEN** all content types are represented in the output, joined by newlines

### Requirement: Connection loss handling

The system SHALL handle transport-level connection failures during tool invocation by returning an error result rather than crashing or silently retrying.

#### Scenario: Stdio subprocess dies during tool call
- **GIVEN** a stdio-based MCP server's subprocess terminates unexpectedly
- **WHEN** the agent calls a tool on that server
- **THEN** the result is `{"success": false, "output": "", "error": "<descriptive error>", "exit_code": 1}`, the server's status changes to `error`, and the agent continues with the next step

#### Scenario: HTTP endpoint becomes unreachable during tool call
- **GIVEN** an HTTP-based MCP server's endpoint becomes unreachable
- **WHEN** the agent calls a tool on that server
- **THEN** the result is `{"success": false, "output": "", "error": "<descriptive error>", "exit_code": 1}`, the server's status changes to `error`, and the agent continues with the next step

#### Scenario: Tool call timeout
- **GIVEN** a connected MCP server does not respond to a tool call within the configured timeout
- **WHEN** the agent calls a tool on that server
- **THEN** the result is `{"success": false, "output": "", "error": "<timeout error>", "exit_code": 1}`

### Requirement: Server status reporting

The system SHALL report the status of all configured MCP servers, including connection state, tool count, and last error.

#### Scenario: List all servers
- **GIVEN** multiple MCP servers are configured with mixed states (active, error, off)
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

### Requirement: Tool existence check

The system SHALL provide a fast lookup to determine whether a given tool name belongs to any connected MCP server.

#### Scenario: Tool exists
- **GIVEN** a connected MCP server provides a tool named `example_tool`
- **WHEN** the system checks if `example_tool` is an MCP tool
- **THEN** the check returns `true`

#### Scenario: Tool does not exist
- **GIVEN** no connected MCP server provides a tool named `nonexistent_tool`
- **WHEN** the system checks if `nonexistent_tool` is an MCP tool
- **THEN** the check returns `false`

### Requirement: MCP server session startup failure handling

The system SHALL handle failures in the MCP SDK session initialization sequence — both during
`initialize()` and during `list_tools()` — by resolving the connection attempt promptly with a
descriptive error rather than blocking until a timeout fires.

#### Scenario: Session initialize failure
- **GIVEN** a configured MCP server where the SDK `initialize()` call raises an exception
- **WHEN** the agent attempts to connect to that server
- **THEN** the connection attempt resolves promptly with `connected=False` and a descriptive
  `last_error` message, and the agent starts without that server's tools

#### Scenario: Tool list failure after successful initialize
- **GIVEN** a configured MCP server where `initialize()` succeeds but `list_tools()` raises
  an exception
- **WHEN** the agent attempts to connect to that server
- **THEN** the connection attempt resolves promptly with `connected=False` and a descriptive
  `last_error` message, and the agent starts without that server's tools

#### Scenario: Unknown transport type in server config
- **GIVEN** a server config entry with a `transport` value that is not one of `stdio`, `http`,
  or `sse`
- **WHEN** the agent attempts to connect to that server
- **THEN** the connection attempt resolves with `connected=False` and a descriptive `last_error`
  message identifying the unsupported transport

### Requirement: MCP tool discovery pagination limits

The system SHALL enforce a maximum number of pagination pages when listing tools from a server,
distinct from the maximum total tool count, to prevent runaway pagination loops.

#### Scenario: Pagination page limit exceeded
- **GIVEN** an MCP server that returns tools across more than 50 consecutive pages, with fewer
  than 500 total tools
- **WHEN** the server's tools are listed
- **THEN** the connection fails with `connected=False` and a descriptive `last_error` message
  indicating the page limit was exceeded, and no tools from that server are registered

### Requirement: MCP tool result content normalization

The system SHALL normalize edge cases in MCP tool result content items to produce safe, readable
output strings regardless of missing or null fields.

#### Scenario: Resource item with null resource field
- **GIVEN** a tool call returns a content item of type `resource` where the resource field is
  `null` or absent
- **WHEN** the result is processed
- **THEN** the output includes `[resource]` as a placeholder for that item

#### Scenario: Tool description truncated at schema limit
- **GIVEN** an MCP server returns a tool with a description longer than 2048 characters
- **WHEN** the tool is registered
- **THEN** the registered description is truncated to 2048 characters and the tool is otherwise
  registered normally

### Requirement: MCP server status display contracts

The system SHALL consistently map transport modes and connection states to display labels in
server status responses.

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
- **THEN** the response includes `status: error` and the `last_error` message describing the
  failure

### Requirement: MCP manager lifecycle idempotency

The system SHALL handle redundant lifecycle operations without spawning duplicate background
resources or triggering unnecessary reconnections.

#### Scenario: Event loop start is idempotent
- **GIVEN** the MCP manager's background event loop is already running
- **WHEN** the loop is started a second time
- **THEN** no additional loop thread is created and the existing loop continues normally

#### Scenario: Enable already-connected server is a no-op
- **GIVEN** a configured MCP server that is currently connected and active
- **WHEN** an operator enables the server via `/mcp on <name>` (which was already on)
- **THEN** no reconnection occurs, the server remains active, and the operation returns success

## MODIFIED Requirements

<!-- No existing spec-level behaviour changes. The MCP tool interface (has_tool, call_tool, get_tools) is unchanged. -->

## REMOVED Requirements

<!-- No behaviours are removed. -->
