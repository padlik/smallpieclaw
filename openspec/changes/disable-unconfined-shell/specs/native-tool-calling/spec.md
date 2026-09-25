## REMOVED Requirements

### Requirement: Tool definition assembly

**Reason**: The prior always-include and generation-independent caching contract is replaced by runtime capability-aware offers and generation-sensitive refresh. A distinct replacement heading avoids losing scenarios under strict validation.

**Migration**: Providers continue receiving OpenAI-format definitions assembled from currently callable built-ins, pseudo-tools, and MCP schemas. Hidden shell built-ins remain reserved against MCP collisions; semantic discovery and cached offers follow the current capability generation.

## ADDED Requirements

### Requirement: Capability-aware tool definition assembly

The system SHALL assemble OpenAI-format tool definitions from currently callable built-in schemas, pseudo-tool schemas, and MCP input schemas. When shell capability is unavailable, the system SHALL omit `shell`, `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get` from callable definitions and semantic discovery. Excluding these built-ins from offers MUST NOT release their reserved names for MCP tools. The system MUST cache tool definitions while the capability generation is unchanged and rebuild before the next LLM request after the generation changes. Executor-side availability checks remain authoritative for calls already emitted or in flight.

#### Scenario: Available built-in tools included
- **GIVEN** `BUILTIN_TOOL_SCHEMAS` contains schemas for the built-in tools and shell capability is available
- **WHEN** `build_tool_definitions()` is called
- **THEN** each currently callable built-in tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry

#### Scenario: Unavailable shell family omitted
- **GIVEN** shell capability is unavailable
- **WHEN** `build_tool_definitions()` and semantic tool discovery are evaluated
- **THEN** `shell`, `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get` are absent from callable definitions and discovery results
- **AND** all otherwise-callable non-shell built-ins remain included

#### Scenario: Excluded built-in names remain reserved from MCP tools
- **GIVEN** shell capability is unavailable
- **AND** an MCP server registers a tool named `shell` or any `shell_env_*` built-in
- **WHEN** tool definitions are assembled
- **THEN** the conflicting MCP tool is rejected or excluded as a built-in name collision
- **AND** it does not appear as a callable substitute for the hidden built-in

#### Scenario: Pseudo-tools included
- **GIVEN** `PSEUDO_TOOL_SCHEMAS` contains schemas for `plan` only
- **WHEN** `build_tool_definitions()` is called
- **THEN** each pseudo-tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry

#### Scenario: MCP tools included
- **GIVEN** an MCP manager is available with registered non-conflicting tools
- **WHEN** `build_tool_definitions()` is called
- **THEN** each MCP tool SHALL produce a `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` entry using its `input_schema`

#### Scenario: Script tools excluded
- **GIVEN** script tools (`.sh`/`.py`) exist in the tool registry
- **WHEN** `build_tool_definitions()` is called
- **THEN** script tools SHALL NOT appear in the tool definitions array

#### Scenario: Tool definitions reused within a capability generation
- **GIVEN** the ReAct loop has built tool definitions for the current capability generation
- **WHEN** the loop iterates through multiple steps without a generation change
- **THEN** the tool definitions SHALL be reused across those steps

#### Scenario: Tool definitions refreshed after runtime loss
- **GIVEN** the ReAct loop has cached definitions for a generation in which shell capability was available
- **AND** runtime backend loss changes the capability generation to unavailable
- **WHEN** the loop is about to make its next LLM request
- **THEN** it rebuilds the definitions for the new generation before sending the request
- **AND** all five shell-family tools are omitted

#### Scenario: Already emitted or in-flight shell call remains executor-gated
- **GIVEN** a shell-family definition was already sent to an LLM before capability loss
- **WHEN** the LLM returns that shell-family call after loss, including via the native or JSON-mode path
- **THEN** the built-in executor returns nonrecoverable `shell_unavailable`
- **AND** no command or shell-environment operation executes and no host fallback occurs
