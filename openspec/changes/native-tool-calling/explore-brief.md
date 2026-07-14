# Explore Brief: Native Tool Calling

## Problem

The ReAct loop uses text-based JSON parsing to extract tool calls from LLM responses. Models like Kimi and GLM fail to produce valid JSON 1-2 times per execution, causing retries and wasted steps. All four models in use (Kimi, GLM, DeepSeek, Gemini) support native tool calling via OpenAI-compatible endpoints.

## Rejected Alternatives

### A) New method on LLMProvider: `chat_with_tools()` returning `str | ToolCallResponse`
Rejected because union return types are awkward in Python's structural typing. A dedicated `ChatResponse` dataclass is cleaner.

### B) Extend `chat()` with optional `tools` parameter, changing return type
Rejected because it breaks backward compatibility silently — existing callers expecting `str` would get `ChatResponse`.

### C) Separate `NativeToolCallingProvider` protocol
Rejected as over-engineering. One protocol with an optional method is simpler and the existing `LLMClient` can implement both paths.

### D) Auto-generate schemas for script tools from text descriptions
Rejected as fragile. Script tools are being phased out in favor of skills. Only built-in and MCP tools get schemas.

### E) Multi-tool calls in the first iteration
Rejected to keep scope minimal. Drop-in replacement means single tool call per turn, matching current behavior.

## Final Approach: `chat_with_tools()` as Optional Protocol Method

### Labels and Dimensions

