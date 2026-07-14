# Use native tool calling as primary path with text-based JSON fallback

## Status

Accepted

## Date

2026-07-14

## Supersedes

None

## Context and Problem Statement

The ReAct loop currently uses text-based JSON parsing (`parse_json()`) to extract tool calls from LLM responses. Models like Kimi and GLM fail to produce valid JSON 1-2 times per execution, causing retries and wasted steps. All four models in active use (Kimi, GLM, DeepSeek, Gemini) plus Ollama support native tool calling via OpenAI-compatible endpoints.

The question is whether to add native tool calling as the primary dispatch path, keeping the text-based JSON path as a universal fallback.

## Considered Options

- **Native-first with text fallback**: Try `chat_with_tools()` first. If the provider returns structured tool calls, dispatch natively. If it returns text, parse it in place with `parse_json()`. If the provider doesn't support tools or errors out, fall through to the existing `chat(json_mode=True)` path.
- **Text-only (status quo)**: Keep the current `chat(json_mode=True)` + `parse_json()` path as the only path. Accept the 1-2 JSON failures per execution for Kimi/GLM.
- **Native-only**: Drop the text-based path entirely. Rejected because Ollama models may not support tools, and Kimi/GLM may return text instead of tool calls.

## Decision Outcome

Chosen option: "Native-first with text fallback", because it eliminates the JSON parsing failure mode for models that support native tool calling while preserving the existing behavior as a safety net for all models. The text-based path is unchanged — it remains the universal fallback.

### Consequences

- Good, because models that support native tool calling (DeepSeek, Gemini, Kimi, GLM, Ollama) no longer depend on JSON parsing for tool dispatch, eliminating the 1-2 failures per execution.
- Good, because the text-based path is preserved unchanged — no regression risk for any model or provider.
- Good, because the protocol change is backward-compatible: `chat_with_tools()` and `chat_with_tools_fallback()` are optional methods on `LLMProvider`; existing `chat()` and `chat_with_fallback()` signatures are unchanged.
- Bad, because the system now has two dispatch paths (native and text-based) that must be kept coherent. Tool schemas in `builtin_tools/schemas.py` and arg descriptions in `builtin_tools/descriptors.py` describe the same interfaces and must be kept in sync.
- Bad, because `create_tool` and `plan` are modeled as pseudo-tools in the native path but remain action types in the text path — two representations of the same capability.
- Neutral, because multi-tool calls are deferred to a follow-on change. The native path dispatches a single tool call per turn, matching current behavior.
