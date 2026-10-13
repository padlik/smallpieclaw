# native-tool-calling Specification

## Purpose

Modify tool-call retry and request budgeting while preserving native-first dispatch, provider payload contracts, and the full existing tool-definition surface.

## MODIFIED Requirements

### Requirement: Native tool calling path

The ReAct loop SHALL use native calling first. A local pre-network unsupported result SHALL select same-model JSON without consuming an attempt. Recognized transient errors SHALL retry the same request form under the shared logical-request budget. Permanent/unexpected errors SHALL terminate without automatic protocol switching. A native text response is parsed in place.

#### Scenario: Native tool call succeeds
- **GIVEN** tool definitions contain built-in, pseudo-tool, and MCP schemas and native calling is supported
- **WHEN** provider returns `ChatResponse(tool_calls=[...])`
- **THEN** first call is dispatched without `parse_json()`
- **AND** assistant native-call and matching tool-result messages are appended
- **AND** additional calls in that response are ignored

#### Scenario: Model returns text instead of tool calls
- **GIVEN** native calling returns `ChatResponse(text="...")` without calls
- **WHEN** the response is handled
- **THEN** the text is parsed in place without another LLM call
- **AND** finish and tool actions use existing handlers

#### Scenario: Provider does not support native tool calling
- **GIVEN** `chat_with_tools()` raises `NotImplementedError` before network I/O
- **WHEN** the loop selects JSON mode
- **THEN** it uses the same pinned model and shared request state
- **AND** the local conversion consumes no outbound attempt

#### Scenario: Transient error during native tool calling
- **GIVEN** native call raises a recognized transient provider error
- **WHEN** an automatic retry remains
- **THEN** native calling is retried in the same form under the shared budget
- **AND** it does not switch to JSON to hide provider failure

#### Scenario: Permanent error during native tool calling
- **GIVEN** native call raises `LLMPermanentError`
- **WHEN** the loop handles the error
- **THEN** it propagates immediately without JSON fallback

#### Scenario: Unexpected exception during native tool calling
- **GIVEN** native call raises an unexpected untyped exception
- **WHEN** the loop handles the error
- **THEN** it logs/classifies the terminal error and does not auto-fallback to JSON

#### Scenario: Message history linearized before json_mode fallback
- **GIVEN** history contains native assistant tool calls and matching tool results
- **WHEN** an allowed JSON-mode request is prepared
- **THEN** native wire turns are flattened 1:1 without native-only fields
- **AND** goal indexes remain valid and previous tool effects are not replayed

### Requirement: Tool definition assembly

The system SHALL build OpenAI-format definitions from 15 built-in schemas, the `plan` pseudo-tool schema, and MCP input schemas. It SHALL exclude script tools and cache definitions across loop steps. Complete tool definitions SHALL count toward actual-wire token budgets and compaction.

#### Scenario: Built-in tools included
- **GIVEN** `BUILTIN_TOOL_SCHEMAS` contains all 15 built-in tools
- **WHEN** `build_tool_definitions()` is called
- **THEN** every built-in produces the established OpenAI function schema

#### Scenario: Pseudo-tools included
- **GIVEN** `PSEUDO_TOOL_SCHEMAS` contains only `plan`
- **WHEN** definitions are built
- **THEN** `plan` is emitted in the established function schema

#### Scenario: MCP tools included
- **GIVEN** an MCP manager has registered tools
- **WHEN** definitions are built
- **THEN** each uses its MCP `input_schema`

#### Scenario: Script tools excluded
- **GIVEN** `.sh` or `.py` script tools are registered
- **WHEN** definitions are built
- **THEN** script tools do not appear in the provider tool array

#### Scenario: Tool definitions cached
- **GIVEN** the ReAct loop iterates multiple steps
- **WHEN** tool definitions are first assembled
- **THEN** the same definitions are reused across loop steps

#### Scenario: Tool definitions count toward request budget
- **GIVEN** the request contains definitions and native arguments/framing
- **WHEN** input budget and compaction are computed
- **THEN** all those tokens count in the complete target payload

### Requirement: LLMProvider protocol extension

`LLMProvider` SHALL retain native methods and existing chat signatures/return types. Compatibility methods SHALL remain single-model and propagate errors to the shared retry owner.

#### Scenario: chat_with_tools returns structured response
- **GIVEN** an implementation supports native calling
- **WHEN** `chat_with_tools(messages,tools,system)` is called
- **THEN** it returns `ChatResponse` with text or tool calls populated

#### Scenario: chat_with_tools_fallback operates single-model
- **GIVEN** the pinned provider call fails transiently
- **WHEN** the wrapper returns control
- **THEN** the failure is passed to the shared retry owner
- **AND** another model is not tried

#### Scenario: Existing chat methods unchanged
- **GIVEN** an `LLMProvider` implementation
- **WHEN** `chat()` or `chat_with_fallback()` is called
- **THEN** established method signatures and return types remain unchanged
- **AND** the fallback-named method remains single-model

### Requirement: Provider-native tool calling implementations

Supported provider adapters SHALL preserve their provider-specific native tool payload and parse structured calls or text. Ordinary chat/native/JSON requests SHALL omit `temperature` and `top_p`; adapter compatibility may strip unsupported fields.

#### Scenario: OpenAI-compatible provider sends tools in payload
- **GIVEN** an OpenAI-compatible model receives a native request
- **WHEN** `_openai_chat_with_tools()` builds its request
- **THEN** payload includes `tools` and `tool_choice="auto"`
- **AND** omits JSON-only response format

#### Scenario: OpenAI-compatible provider parses tool_calls
- **GIVEN** the response contains `choices[0].message.tool_calls`
- **WHEN** the adapter parses it
- **THEN** each returned call includes ID, name, and parsed arguments

#### Scenario: OpenAI-compatible provider handles text response
- **GIVEN** response content exists without tool calls
- **WHEN** the adapter parses it
- **THEN** it returns `ChatResponse(text=content)`

#### Scenario: Google Gemini provider sends tools in payload
- **GIVEN** Gemini is used through its OpenAI-compatible endpoint
- **WHEN** `_google_chat_with_tools()` builds its request
- **THEN** payload includes tools and automatic tool selection

#### Scenario: Ollama provider sends tools to the client
- **GIVEN** cloud Ollama receives a native request
- **WHEN** `_ollama_chat_with_tools()` builds its request
- **THEN** it passes tools to the Ollama client
- **AND** omits unsupported `tool_choice`