| Dimension | Value |
|-----------|-------|
| Protocol method | `chat_with_tools(messages, tools, system, progress_cb) → ChatResponse` |
| Fallback method | `chat_with_tools_fallback(messages, tools, system, progress_cb) → ChatResponse` |
| Response type | `ChatResponse(text: str \| None, tool_calls: list[ToolCall] \| None)` |
| Tool call type | `ToolCall(id: str, name: str, arguments: dict)` |
| `tool_choice` | `"auto"` for all providers (Kimi/GLM don't support `"required"`) |
| Schema source | Built-in tools: `BUILTIN_TOOL_SCHEMAS` dict. MCP tools: `input_schema` field. Script tools: excluded. |
| Schema format | OpenAI function-calling format: `{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}` |
| Dispatch path | Native tool calls → `_dispatch_tool()` directly (skip JSON parsing). Text response → `parse_json()` fallback. |
| `finish` detection | Model returns text with no tool calls → `parse_json()` must find `{"action": "finish"}`. Non-JSON text is treated as a protocol error (same as today). |
| Prompt templates | Unchanged. JSON instructions remain for fallback path. |
| Tool result feedback | Native format: `{"role": "tool", "tool_call_id": tc.id, "content": result}` |
| Caching | Tool definitions built once at loop start, cached on `ReactContext`. |

### Tool Schema Mapping Table

| Tool | Source | Has Schema |
|------|--------|-----------|
| `shell` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `file_read` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `file_write` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `file_diff` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `file_patch` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `file_send` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `spawn_agent` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `get_agent_result` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `memory_write` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `memory_graph_search` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `memory_graph_store` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `schedule` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `secret_get` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `log_query` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| `vision_query` | Built-in | ✅ `BUILTIN_TOOL_SCHEMAS` |
| MCP tools (all) | MCP | ✅ `tool.input_schema` |
| Script tools (.sh/.py) | File system | ❌ Excluded |

### Provider Support Matrix

| Provider | `tools` param | `tool_choice` | `"required"` | Strategy |
|----------|--------------|---------------|-------------|----------|
| DeepSeek | ✅ | ✅ Full | ✅ | `tool_choice="auto"` |
| Gemini (OpenAI compat) | ✅ | ✅ Full | ✅ | `tool_choice="auto"` |
| Kimi | ✅ | ⚠️ Partial | ❌ | `tool_choice="auto"`, fallback to text |
| GLM | ✅ | ⚠️ Limited | ❌ | `tool_choice="auto"`, fallback to text |
| Ollama | ⚠️ Model-dependent | ⚠️ | ⚠️ | Try native, fallback to json_mode |
| Anthropic (native) | N/A | N/A | N/A | Not in use; not implemented |

### Key Cross-Module Data Flows

```
react_loop()
  │
  ├─► tool_schemas.build_tool_definitions(builtin_executor, mcp_manager)
  │     Returns list[dict] — OpenAI-format tool definitions
  │     Cached on ctx._tool_defs
  │
  ├─► ctx.llm.chat_with_tools_fallback(messages, tools=tool_defs, system, progress_cb)
  │     │
  │     ├─► LLMClient.chat_with_tools_fallback()
  │     │     │
  │     │     ├─► LLMClient.chat_with_tools()  [dispatches to _openai_chat_with_tools, etc.]
  │     │     │     Returns ChatResponse
  │     │     │
  │     │     └─► Fallback chain: same as chat_with_fallback (vision filtering, model switching)
  │     │
  │     └─► Returns ChatResponse
  │
  ├─► If ChatResponse.tool_calls:
  │     For each ToolCall:
  │       _dispatch_tool(ctx, {"action": "tool", "tool": tc.name, "args": tc.arguments})
  │       messages.append({"role": "assistant", "tool_calls": [...]})
  │       messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
  │
  └─► If ChatResponse.text (or NotImplementedError):
        Fall through to existing json_mode path (unchanged)
```

### Files Changed

| File | Change Type | Description |
|------|------------|-------------|
| `interfaces.py` | Modify | Add `ToolCall`, `ChatResponse` dataclasses; add `chat_with_tools()`, `chat_with_tools_fallback()` to `LLMProvider` |
| `llm_client.py` | Modify | Add `chat_with_tools()`, `chat_with_tools_fallback()`, `_openai_chat_with_tools()`, `_google_chat_with_tools()`, `_ollama_chat_with_tools()` |
| `react_loop.py` | Modify | Try native path before text path; add native tool result feedback |
| `tool_schemas.py` | **New** | `BUILTIN_TOOL_SCHEMAS` dict, `build_tool_definitions()` function |
| `agent_controller.py` | Modify | Pass tool definitions into `ReactContext` or build them in the loop |
| `vulture_whitelist.py` | Modify | Add new public symbols |

### Files Unchanged

- `parse_json()`, `extract_json_candidates()` — still the fallback
- `format_tools()` in `prompt_builder.py` — still used for text-based fallback
- All prompt templates (`05-response-format.md`, etc.) — still instruct JSON for fallback
- All tool execution (`_dispatch_tool`, `BuiltinExecutor`, MCP) — unchanged
- Confirmation flow — unchanged
- Context compaction — unchanged
- Cancellation — unchanged

## Resolved Questions

1. **Ollama native tool support**: Ollama supports OpenAI-compatible tool calling (docs.ollama.com/capabilities/tool-calling). All four providers get native paths.
2. **Tool definition size**: 15 built-in tools + MCP tools + `create_tool` + `plan` pseudo-tools. Well within OpenAI's 128-function limit.
3. **`finish` with native tools**: Model returns text with no tool calls → `parse_json()` must find `{"action": "finish"}`. Non-JSON text is treated as a protocol error (same as today).
4. **`create_tool` and `plan` actions**: Modeled as native tools. The ReAct loop intercepts them before `_dispatch_tool()` and handles them via existing `_dispatch_create_tool()` and plan execution paths.
5. **Error handling**: Fine-grained. `NotImplementedError` → fall through to json_mode. Transient `LLMError` → retry once with native, then fall through. `LLMPermanentError` → propagate (json_mode won't help). Other exceptions → log warning, fall through.
6. **System prompt**: Keep JSON instructions unchanged. Models that support native tool calling will use tools regardless of text prompt.
