## 1. Protocol and types (interfaces.py)

- [x] 1.1 Add `ToolCall` dataclass with `id`, `name`, `arguments` fields
- [x] 1.2 Add `ChatResponse` dataclass with `text`, `tool_calls`, and `is_tool_call` property
- [x] 1.3 Add `chat_with_tools()` method to `LLMProvider` protocol
- [x] 1.4 Add `chat_with_tools_fallback()` method to `LLMProvider` protocol

## 2. Tool schemas (builtin_tools/schemas.py)

- [x] 2.1 Create `builtin_tools/schemas.py` with `BUILTIN_TOOL_SCHEMAS` dict covering all 15 built-in tools (shell, file_read, file_write, file_diff, file_patch, file_send, spawn_agent, get_agent_result, memory_write, memory_graph_search, memory_graph_store, schedule, secret_get, log_query, vision_query)
- [x] 2.2 Add `PSEUDO_TOOL_SCHEMAS` dict for `create_tool` and `plan`
- [x] 2.3 Implement `build_tool_definitions(builtin_executor, mcp_manager) -> list[dict]` that merges built-in, pseudo, and MCP schemas into OpenAI-format tool definitions
- [x] 2.4 Add comment noting descriptor/schema co-location and sync requirement

## 3. LLMClient native implementations (llm_client.py)

- [x] 3.1 Add `chat_with_tools()` method to `LLMClient` that dispatches to provider-specific `_*_chat_with_tools()` methods based on `self.llm_cfg["provider"]`. Providers without a native implementation (e.g., `anthropic`) SHALL raise `NotImplementedError`
- [x] 3.2 Add `chat_with_tools_fallback()` method to `LLMClient` that mirrors `chat_with_fallback()` fallback chain (vision filtering, model switching, `_active_idx` management) but calls `chat_with_tools()` instead of `chat()`
- [x] 3.3 Implement `_openai_chat_with_tools()` — mirrors `_openai_chat()` but adds `tools`/`tool_choice:"auto"` to payload, removes `response_format`, parses `tool_calls` from response, returns `ChatResponse`
- [x] 3.4 Implement `_google_chat_with_tools()` — mirrors `_google_chat()` with same tool payload additions
- [x] 3.5 Implement `_ollama_chat_with_tools()` — mirrors `_ollama_chat()` with same tool payload additions

## 4. ReAct loop integration (react_loop.py)

- [x] 4.1 Add `_tool_defs` field to `ReactContext` dataclass (default `None`)
- [x] 4.2 Import `build_tool_definitions` from `builtin_tools.schemas`
- [x] 4.3 Build and cache tool definitions at loop start: call `build_tool_definitions()` once, store on `ctx._tool_defs`
- [x] 4.4 Add native tool calling attempt before the existing `json_mode` path: try `chat_with_tools_fallback()`, handle three outcomes (tool_calls → dispatch, text → parse_json in place, error → fall through)
- [x] 4.5 Implement special-case intercepts: route `create_tool` to `_dispatch_create_tool()`, `plan` to plan execution, `vision_query` to `_exec_vision_query()`
- [x] 4.6 Implement native tool result feedback: append assistant `tool_calls` message followed by `role:"tool"` message with `tool_call_id`
- [x] 4.7 Implement error handling: `NotImplementedError` → fall through, transient `LLMError` → retry once then fall through, `LLMPermanentError` → propagate, unexpected → log warning then fall through
- [x] 4.8 Ensure existing `json_mode` path is unchanged and still functions as fallback

## 5. Wiring (agent_controller.py)

- [x] 5.1 Verify `agent_controller.py` needs no changes (tool definitions are built in the loop, not wired from outside). If `ReactContext` construction needs `_tool_defs` initialization, add it.

## 6. Vulture whitelist

- [x] 6.1 Add `ToolCall`, `ChatResponse`, `chat_with_tools`, `chat_with_tools_fallback`, `build_tool_definitions`, `BUILTIN_TOOL_SCHEMAS`, `PSEUDO_TOOL_SCHEMAS` to `vulture_whitelist.py`

## 7. Tests

- [x] 7.1 Test `build_tool_definitions()`: verify output format, built-in + pseudo + MCP merge, script tool exclusion
- [x] 7.1a Test tool definition caching: assert `build_tool_definitions()` is invoked once across a multi-step `ScriptedLLM` run, and `ctx._tool_defs` is reused
- [x] 7.2 Test `ChatResponse` dataclass: `is_tool_call` property, `text`/`tool_calls` mutual exclusivity
- [x] 7.3 Test `_openai_chat_with_tools()`: payload includes `tools`/`tool_choice`, excludes `response_format`, parses `tool_calls` correctly, handles text-only response
- [x] 7.3a Test `_google_chat_with_tools()`: payload includes `tools`/`tool_choice`, excludes `response_format`
- [x] 7.3b Test `_ollama_chat_with_tools()`: payload includes `tools`/`tool_choice`, excludes `response_format`
- [x] 7.4 Test `chat_with_tools_fallback()`: fallback chain behavior, vision-model filtering
- [x] 7.5 Test ReAct loop native dispatch path using `ScriptedLLM` from `execution_harness.py`: tool_calls → dispatch, text → parse_json in place, error → fallback
- [x] 7.6 Test special-case intercepts: `create_tool` → `_dispatch_create_tool()`, `plan` → plan execution, `vision_query` → `_exec_vision_query()`
- [x] 7.7 Test error handling: `NotImplementedError` fallback, transient `LLMError` retry+fallback, `LLMPermanentError` propagation, unexpected exception fallback

## 8. Verification

- [x] 8.1 Run `ruff check .` and fix any issues
- [x] 8.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and fix any issues
- [x] 8.3 Run `pytest tests/ -v --tb=short` and ensure all tests pass
- [x] 8.4 Run `openspec validate native-tool-calling --type change --strict`
