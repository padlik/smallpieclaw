"""Tests for mcp_client.py — SDK-based MCP transport."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch


from mcp_client import (
    MCPManager,
    _SdkClientWrapper,
    _sdk_result_to_outcome,
    _sdk_tools_to_registry,
    _tool_outcome,
)


# ---------------------------------------------------------------------------
# TestToolOutcome
# ---------------------------------------------------------------------------


class TestToolOutcome:
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


# ---------------------------------------------------------------------------
# TestSdkResultToOutcome
# ---------------------------------------------------------------------------


def _text_item(text: str) -> MagicMock:
    item = MagicMock()
    item.type = "text"
    item.text = text
    return item


def _image_item(mime: str) -> MagicMock:
    item = MagicMock()
    item.type = "image"
    item.mimeType = mime
    return item


def _resource_item(uri: str) -> MagicMock:
    item = MagicMock()
    item.type = "resource"
    item.resource = MagicMock()
    item.resource.uri = uri
    return item


def _audio_item(mime: str) -> MagicMock:
    item = MagicMock()
    item.type = "audio"
    item.mimeType = mime
    return item


def _resource_link_item(uri: str) -> MagicMock:
    item = MagicMock()
    item.type = "resource_link"
    item.uri = uri
    return item


def _make_result(content: list, is_error: bool = False) -> MagicMock:
    result = MagicMock()
    result.content = content
    result.isError = is_error
    return result


class TestSdkResultToOutcome:
    def test_text_success(self):
        out = _sdk_result_to_outcome(_make_result([_text_item("hello")]))
        assert out == {"success": True, "output": "hello", "error": "", "exit_code": 0}

    def test_is_error_true(self):
        out = _sdk_result_to_outcome(_make_result([_text_item("failed")], is_error=True))
        assert out["success"] is False
        assert out["error"] == "failed"
        assert out["exit_code"] == 1

    def test_image_content(self):
        out = _sdk_result_to_outcome(_make_result([_image_item("image/png")]))
        assert "[image: image/png]" in out["output"]
        assert out["success"] is True

    def test_resource_content(self):
        out = _sdk_result_to_outcome(_make_result([_resource_item("file:///foo.txt")]))
        assert "[resource: file:///foo.txt]" in out["output"]

    def test_audio_content(self):
        out = _sdk_result_to_outcome(_make_result([_audio_item("audio/mp3")]))
        assert "[audio: audio/mp3]" in out["output"]

    def test_resource_link_content(self):
        out = _sdk_result_to_outcome(_make_result([_resource_link_item("https://example.com")]))
        assert "[resource_link: https://example.com]" in out["output"]

    def test_mixed_content(self):
        out = _sdk_result_to_outcome(_make_result([
            _text_item("result text"),
            _image_item("image/jpeg"),
            _resource_item("file:///bar.txt"),
        ]))
        assert "result text" in out["output"]
        assert "[image: image/jpeg]" in out["output"]
        assert "[resource: file:///bar.txt]" in out["output"]
        assert out["success"] is True

    def test_empty_content(self):
        out = _sdk_result_to_outcome(_make_result([]))
        assert out["success"] is True
        assert out["output"] == ""

    def test_unknown_type_skipped(self):
        unknown = MagicMock()
        unknown.type = "future_type"
        out = _sdk_result_to_outcome(_make_result([unknown, _text_item("hello")]))
        assert out["output"] == "hello"


# ---------------------------------------------------------------------------
# TestSdkToolsToRegistry
# ---------------------------------------------------------------------------


def _sdk_tool(name: str, description: str = "", input_schema: dict | None = None) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = input_schema or {}
    return t


class TestSdkToolsToRegistry:
    def test_basic_conversion(self):
        tools = _sdk_tools_to_registry("srv", [_sdk_tool("read_file", "Read a file", {"type": "object"})])
        assert len(tools) == 1
        assert tools[0].name == "read_file"
        assert tools[0].is_mcp is True
        assert tools[0].server_name == "srv"
        assert tools[0].description == "Read a file"
        assert tools[0].input_schema == {"type": "object"}

    def test_empty_name_skipped(self):
        tools = _sdk_tools_to_registry("srv", [_sdk_tool(""), _sdk_tool("valid")])
        assert len(tools) == 1
        assert tools[0].name == "valid"

    def test_missing_description_fallback(self):
        t = MagicMock()
        t.name = "my_tool"
        t.description = None
        t.inputSchema = {}
        tools = _sdk_tools_to_registry("myserver", [t])
        assert "my_tool" in tools[0].description
        assert "myserver" in tools[0].description

    def test_mcp_language_set(self):
        tools = _sdk_tools_to_registry("srv", [_sdk_tool("t")])
        assert tools[0].language == "mcp"
        assert tools[0].path == ""


# ---------------------------------------------------------------------------
# Helpers for _SdkClientWrapper tests
# ---------------------------------------------------------------------------


def _start_test_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_test_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def _make_mock_session(tools: list | None = None, next_cursor: str | None = None) -> MagicMock:
    """Build a mock ClientSession with optional tool list."""
    session = MagicMock()
    session.initialize = AsyncMock()
    page = MagicMock()
    page.tools = tools or []
    page.nextCursor = next_cursor
    session.list_tools = AsyncMock(return_value=page)
    return session


def _make_stdio_patches(session: MagicMock) -> tuple:
    """Return (mock_stdio_cm, mock_session_cm) context manager mocks."""
    read, write = AsyncMock(), AsyncMock()

    stdio_cm = MagicMock()
    stdio_cm.__aenter__ = AsyncMock(return_value=(read, write))
    stdio_cm.__aexit__ = AsyncMock(return_value=None)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    return stdio_cm, session_cm


# ---------------------------------------------------------------------------
# TestSdkClientWrapper
# ---------------------------------------------------------------------------


class TestSdkClientWrapper:
    def setup_method(self):
        self.loop, self.thread = _start_test_loop()

    def teardown_method(self):
        _stop_test_loop(self.loop, self.thread)

    def _make_wrapper(self, cfg: dict | None = None) -> _SdkClientWrapper:
        if cfg is None:
            cfg = {"name": "test", "command": ["echo", "hello"], "transport": "stdio", "timeout": 5}
        return _SdkClientWrapper(cfg, self.loop)

    def test_connect_stdio_success(self):
        sdk_tool = _sdk_tool("tool1", "Tool 1")
        session = _make_mock_session(tools=[sdk_tool])
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is True
        assert len(tools) == 1
        assert tools[0].name == "tool1"

    def test_connect_stdio_env_merge(self):
        """os.environ keys must be passed to StdioServerParameters."""
        captured: list = []

        session = _make_mock_session()
        stdio_cm, session_cm = _make_stdio_patches(session)

        def _capture_params(params):
            captured.append(params)
            return stdio_cm

        with patch("mcp_client.stdio_client", side_effect=_capture_params):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                cfg = {
                    "name": "test",
                    "command": ["echo"],
                    "transport": "stdio",
                    "timeout": 5,
                    "env": {"MY_VAR": "1"},
                }
                wrapper = _SdkClientWrapper(cfg, self.loop)
                wrapper.connect()

        assert len(captured) == 1
        env = captured[0].env
        assert "MY_VAR" in env
        # PATH should be inherited from os.environ
        assert "PATH" in env

    def test_connect_failure(self):
        stdio_cm = MagicMock()
        stdio_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("subprocess failed"))
        stdio_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            wrapper = self._make_wrapper()
            tools = wrapper.connect()

        assert wrapper.connected is False
        assert "subprocess failed" in wrapper.last_error
        assert tools == []

    def test_connect_timeout(self):
        """If session runner never sets ready, connect returns empty after timeout."""
        # Never-completing stdio client
        async def _never_return(params):  # noqa: ARG001
            await asyncio.sleep(999)
            yield MagicMock(), MagicMock()

        stdio_cm = MagicMock()
        stdio_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        stdio_cm.__aexit__ = AsyncMock(return_value=None)

        cfg = {"name": "test", "command": ["x"], "transport": "stdio", "timeout": 1}
        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            wrapper = _SdkClientWrapper(cfg, self.loop)
            tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []

    def test_call_tool_success(self):
        call_result = MagicMock()
        call_result.isError = False
        call_result.content = [_text_item("ok output")]

        session = _make_mock_session()
        session.call_tool = AsyncMock(return_value=call_result)
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                wrapper.connect()
                result = wrapper.call_tool("tool1", {"key": "val"})

        assert result["success"] is True
        assert result["output"] == "ok output"

    def test_call_tool_error_result(self):
        call_result = MagicMock()
        call_result.isError = True
        call_result.content = [_text_item("server error")]

        session = _make_mock_session()
        session.call_tool = AsyncMock(return_value=call_result)
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                wrapper.connect()
                result = wrapper.call_tool("tool1", {})

        assert result["success"] is False
        assert result["error"] == "server error"

    def test_call_tool_exception_returns_error_dict(self):
        session = _make_mock_session()
        session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                wrapper.connect()
                result = wrapper.call_tool("tool1", {})

        assert result["success"] is False
        assert "connection lost" in result["error"]

    def test_call_tool_not_connected_returns_error(self):
        wrapper = self._make_wrapper()
        # Don't connect — wrapper.connected is False
        result = wrapper.call_tool("tool1", {})
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_paginated_list_tools(self):
        """list_tools with nextCursor must collect all pages."""
        tool_a = _sdk_tool("tool_a")
        tool_b = _sdk_tool("tool_b")

        page1 = MagicMock()
        page1.tools = [tool_a]
        page1.nextCursor = "cursor1"

        page2 = MagicMock()
        page2.tools = [tool_b]
        page2.nextCursor = None

        session = MagicMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(side_effect=[page1, page2])
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert len(tools) == 2
        assert {t.name for t in tools} == {"tool_a", "tool_b"}

    def test_close_disconnects(self):
        session = _make_mock_session()
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                wrapper.connect()
                assert wrapper.connected is True
                wrapper.close()

        assert wrapper.connected is False


# ---------------------------------------------------------------------------
# TestMCPManager
# ---------------------------------------------------------------------------


class TestMCPManager:
    def test_connect_disabled_servers_skipped(self):
        cfgs = [
            {"name": "active", "transport": "stdio", "command": ["echo"], "enabled": True},
            {"name": "off", "transport": "stdio", "command": ["echo"], "enabled": False},
        ]
        mgr = MCPManager(cfgs)
        connected: list = []
        mgr._connect_server = lambda name, cfg: connected.append(name)  # type: ignore[method-assign]
        mgr.connect_all()
        assert "active" in connected
        assert "off" not in connected

    def test_has_tool_after_register(self):
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        mgr._tool_to_server["my_tool"] = "srv"
        assert mgr.has_tool("my_tool")
        assert not mgr.has_tool("unknown_tool")

    def test_call_tool_unknown(self):
        mgr = MCPManager([])
        result = mgr.call_tool("no_such_tool", {})
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_call_tool_no_wrapper(self):
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        mgr._tool_to_server["tool1"] = "srv"
        # No wrapper in _wrappers
        result = mgr.call_tool("tool1", {})
        assert result["success"] is False
        assert "not connected" in result["error"]

    def test_call_tool_delegates_to_wrapper(self):
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        mock_wrapper = MagicMock()
        mock_wrapper.call_tool.return_value = _tool_outcome(output="result", success=True)
        mgr._wrappers["srv"] = mock_wrapper
        mgr._tool_to_server["tool1"] = "srv"

        result = mgr.call_tool("tool1", {"key": "val"})
        mock_wrapper.call_tool.assert_called_once_with("tool1", {"key": "val"})
        assert result["success"] is True
        assert result["output"] == "result"

    def test_get_tools(self):
        mgr = MCPManager([{"name": "a", "transport": "stdio", "command": ["x"]}])
        mock_wrapper = MagicMock()
        mock_wrapper.tools = _sdk_tools_to_registry("a", [_sdk_tool("t1"), _sdk_tool("t2")])
        mgr._wrappers["a"] = mock_wrapper

        tools = mgr.get_tools()
        assert len(tools) == 2
        assert tools[0].name == "t1"

    def test_set_enabled_false(self):
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"], "enabled": True}]
        mgr = MCPManager(cfgs)
        result = mgr.set_enabled("srv", False)
        assert result is True
        assert mgr._enabled["srv"] is False

    def test_set_enabled_unknown_server(self):
        mgr = MCPManager([])
        result = mgr.set_enabled("nonexistent", True)
        assert result is False

    def test_set_enabled_off_closes_wrapper_and_removes_tools(self):
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mock_wrapper = MagicMock()
        mgr._wrappers["srv"] = mock_wrapper
        mgr._tool_to_server["tool1"] = "srv"

        mgr.set_enabled("srv", False)

        mock_wrapper.close.assert_called_once()
        assert "tool1" not in mgr._tool_to_server
        assert "srv" not in mgr._wrappers

    def test_close_all(self):
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mock_wrapper = MagicMock()
        mgr._wrappers["srv"] = mock_wrapper
        mgr._tool_to_server["t1"] = "srv"

        mgr.close_all()

        mock_wrapper.close.assert_called_once()
        assert len(mgr._wrappers) == 0
        assert len(mgr._tool_to_server) == 0

    def test_tool_name_conflict_keeps_first(self):
        cfgs = [
            {"name": "a", "transport": "stdio", "command": ["x"]},
            {"name": "b", "transport": "stdio", "command": ["y"]},
        ]
        mgr = MCPManager(cfgs)
        mgr._start_loop()

        tool_a = _sdk_tool("shared_tool")
        tool_b = _sdk_tool("shared_tool")
        session_a = _make_mock_session(tools=[tool_a])
        session_b = _make_mock_session(tools=[tool_b])
        stdio_cm_a, session_cm_a = _make_stdio_patches(session_a)
        stdio_cm_b, session_cm_b = _make_stdio_patches(session_b)

        call_count = 0

        def _stdio_factory(params):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return stdio_cm_a if call_count == 1 else stdio_cm_b

        session_call_count = 0

        def _session_factory(r, w):  # noqa: ARG001
            nonlocal session_call_count
            session_call_count += 1
            return session_cm_a if session_call_count == 1 else session_cm_b

        with patch("mcp_client.stdio_client", side_effect=_stdio_factory):
            with patch("mcp_client.ClientSession", side_effect=_session_factory):
                mgr._connect_server("a", cfgs[0])
                mgr._connect_server("b", cfgs[1])

        # First server wins
        assert mgr._tool_to_server["shared_tool"] == "a"
        mgr.close_all()

    def test_list_servers_mixed_states(self):
        cfgs = [
            {"name": "active_srv", "transport": "stdio", "command": ["x"], "enabled": True},
            {"name": "off_srv", "transport": "http", "url": "http://x", "enabled": False},
            {"name": "error_srv", "transport": "stdio", "command": ["x"], "enabled": True},
        ]
        mgr = MCPManager(cfgs)
        active_wrapper = MagicMock()
        active_wrapper.connected = True
        active_wrapper.tools = []
        active_wrapper.last_error = ""
        mgr._wrappers["active_srv"] = active_wrapper

        error_wrapper = MagicMock()
        error_wrapper.connected = False
        error_wrapper.tools = []
        error_wrapper.last_error = "failed"
        mgr._wrappers["error_srv"] = error_wrapper

        servers = mgr.list_servers()
        by_name = {s["name"]: s for s in servers}
        assert by_name["active_srv"]["status"] == "active"
        assert by_name["off_srv"]["status"] == "off"
        assert by_name["error_srv"]["status"] == "error"

    def test_get_server_info_found(self):
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"], "timeout": 10}]
        mgr = MCPManager(cfgs)
        mock_wrapper = MagicMock()
        mock_wrapper.connected = True
        mock_wrapper.tools = []
        mock_wrapper.last_error = ""
        mgr._wrappers["srv"] = mock_wrapper

        info = mgr.get_server_info("srv")
        assert info is not None
        assert info["name"] == "srv"
        assert info["status"] == "active"
        assert info["timeout"] == 10

    def test_get_server_info_not_found(self):
        mgr = MCPManager([])
        assert mgr.get_server_info("unknown") is None
