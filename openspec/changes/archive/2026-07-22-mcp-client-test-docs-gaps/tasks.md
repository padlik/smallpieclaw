## 1. Dead Code Removal

- [x] 1.1 Delete `MCPManager.last_error()` method from `mcp_client.py` (lines 510–512)

## 2. Module Documentation

- [x] 2.1 Update module-level docstring in `mcp_client.py`: add SSE as a third transport, update
      the public API list to include `has_tool`, `set_enabled`, `list_servers`, `get_server_info`
- [x] 2.2 Add `MCPManager` class docstring describing the threading model (single daemon event
      loop, per-wrapper session runner, queue-based tool-call serialization)
- [x] 2.3 Add server config dict schema to the module header as an inline comment block, listing
      all keys with types, required/optional status, and defaults

## 3. Tests: Session Startup Error Propagation

- [x] 3.1 Add `TestSdkClientWrapper.test_connect_initialize_failure`: mock `session.initialize`
      to raise; assert `wrapper.connected is False`, `tools == []`, and `wrapper.last_error`
      is non-empty
- [x] 3.2 Add `TestSdkClientWrapper.test_connect_list_tools_failure_after_init`: mock
      `session.initialize` to succeed and `session.list_tools` to raise; assert
      `wrapper.connected is False`, `tools == []`, and `wrapper.last_error` is non-empty

## 4. Tests: Pagination Guards

- [x] 4.1 Add `TestSdkClientWrapper.test_pagination_page_limit`: configure mock to return 51
      pages of 9 tools each (under `_MAX_TOOLS`); assert `wrapper.connected is False` and
      `tools == []`, confirming `_MAX_TOOL_PAGES` fires before `_MAX_TOOLS`

## 5. Tests: Unknown Transport

- [x] 5.1 Add `TestSdkClientWrapper.test_connect_unknown_transport`: configure cfg with
      `transport: "ws"`; assert `wrapper.connected is False` and `"ws"` appears in
      `wrapper.last_error`

## 6. Tests: Minor Edge Cases

- [x] 6.1 Add `TestSdkResultToOutcome.test_resource_none_field`: build a resource item where
      `item.resource is None`; assert output contains `"[resource]"`
- [x] 6.2 Add `TestSdkToolsToRegistry.test_description_truncated_at_max_len`: pass a description
      of 2049 characters; assert registered description length is exactly 2048
- [x] 6.3 Add `TestMCPManager.test_list_servers_transport_labels`: configure one stdio, one http,
      one sse server; assert their `transport` fields are `"stdio"`, `"web"`, `"web"`
      respectively
- [x] 6.4 Add `TestMCPManager.test_start_loop_idempotent`: call `_start_loop()`, poll until
      `mgr._loop.is_running()` is True, then call `_start_loop()` a second time; assert
      `_loop` and `_loop_thread` object identity is unchanged (same objects, no second thread)
- [x] 6.5 Add `TestMCPManager.test_get_server_info_disabled`: configure a server with
      `enabled: false`; assert `get_server_info()` returns `status == "off"`
- [x] 6.6 Add `TestMCPManager.test_get_server_info_error_state`: install a wrapper with
      `connected=False` and server `enabled=True`; assert `get_server_info()` returns
      `status == "error"`
- [x] 6.7 Add `TestMCPManager.test_set_enabled_true_already_connected`: install a wrapper
      with `connected=True`; call `set_enabled("srv", True)`; assert `_connect_server` is not
      called and return value is `True`

## 7. Verification

- [x] 7.1 Run `make check` — ruff, vulture, and pytest all pass with no new warnings
- [x] 7.2 Run `openspec validate mcp-client-test-docs-gaps --type change --strict` to confirm
      spec delta is well-formed
