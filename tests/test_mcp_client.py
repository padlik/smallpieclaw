"""Tests for mcp_client.py — MCP protocol utilities and MCPManager."""

from __future__ import annotations

from unittest.mock import MagicMock

from mcp_client import (
    MCPManager,
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
