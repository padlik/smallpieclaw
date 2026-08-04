"""Tests for mcp_client.py — SDK-based MCP transport."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch


import mcp_client
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
        out = _sdk_result_to_outcome(
            _make_result([_text_item("failed")], is_error=True)
        )
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
        out = _sdk_result_to_outcome(
            _make_result([_resource_link_item("https://example.com")])
        )
        assert "[resource_link: https://example.com]" in out["output"]

    def test_mixed_content(self):
        out = _sdk_result_to_outcome(
            _make_result(
                [
                    _text_item("result text"),
                    _image_item("image/jpeg"),
                    _resource_item("file:///bar.txt"),
                ]
            )
        )
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

    def test_resource_none_field(self):
        item = MagicMock()
        item.type = "resource"
        item.resource = None
        out = _sdk_result_to_outcome(_make_result([item]))
        assert "[resource]" in out["output"]
        assert out["success"] is True


# ---------------------------------------------------------------------------
# TestSdkToolsToRegistry
# ---------------------------------------------------------------------------


def _sdk_tool(
    name: str, description: str = "", input_schema: dict | None = None
) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = input_schema or {}
    return t


class TestSdkToolsToRegistry:
    def test_basic_conversion(self):
        tools = _sdk_tools_to_registry(
            "srv", [_sdk_tool("read_file", "Read a file", {"type": "object"})]
        )
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

    def test_invalid_tool_name_skipped(self):
        invalid = _sdk_tool("bad name!")  # space and ! are invalid
        valid = _sdk_tool("good_tool")
        tools = _sdk_tools_to_registry("srv", [invalid, valid])
        assert len(tools) == 1
        assert tools[0].name == "good_tool"

    def test_description_control_chars_stripped(self):
        t = _sdk_tool("my_tool", "Hello\x00\x01World\x7f")
        tools = _sdk_tools_to_registry("srv", [t])
        assert "\x00" not in tools[0].description
        assert "HelloWorld" in tools[0].description

    def test_description_truncated_at_max_len(self):
        long_desc = "a" * 2049
        tools = _sdk_tools_to_registry("srv", [_sdk_tool("my_tool", long_desc)])
        assert len(tools[0].description) == 2048


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


def _make_mock_session(
    tools: list | None = None, next_cursor: str | None = None
) -> MagicMock:
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
        self._wrappers: list[_SdkClientWrapper] = []

    def teardown_method(self):
        # Cancel and await each wrapper's session-runner task (if any) on the
        # loop before stopping it — otherwise asyncio warns "Task was
        # destroyed but it is pending!" whenever the garbage collector
        # happens to run during a later test.
        tasks = [w._task for w in self._wrappers if w._task is not None]
        if tasks and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    mcp_client._cancel_and_wait(tasks), self.loop
                ).result(timeout=2)
            except Exception:
                pass
        _stop_test_loop(self.loop, self.thread)

    def _make_wrapper(self, cfg: dict | None = None) -> _SdkClientWrapper:
        if cfg is None:
            cfg = {
                "name": "test",
                "command": ["echo", "hello"],
                "transport": "stdio",
                "timeout": 5,
            }
        wrapper = _SdkClientWrapper(cfg, self.loop)
        self._wrappers.append(wrapper)
        return wrapper

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
        """cfg['env'] keys must be passed; os.environ secrets must NOT be forwarded."""
        captured: list = []
        session = _make_mock_session()
        stdio_cm, session_cm = _make_stdio_patches(session)

        def _capture_params(params):
            captured.append(params)
            return stdio_cm

        fake_default_env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        with patch("mcp_client.get_default_environment", return_value=fake_default_env):
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
        assert "PATH" in env
        # Sensitive keys from os.environ must NOT appear (not in get_default_environment),
        # except TMPDIR/TMP/TEMP which are intentionally forwarded.
        import os

        _tmp_vars = {"TMPDIR", "TMP", "TEMP"}
        secret_keys = [
            k
            for k in os.environ
            if k not in fake_default_env and k != "MY_VAR" and k not in _tmp_vars
        ]
        for k in secret_keys[:5]:  # spot-check first 5
            assert k not in env

    def test_connect_stdio_tmpdir_forwarded(self):
        """TMPDIR/TMP/TEMP from os.environ are forwarded to stdio MCP subprocesses."""
        captured: list = []
        session = _make_mock_session()
        stdio_cm, session_cm = _make_stdio_patches(session)

        def _capture_params(params):
            captured.append(params)
            return stdio_cm

        import os

        fake_default_env = {"PATH": "/usr/bin"}
        with patch("mcp_client.get_default_environment", return_value=fake_default_env):
            with patch(
                "mcp_client.os.environ", {**os.environ, "TMPDIR": "/configured/tmp"}
            ):
                with patch("mcp_client.stdio_client", side_effect=_capture_params):
                    with patch("mcp_client.ClientSession", return_value=session_cm):
                        cfg = {
                            "name": "t",
                            "command": ["echo"],
                            "transport": "stdio",
                            "timeout": 5,
                        }
                        _SdkClientWrapper(cfg, self.loop).connect()

        assert len(captured) == 1
        assert captured[0].env.get("TMPDIR") == "/configured/tmp"

    def test_connect_stdio_tmpdir_cfg_env_wins(self):
        """Per-server cfg['env'] TMPDIR overrides the inherited os.environ value."""
        captured: list = []
        session = _make_mock_session()
        stdio_cm, session_cm = _make_stdio_patches(session)

        def _capture_params(params):
            captured.append(params)
            return stdio_cm

        import os

        fake_default_env = {"PATH": "/usr/bin"}
        with patch("mcp_client.get_default_environment", return_value=fake_default_env):
            with patch(
                "mcp_client.os.environ", {**os.environ, "TMPDIR": "/process/tmp"}
            ):
                with patch("mcp_client.stdio_client", side_effect=_capture_params):
                    with patch("mcp_client.ClientSession", return_value=session_cm):
                        cfg = {
                            "name": "t",
                            "command": ["echo"],
                            "transport": "stdio",
                            "timeout": 5,
                            "env": {"TMPDIR": "/server/tmp"},
                        }
                        _SdkClientWrapper(cfg, self.loop).connect()

        assert len(captured) == 1
        assert captured[0].env.get("TMPDIR") == "/server/tmp"

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

    def test_pagination_cursor_repeat_raises(self):
        """Repeated cursor must raise and resolve ready_future with error."""
        page = MagicMock()
        page.tools = []
        page.nextCursor = "same-cursor"

        session = MagicMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(return_value=page)
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []
        assert "cursor" in wrapper.last_error.lower() or wrapper.last_error != ""

    def test_pagination_tool_limit(self):
        """Exceeding _MAX_TOOLS must fail connect gracefully."""
        from mcp_client import _MAX_TOOLS

        many_tools = [_sdk_tool(f"t{i}") for i in range(_MAX_TOOLS + 1)]

        page1 = MagicMock()
        page1.tools = many_tools
        page1.nextCursor = "next"

        page2 = MagicMock()
        page2.tools = []
        page2.nextCursor = None

        session = MagicMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(side_effect=[page1, page2])
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []

    def test_pagination_tool_limit_single_page(self):
        """_MAX_TOOLS guard must fire even on a single page with no nextCursor."""
        from mcp_client import _MAX_TOOLS

        many_tools = [_sdk_tool(f"t{i}") for i in range(_MAX_TOOLS + 1)]

        page1 = MagicMock()
        page1.tools = many_tools
        page1.nextCursor = None  # single page — no cursor

        session = MagicMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(return_value=page1)
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []

    def test_call_tool_closed_loop_returns_error(self):
        """call_tool must return an error dict, not raise, when the loop is closed."""
        loop = asyncio.new_event_loop()
        cfg = {"name": "srv", "transport": "stdio", "command": ["x"], "timeout": 5}
        wrapper = _SdkClientWrapper(cfg, loop)
        wrapper.connected = True
        wrapper._queue = MagicMock()
        loop.close()  # closed loop causes call_soon_threadsafe to raise RuntimeError

        result = wrapper.call_tool("any_tool", {})
        assert result["success"] is False
        assert "closing" in result["error"].lower()

    def test_connect_http_transport(self):
        session = _make_mock_session(tools=[_sdk_tool("http_tool")])
        read, write = AsyncMock(), AsyncMock()
        http_cm = MagicMock()
        http_cm.__aenter__ = AsyncMock(return_value=(read, write, None))
        http_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        cfg = {
            "name": "http_srv",
            "transport": "http",
            "url": "http://localhost:8080",
            "timeout": 5,
        }
        with patch("mcp_client.streamablehttp_client", return_value=http_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = _SdkClientWrapper(cfg, self.loop)
                tools = wrapper.connect()

        assert wrapper.connected is True
        assert len(tools) == 1
        assert tools[0].name == "http_tool"

    def test_connect_sse_transport(self):
        session = _make_mock_session(tools=[_sdk_tool("sse_tool")])
        read, write = AsyncMock(), AsyncMock()
        sse_cm = MagicMock()
        sse_cm.__aenter__ = AsyncMock(return_value=(read, write))
        sse_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        cfg = {
            "name": "sse_srv",
            "transport": "sse",
            "url": "http://localhost:9090/sse",
            "timeout": 5,
        }
        with patch("mcp_client.sse_client", return_value=sse_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = _SdkClientWrapper(cfg, self.loop)
                tools = wrapper.connect()

        assert wrapper.connected is True
        assert len(tools) == 1
        assert tools[0].name == "sse_tool"

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

    def test_close_drains_queued_requests(self):
        """Requests queued but not yet processed must resolve on close."""
        import concurrent.futures as _cf
        from mcp_client import _ToolRequest

        session = _make_mock_session()
        block_event = asyncio.Event()

        async def _blocking_call(name, args):  # noqa: ARG001
            await block_event.wait()
            return MagicMock(isError=False, content=[])

        session.call_tool = _blocking_call
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                wrapper.connect()

                # Inject a request directly into the queue
                pending_future = _cf.Future()
                req = _ToolRequest(name="tool2", args={}, future=pending_future)
                self.loop.call_soon_threadsafe(wrapper._queue.put_nowait, req)

                # Close; drain must resolve the pending future
                wrapper.close()

                result = pending_future.result(timeout=3)
                assert result["success"] is False

    def test_stop_loop_clears_state(self):
        """close_all() twice must not raise — _loop/_loop_thread cleared after first stop."""
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mgr._start_loop()
        # First stop
        mgr._stop_loop()
        assert mgr._loop is None
        assert mgr._loop_thread is None
        # Second stop must be a no-op
        mgr._stop_loop()  # should not raise

    def test_connect_initialize_failure(self):
        session = _make_mock_session()
        session.initialize = AsyncMock(side_effect=RuntimeError("init failed"))
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []
        assert wrapper.last_error != ""

    def test_connect_list_tools_failure_after_init(self):
        session = _make_mock_session()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(side_effect=RuntimeError("list_tools failed"))
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []
        assert wrapper.last_error != ""

    def test_pagination_page_limit(self):
        # 9 tools/page × 51 pages = 459 total < _MAX_TOOLS (500), so _MAX_TOOL_PAGES fires first
        pages = []
        for i in range(51):
            page = MagicMock()
            page.tools = [_sdk_tool(f"t{i}_{j}") for j in range(9)]
            page.nextCursor = f"cursor{i}" if i < 50 else None
            pages.append(page)

        session = MagicMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(side_effect=pages)
        stdio_cm, session_cm = _make_stdio_patches(session)

        with patch("mcp_client.stdio_client", return_value=stdio_cm):
            with patch("mcp_client.ClientSession", return_value=session_cm):
                wrapper = self._make_wrapper()
                tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []
        assert wrapper.last_error != ""

    def test_connect_unknown_transport(self):
        cfg = {"name": "test", "transport": "ws", "url": "ws://localhost", "timeout": 5}
        wrapper = self._make_wrapper(cfg=cfg)
        tools = wrapper.connect()

        assert wrapper.connected is False
        assert tools == []
        assert "ws" in wrapper.last_error


# ---------------------------------------------------------------------------
# TestMCPManager
# ---------------------------------------------------------------------------


class TestMCPManager:
    def test_connect_disabled_servers_skipped(self):
        cfgs = [
            {
                "name": "active",
                "transport": "stdio",
                "command": ["echo"],
                "enabled": True,
            },
            {
                "name": "off",
                "transport": "stdio",
                "command": ["echo"],
                "enabled": False,
            },
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
        mgr._start_loop()
        result = mgr.call_tool("no_such_tool", {})
        assert result["success"] is False
        assert "not found" in result["error"]
        mgr._stop_loop()

    def test_call_tool_no_wrapper(self):
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        mgr._start_loop()
        mgr._tool_to_server["tool1"] = "srv"
        # No wrapper in _wrappers
        result = mgr.call_tool("tool1", {})
        assert result["success"] is False
        assert "not connected" in result["error"]
        mgr._stop_loop()

    def test_call_tool_delegates_to_wrapper(self):
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        mgr._start_loop()
        mock_wrapper = MagicMock()
        mock_wrapper.call_tool.return_value = _tool_outcome(
            output="result", success=True
        )
        mgr._wrappers["srv"] = mock_wrapper
        mgr._tool_to_server["tool1"] = "srv"

        result = mgr.call_tool("tool1", {"key": "val"})
        mock_wrapper.call_tool.assert_called_once_with("tool1", {"key": "val"})
        assert result["success"] is True
        assert result["output"] == "result"
        mgr._stop_loop()

    def test_get_tools(self):
        mgr = MCPManager([{"name": "a", "transport": "stdio", "command": ["x"]}])
        mock_wrapper = MagicMock()
        mock_wrapper.tools = _sdk_tools_to_registry(
            "a", [_sdk_tool("t1"), _sdk_tool("t2")]
        )
        mgr._wrappers["a"] = mock_wrapper

        tools = mgr.get_tools()
        assert len(tools) == 2
        assert tools[0].name == "t1"

    def test_set_enabled_false(self):
        cfgs = [
            {"name": "srv", "transport": "stdio", "command": ["x"], "enabled": True}
        ]
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
        mock_wrapper._queue = None
        mock_wrapper._task = None
        mgr._wrappers["srv"] = mock_wrapper
        mgr._tool_to_server["t1"] = "srv"

        mgr.close_all()

        assert mock_wrapper.connected is False
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
            {
                "name": "active_srv",
                "transport": "stdio",
                "command": ["x"],
                "enabled": True,
            },
            {
                "name": "off_srv",
                "transport": "http",
                "url": "http://x",
                "enabled": False,
            },
            {
                "name": "error_srv",
                "transport": "stdio",
                "command": ["x"],
                "enabled": True,
            },
        ]
        mgr = MCPManager(cfgs)
        active_wrapper = MagicMock()
        active_wrapper.connected = True
        active_wrapper.needs_auth = False
        active_wrapper.tools = []
        active_wrapper.last_error = ""
        mgr._wrappers["active_srv"] = active_wrapper

        error_wrapper = MagicMock()
        error_wrapper.connected = False
        error_wrapper.needs_auth = False
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
        mock_wrapper.needs_auth = False
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

    def test_set_enabled_true_concurrent_idempotent(self):
        """Concurrent set_enabled(True) must not spawn two wrappers."""
        import threading

        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mgr._start_loop()

        connected_calls = []

        def _fake_connect(name, cfg):  # noqa: ARG001
            import time

            time.sleep(0.05)  # simulate slow connect
            connected_calls.append(name)
            with mgr._lock:
                mgr._wrappers[name] = MagicMock()

        mgr._connect_server = _fake_connect  # type: ignore[method-assign]

        threads = [
            threading.Thread(target=mgr.set_enabled, args=("srv", True))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(connected_calls) == 1, (
            f"Expected 1 connect, got {len(connected_calls)}"
        )
        mgr._stop_loop()

    def test_set_enabled_true_reconnects_failed_wrapper(self):
        """set_enabled(True) on a failed (connected=False) wrapper should reconnect."""
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mgr._start_loop()

        connect_calls = []

        def _fake_connect(name, cfg):  # noqa: ARG001
            connect_calls.append(name)
            with mgr._lock:
                mock_w = MagicMock()
                mock_w.connected = True
                mgr._wrappers[name] = mock_w

        mgr._connect_server = _fake_connect  # type: ignore[method-assign]

        # Install a failed (connected=False) wrapper
        failed_wrapper = MagicMock()
        failed_wrapper.connected = False
        mgr._wrappers["srv"] = failed_wrapper

        # set_enabled(True) should reconnect despite wrapper being present
        result = mgr.set_enabled("srv", True)

        assert result is True
        assert len(connect_calls) == 1
        mgr._stop_loop()

    def test_call_tool_loop_not_running_returns_error(self):
        """call_tool must fail fast if event loop is not running."""
        mgr = MCPManager([{"name": "srv", "transport": "stdio", "command": ["x"]}])
        # Do not start the loop
        mgr._tool_to_server["tool1"] = "srv"
        result = mgr.call_tool("tool1", {})
        assert result["success"] is False
        assert "loop" in result["error"].lower()

    def test_list_servers_transport_labels(self):
        cfgs = [
            {"name": "stdio_srv", "transport": "stdio", "command": ["x"]},
            {"name": "http_srv", "transport": "http", "url": "http://x"},
            {"name": "sse_srv", "transport": "sse", "url": "http://x/sse"},
        ]
        mgr = MCPManager(cfgs)
        servers = mgr.list_servers()
        by_name = {s["name"]: s for s in servers}
        assert by_name["stdio_srv"]["transport"] == "stdio"
        assert by_name["http_srv"]["transport"] == "web"
        assert by_name["sse_srv"]["transport"] == "web"

    def test_start_loop_idempotent(self):
        import time

        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        mgr._start_loop()
        assert mgr._loop is not None
        deadline = time.time() + 2
        while not mgr._loop.is_running() and time.time() < deadline:
            time.sleep(0.01)
        assert mgr._loop.is_running()

        first_loop = mgr._loop
        first_thread = mgr._loop_thread

        mgr._start_loop()  # second call — must be a no-op

        try:
            assert mgr._loop is first_loop
            assert mgr._loop_thread is first_thread
        finally:
            mgr._stop_loop()

    def test_get_server_info_disabled(self):
        cfgs = [
            {"name": "srv", "transport": "stdio", "command": ["x"], "enabled": False}
        ]
        mgr = MCPManager(cfgs)
        info = mgr.get_server_info("srv")
        assert info is not None
        assert info["status"] == "off"

    def test_get_server_info_error_state(self):
        cfgs = [
            {"name": "srv", "transport": "stdio", "command": ["x"], "enabled": True}
        ]
        mgr = MCPManager(cfgs)
        error_wrapper = MagicMock()
        error_wrapper.connected = False
        error_wrapper.needs_auth = False
        error_wrapper.tools = []
        error_wrapper.last_error = "connection refused"
        mgr._wrappers["srv"] = error_wrapper

        info = mgr.get_server_info("srv")
        assert info is not None
        assert info["status"] == "error"

    def test_set_enabled_true_already_connected(self):
        cfgs = [{"name": "srv", "transport": "stdio", "command": ["x"]}]
        mgr = MCPManager(cfgs)
        connected_wrapper = MagicMock()
        connected_wrapper.connected = True
        mgr._wrappers["srv"] = connected_wrapper

        connect_calls: list = []
        mgr._connect_server = lambda name, cfg: connect_calls.append(name)  # type: ignore[method-assign]

        result = mgr.set_enabled("srv", True)

        assert result is True
        assert connect_calls == []


# ---------------------------------------------------------------------------
# Test MCP OAuth integration
# ---------------------------------------------------------------------------


class TestMCPOAuthIntegration:
    def setup_method(self):
        self.loop, self.thread = _start_test_loop()

    def teardown_method(self):
        _stop_test_loop(self.loop, self.thread)

    def _oauth_cfg(self, tmp_path):
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        cert_path.write_text("cert")
        key_path.write_text("key")
        return {
            "client_id": "client1",
            "client_secret": "secret1",
            "redirect_uri": "https://localhost/cb",
            "scope": "tools",
            "callback_port": 8123,
            "callback_bind": "127.0.0.1",
            "cert_path": str(cert_path),
            "key_path": str(key_path),
        }

    def test_oauth_server_without_token_needs_auth(self, tmp_path):
        """An HTTP server configured for OAuth but lacking tokens stays in needs_auth."""
        mcp_tokens_dir = tmp_path / "tokens"
        mcp_tokens_dir.mkdir()
        oauth = self._oauth_cfg(tmp_path)
        cfgs = [
            {
                "name": "oauth_srv",
                "transport": "http",
                "url": "http://localhost:8080",
                "oauth": oauth,
            },
        ]
        mgr = MCPManager(cfgs, mcp_tokens_dir=mcp_tokens_dir)
        mgr._start_loop()

        with patch("mcp_client.streamablehttp_client") as mock_http:
            mock_http.return_value.__aenter__ = AsyncMock()
            mock_http.return_value.__aexit__ = AsyncMock()
            mgr.connect_all()

        servers = mgr.list_servers()
        by_name = {s["name"]: s for s in servers}
        assert by_name["oauth_srv"]["status"] == "needs_auth"
        mock_http.return_value.__aenter__.assert_not_awaited()
        mgr.close_all()

    def test_oauth_server_with_valid_token_connects(self, tmp_path):
        """A valid stored token lets the HTTP OAuth server connect normally."""
        mcp_tokens_dir = tmp_path / "tokens"
        mcp_tokens_dir.mkdir()
        (mcp_tokens_dir / "oauth_srv.json").write_text(
            '{"access_token": "tok", "token_type": "Bearer"}'
        )
        oauth = self._oauth_cfg(tmp_path)
        cfgs = [
            {
                "name": "oauth_srv",
                "transport": "http",
                "url": "http://localhost:8080",
                "timeout": 5,
                "oauth": oauth,
            },
        ]

        session = _make_mock_session(tools=[_sdk_tool("oauth_tool")])
        read, write = AsyncMock(), AsyncMock()
        http_cm = MagicMock()
        http_cm.__aenter__ = AsyncMock(return_value=(read, write, None))
        http_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        mgr = MCPManager(cfgs, mcp_tokens_dir=mcp_tokens_dir)
        mgr._start_loop()

        with patch.object(
            mcp_client, "streamablehttp_client", return_value=http_cm
        ) as mock_http:
            with patch("mcp_client.ClientSession", return_value=session_cm):
                mgr.connect_all()

        servers = mgr.list_servers()
        by_name = {s["name"]: s for s in servers}
        assert by_name["oauth_srv"]["status"] == "active"
        assert by_name["oauth_srv"]["tool_count"] == 1
        # provider passed as auth= keyword
        _, kwargs = mock_http.call_args
        assert kwargs.get("auth") is not None
        mgr.close_all()

    def test_concurrent_oauth_flow_rejected(self, tmp_path):
        """Only one interactive OAuth flow may run at a time."""
        mcp_tokens_dir = tmp_path / "tokens"
        mcp_tokens_dir.mkdir()
        oauth = self._oauth_cfg(tmp_path)
        cfgs = [
            {"name": "server1", "transport": "http", "url": "http://x", "oauth": oauth},
            {"name": "server2", "transport": "http", "url": "http://y", "oauth": oauth},
        ]

        mgr = MCPManager(cfgs, mcp_tokens_dir=mcp_tokens_dir)
        mgr._start_loop()

        self._oauth_flow_started = threading.Event()
        self._oauth_flow_release = threading.Event()

        async def _slow_flow(*args, **kwargs):  # noqa: ARG001
            self._oauth_flow_started.set()
            # Block until the test signals release, so the first flow stays
            # "in progress" while we assert the second is rejected.
            for _ in range(100):
                if self._oauth_flow_release.is_set():
                    break
                await asyncio.sleep(0.05)
            return {"success": True}

        with patch.object(mgr, "_run_oauth_flow", side_effect=_slow_flow):
            first_thread = threading.Thread(
                target=mgr.start_oauth_flow, args=("server1", None), daemon=True
            )
            first_thread.start()
            assert self._oauth_flow_started.wait(timeout=2)

            result = mgr.start_oauth_flow("server2", None)
            assert result["success"] is False
            assert "already in progress" in result["error"].lower()

            # Release the first flow so it can complete and free the lock.
            self._oauth_flow_release.set()
            first_thread.join(timeout=2)

        mgr.close_all()

    def test_needs_auth_in_list_servers(self, tmp_path):
        """list_servers returns the needs_auth status for OAuth servers."""
        mcp_tokens_dir = tmp_path / "tokens"
        mcp_tokens_dir.mkdir()
        oauth = self._oauth_cfg(tmp_path)
        cfgs = [
            {
                "name": "oauth_srv",
                "transport": "http",
                "url": "http://localhost:8080",
                "oauth": oauth,
            },
        ]
        mgr = MCPManager(cfgs, mcp_tokens_dir=mcp_tokens_dir)
        mgr._start_loop()

        with patch("mcp_client.streamablehttp_client") as mock_http:
            mock_http.return_value.__aenter__ = AsyncMock()
            mock_http.return_value.__aexit__ = AsyncMock()
            mgr.connect_all()

        servers = mgr.list_servers()
        assert any(s["status"] == "needs_auth" for s in servers)
        mgr.close_all()

    def test_oauth_flow_success_keeps_session_alive(self, tmp_path):
        """A successful start_oauth_flow keeps the session runner alive and callable."""
        mcp_tokens_dir = tmp_path / "tokens"
        mcp_tokens_dir.mkdir()
        (mcp_tokens_dir / "oauth_srv.json").write_text(
            '{"access_token": "tok", "token_type": "Bearer"}'
        )
        oauth = self._oauth_cfg(tmp_path)
        cfgs = [
            {
                "name": "oauth_srv",
                "transport": "http",
                "url": "http://localhost:8080",
                "timeout": 5,
                "oauth": oauth,
            },
        ]

        session = _make_mock_session(tools=[_sdk_tool("oauth_tool")])
        session.call_tool = AsyncMock(
            return_value=_make_result([_text_item("oauth result")])
        )
        read, write = AsyncMock(), AsyncMock()
        http_cm = MagicMock()
        http_cm.__aenter__ = AsyncMock(return_value=(read, write, None))
        http_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        mgr = MCPManager(cfgs, mcp_tokens_dir=mcp_tokens_dir)
        mgr._start_loop()

        mock_provider = MagicMock()
        with patch.object(
            mcp_client, "streamablehttp_client", return_value=http_cm
        ) as mock_http:
            with patch("mcp_client.ClientSession", return_value=session_cm):
                with patch(
                    "mcp_oauth.CallbackServer.start", new_callable=AsyncMock
                ):
                    with patch(
                        "mcp_oauth.CallbackServer.stop", new_callable=AsyncMock
                    ):
                        with patch(
                            "mcp_oauth.OAuthProviderFactory.build",
                            return_value=mock_provider,
                        ):
                            result = mgr.start_oauth_flow("oauth_srv", None)

        assert result["success"] is True

        wrapper = mgr._wrappers.get("oauth_srv")
        assert wrapper is not None
        assert wrapper.connected is True
        assert wrapper._task is not None
        assert not wrapper._task.done(), "Session runner must still be alive after flow"
        assert len(wrapper.tools) == 1
        assert wrapper.tools[0].name == "oauth_tool"

        # provider passed as auth= keyword
        _, kwargs = mock_http.call_args
        assert kwargs.get("auth") is mock_provider

        # The live session must still be able to serve tool calls.
        tool_result = mgr.call_tool("oauth_tool", {})
        assert tool_result["success"] is True
        assert "oauth result" in tool_result["output"]

        mgr.close_all()
