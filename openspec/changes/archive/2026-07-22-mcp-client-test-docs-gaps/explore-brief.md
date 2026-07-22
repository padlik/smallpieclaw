# Explore Brief: mcp-client-test-docs-gaps

## Problem
`mcp_client.py` (513 lines, SDK-based MCP transport) has 6 identified gaps across tests and
documentation. All gaps were found by systematic coverage mapping against the 55-test suite in
`tests/test_mcp_client.py`.

## Rejected alternatives

- **"Just add missing tests, skip docs"** — rejected: the config schema gap is the highest
  operator-facing pain point; docs and tests should ship together.
- **"Add integration tests with a real MCP subprocess"** — rejected: all existing tests mock the
  SDK; keeping that pattern is a project convention. Real subprocess tests would be a separate
  concern.
- **"Fix dead code via vulture whitelist"** — rejected: the dead method (`MCPManager.last_error()`)
  should be deleted outright, not whitelisted. It creates naming confusion with the same-named
  attribute on `_SdkClientWrapper`.

## The 6 gaps (ordered by priority)

| # | Gap | Risk | Type |
|---|-----|------|------|
| 1 | `MCPManager.last_error()` — dead public method, never called | dead code / naming confusion | delete |
| 2 | `list_tools()` fails after `initialize()` succeeds → `_ready_future` never resolves → 30s hang | hang risk | test |
| 3 | `_MAX_TOOL_PAGES` (50) guard — distinct from `_MAX_TOOLS` (500), never tested | correctness | test |
| 4 | Unknown transport → `ValueError` in `_session_runner` — no test | error path | test |
| 5 | Module docstring: SSE transport missing, 5 public methods not listed, config schema nowhere documented | maintainability | docs |
| 6 | Minor edge cases: `resource=None→"[resource]"`, description truncation at 2048, transport label mapping ("web" vs "stdio"), `_start_loop()` idempotency | low risk | test |

## Key data flows

- `MCPManager.last_error(name)` (line 510–512) — confirmed dead: callers in
  `telegram_commands.py:761,776` read `last_error` from the dict returned by `get_server_info()`
  and `list_servers()`, not from this method. Safe to delete.
- `_session_runner` hang path: `session.initialize()` → `session.list_tools()` raises →
  `_ready_future.set_result()` never called → `wrapper.connect()` blocks until
  `concurrent.futures.TimeoutError` (default 30s).
- Transport label mapping: both `"http"` and `"sse"` map to `"web"` in `list_servers()` and
  `get_server_info()`; `"stdio"` maps to `"stdio"`. This is a display contract used in Telegram
  commands but never asserted in tests.

## Config schema (inferred from source, never documented)

```
{
  "name":      str       # required
  "transport": "stdio" | "http" | "sse"  # default: "stdio"
  "command":   list[str] # required for stdio
  "url":       str       # required for http/sse
  "env":       dict      # optional, stdio only
  "headers":   dict      # optional, http/sse
  "timeout":   int       # default: 30
  "enabled":   bool      # default: True
}
```

## Open questions at time of exploration

- None — all gaps were fully identified and confirmed against live grep of callers.
