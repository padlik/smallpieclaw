"""
mcp_client.py
-------------
MCP server communication via the official mcp Python SDK.

Three transport modes:
  stdio — subprocess via SDK stdio_client
  http  — streamable HTTP via SDK streamablehttp_client
  sse   — Server-Sent Events via SDK sse_client

Public API:
    manager = MCPManager(server_configs)
    manager.connect_all()
    tools   = manager.get_tools()
    outcome = manager.call_tool(name, args)
    found   = manager.has_tool(name)
    ok      = manager.set_enabled(name, on)
    servers = manager.list_servers()
    info    = manager.get_server_info(name)
    manager.close_all()
"""

# Server config dict schema (one entry in the list passed to MCPManager):
#
# Required keys (all transports):
#   name        str   — unique server identifier
#   transport   str   — "stdio" | "http" | "sse"  (default: "stdio")
#
# Required for stdio transport:
#   command     list[str]  — executable + args, e.g. ["npx", "-y", "my-mcp-server"]
#
# Required for http/sse transports:
#   url         str   — base URL, e.g. "http://localhost:8080"
#
# Optional (all transports):
#   enabled     bool  — whether to connect on startup (default: True)
#   timeout     int   — per-operation timeout in seconds (default: 30)
#
# Optional for http/sse transports:
#   headers     dict[str, str]  — extra HTTP headers forwarded with each request
#                                 (default: {})
#
# Optional for stdio transport:
#   env         dict[str, str]  — extra env vars merged into the subprocess
#                                 environment (default: {})

from __future__ import annotations

import asyncio
import os
import concurrent.futures
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp.types import CallToolResult

from tool_registry import Tool

logger = logging.getLogger(__name__)

_MAX_TOOL_PAGES = 50
_MAX_TOOLS = 500

_VALID_TOOL_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
_MAX_DESCRIPTION_LEN = 2048
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_outcome(output: str = "", error: str = "", success: bool = True) -> dict:
    return {"success": success, "output": output, "error": error, "exit_code": 0 if success else 1}

def _sdk_result_to_outcome(result: CallToolResult) -> dict:
    """Flatten SDK CallToolResult into our standard outcome dict."""
    parts: list[str] = []
    for item in result.content:
        t = getattr(item, "type", None)
        if t == "text":
            parts.append(item.text)
        elif t == "image":
            parts.append(f"[image: {item.mimeType}]")
        elif t == "resource":
            uri = getattr(item.resource, "uri", "") if item.resource else ""
            parts.append(f"[resource: {uri}]" if uri else "[resource]")
        elif t == "audio":
            parts.append(f"[audio: {item.mimeType}]")
        elif t == "resource_link":
            parts.append(f"[resource_link: {item.uri}]")
    text = "\n".join(parts)
    if result.isError:
        return _tool_outcome(error=text, success=False)
    return _tool_outcome(output=text)

def _sdk_tools_to_registry(server_name: str, sdk_tools: list) -> list[Tool]:
    """Convert SDK Tool objects to our Tool dataclass list."""
    result: list[Tool] = []
    for t in sdk_tools:
        name: str = getattr(t, "name", "") or ""
        if not name:
            continue
        if not _VALID_TOOL_NAME_RE.match(name):
            logger.warning("MCP [%s] skipping tool with invalid name: %r", server_name, name)
            continue
        raw_desc: str = getattr(t, "description", None) or f"MCP tool '{name}' from {server_name}"
        description = _CONTROL_CHARS_RE.sub("", raw_desc)[:_MAX_DESCRIPTION_LEN]
        input_schema: dict[str, Any] = getattr(t, "inputSchema", {}) or {}
        result.append(Tool(
            name=name,
            path="",
            language="mcp",
            description=description,
            is_mcp=True,
            server_name=server_name,
            input_schema=input_schema,
        ))
    return result

