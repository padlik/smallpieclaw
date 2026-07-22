## ADDED Requirements

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

<!-- No existing requirement text is being changed — all additions above are new scenarios
     for behaviors that existed in the implementation but were not previously captured in spec. -->

## REMOVED Requirements

<!-- No behaviours are removed. -->
