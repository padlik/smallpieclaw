# Explore Brief: Replace MCP Transport with Official SDK

## Alternatives Rejected

| Alternative | Why Rejected |
|---|---|
| **B: asyncio.run() per call** | Creates/destroys event loop per tool call. Can't reuse connections. Conflicts if called from within an existing event loop. |
| **C: Make everything async** | Massive refactor touching react_loop.py, agent_controller.py, agent_runtime.py, and all callers. The ReAct loop is deeply synchronous. Risk/reward ratio too high. |
| **Strangler fig / gradual migration** | The public API is identical. No need for dual-path complexity. The change is entirely internal to mcp_client.py. |

## Final Approach: Path A — Own Event Loop Thread

MCPManager runs a background daemon thread with its own `asyncio` event loop. All SDK operations (connect, list_tools, call_tool, close) run on that loop via `asyncio.run_coroutine_threadsafe().result()`. The public API (`has_tool`, `call_tool`, `get_tools`, `list_servers`, `set_enabled`, `connect_all`, `close_all`) is unchanged.

## Mapping Tables

### Config → SDK Transport Mapping

| Config field | Old code path | New code path |
|---|---|---|
| `transport: "stdio"` | `MCPStdioClient` → subprocess + manual JSON-RPC | `Client(stdio_client(StdioServerParameters(command=cfg.command[0], args=cfg.command[1:], env=cfg.env)))` |
| `transport: "http"` | `MCPHttpClient` → `requests.post()` + custom SSE | `Client(cfg.url)` — URL string directly, no wrapper needed |
| `timeout` | Passed to `select.select()` / `requests.Timeout` | SDK handles internally via httpx timeouts |
| `env` | Merged into subprocess env | Passed to `StdioServerParameters(env=...)` |
| `headers` | Added to `requests.Session.headers` | For HTTP: configure via `httpx.AsyncClient` passed to `streamable_http_client()` |
| `enabled` | Skip in `connect_all()` | Same logic, unchanged |

### SDK Result → Legacy Dict Mapping

| SDK Type | Legacy dict field |
|---|---|
| `CallToolResult.is_error == False` | `{"success": True, "output": <flattened content>, "error": ""}` |
| `CallToolResult.is_error == True` | `{"success": False, "output": "", "error": <flattened content>}` |
| `TextContent` | Append `.text` to output string |
| `ImageContent` | Append `[image: {mime_type}]` |
| `EmbeddedResource` | Append `[resource: {uri}]` |
| `AudioContent` | Append `[audio: {mime_type}]` |
| `ResourceLink` | Append `[resource_link: {uri}]` |
| SDK exception (`MCPError`, etc.) | `{"success": False, "error": str(e)}` |

### SDK Tool → Our Tool Dataclass Mapping

| SDK `Tool` field | Our `Tool` field |
|---|---|
| `t.name` | `name` |
| `t.description` | `description` (fallback: `f"MCP tool '{name}' from {server_name}"`) |
| `t.input_schema` | `input_schema` (both are `dict[str, Any]`, identical format) |
| — | `path=""` (always empty for MCP) |
| — | `language="mcp"` (always) |
| — | `is_mcp=True` (always) |
| — | `server_name=<server name>` |

### Exception Mapping

| SDK raises | MCPManager returns |
|---|---|
| `MCPError` (tool call fails) | `{"success": False, "error": str(e)}` |
| `MCPError` (connection drops) | `{"success": False, "error": str(e)}` |
| `RuntimeError` (protocol mismatch) | Logged, server marked "error" in `list_servers()` |
| Any other exception | `{"success": False, "error": str(e)}` |

## Cross-Module Data Flows

```
config.toml → config_schema.py (expand_env, parse_mcp_server) → MCPServerConfig
  → main.py (MCPManager(cfgs), connect_all(), get_tools(), register_mcp_tools())
    → MCPManager (event loop thread, _SdkClientWrapper per server)
      → mcp.client.Client (SDK)
        → stdio_client() or URL string

At runtime:
  react_loop.py → ctx.mcp_manager.has_tool(name) → MCPManager.has_tool()
  react_loop.py → ctx.mcp_manager.call_tool(name, args) → MCPManager.call_tool()
    → _run_async(wrapper._call_tool_async(name, args))
      → client.call_tool(name, args) → CallToolResult
        → _sdk_result_to_outcome() → {"success": bool, "output": str, "error": str}

Telegram:
  telegram_commands.py → /mcp list|on|off|info → MCPManager.list_servers()/set_enabled()/get_server_info()
```

## Files Changed

| File | Change |
|---|---|
| `mcp_client.py` | Rewrite (~750→~250 lines). Remove: MCPBaseClient, MCPStdioClient, MCPHttpClient, all transport internals. Add: _SdkClientWrapper, _sdk_result_to_outcome, event loop thread. Keep: _tool_outcome, MCPManager public API. |
| `tests/test_mcp_client.py` | Rewrite. Mock `mcp.client.Client` instead of `requests.Session`. |
| `requirements.txt` | Add `mcp>=1.27.0`. |

## Files NOT Changed

`interfaces.py`, `config_schema.py`, `config.toml`, `main.py`, `react_loop.py`, `telegram_commands.py`, `builtin_tools/schemas.py`, `exceptions.py` — all zero changes.

## Open Questions

1. **Stdio subprocess auto-restart**: Today MCPStdioClient auto-restarts on subprocess death. Does the SDK handle this, or do we need a thin retry wrapper in _SdkClientWrapper? → Verified: SDK's Client handles transport-level reconnection. If call_tool() raises on dead connection, we catch and return error dict. No auto-restart needed — the SDK manages the transport lifecycle.

2. **HTTP headers for auth**: Today headers are set on `requests.Session`. With SDK, HTTP servers use URL strings directly. For custom headers, we'd need `streamable_http_client(url, http_client=httpx.AsyncClient(headers=...))` instead of a bare URL. → Decision: If `cfg.headers` is non-empty for an HTTP transport, use `streamable_http_client()` with a configured `httpx.AsyncClient`. Otherwise, pass the URL string directly to `Client()`.

3. **`requests` dependency**: Only used in `mcp_client.py`. After migration, can we remove it from requirements.txt? → Verified: `requests` is only imported in `mcp_client.py` (confirmed by grep across all `.py` files). Safe to remove from `requirements.txt` after migration.

4. **SDK version stability**: v1.27.0 is stable (not beta). API is unlikely to change in breaking ways. Pin `mcp>=1.27.0,<2.0` for safety.