async def _cancel_and_wait(tasks: list) -> None:
    """Cancel asyncio tasks and await their completion so context managers finalize."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

# ---------------------------------------------------------------------------
# Per-server SDK wrapper
# ---------------------------------------------------------------------------

@dataclass
class _ToolRequest:
    name: str
    args: dict
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)

class _SdkClientWrapper:
    """
    Wraps one MCP server connection using the SDK's long-lived session-runner pattern.

    The session runner coroutine enters the transport + ClientSession context once
    and stays alive, processing tool calls from an asyncio.Queue.  All sync-to-async
    bridging goes through concurrent.futures.Future (thread-safe) and
    asyncio.run_coroutine_threadsafe. Tool calls are serialized — only one outstanding
    call per server at a time.
    """

    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop) -> None:
        self.name: str = cfg["name"]
        self._cfg = cfg
        self._loop = loop
        self._timeout: int = int(cfg.get("timeout", 30))
        self._ready_future: concurrent.futures.Future = concurrent.futures.Future()
        self._task: Optional[asyncio.Task] = None
        self._queue: Optional[asyncio.Queue] = None
        self.connected: bool = False
        self.last_error: str = ""
        self._tools: list[Tool] = []

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    def connect(self) -> list[Tool]:
        """Schedule the session runner on the event loop; block until ready."""
        def _create_task() -> None:
            self._task = self._loop.create_task(self._session_runner())

        self._loop.call_soon_threadsafe(_create_task)
        try:
            self._ready_future.result(timeout=self._timeout)
            self.connected = True
            logger.info("MCP [%s] connected: %d tool(s)", self.name, len(self._tools))
            return self._tools
        except concurrent.futures.TimeoutError:
            self.last_error = f"MCP [{self.name}] connect timeout"
            self.connected = False
            # Cancel the background task so it doesn't spin orphaned
            if self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
            logger.error("MCP [%s] connect timed out", self.name)
            return []
        except Exception as exc:
            self.last_error = str(exc)
            self.connected = False
            logger.error("MCP [%s] connect failed: %s", self.name, exc)
            return []

    def call_tool(self, tool_name: str, args: dict) -> dict:
        """Enqueue a tool call request; block until result or timeout."""
        if not self.connected or self._queue is None:
            return _tool_outcome(
                error=f"MCP server '{self.name}' not connected", success=False
            )
        req = _ToolRequest(name=tool_name, args=args)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, req)
        except RuntimeError:
            return _tool_outcome(error=f"MCP server '{self.name}' is closing", success=False)
        try:
            return req.future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            self.last_error = f"MCP [{self.name}] tool '{tool_name}' timeout"
            return _tool_outcome(error=self.last_error, success=False)

    def close(self) -> None:
        """Send shutdown sentinel and cancel the session-runner task."""
        self.connected = False
        if self._queue is not None:
            # put_nowait is synchronous; call_soon_threadsafe avoids creating an
            # unawaited coroutine when the loop may already be stopping.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        if self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def _drain_queue(self, error_msg: str) -> None:
        """Resolve all pending queued requests so callers do not hang."""
        if self._queue is None:
            return
        while True:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if req is not None and not req.future.done():
                req.future.set_result(_tool_outcome(error=error_msg, success=False))

    async def _session_runner(self) -> None:
        """Long-lived coroutine — owns the transport + ClientSession for this server."""
        self._queue = asyncio.Queue()
        cfg = self._cfg
        transport = cfg.get("transport", "stdio").lower()
        try:
            if transport == "stdio":
                merged_env = {**get_default_environment(), **(cfg.get("env") or {})}
                for _k in ("TMPDIR", "TMP", "TEMP"):
                    if _k in os.environ:
                        merged_env.setdefault(_k, os.environ[_k])
                params = StdioServerParameters(
                    command=cfg["command"][0],
                    args=cfg["command"][1:],
                    env=merged_env,
                )
                async with stdio_client(params) as (read, write):
                    await self._run_session(read, write)
            elif transport == "http":
                async with streamablehttp_client(
                    cfg["url"], headers=cfg.get("headers") or {}
                ) as (read, write, _):
                    await self._run_session(read, write)
            elif transport == "sse":
                async with sse_client(
                    cfg["url"], headers=cfg.get("headers") or {}
                ) as (read, write):
                    await self._run_session(read, write)
            else:
                raise ValueError(f"Unknown transport: {transport!r}")
        except Exception as exc:
            if not self._ready_future.done():
                self._ready_future.set_exception(exc)
            else:
                self.connected = False
                self.last_error = str(exc)
                logger.error("MCP [%s] session error: %s", self.name, exc)

    async def _run_session(self, read: Any, write: Any) -> None:
        """Enter ClientSession, discover tools, then process requests."""
        async with ClientSession(read, write) as session:
            await session.initialize()
            all_tools: list = []
            cursor: Optional[str] = None
            seen_cursors: set[str] = set()
            page_count = 0
            while True:
                page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                all_tools.extend(page.tools)
                page_count += 1
                if len(all_tools) > _MAX_TOOLS:
                    raise RuntimeError(
                        f"MCP server '{self.name}' exceeded {_MAX_TOOLS} tool limit"
                    )
                cursor = getattr(page, "nextCursor", None)
                if not cursor:
                    break
                if page_count >= _MAX_TOOL_PAGES:
                    raise RuntimeError(
                        f"MCP server '{self.name}' exceeded {_MAX_TOOL_PAGES} pagination pages"
                    )
                if cursor in seen_cursors:
                    raise RuntimeError(
                        f"MCP server '{self.name}' returned duplicate pagination cursor"
                    )
                seen_cursors.add(cursor)
            self._tools = _sdk_tools_to_registry(self.name, all_tools)
            self._ready_future.set_result(True)
            try:
                while True:
                    req = await self._queue.get()
                    if req is None:
                        break
                    try:
                        result = await session.call_tool(req.name, req.args)
                        if not req.future.done():
                            req.future.set_result(_sdk_result_to_outcome(result))
                    except asyncio.CancelledError:
                        if not req.future.done():
                            req.future.set_result(_tool_outcome(
                                error=f"MCP server '{self.name}' closing", success=False
                            ))
                        raise
                    except Exception as exc:
                        self.connected = False
                        self.last_error = str(exc)
                        if not req.future.done():
                            req.future.set_result(_tool_outcome(error=str(exc), success=False))
            finally:
                self._drain_queue(f"MCP server '{self.name}' session ended")

# ---------------------------------------------------------------------------
# MCPManager
# ---------------------------------------------------------------------------

class MCPManager:
    """
    Manages multiple MCP server connections.

    Threading model:
      - A single daemon event loop runs in its own background thread (_loop_thread).
      - Each configured server gets one _SdkClientWrapper, which schedules a
        long-lived _session_runner coroutine on that shared loop.
      - Tool calls are serialized per server via an asyncio.Queue; only one
        outstanding call per server at a time.
      - connect() / call_tool() bridge sync callers to the async loop via
        loop.call_soon_threadsafe() to schedule work, plus a
        concurrent.futures.Future on each _ToolRequest / _ready_future to
        block the caller until the result is ready.
      - asyncio.run_coroutine_threadsafe is used only in close_all() to drain
        pending tasks before the loop stops.
      - The loop starts lazily on first connect_all() and stops on close_all().
    """

    def __init__(self, server_configs: list[dict]) -> None:
        self._cfgs: dict[str, dict] = {cfg["name"]: cfg for cfg in server_configs if "name" in cfg}
        self._wrappers: dict[str, _SdkClientWrapper] = {}
        self._tool_to_server: dict[str, str] = {}
        self._enabled: dict[str, bool] = {
            name: bool(cfg.get("enabled", True))
            for name, cfg in self._cfgs.items()
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connecting: set[str] = set()

    def _start_loop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def _stop_loop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        if self._loop is not None:
            try:
                self._loop.close()
            except Exception:
                pass
        self._loop = None
        self._loop_thread = None

    def connect_all(self) -> None:
        """Connect all enabled servers. Errors are logged but do not abort startup."""
        self._start_loop()
        for name, cfg in self._cfgs.items():
            if not self._enabled.get(name, True):
                logger.info("MCP [%s] skipped (disabled)", name)
                continue
            self._connect_server(name, cfg)

    def _connect_server(self, name: str, cfg: dict) -> None:
        if self._loop is None:
            self._start_loop()
        wrapper = _SdkClientWrapper(cfg, self._loop)
        tools = wrapper.connect()
        with self._lock:
            self._wrappers[name] = wrapper
            for tool in tools:
                if tool.name in self._tool_to_server:
                    logger.warning(
                        "MCP tool name conflict: '%s' claimed by both '%s' and '%s' — keeping first",
                        tool.name, self._tool_to_server[tool.name], name,
                    )
                else:
                    self._tool_to_server[tool.name] = name

    def close_all(self) -> None:
        tasks: list = []
        with self._lock:
            for name, wrapper in list(self._wrappers.items()):
                try:
                    wrapper.connected = False
                    if self._loop is not None and wrapper._queue is not None:
                        self._loop.call_soon_threadsafe(wrapper._queue.put_nowait, None)
                    if wrapper._task is not None:
                        tasks.append(wrapper._task)
                except Exception as exc:
                    logger.warning("MCP [%s] close error: %s", name, exc)
            self._wrappers.clear()
            self._tool_to_server.clear()
        # Drive cancellation to completion so stdio_client.__aexit__ runs and terminates children
        if tasks and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    _cancel_and_wait(tasks), self._loop
                ).result(timeout=10)
            except Exception:
                pass
        self._stop_loop()

    def has_tool(self, tool_name: str) -> bool:
        with self._lock:
            return tool_name in self._tool_to_server

    def call_tool(self, tool_name: str, args: dict) -> dict:
        if self._loop is None or not self._loop.is_running():
            return _tool_outcome(error="MCP event loop not running", success=False)
        with self._lock:
            server_name = self._tool_to_server.get(tool_name)
            if not server_name:
                return _tool_outcome(error=f"MCP tool '{tool_name}' not found", success=False)
            wrapper = self._wrappers.get(server_name)
        if not wrapper:
            return _tool_outcome(error=f"MCP server '{server_name}' not connected", success=False)
        return wrapper.call_tool(tool_name, args)

    def get_tools(self) -> list[Tool]:
        """Return all tools from all currently connected servers."""
        tools: list[Tool] = []
        with self._lock:
            for wrapper in self._wrappers.values():
                tools.extend(wrapper.tools)
        return tools

    def set_enabled(self, name: str, on: bool) -> bool:
        """Enable or disable a server at runtime. Returns False if name not found."""
        if name not in self._cfgs:
            return False
        with self._lock:
            self._enabled[name] = on
        if on:
            with self._lock:
                existing = self._wrappers.get(name)
                # Allow reconnect if wrapper is present but not connected
                if (existing is not None and existing.connected) or name in self._connecting:
                    return True
                self._connecting.add(name)
            try:
                self._connect_server(name, self._cfgs[name])
            finally:
                with self._lock:
                    self._connecting.discard(name)
                    # If disabled during the connect window, close the just-registered wrapper
                    if not self._enabled.get(name, True):
                        wrapper = self._wrappers.pop(name, None)
                        for tool_name in [k for k, v in self._tool_to_server.items() if v == name]:
                            del self._tool_to_server[tool_name]
                        if wrapper:
                            try:
                                wrapper.close()
                            except Exception:
                                pass
        else:
            with self._lock:
                wrapper = self._wrappers.pop(name, None)
                for tool_name in [k for k, v in self._tool_to_server.items() if v == name]:
                    del self._tool_to_server[tool_name]
            if wrapper:
                try:
                    wrapper.close()
                except Exception:
                    pass
            logger.info("MCP [%s] disabled and disconnected", name)
        return True

    def list_servers(self) -> list[dict]:
        """Return status info for all configured servers (for /mcp list)."""
        with self._lock:
            wrappers_snap = dict(self._wrappers)
            enabled_snap = dict(self._enabled)
        result = []
        for name, cfg in self._cfgs.items():
            transport = cfg.get("transport", "stdio").lower()
            wrapper = wrappers_snap.get(name)
            enabled = enabled_snap.get(name, True)
            if not enabled:
                status = "off"
            elif wrapper and wrapper.connected:
                status = "active"
            else:
                status = "error"
            result.append({
                "name": name,
                "transport": "web" if transport in ("http", "sse") else "stdio",
                "status": status,
                "tool_count": len(wrapper.tools) if wrapper else 0,
                "last_error": wrapper.last_error if wrapper else "",
            })
        return result

    def get_server_info(self, name: str) -> Optional[dict]:
        """Return detailed server info for /mcp info."""
        cfg = self._cfgs.get(name)
        if not cfg:
            return None
        with self._lock:
            wrapper = self._wrappers.get(name)
            enabled = self._enabled.get(name, True)
        transport = cfg.get("transport", "stdio").lower()
        if not enabled:
            status = "off"
        elif wrapper and wrapper.connected:
            status = "active"
        else:
            status = "error"
        return {
            "name": name,
            "transport": "web" if transport in ("http", "sse") else "stdio",
            "status": status,
            "url": cfg.get("url", ""),
            "command": cfg.get("command", []),
            "timeout": cfg.get("timeout", 30),
            "headers": cfg.get("headers") or {},
            "env": cfg.get("env") or {},
            "tools": wrapper.tools if wrapper else [],
            "last_error": wrapper.last_error if wrapper else "",
        }

