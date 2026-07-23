# native-tool-calling Delta Spec: remove-handwritten-tools

## MODIFIED Requirements

### Requirement: Special-case tool interception

The ReAct loop SHALL intercept `plan` and `vision_query` native tool calls before `_dispatch_tool()` and route them to their existing special-case handlers. `create_tool` is not a recognized tool and SHALL NOT be intercepted; it is handled as an unknown tool by `_dispatch_tool()`.

#### Scenario: plan intercepted
- **GIVEN** the model returns a native tool call with name `plan`
- **WHEN** the loop processes the tool call
- **THEN** the loop SHALL route it to the plan execution path instead of `_dispatch_tool()`

#### Scenario: vision_query intercepted
- **GIVEN** the model returns a native tool call with name `vision_query`
- **WHEN** the loop processes the tool call
- **THEN** the loop SHALL route it to `_exec_vision_query()` instead of `_dispatch_tool()`

#### Scenario: create_tool is not intercepted
- **GIVEN** the model returns a native tool call with name `create_tool`
- **WHEN** the loop processes the tool call
- **THEN** the loop SHALL NOT route it to any special-case handler
- **AND** the loop SHALL return an error result with the message `Tool 'create_tool' is not a built-in tool, MCP tool, or vision_query.`

### Requirement: Tool definition assembly

The system SHALL assemble OpenAI-format tool definitions from built-in schemas, pseudo-tool schemas, and MCP input schemas.

#### Scenario: Built-in tools included
- **GIVEN** `BUILTIN_TOOL_SCHEMAS` contains schemas for all 15 built-in tools
- **WHEN** `build_tool_definitions()` is called
- **THEN** each built-in tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry

#### Scenario: Pseudo-tools included
- **GIVEN** `PSEUDO_TOOL_SCHEMAS` contains schemas for `plan` only
- **WHEN** `build_tool_definitions()` is called
- **THEN** each pseudo-tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry

#### Scenario: MCP tools included
- **GIVEN** an MCP manager is available with registered tools
- **WHEN** `build_tool_definitions()` is called
- **THEN** each MCP tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry using its `input_schema`

#### Scenario: Tool definitions cached
- **GIVEN** the ReAct loop has built tool definitions
- **WHEN** the loop iterates through multiple steps
- **THEN** the tool definitions SHALL be built once and reused across all steps