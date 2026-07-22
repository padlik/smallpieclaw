## Why

`mcp_client.py` has 55 passing tests, but systematic coverage mapping found 6 gaps: one dead public
method, several untested error-propagation paths, two missing pagination guard tests, three minor
data-conversion edge cases, and an incomplete module docstring with no config schema. Left
unaddressed, these gaps allow future refactors to silently break correct behavior and leave
operators without a written contract for server configuration.

## What Changes

- **Remove** `MCPManager.last_error(name)` — dead method, never called; callers read `last_error`
  from the dicts returned by `get_server_info()` / `list_servers()` directly.
- **Add** tests confirming `_session_runner` error propagation: `initialize()` failure and
  `list_tools()` failure after initialize both resolve `_ready_future` promptly and return
  `connected=False` (regression guard, not bug fix).
- **Add** test for `_MAX_TOOL_PAGES` (50-page) limit — distinct from the `_MAX_TOOLS` (500-tool)
  limit already tested; the two guards interact and neither should silently shadow the other.
- **Add** test for unknown transport string — confirms the `ValueError` in `_session_runner` is
  surfaced correctly to the caller.
- **Add** tests for minor data-conversion edge cases: `resource` item with `None` resource field,
  description truncation at 2048 characters, transport label mapping (`"http"`/`"sse"` → `"web"`),
  `_start_loop()` idempotency, `get_server_info()` status for disabled/error wrapper states, and
  `set_enabled(True)` short-circuit when wrapper is already connected.
- **Update** `mcp_client.py` module docstring: add SSE as a third transport mode, list all public
  methods (`has_tool`, `set_enabled`, `list_servers`, `get_server_info`), add a `MCPManager` class
  docstring describing the threading model, and document the server config dict schema (keys,
  types, required vs optional, defaults) inline in the module header.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-transport`: Adding missing Gherkin scenarios to cover `_session_runner` error propagation
  (initialize failure, list_tools failure), `_MAX_TOOL_PAGES` exceeded, unknown transport,
  resource-item null field, description truncation, transport label mapping, and `_start_loop()`
  idempotency. No behavior changes — spec catches up to existing correct implementation.

## Impact

- `mcp_client.py` — remove `last_error()` method (~3 lines), update module docstring and
  `MCPManager` class docstring (~30 lines)
- `tests/test_mcp_client.py` — append ~10–12 new test methods (~100–120 lines)
- `openspec/specs/mcp-transport/spec.md` — add missing scenarios (~50 lines)
- No API changes visible to callers; no behavior changes; `vulture_whitelist.py` unaffected
