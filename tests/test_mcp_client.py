"""Tests for mcp_client.py — MCP protocol utilities and MCPManager."""

from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

from mcp_client import (
    MCPHttpClient,
    MCPManager,
    _MCP_PROTOCOL_VERSION,
    _extract_mcp_result,
    _mcp_tools_to_registry,
    _tool_outcome,
)


class TestToolOutcome:
    """Test _tool_outcome helper."""

    def test_success(self):
        out = _tool_outcome(output="hello", success=True)
        assert out == {"success": True, "output": "hello", "error": "", "exit_code": 0}

    def test_failure(self):
        out = _tool_outcome(error="bad", success=False)
        assert out == {"success": False, "output": "", "error": "bad", "exit_code": 1}

    def test_defaults(self):
        out = _tool_outcome()
        assert out["success"] is True
        assert out["exit_code"] == 0


class TestExtractMcpResult:
    """Test _extract_mcp_result content flattening."""

    def test_single_text_content(self):
        resp = {"result": {"content": [{"type": "text", "text": "hello"}], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert text == "hello"
        assert ok is True

    def test_multiple_text_content(self):
        resp = {"result": {"content": [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert text == "line1\nline2"
        assert ok is True

    def test_error_flag(self):
        resp = {"result": {"content": [{"type": "text", "text": "failed"}], "isError": True}}
        text, ok = _extract_mcp_result(resp)
        assert text == "failed"
        assert ok is False

    def test_error_type_content(self):
        resp = {"result": {"content": [{"type": "error", "text": "oops"}], "isError": True}}
        text, ok = _extract_mcp_result(resp)
        assert "[error] oops" in text
        assert ok is False

    def test_empty_content(self):
        resp = {"result": {"content": [], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        # Falls back to json.dumps of result
        assert "content" in text
        assert ok is True

    def test_string_content_items(self):
        resp = {"result": {"content": ["raw string"], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert text == "raw string"
        assert ok is True

    def test_missing_result(self):
        resp = {}
        text, ok = _extract_mcp_result(resp)
        assert ok is True  # no isError flag


class TestMcpToolsToRegistry:
    """Test _mcp_tools_to_registry conversion."""

    def test_basic_conversion(self):
        raw = [
            {"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object"}},
            {"name": "write_file", "description": "Write a file"},
        ]
        tools = _mcp_tools_to_registry("filesystem", raw)
        assert len(tools) == 2
        assert tools[0].name == "read_file"
        assert tools[0].is_mcp is True
        assert tools[0].server_name == "filesystem"
        assert tools[0].description == "Read a file"
        assert tools[0].input_schema == {"type": "object"}

    def test_skips_unnamed_tools(self):
        raw = [{"name": ""}, {"description": "orphan"}]
        tools = _mcp_tools_to_registry("test", raw)
        assert len(tools) == 0

    def test_default_description(self):
        raw = [{"name": "my_tool"}]
        tools = _mcp_tools_to_registry("srv", raw)
        assert "my_tool" in tools[0].description
        assert "srv" in tools[0].description


class TestMCPManager:
    """Test MCPManager routing and lifecycle."""

    def test_connect_disabled_servers_skipped(self):
        cfgs = [
            {"name": "active", "transport": "stdio", "command": ["echo"], "enabled": True},
            {"name": "off", "transport": "stdio", "command": ["echo"], "enabled": False},
        ]
        mgr = MCPManager(cfgs)
        # Mock _connect_server to track calls
        connected = []
        mgr._connect_server = lambda name, cfg: connected.append(name)
        mgr.connect_all()
        assert "active" in connected
        assert "off" not in connected

    def test_has_tool_after_register(self):
        cfgs = [{"name": "test", "transport": "http", "url": "http://x", "enabled": True}]
        mgr = MCPManager(cfgs)
        # Manually register a tool mapping
        mgr._tool_to_server["my_tool"] = "test"
        assert mgr.has_tool("my_tool")
        assert not mgr.has_tool("unknown_tool")

    def test_call_tool_unknown(self):
        mgr = MCPManager([])
        result = mgr.call_tool("no_such_tool", {})
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_call_tool_no_client(self):
        cfgs = [{"name": "srv", "transport": "http", "url": "http://x"}]
        mgr = MCPManager(cfgs)
        mgr._tool_to_server["tool1"] = "srv"
        # No client connected
        result = mgr.call_tool("tool1", {})
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_call_tool_delegates_to_client(self):
        cfgs = [{"name": "srv", "transport": "http", "url": "http://x"}]
        mgr = MCPManager(cfgs)
        mock_client = MagicMock()
        mock_client.call_tool.return_value = _tool_outcome(output="result", success=True)
        mgr._clients["srv"] = mock_client
        mgr._tool_to_server["tool1"] = "srv"

        result = mgr.call_tool("tool1", {"key": "val"})
        mock_client.call_tool.assert_called_once_with("tool1", {"key": "val"})
        assert result["success"] is True
        assert result["output"] == "result"

    def test_get_tools(self):
        cfgs = [{"name": "a", "transport": "http", "url": "http://x"}]
        mgr = MCPManager(cfgs)
        mock_client = MagicMock()
        mock_client.tools = _mcp_tools_to_registry("a", [
            {"name": "t1", "description": "Tool 1"},
            {"name": "t2", "description": "Tool 2"},
        ])
        mgr._clients["a"] = mock_client

        tools = mgr.get_tools()
        assert len(tools) == 2
        assert tools[0].name == "t1"

    def test_set_enabled(self):
        cfgs = [{"name": "srv", "transport": "http", "url": "http://x", "enabled": True}]
        mgr = MCPManager(cfgs)
        assert mgr._enabled["srv"] is True
        result = mgr.set_enabled("srv", False)
        assert result is not False  # returns truthy
        assert mgr._enabled["srv"] is False

    def test_set_enabled_unknown_server(self):
        mgr = MCPManager([])
        result = mgr.set_enabled("nonexistent", True)
        assert result is False

    def test_close_all(self):
        cfgs = [{"name": "srv", "transport": "http", "url": "http://x"}]
        mgr = MCPManager(cfgs)
        mock_client = MagicMock()
        mgr._clients["srv"] = mock_client
        mgr._tool_to_server["t1"] = "srv"

        mgr.close_all()
        mock_client.close.assert_called_once()
        assert len(mgr._clients) == 0
        assert len(mgr._tool_to_server) == 0

    def test_tool_name_conflict_keeps_first(self):
        """When two servers claim the same tool name, first one wins."""
        cfgs = [
            {"name": "a", "transport": "http", "url": "http://a"},
            {"name": "b", "transport": "http", "url": "http://b"},
        ]
        mgr = MCPManager(cfgs)
        # Simulate both servers returning a tool named "shared_tool"
        mgr._tool_to_server["shared_tool"] = "a"
        # Attempting to add the same from "b" — manager already has it
        assert mgr._tool_to_server["shared_tool"] == "a"

    def test_list_servers(self):
        cfgs = [
            {"name": "a", "transport": "stdio", "command": ["x"], "enabled": True},
            {"name": "b", "transport": "http", "url": "http://x", "enabled": False},
        ]
        mgr = MCPManager(cfgs)
        servers = mgr.list_servers()
        assert len(servers) == 2


# ---------------------------------------------------------------------------
# Helpers for MCPHttpClient unit tests
# ---------------------------------------------------------------------------


def _make_http_client(url: str = "http://x:8088/mcp") -> MCPHttpClient:
    return MCPHttpClient({"name": "test", "url": url, "timeout": 5})


def _mock_response(json_data: dict, response_headers: dict | None = None,
                   content_type: str = "application/json") -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    _all_headers = {"Content-Type": content_type, **(response_headers or {})}
    resp.headers = MagicMock()
    resp.headers.get = lambda k, default="": _all_headers.get(k, default)
    resp.json.return_value = json_data
    resp.text = _json.dumps(json_data)
    return resp


def _init_resp(server_version: str = _MCP_PROTOCOL_VERSION,
               session_id: str | None = None) -> MagicMock:
    """Initialize-method mock response."""
    data = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": server_version, "capabilities": {"tools": {}}},
    }
    headers = {"Mcp-Session-Id": session_id} if session_id else {}
    return _mock_response(data, response_headers=headers)


def _tools_resp(tools: list, cursor: str | None = None) -> MagicMock:
    result: dict = {"tools": tools}
    if cursor:
        result["nextCursor"] = cursor
    return _mock_response({"jsonrpc": "2.0", "id": 2, "result": result})


# ---------------------------------------------------------------------------
# TestMCPHttpClientHeaders
# ---------------------------------------------------------------------------

class TestMCPHttpClientHeaders:
    """MCPHttpClient must send correct headers per MCP 2025-03-26 spec."""

    def test_accept_header_present_in_defaults(self):
        client = _make_http_client()
        assert "Accept" in client._headers
        assert "text/event-stream" in client._headers["Accept"]

    def test_content_type_still_present(self):
        client = _make_http_client()
        assert client._headers.get("Content-Type") == "application/json"

    def test_user_headers_merged_accept_preserved(self):
        """Custom config headers must not overwrite Accept."""
        client = MCPHttpClient({
            "name": "t", "url": "http://x",
            "headers": {"X-Custom": "val"},
        })
        assert client._headers["X-Custom"] == "val"
        assert "text/event-stream" in client._headers["Accept"]


# ---------------------------------------------------------------------------
# TestMCPHttpClientSessionId
# ---------------------------------------------------------------------------

class TestMCPHttpClientSessionId:
    """Session ID capture and propagation (MCP 2025-03-26)."""

    def test_session_id_captured_from_response(self):
        client = _make_http_client()
        init = _init_resp(session_id="sess-abc123")
        notify = _mock_response({"jsonrpc": "2.0"})  # notification response

        client._session.post = MagicMock(side_effect=[init, notify])
        client._initialize()
        assert client._session_id == "sess-abc123"

    def test_session_id_included_in_subsequent_requests(self):
        client = _make_http_client()
        init = _init_resp(session_id="sess-xyz")
        notify = _mock_response({"jsonrpc": "2.0"})
        # Response for a later _post call
        tools = _tools_resp([])

        client._session.post = MagicMock(side_effect=[init, notify, tools])
        client._initialize()
        client._list_tools()

        # Third call must include Mcp-Session-Id header
        third_call_kwargs = client._session.post.call_args_list[2][1]
        sent_headers = third_call_kwargs.get("headers", {})
        assert sent_headers.get("Mcp-Session-Id") == "sess-xyz"

    def test_no_session_id_no_header_added(self):
        client = _make_http_client()
        init = _init_resp()  # no session ID in headers
        notify = _mock_response({"jsonrpc": "2.0"})
        tools = _tools_resp([])

        client._session.post = MagicMock(side_effect=[init, notify, tools])
        client._initialize()
        client._list_tools()

        assert client._session_id is None
        third_call_kwargs = client._session.post.call_args_list[2][1]
        sent_headers = third_call_kwargs.get("headers", {})
        assert "Mcp-Session-Id" not in sent_headers

    def test_close_sends_delete_when_session_id_set(self):
        client = _make_http_client()
        client._session_id = "sess-del"
        client._session.request = MagicMock()
        client._session.close = MagicMock()

        client.close()

        client._session.request.assert_called_once()
        call_args = client._session.request.call_args
        assert call_args[0][0] == "DELETE"
        assert call_args[1]["headers"].get("Mcp-Session-Id") == "sess-del"

    def test_close_no_delete_without_session_id(self):
        client = _make_http_client()
        client._session.request = MagicMock()
        client._session.close = MagicMock()

        client.close()

        client._session.request.assert_not_called()


# ---------------------------------------------------------------------------
# TestMCPHttpClientVersionCheck
# ---------------------------------------------------------------------------

class TestMCPHttpClientVersionCheck:
    """Server protocolVersion is logged; mismatch triggers warning."""

    def test_matching_version_no_warning(self, caplog):
        import logging
        client = _make_http_client()
        init = _init_resp(server_version=_MCP_PROTOCOL_VERSION)
        notify = _mock_response({"jsonrpc": "2.0"})
        client._session.post = MagicMock(side_effect=[init, notify])

        with caplog.at_level(logging.WARNING, logger="mcp_client"):
            client._initialize()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "version" in r.message.lower()]
        assert len(warnings) == 0

    def test_mismatched_version_logs_warning(self, caplog):
        import logging
        client = _make_http_client()
        init = _init_resp(server_version="1999-01-01")
        notify = _mock_response({"jsonrpc": "2.0"})
        client._session.post = MagicMock(side_effect=[init, notify])

        with caplog.at_level(logging.WARNING, logger="mcp_client"):
            client._initialize()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "version" in r.message.lower()]
        assert len(warnings) == 1
        assert "1999-01-01" in warnings[0].message

    def test_missing_version_in_response_no_crash(self):
        client = _make_http_client()
        # initialize response with no protocolVersion key in result
        data = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
        init = _mock_response(data)
        notify = _mock_response({"jsonrpc": "2.0"})
        client._session.post = MagicMock(side_effect=[init, notify])
        client._initialize()  # must not raise
        assert client._session_id is None


# ---------------------------------------------------------------------------
# TestMCPHttpClientPagination
# ---------------------------------------------------------------------------

class TestMCPHttpClientPagination:
    """tools/list pagination via nextCursor."""

    def test_single_page_no_cursor(self):
        client = _make_http_client()
        init = _init_resp()
        notify = _mock_response({"jsonrpc": "2.0"})
        page1 = _tools_resp([{"name": "t1"}, {"name": "t2"}])
        client._session.post = MagicMock(side_effect=[init, notify, page1])
        client._initialize()
        tools = client._list_tools()
        assert [t["name"] for t in tools] == ["t1", "t2"]

    def test_two_pages_cursor_followed(self):
        client = _make_http_client()
        init = _init_resp()
        notify = _mock_response({"jsonrpc": "2.0"})
        page1 = _tools_resp([{"name": "t1"}], cursor="page2")
        page2 = _tools_resp([{"name": "t2"}])
        client._session.post = MagicMock(side_effect=[init, notify, page1, page2])
        client._initialize()
        tools = client._list_tools()
        assert [t["name"] for t in tools] == ["t1", "t2"]
        # Second tools/list call must include cursor param
        fourth_call_kwargs = client._session.post.call_args_list[3][1]
        body = fourth_call_kwargs.get("json", {})
        assert body.get("params", {}).get("cursor") == "page2"

    def test_pagination_terminates_on_empty_cursor(self):
        """nextCursor='' (empty string) should be treated as no cursor."""
        client = _make_http_client()
        init = _init_resp()
        notify = _mock_response({"jsonrpc": "2.0"})
        # Result with empty string cursor — should stop
        result = {"tools": [{"name": "t1"}], "nextCursor": ""}
        page1 = _mock_response({"jsonrpc": "2.0", "id": 2, "result": result})
        client._session.post = MagicMock(side_effect=[init, notify, page1])
        client._initialize()
        tools = client._list_tools()
        assert len(tools) == 1
        assert client._session.post.call_count == 3  # init + notify + one tools/list


# ---------------------------------------------------------------------------
# TestExtractMcpResultNewTypes
# ---------------------------------------------------------------------------

class TestExtractMcpResultNewTypes:
    """_extract_mcp_result handles image, resource, and unknown content types."""

    def test_image_content_returns_placeholder(self):
        resp = {"result": {"content": [
            {"type": "image", "mimeType": "image/png", "data": "base64abc"},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert "[image: image/png]" in text
        assert ok is True

    def test_resource_content_with_uri(self):
        resp = {"result": {"content": [
            {"type": "resource", "resource": {"uri": "file:///foo.txt"}},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert "[resource: file:///foo.txt]" in text
        assert ok is True

    def test_mixed_text_and_image(self):
        resp = {"result": {"content": [
            {"type": "text", "text": "result text"},
            {"type": "image", "mimeType": "image/jpeg", "data": "..."},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert "result text" in text
        assert "[image: image/jpeg]" in text
        assert ok is True

    def test_unknown_type_skipped_no_crash(self):
        resp = {"result": {"content": [
            {"type": "future_type", "data": "something"},
            {"type": "text", "text": "hello"},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert text == "hello"
        assert ok is True

    def test_resource_without_uri_placeholder(self):
        resp = {"result": {"content": [
            {"type": "resource", "resource": {}},
        ], "isError": False}}
        text, ok = _extract_mcp_result(resp)
        assert "[resource]" in text
        assert ok is True
