## ADDED Requirements

### Requirement: Native tool calling path

The ReAct loop SHALL attempt native tool calling before falling back to the text-based JSON path.

#### Scenario: Native tool call succeeds
- **GIVEN** the ReAct loop has built tool definitions from built-in schemas, pseudo-tool schemas, and MCP input schemas
- **AND** the LLM provider supports native tool calling
- **WHEN** the loop calls `chat_with_tools_fallback()` with messages and tool definitions
- **AND** the provider returns `ChatResponse(tool_calls=[...])`
- **THEN** the loop SHALL dispatch the first tool call via `_dispatch_tool()` without running `parse_json()`
- **AND** the loop SHALL append an assistant message carrying the `tool_calls` block, followed by a `role: "tool"` message with the matching `tool_call_id` and the tool result
- **AND** additional tool calls in the response SHALL be ignored (single tool call per turn)

#### Scenario: Model returns text instead of tool calls
- **GIVEN** the LLM provider supports native tool calling
- **WHEN** the loop calls `chat_with_tools_fallback()` with messages and tool definitions
- **AND** the provider returns `ChatResponse(text="...")` with no tool calls
- **THEN** the loop SHALL run `parse_json()` on the returned text in place without issuing a second LLM call
- **AND** if `parse_json()` returns `{"action": "finish", ...}`, the loop SHALL handle it as a finish action
- **AND** if `parse_json()` returns `{"action": "tool", ...}`, the loop SHALL dispatch it via the existing text-based path

#### Scenario: Provider does not support native tool calling
- **GIVEN** the LLM provider does not implement `chat_with_tools()`
- **WHEN** the loop calls `chat_with_tools_fallback()`
- **AND** the provider raises `NotImplementedError`
- **THEN** the loop SHALL fall through to the existing `chat_with_fallback(json_mode=True)` path
- **AND** the loop SHALL parse the response with `parse_json()` as it does today

#### Scenario: Transient error during native tool calling
- **GIVEN** the LLM provider supports native tool calling
- **WHEN** the loop calls `chat_with_tools_fallback()`
- **AND** the provider raises a transient `LLMError`
- **THEN** the loop SHALL retry the native call once
- **AND** if the retry also fails, the loop SHALL fall through to the existing `chat_with_fallback(json_mode=True)` path

#### Scenario: Permanent error during native tool calling
- **GIVEN** the LLM provider supports native tool calling
- **WHEN** the loop calls `chat_with_tools_fallback()`
- **AND** the provider raises `LLMPermanentError` (e.g., bad API key, content filter)
- **THEN** the loop SHALL propagate the error immediately without falling back to `json_mode`

#### Scenario: Unexpected exception during native tool calling
- **GIVEN** the LLM provider supports native tool calling
- **WHEN** the loop calls `chat_with_tools_fallback()`
- **AND** the provider raises an unexpected exception (not `LLMError`, `LLMPermanentError`, or `NotImplementedError`)
- **THEN** the loop SHALL log a warning
- **AND** the loop SHALL fall through to the existing `chat_with_fallback(json_mode=True)` path

#### Scenario: Message history linearized before json_mode fallback
- **GIVEN** a prior step dispatched a native tool call, appending an assistant `tool_calls` message (`content: null`) and a `role: "tool"` result message to the history
- **AND** a later step falls through to the `chat_with_fallback(json_mode=True)` path (unsupported provider, transient error after retry, or unexpected exception)
- **WHEN** the loop issues the json_mode call
- **THEN** the native-wire-format turns SHALL be flattened to plain text, so the payload contains no `content: null`, `tool_calls`, `role: "tool"`, or `tool_call_id` fields
- **AND** the flattening SHALL be 1:1 (message count preserved) so any pinned goal index stays valid

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

#### Scenario: Script tools excluded
- **GIVEN** script tools (`.sh`/`.py`) exist in the tool registry
- **WHEN** `build_tool_definitions()` is called
- **THEN** script tools SHALL NOT appear in the tool definitions array

#### Scenario: Tool definitions cached
- **GIVEN** the ReAct loop has built tool definitions
- **WHEN** the loop iterates through multiple steps
- **THEN** the tool definitions SHALL be built once and reused across all steps

### Requirement: LLMProvider protocol extension

The `LLMProvider` protocol SHALL support optional native tool calling methods.

#### Scenario: chat_with_tools returns structured response
- **GIVEN** an `LLMProvider` implementation supports native tool calling
- **WHEN** `chat_with_tools(messages, tools, system)` is called
- **THEN** it SHALL return a `ChatResponse` with either `text` or `tool_calls` populated

#### Scenario: chat_with_tools_fallback chains fallback models
- **GIVEN** an `LLMProvider` implementation supports native tool calling with fallback models configured
- **WHEN** `chat_with_tools_fallback(messages, tools, system)` is called
- **AND** the primary model fails with a transient error
- **THEN** it SHALL try each fallback model in order, same as `chat_with_fallback()`

#### Scenario: Existing chat methods unchanged
- **GIVEN** an `LLMProvider` implementation
- **WHEN** `chat()` or `chat_with_fallback()` is called
- **THEN** the method signatures and return types SHALL be unchanged from before this change

### Requirement: Provider-native tool calling implementations

Each supported provider SHALL have a native tool calling implementation that sends tool definitions in the API request and parses structured tool calls from the response.

#### Scenario: OpenAI-compatible provider sends tools in payload
- **GIVEN** the active model uses an OpenAI-compatible provider (Kimi, GLM, DeepSeek)
- **WHEN** `_openai_chat_with_tools()` is called
- **THEN** the API payload SHALL include `"tools"` and `"tool_choice": "auto"`
- **AND** the API payload SHALL NOT include `"response_format": {"type": "json_object"}`

#### Scenario: OpenAI-compatible provider parses tool_calls
- **GIVEN** the API response contains `choices[0].message.tool_calls`
- **WHEN** `_openai_chat_with_tools()` processes the response
- **THEN** it SHALL return `ChatResponse(tool_calls=[...])` with each tool call's `id`, `name`, and parsed `arguments`

#### Scenario: OpenAI-compatible provider handles text response
- **GIVEN** the API response contains `choices[0].message.content` but no `tool_calls`
- **WHEN** `_openai_chat_with_tools()` processes the response
- **THEN** it SHALL return `ChatResponse(text=content)`

#### Scenario: Google Gemini provider sends tools in payload
- **GIVEN** the active model uses Google Gemini via OpenAI-compatible endpoint
- **WHEN** `_google_chat_with_tools()` is called
- **THEN** the API payload SHALL include `"tools"` and `"tool_choice": "auto"`

#### Scenario: Ollama provider sends tools to the client
- **GIVEN** the active model uses Ollama
- **WHEN** `_ollama_chat_with_tools()` is called
- **THEN** the request SHALL pass `tools` to the Ollama client
- **AND** the request SHALL NOT set `tool_choice` — the Ollama SDK/API exposes no such parameter; tool selection is implicitly auto (the model decides)

## MODIFIED Requirements

<!-- No existing spec-level behavior changes. The ReAct loop's iteration order changes (native-before-text), but this is an implementation detail of the new capability, not a modification to an existing spec. -->

## REMOVED Requirements

<!-- No requirements removed. -->
