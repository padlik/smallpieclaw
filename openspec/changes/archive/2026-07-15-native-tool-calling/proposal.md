## Why

The ReAct loop parses tool calls from LLM text output via a three-stage JSON extractor. Models like Kimi and GLM fail to produce valid JSON 1-2 times per execution, causing retries, wasted steps, and degraded reliability. All four models in active use (Kimi, GLM, DeepSeek, Gemini) plus Ollama support native tool calling via OpenAI-compatible endpoints. Adding a native path eliminates the JSON parsing failure mode for these models while keeping the text-based path as a universal fallback.

## What Changes

- Add `chat_with_tools()` and `chat_with_tools_fallback()` methods to the `LLMProvider` protocol and `LLMClient` implementation, returning a `ChatResponse` dataclass (text or structured tool calls)
- Add provider-specific native tool calling implementations for OpenAI-compatible, Google Gemini, and Ollama endpoints
- Add `builtin_tools/schemas.py` with JSON Schema parameter definitions for all 15 built-in tools, MCP tools (via their existing `input_schema`), plus `create_tool` and `plan` as pseudo-tools
- Modify the ReAct loop to try native tool calling first, falling back to the existing `json_mode` + `parse_json()` path on `NotImplementedError`, transient `LLMError`, or when the model returns text. `LLMPermanentError` (bad API key, content filter) propagates immediately — it cannot be fixed by switching to `json_mode`
- Intercept `create_tool`, `plan`, and `vision_query` native tool calls in the loop before `_dispatch_tool()`, routing them to their existing special-case handlers
- Feed tool results back to the model in native format (`role: "tool"` messages with `tool_call_id`)
- Keep all existing JSON parsing, prompt templates, tool execution, confirmation, and context compaction unchanged

## Capabilities

### New Capabilities
- `native-tool-calling`: The LLM provider layer supports passing tool definitions in API calls and receiving structured tool call responses. The ReAct loop tries this native path before the text-based JSON path, dispatches native tool calls directly (bypassing JSON parsing), and feeds results back in native message format. Falls back to the existing `json_mode` text path when the provider doesn't support tools, on transient errors, or when the model returns text instead of tool calls.

### Modified Capabilities
<!-- No existing spec-level behavior changes. The ReAct loop's iteration order changes (native-before-text), but this is an implementation detail of the new capability, not a modification to an existing spec. -->

## Impact

- **Affected code**: `interfaces.py` (protocol), `llm_client.py` (implementation), `react_loop.py` (integration), `agent_controller.py` (wiring), `vulture_whitelist.py` (new symbols)
- **New file**: `builtin_tools/schemas.py`
- **Dependencies**: None new. Uses existing OpenAI-compatible HTTP endpoints already configured
- **Breaking changes**: None. `LLMProvider` gains optional methods; existing `chat()` and `chat_with_fallback()` signatures unchanged
- **Risk**: Low. The text-based JSON path is preserved as fallback. If native tool calling fails for any reason, the agent falls through to the existing behavior. Native-capable models may still emit JSON-in-text alongside tool calls — this is absorbed by the fallback path
