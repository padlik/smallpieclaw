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
import json
import os
import concurrent.futures
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp.types import CallToolResult

import mcp_oauth
import httpx
from tool_registry import Tool

logger = logging.getLogger(__name__)

_MAX_TOOL_PAGES = 50
_MAX_TOOLS = 500

# MCP protocol version sent on the proactive OAuth probe (design D3).
# The SDK reads this to decide whether to include the RFC 8707 resource param.
# Update this single value when the SDK bumps the protocol version.
_PROBE_MCP_PROTOCOL_VERSION = "2025-11-25"

# Maximum response body size for the probe's tools/list request.  A
# tools/list response is typically a few KB; cap at 1 MB as
# defense-in-depth against a malicious or compromised server returning
# a huge body to exhaust memory.
_PROBE_MAX_RESPONSE_BYTES = 1_048_576

# Tool name prefixes that suggest a mutating/side-effecting operation.
# The probe prefers non-mutating tools to minimize risk if a server
# executes before checking auth (defense-in-depth beyond the design's
# accepted risk in design.md).
_PROBE_MUTATING_TOOL_PREFIXES = (
    "send_",
    "delete_",
    "write_",
    "update_",
    "create_",
    "remove_",
    "set_",
    "put_",
    "post_",
    "add_",
    "insert_",
    "modify_",
    "edit_",
    "move_",
    "rename_",
    "clear_",
    "reset_",
    "upload_",
    "submit_",
    "execute_",
    "run_",
)

# POST probe constants (design D2/D3): when the GET probe returns 200 or 405
# without an auth challenge, issue a two-step discovery probe: POST
# tools/list to discover real tool names, then POST tools/call with the
# first real tool name and empty arguments.  The headers match the SDK's
# streamable_http transport so the server routes the request through its
# auth middleware.
_PROBE_GET_HEADERS = {
    "MCP-Protocol-Version": _PROBE_MCP_PROTOCOL_VERSION,
}
_PROBE_POST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": _PROBE_MCP_PROTOCOL_VERSION,
}

_VALID_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_MAX_DESCRIPTION_LEN = 2048
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_outcome(output: str = "", error: str = "", success: bool = True) -> dict:
    return {
        "success": success,
        "output": output,
        "error": error,
        "exit_code": 0 if success else 1,
    }


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
            logger.warning(
                "MCP [%s] skipping tool with invalid name: %r", server_name, name
            )
            continue
        raw_desc: str = (
            getattr(t, "description", None) or f"MCP tool '{name}' from {server_name}"
        )
        description = _CONTROL_CHARS_RE.sub("", raw_desc)[:_MAX_DESCRIPTION_LEN]
        input_schema: dict[str, Any] = getattr(t, "inputSchema", {}) or {}
        result.append(
            Tool(
                name=name,
                language="mcp",
                description=description,
                is_mcp=True,
                server_name=server_name,
                input_schema=input_schema,
            )
        )
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
        self.needs_auth: bool = False
        self.last_error: str = ""
        self._tools: list[Tool] = []
        self._oauth_provider: Any = None
        self._mcp_tokens_dir: Path | None = None
        self._interactive: bool = False
        self._tg_iface: object | None = None

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @property
    def ready_future(self) -> concurrent.futures.Future:
        """Return the future that signals when the session runner is ready."""
        return self._ready_future

    def configure(
        self,
        *,
        mcp_tokens_dir: Path | None,
        tg_iface: object | None,
    ) -> None:
        """Set the token directory and Telegram interface used by this wrapper.

        These values are normally wired once by ``MCPManager`` before the
        session runner is started.
        """
        self._mcp_tokens_dir = mcp_tokens_dir
        self._tg_iface = tg_iface

    def clear_tools(self) -> None:
        """Clear the cached tool list (used when revoking a server)."""
        self._tools = []

    def drain_task(self) -> Optional[asyncio.Task]:
        """Return the current session task and clear it from the wrapper.

        The caller is responsible for awaiting or cancelling the returned task.
        """
        task = self._task
        self._task = None
        return task

    def set_oauth(self, provider: Any, interactive: bool) -> None:
        """Set the OAuth provider and interactive flag for this session."""
        self._oauth_provider = provider
        self._interactive = interactive

    def set_task(self, task: asyncio.Task) -> None:
        """Assign the live session-runner task to this wrapper."""
        self._task = task

    def create_session_runner(self) -> asyncio.Task:
        """Create and return an asyncio task running the session runner."""
        return self._loop.create_task(self._session_runner())

    @property
    def queue(self) -> Optional[asyncio.Queue]:
        """Return the request queue for this wrapper, if created."""
        return self._queue

    def connect(self) -> list[Tool]:
        """Schedule the session runner on the event loop; block until ready."""

        def _create_task() -> None:
            self._task = self._loop.create_task(self._session_runner())

        self._loop.call_soon_threadsafe(_create_task)
        try:
            self._ready_future.result(timeout=self._timeout)
            if self.needs_auth:
                return []
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
            return _tool_outcome(
                error=f"MCP server '{self.name}' is closing", success=False
            )
        logger.debug("MCP [%s] calling tool '%s'", self.name, tool_name)
        try:
            return req.future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            self.last_error = f"MCP [{self.name}] tool '{tool_name}' timeout"
            logger.error(
                "MCP [%s] tool '%s' timed out after %ds (connected=%s)",
                self.name,
                tool_name,
                self._timeout,
                self.connected,
            )
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
            oauth_cfg = cfg.get("oauth")
            if oauth_cfg and transport in ("http", "sse"):
                if not self._interactive:
                    await self._prepare_oauth_provider(oauth_cfg)
                    if self.needs_auth:
                        self._ready_future.set_result([])
                        return
                # For interactive flows, _oauth_provider is already set by _run_oauth_flow
                # and we proceed directly to connection so the SDK's async_auth_flow fires.

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
                    cfg["url"],
                    headers=cfg.get("headers") or {},
                    auth=self._oauth_provider,
                ) as (read, write, _):
                    await self._run_session(read, write)
            elif transport == "sse":
                async with sse_client(
                    cfg["url"],
                    headers=cfg.get("headers") or {},
                    auth=self._oauth_provider,
                ) as (read, write):
                    await self._run_session(read, write)
            else:
                raise ValueError(f"Unknown transport: {transport!r}")
        except Exception as exc:
            if not self._ready_future.done():
                self._ready_future.set_exception(exc)
            else:
                self.connected = False
                if self.needs_auth:
                    logger.info("MCP [%s] session ended (needs_auth)", self.name)
                else:
                    self.last_error = str(exc)
                    logger.error("MCP [%s] session error: %s", self.name, exc)

    async def _prepare_oauth_provider(self, oauth_cfg: dict) -> None:
        """Resolve OAuth provider and detect whether an interactive flow is needed.

        If ``mcp_tokens_dir`` is configured but no stored token exists, mark the
        server as needing authentication and skip the transport connection. The
        manager can later trigger ``start_oauth_flow`` to acquire a token.
        """
        if self._mcp_tokens_dir is None:
            return
        # Create an unstarted callback server. If an interactive flow is later
        # triggered via ``start_oauth_flow`` it is started up front; otherwise
        # the redirect handler starts it lazily if a fallback full-redirect
        # flow is triggered (e.g. an invalid stored refresh token).
        cb_server = mcp_oauth.CallbackServer(
            port=oauth_cfg.get("callback_port", 8000),
            bind=oauth_cfg.get("callback_bind", "0.0.0.0"),
            cert_path=oauth_cfg["cert_path"],
            key_path=oauth_cfg["key_path"],
            loop=asyncio.get_running_loop(),
        )
        def _on_non_interactive() -> None:
            self.needs_auth = True
            if not self._ready_future.done():
                self._ready_future.set_result([])

        self._oauth_provider = mcp_oauth.OAuthProviderFactory.build(
            self._cfg,
            self._mcp_tokens_dir,
            cb_server=cb_server,
            tg_iface=self._tg_iface,
            on_non_interactive=_on_non_interactive,
        )
        storage = mcp_oauth.FileTokenStorage(
            server_name=self.name,
            mcp_tokens_dir=self._mcp_tokens_dir,
            client_id=oauth_cfg["client_id"],
            client_secret=oauth_cfg["client_secret"],
        )
        existing = await storage.get_tokens()
        if existing is None:
            self.needs_auth = True
            self.connected = False
            logger.info("MCP [%s] no stored token; marking needs_auth", self.name)
        else:
            self.needs_auth = False
            logger.info(
                "MCP [%s] stored token found; connecting with existing token", self.name
            )

    async def _run_session(self, read: Any, write: Any) -> None:
        """Enter ClientSession, discover tools, then process requests."""
        async with ClientSession(read, write) as session:
            logger.debug("MCP [%s] session.initialize() start", self.name)
            await session.initialize()
            logger.debug("MCP [%s] session.initialize() done", self.name)
            all_tools: list = []
            cursor: Optional[str] = None
            seen_cursors: set[str] = set()
            page_count = 0
            while True:
                page = (
                    await session.list_tools(cursor=cursor)
                    if cursor
                    else await session.list_tools()
                )
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
            logger.debug(
                "MCP [%s] list_tools: %d tools discovered", self.name, len(all_tools)
            )
            if not self._ready_future.done():
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
                            req.future.set_result(
                                _tool_outcome(
                                    error=f"MCP server '{self.name}' closing",
                                    success=False,
                                )
                            )
                        raise
                    except Exception as exc:
                        self.connected = False
                        self.last_error = str(exc)
                        if not req.future.done():
                            req.future.set_result(
                                _tool_outcome(error=str(exc), success=False)
                            )
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

    def __init__(
        self,
        server_configs: list[dict],
        mcp_tokens_dir: Path | None = None,
        tg_iface: object | None = None,
    ) -> None:
        self._cfgs: dict[str, dict] = {
            cfg["name"]: cfg for cfg in server_configs if "name" in cfg
        }
        self._wrappers: dict[str, _SdkClientWrapper] = {}
        self._tool_to_server: dict[str, str] = {}
        self._enabled: dict[str, bool] = {
            name: bool(cfg.get("enabled", True)) for name, cfg in self._cfgs.items()
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connecting: set[str] = set()
        self._mcp_tokens_dir: Path | None = mcp_tokens_dir
        self._tg_iface: object | None = tg_iface
        self._oauth_flow_in_progress: bool = False
        self._oauth_cancel_requested: bool = False
        self._oauth_chat_id: int | None = None

    def set_tg_iface(self, tg_iface: object) -> None:
        """Set the Telegram interface used for OAuth redirect URL delivery.

        Called once from the composition root after both ``MCPManager`` and
        ``TelegramInterface`` are constructed.  This is the only supported way
        to wire the Telegram dependency post-construction; direct attribute
        writes are not part of the public API.

        Args:
            tg_iface: Telegram interface instance whose ``app.bot`` can send
                messages during interactive OAuth flows.
        """
        self._tg_iface = tg_iface

    def server_name_for_tool(self, tool_name: str) -> str:
        """Return the server name owning a tool, or empty string (thread-safe)."""
        with self._lock:
            return self._tool_to_server.get(tool_name, "")

    def server_has_oauth(self, server_name: str) -> bool:
        """Return True if the server has OAuth configured (thread-safe)."""
        with self._lock:
            cfg = self._cfgs.get(server_name, {})
        return bool(cfg.get("oauth"))

    def get_oauth_timeout(self, name: str) -> int:
        """Return the configured OAuth timeout for a server (default 300s).

        Thread-safe.

        Args:
            name: Server name.

        Returns:
            Timeout in seconds, or 300 if not configured / not found.
        """
        with self._lock:
            cfg = self._cfgs.get(name)
        if cfg is None:
            return 300
        oauth_cfg = cfg.get("oauth")
        if oauth_cfg is None:
            return 300
        return int(oauth_cfg.get("timeout", 300))

    def get_token_info(self, server_name: str) -> dict[str, Any] | None:
        """Return token info for a server for status display (thread-safe).

        Reads the persisted token file directly without touching the event
        loop, so this method is safe to call from synchronous code.

        Returns:
            A dict with keys ``has_token`` (bool), ``expires_in`` (int|None, remaining seconds until expiry),
            ``has_refresh`` (bool), and ``scope`` (str|None). Returns ``None``
            if the server is not found or has no OAuth configuration.
        """
        if not self.server_has_oauth(server_name):
            return None

        cfg = self._cfgs.get(server_name, {})
        oauth_cfg = cfg.get("oauth") or {}
        if self._mcp_tokens_dir is None:
            return {
                "has_token": False,
                "expires_in": None,
                "has_refresh": False,
                "scope": oauth_cfg.get("scope"),
            }

        storage = mcp_oauth.FileTokenStorage(
            server_name=server_name,
            mcp_tokens_dir=self._mcp_tokens_dir,
            client_id=oauth_cfg["client_id"],
            client_secret=oauth_cfg["client_secret"],
        )
        status = storage.read_status()
        if status is None:
            return {
                "has_token": False,
                "expires_in": None,
                "has_refresh": False,
                "scope": oauth_cfg.get("scope"),
            }
        status["scope"] = status.get("scope") or oauth_cfg.get("scope")
        return status

    def mark_needs_auth(self, server_name: str) -> None:
        """Mark a server as needing authentication (thread-safe).

        Sets ``needs_auth=True`` and ``connected=False`` on the server's wrapper
        under the class lock. Safe to call from any thread.

        Args:
            server_name: Name of the configured MCP server.
        """
        with self._lock:
            wrapper = self._wrappers.get(server_name)
            if wrapper is not None:
                wrapper.needs_auth = True
                wrapper.connected = False

    def revoke_server(self, server_name: str) -> bool:
        """Revoke stored OAuth tokens and unregister tools for a server.

        Deletes the token file, closes the wrapper, and removes the server's
        tools from the registry — all under the class lock. The wrapper is
        left in place marked ``needs_auth`` (rather than removed) so status
        queries report "needs_auth" instead of falling back to "error".
        Returns False if the server name is not configured or has no OAuth.

        Safe to call from any thread.

        Args:
            server_name: Name of the configured MCP server.

        Returns:
            True if the server existed and had OAuth configured; False otherwise.
        """
        cfg = self._cfgs.get(server_name)
        if cfg is None:
            return False
        if not cfg.get("oauth"):
            return False
        # Delete token file
        if self._mcp_tokens_dir is not None:
            token_file = self._mcp_tokens_dir / f"{server_name}.json"
            try:
                token_file.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("MCP revoke: could not delete token file for %s: %s", server_name, exc)
        with self._lock:
            wrapper = self._wrappers.get(server_name)
            for tool_name in [k for k, v in self._tool_to_server.items() if v == server_name]:
                del self._tool_to_server[tool_name]
        if wrapper is not None:
            try:
                wrapper.close()
            except Exception as exc:
                logger.warning("MCP revoke close error for %s: %s", server_name, exc)
            wrapper.needs_auth = True
            wrapper.clear_tools()
        return True

    def cancel_oauth_flow(self) -> dict:
        """Request cancellation of the currently running OAuth flow.

        The next time the running flow polls the cancel flag, it aborts. If no
        flow is in progress, this returns an error.
        """
        with self._lock:
            if not self._oauth_flow_in_progress:
                return {"success": False, "error": "No OAuth flow in progress"}
            self._oauth_cancel_requested = True
        return {"success": True}

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
        wrapper.configure(
            mcp_tokens_dir=self._mcp_tokens_dir,
            tg_iface=self._tg_iface,
        )
        tools = wrapper.connect()
        with self._lock:
            self._wrappers[name] = wrapper
            for tool in tools:
                if tool.name in self._tool_to_server:
                    logger.warning(
                        "MCP tool name conflict: '%s' claimed by both '%s' and '%s' — keeping first",
                        tool.name,
                        self._tool_to_server[tool.name],
                        name,
                    )
                else:
                    self._tool_to_server[tool.name] = name

    def close_all(self) -> None:
        tasks: list = []
        with self._lock:
            for name, wrapper in list(self._wrappers.items()):
                try:
                    wrapper.connected = False
                    queue = wrapper.queue
                    if self._loop is not None and queue is not None:
                        self._loop.call_soon_threadsafe(queue.put_nowait, None)
                    task = wrapper.drain_task()
                    if task is not None:
                        tasks.append(task)
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
                return _tool_outcome(
                    error=f"MCP tool '{tool_name}' not found", success=False
                )
            wrapper = self._wrappers.get(server_name)
        if not wrapper:
            return _tool_outcome(
                error=f"MCP server '{server_name}' not connected", success=False
            )
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
                if (
                    existing is not None and existing.connected
                ) or name in self._connecting:
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
                        for tool_name in [
                            k for k, v in self._tool_to_server.items() if v == name
                        ]:
                            del self._tool_to_server[tool_name]
                        if wrapper:
                            try:
                                wrapper.close()
                            except Exception:
                                pass
        else:
            with self._lock:
                wrapper = self._wrappers.pop(name, None)
                for tool_name in [
                    k for k, v in self._tool_to_server.items() if v == name
                ]:
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
            elif wrapper and wrapper.needs_auth:
                status = "needs_auth"
            else:
                status = "error"
            result.append(
                {
                    "name": name,
                    "transport": "web" if transport in ("http", "sse") else "stdio",
                    "status": status,
                    "tool_count": len(wrapper.tools) if wrapper else 0,
                    "last_error": wrapper.last_error if wrapper else "",
                }
            )
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
        elif wrapper and wrapper.needs_auth:
            status = "needs_auth"
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

    def start_oauth_flow(self, name: str, chat_id: int | None = None) -> dict:
        """Start an interactive OAuth flow for the named server.

        Called from a sync context; the actual async flow runs on the MCP event
        loop while the caller blocks until completion or timeout.

        Args:
            name: Name of the configured MCP server to authorize.
            chat_id: Telegram chat ID to send the authorization prompt to.

        Returns:
            A dict with ``success`` and optional ``error`` keys.
        """
        with self._lock:
            if self._oauth_flow_in_progress:
                return {"success": False, "error": "OAuth flow already in progress"}
            self._oauth_flow_in_progress = True
            self._oauth_chat_id = chat_id
            # Reset any stale cancel flag from a racy cancel that arrived
            # after the previous flow's _run_oauth_flow finished but before
            # this method's finally block ran.  Without this, a stale True
            # would cause the next flow to abort immediately.
            self._oauth_cancel_requested = False

        try:
            cfg = self._cfgs.get(name)
            if cfg is None:
                return {"success": False, "error": f"Server '{name}' not found"}

            oauth_cfg = cfg.get("oauth")
            if oauth_cfg is None:
                return {
                    "success": False,
                    "error": f"Server '{name}' has no OAuth configuration",
                }

            if self._mcp_tokens_dir is None:
                return {
                    "success": False,
                    "error": "OAuth token directory not configured",
                }

            timeout = oauth_cfg.get("timeout", 300)
            future = asyncio.run_coroutine_threadsafe(
                self._run_oauth_flow(name, cfg, oauth_cfg),
                self._loop,
            )
            return future.result(timeout=timeout + 10)
        except concurrent.futures.TimeoutError:
            logger.warning("MCP [%s] OAuth flow timed out after %ss", name, timeout)
            return {"success": False, "error": "OAuth flow timed out"}
        except Exception as exc:
            logger.warning(
                "MCP [%s] OAuth flow error: %s",
                name,
                exc,
                exc_info=True,
            )
            return {"success": False, "error": str(exc)}
        finally:
            with self._lock:
                self._oauth_flow_in_progress = False
                self._oauth_chat_id = None

    async def _watch_cancel(self) -> None:
        """Coroutine that completes once an operator cancel is requested."""
        while not self._oauth_cancel_requested:
            await asyncio.sleep(0.5)

    @staticmethod
    def _extract_first_tool_name(
        list_response: httpx.Response,
    ) -> str | None:
        """Extract a safe tool name from a ``tools/list`` JSON-RPC response.

        Handles both ``application/json`` (bare JSON-RPC body) and
        ``text/event-stream`` (SSE-framed JSON-RPC) response framings.
        Prefers the first non-mutating tool name (skips verbs like
        ``send_``, ``delete_``, ``write_``) to minimize risk if a server
        executes before checking auth.  Returns ``None`` if the response
        is malformed, the tool list is empty, or all tools are mutating.

        Args:
            list_response: The HTTP response from the POST ``tools/list``.

        Returns:
            A non-mutating tool name, or ``None`` if no safe tool is found.
        """
        content_type = list_response.headers.get("content-type", "")
        try:
            if "text/event-stream" in content_type:
                # SSE framing: split into events (blank-line delimited),
                # parse each event's data: lines joined with \n per the
                # SSE spec.  Return the first well-formed JSON-RPC frame.
                text = list_response.text
                if len(text) > _PROBE_MAX_RESPONSE_BYTES:
                    return None
                parsed: dict | None = None
                for event_block in text.split("\n\n"):
                    parts: list[str] = []
                    for line in event_block.splitlines():
                        if line.startswith("data:"):
                            parts.append(line.removeprefix("data:").lstrip())
                    if not parts:
                        continue
                    event_data = "\n".join(parts)
                    try:
                        candidate = json.loads(event_data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # Skip events without a result.tools list (e.g.
                    # heartbeat/ping frames) — keep scanning for the
                    # JSON-RPC frame that carries the tool list.
                    if (
                        isinstance(candidate, dict)
                        and isinstance(candidate.get("result"), dict)
                        and isinstance(
                            candidate["result"].get("tools"), list
                        )
                    ):
                        parsed = candidate
                        break
                if parsed is None:
                    return None
            else:
                parsed = list_response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("MCP probe tools/list parse error: %s", exc)
            return None

        if not isinstance(parsed, dict):
            return None
        result = parsed.get("result")
        if not isinstance(result, dict):
            return None
        tools = result.get("tools")
        if not isinstance(tools, list) or not tools:
            return None
        # Prefer the first non-mutating tool name (defense-in-depth
        # against servers that execute before checking auth).
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not name.startswith(_PROBE_MUTATING_TOOL_PREFIXES):
                return name
        # All tools are mutating or unnamed — skip tools/call
        return None

    @staticmethod
    def _probe_response_within_size(name: str, response: httpx.Response) -> bool:
        """Return True if the response body is within the probe size guard.

        Logs a warning and returns False if the ``Content-Length`` header or
        the actual body size exceeds ``_PROBE_MAX_RESPONSE_BYTES``.
        """
        content_length = response.headers.get("content-length")
        if (
            content_length is not None
            and int(content_length) > _PROBE_MAX_RESPONSE_BYTES
        ):
            logger.warning(
                "MCP [%s] probe tools/list response too "
                "large (%s bytes) — skipping tools/call",
                name,
                content_length,
            )
            return False
        if len(response.content) > _PROBE_MAX_RESPONSE_BYTES:
            logger.warning(
                "MCP [%s] probe tools/list response too "
                "large (%d bytes) — skipping tools/call",
                name,
                len(response.content),
            )
            return False
        return True

    async def _post_discovery_probe(
        self, name: str, server_url: str, client: httpx.AsyncClient,
        challenge_flag: list[bool],
    ) -> int | None:
        """Run the two-step POST discovery probe.

        Issues ``tools/list``, parses the first safe tool name, then issues
        ``tools/call`` with empty arguments. Returns the HTTP status code of the
        final response (``tools/call`` when run, otherwise ``tools/list``).

        Args:
            name: Server name (for logging).
            server_url: MCP server URL.
            client: Authenticated httpx client with event hooks installed.
            challenge_flag: A mutable ``[bool]`` box shared with the caller's
                event hook.  The hook sets ``challenge_flag[0] = True`` when a
                401/403 is observed.  This method re-checks the flag **after**
                the ``tools/list`` POST to detect an OAuth challenge that fired
                during that POST (intermediate 401 → retry → 200).

        Returns:
            HTTP status code of the final probe response, or ``None`` if the
            probe is skipped.
        """
        list_body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/list",
            "params": {},
        }
        list_response = await client.post(
            server_url,
            json=list_body,
            headers=_PROBE_POST_HEADERS,
        )
        final_status = list_response.status_code

        # Re-check the shared challenge flag AFTER the POST — the event hook
        # may have observed an intermediate 401 during tools/list (OAuth fired,
        # retried to 200).  If so, skip tools/call.
        # Also skip on non-200 status (e.g. 405, 500).
        if challenge_flag[0] or final_status != 200:
            return final_status

        # Defense-in-depth: reject oversized responses before parsing to avoid
        # memory exhaustion.
        if not self._probe_response_within_size(name, list_response):
            return final_status

        first_tool_name = self._extract_first_tool_name(list_response)
        if first_tool_name is None:
            logger.warning(
                "MCP [%s] probe tools/list returned no "
                "usable tool name — skipping tools/call, "
                "proceeding to session connection",
                name,
            )
            return final_status

        call_body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {
                "name": first_tool_name,
                "arguments": {},
            },
        }
        call_response = await client.post(
            server_url,
            json=call_body,
            headers=_PROBE_POST_HEADERS,
        )
        return call_response.status_code

    async def _probe_oauth_challenge(
        self, name: str, server_url: str, provider: httpx.Auth, oauth_cfg: dict
    ) -> tuple[bool, int | None, str | None]:
        """Proactively trigger the SDK's OAuth handshake via a standalone HTTP probe.

        Makes a GET request to ``server_url`` with ``auth=provider``.  If the
        server returns 401 or 403, the SDK's ``async_auth_flow`` fires the full
        handshake (discovery → registration → redirect_handler →
        callback_handler → token exchange → set_tokens → retry).  An httpx
        response event hook records whether any 401/403 was observed, since the
        returned response is the post-retry result and its status is not 401.

        If the GET probe returns 200 OK or 405 Method Not Allowed (POST-only
        servers like Gmail) without an auth challenge, a two-step discovery
        probe is issued: a POST ``tools/list`` to discover real tool names,
        followed by a POST ``tools/call`` with the first real tool name and
        empty arguments, carrying MCP streamable-http transport headers.  Both
        POSTs use the same client (auth + event hooks) so a 401 on either POST
        fires the OAuth handshake the same way.  ``_extract_first_tool_name``
        parses the ``tools/list`` response (JSON or SSE framing) and returns
        ``None`` for malformed or empty responses.  ``final_status`` is updated
        to the last POST response status before the logging block runs.

        Exceptions are caught internally so that ``probe_saw_auth_challenge``
        survives even when the OAuth flow fires (event hook sees 401) but then
        fails (e.g. callback timeout, discovery error).  The caller uses the
        flag to decide whether to suppress the session-connection fallback.

        Args:
            name: Server name (for logging).
            server_url: MCP server URL to probe.
            provider: The ``OAuthClientProvider`` (an ``httpx.Auth`` subclass).
            oauth_cfg: OAuth config dict (for the timeout).

        Returns:
            A tuple ``(probe_saw_auth_challenge, final_status, error)`` where
            ``probe_saw_auth_challenge`` is True if a 401 or 403 was observed by
            the event hook (even if the flow subsequently failed),
            ``final_status`` is the HTTP status code of the final response (or
            None if the request failed before a response), and ``error`` is a
            string describing the failure (or None on success).
        """
        probe_saw_auth_challenge = False
        # Mutable box shared with _post_discovery_probe so it can re-check
        # the flag AFTER its own tools/list POST (the nonlocal below writes
        # to probe_saw_auth_challenge, and challenge_flag[0] mirrors it).
        challenge_flag = [False]
        final_status: int | None = None
        error: str | None = None

        async def _on_response(response: httpx.Response) -> None:
            nonlocal probe_saw_auth_challenge
            if response.status_code in (401, 403):
                probe_saw_auth_challenge = True
                challenge_flag[0] = True

        timeout = oauth_cfg.get("timeout", 300)
        logger.info(
            "MCP [%s] proactive OAuth probe starting (url=%s)", name, server_url
        )
        try:
            async with httpx.AsyncClient(
                auth=provider,
                timeout=timeout,
                event_hooks={"response": [_on_response]},
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    server_url,
                    headers=_PROBE_GET_HEADERS,
                )
                final_status = response.status_code
                # Two-step discovery probe (design D1): when the GET probe
                # returns 200 or 405 without an auth challenge, POST
                # tools/list to discover real tool names, then POST tools/call
                # with the first real tool name and empty arguments.  Some
                # MCP servers (e.g. Gmail) enforce auth at the tool execution
                # layer — a dummy tool name returns 200 (tool not found)
                # instead of 401.  The tools/call 401 fires the SDK's
                # async_auth_flow via the event hook.
                if not probe_saw_auth_challenge and final_status in (200, 405):
                    final_status = None
                    final_status = await self._post_discovery_probe(
                        name, server_url, client, challenge_flag,
                    )
        except Exception as exc:
            error = str(exc)
            if probe_saw_auth_challenge:
                logger.warning(
                    "MCP [%s] OAuth probe failed after auth challenge: %s",
                    name,
                    exc,
                )
            else:
                logger.warning(
                    "MCP [%s] OAuth probe failed: %s — proceeding to session "
                    "connection as fallback",
                    name,
                    exc,
                )
            return probe_saw_auth_challenge, final_status, error

        if probe_saw_auth_challenge:
            logger.info(
                "MCP [%s] probe triggered OAuth handshake (status=%s)", name, final_status
            )
        elif final_status == 200:
            logger.info(
                "MCP [%s] probe returned 200 — server did not require OAuth", name
            )
        else:
            logger.info(
                "MCP [%s] probe returned %s", name, final_status
            )
        return probe_saw_auth_challenge, final_status, error

    async def _run_probe_step(
        self, name: str, cfg: dict, provider: httpx.Auth, oauth_cfg: dict
    ) -> tuple[bool, bool, dict | None]:
        """Run the proactive OAuth probe, racing it against operator cancel.

        Creates the probe task and the cancel watcher, then races them with
        ``asyncio.wait(FIRST_COMPLETED)``.  Handles all probe outcomes:

        - Cancel wins → returns ``(True, False, cancel_result)``.
        - Probe saw auth challenge but failed → returns
          ``(False, True, error_result)`` — caller should NOT fall back to
          session connection (Rec 2: double auth-link prevention).
        - Probe failed without auth challenge → logs WARNING, returns
          ``(False, False, None)`` — caller should fall back to session.
        - Probe succeeded with unexpected non-200 status → logs WARNING,
          returns ``(False, saw_challenge, None)`` — caller falls back.
        - Probe succeeded normally → returns ``(False, saw_challenge, None)``.

        Args:
            name: Server name (for logging).
            cfg: Server config dict (must contain ``url``).
            provider: The ``OAuthClientProvider`` (an ``httpx.Auth`` subclass).
            oauth_cfg: OAuth config dict.

        Returns:
            ``(cancelled, probe_saw_auth_challenge, error_result)`` where
            ``error_result`` is a failure dict the caller should return
            directly, or None to proceed to session connection.
        """
        probe_task = asyncio.create_task(
            self._probe_oauth_challenge(name, cfg["url"], provider, oauth_cfg)
        )
        cancel_task = asyncio.create_task(self._watch_cancel())

        done, pending = await asyncio.wait(
            {probe_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done and self._oauth_cancel_requested:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
            return True, False, {"success": False, "error": "Cancelled by operator"}

        probe_saw_auth_challenge = False

        if probe_task in done:
            exc = probe_task.exception()
            if exc is not None:
                logger.warning(
                    "MCP [%s] OAuth probe task error: %s — proceeding to "
                    "session connection as fallback",
                    name,
                    exc,
                )
            else:
                probe_saw_auth_challenge, final_status, probe_error = (
                    probe_task.result()
                )
                if probe_error is not None:
                    if probe_saw_auth_challenge:
                        logger.warning(
                            "MCP [%s] OAuth probe failed after auth "
                            "challenge: %s",
                            name,
                            probe_error,
                        )
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return (
                            False,
                            True,
                            {
                                "success": False,
                                "error": f"OAuth probe failed after auth challenge: {probe_error}",
                            },
                        )
                    logger.warning(
                        "MCP [%s] OAuth probe failed: %s — proceeding to "
                        "session connection as fallback",
                        name,
                        probe_error,
                    )
                    if final_status is not None and final_status != 200:
                        logger.warning(
                            "MCP [%s] probe returned unexpected status %s — "
                            "proceeding to session connection as fallback",
                            name,
                            final_status,
                        )
                else:
                    if (
                        not probe_saw_auth_challenge
                        and final_status is not None
                        and final_status != 200
                    ):
                        logger.warning(
                            "MCP [%s] probe returned unexpected status %s — "
                            "proceeding to session connection as fallback",
                            name,
                            final_status,
                        )

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        return False, probe_saw_auth_challenge, None

    async def _await_session_ready(
        self, wrapper: _SdkClientWrapper
    ) -> tuple[bool, str | None]:
        """Wait for the wrapper's session to become ready or be cancelled.

        Args:
            wrapper: The SDK wrapper whose ``_ready_future`` to watch.

        Returns:
            A tuple ``(cancelled, error)``. ``cancelled`` is True if the flow
            was cancelled before the session became ready. ``error`` is a human
            readable failure message, or ``None`` when the session is ready.
        """
        cancel_task = asyncio.create_task(self._watch_cancel())
        ready_task = asyncio.ensure_future(
            asyncio.wrap_future(wrapper.ready_future)
        )

        done, pending = await asyncio.wait(
            {ready_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if cancel_task in done and self._oauth_cancel_requested:
            return True, None

        if ready_task in done:
            exc = ready_task.exception()
            if exc is not None:
                return False, str(exc)
            return False, None

        return False, "Unexpected state in OAuth flow"

    def _verify_token_persisted(
        self, name: str, probe_saw_challenge: bool
    ) -> dict | None:
        """Verify that the OAuth token file was written after a flow.

        Args:
            name: Server name (for logging and the token file path).
            probe_saw_challenge: Whether the probe observed a 401/403.

        Returns:
            An error dict if the token file is missing and the server required
            OAuth, or ``None`` when no token verification issue is detected.
        """
        if self._mcp_tokens_dir is None:
            return None
        token_file = self._mcp_tokens_dir / f"{name}.json"
        if token_file.exists():
            return None
        if probe_saw_challenge:
            logger.warning(
                "MCP [%s] OAuth flow returned success but no token file found — "
                "redirect_handler may not have fired. "
                "Retry `/mcp auth %s`.",
                name,
                name,
            )
            return {
                "success": False,
                "error": (
                    "OAuth flow completed but no token was stored — "
                    "the authorization link may not have been delivered. "
                    f"Retry /mcp auth {name}."
                ),
            }
        logger.info(
            "MCP [%s] server did not require OAuth on probe; "
            "connecting without token",
            name,
        )
        return None

    def _register_wrapper(self, name: str, wrapper: _SdkClientWrapper) -> None:
        """Register a wrapper's tools in the manager registry.

        Replaces any existing wrapper for ``name`` and maps each tool name to
        the server, warning on conflicts. Existing wrapper is closed before
        replacement.
        """
        with self._lock:
            old = self._wrappers.get(name)
            if old is not None:
                old.close()
            self._wrappers[name] = wrapper
            for tool in wrapper.tools:
                if tool.name in self._tool_to_server:
                    logger.warning(
                        "MCP tool name conflict: '%s' claimed by both '%s' and '%s' — keeping first",
                        tool.name,
                        self._tool_to_server[tool.name],
                        name,
                    )
                else:
                    self._tool_to_server[tool.name] = name

    async def _run_oauth_flow(self, name: str, cfg: dict, oauth_cfg: dict) -> dict:
        """Run the interactive OAuth flow by connecting a real session with auth=provider.

        The SDK's ``async_auth_flow`` generator drives the full handshake
        (discovery → redirect_handler → callback_handler → token exchange →
        set_tokens → retry) as a side effect of the HTTP request hitting a 401.
        """
        logger.info("MCP [%s] interactive OAuth flow starting", name)
        cb_server = mcp_oauth.CallbackServer(
            port=oauth_cfg.get("callback_port", 8000),
            bind=oauth_cfg.get("callback_bind", "0.0.0.0"),
            cert_path=oauth_cfg["cert_path"],
            key_path=oauth_cfg["key_path"],
            loop=asyncio.get_running_loop(),
        )
        session_task: asyncio.Task | None = None
        try:
            await cb_server.start()

            provider = mcp_oauth.OAuthProviderFactory.build(
                cfg,
                self._mcp_tokens_dir,
                cb_server=cb_server,
                tg_iface=self._tg_iface,
                chat_id=self._oauth_chat_id,
            )

            # --- Proactive OAuth probe (D1) ---
            # Race the probe against operator cancel.  The helper handles all
            # probe outcomes (cancel, auth-challenge failure, unexpected status,
            # success) and returns (cancelled, saw_challenge, error_result).
            cancelled, probe_saw_auth_challenge, error_result = (
                await self._run_probe_step(name, cfg, provider, oauth_cfg)
            )
            if cancelled or error_result is not None:
                return error_result or {
                    "success": False,
                    "error": "Cancelled by operator",
                }

            # --- Session connection ---
            # Create a wrapper that will connect with the OAuth provider.
            # The SDK's async_auth_flow fires when session.initialize() hits a 401,
            # driving the full handshake: redirect → callback → token exchange →
            # set_tokens → retry with Bearer token.
            wrapper = _SdkClientWrapper(cfg, self._loop)
            wrapper.configure(
                mcp_tokens_dir=self._mcp_tokens_dir,
                tg_iface=self._tg_iface,
            )
            wrapper.set_oauth(provider, interactive=True)

            session_task = wrapper.create_session_runner()
            cancelled, ready_error = await self._await_session_ready(wrapper)
            if cancelled:
                return {"success": False, "error": "Cancelled by operator"}
            if ready_error is not None:
                logger.warning(
                    "MCP [%s] OAuth session failed: %s",
                    name,
                    ready_error,
                    exc_info=True,
                )
                return {"success": False, "error": ready_error}

            # Session is ready — the SDK has completed the OAuth flow and
            # persisted tokens via storage.set_tokens().
            logger.info("MCP [%s] session ready; verifying token storage", name)
            if wrapper.needs_auth:
                logger.warning(
                    "MCP [%s] server still needs auth after OAuth flow",
                    name,
                )
                return {
                    "success": False,
                    "error": "Server still needs auth after flow",
                }

            token_error = self._verify_token_persisted(name, probe_saw_auth_challenge)
            if token_error is not None:
                return token_error

            wrapper.connected = True
            # Hand off the session task to the wrapper so it owns its
            # lifecycle (close_all, close, set_enabled).  Null out the
            # local so the ``finally`` block does NOT cancel the live
            # session runner — it must keep running to serve tool calls.
            wrapper.set_task(session_task)
            session_task = None
            self._register_wrapper(name, wrapper)
            return {"success": True}
        except Exception as exc:
            logger.warning(
                "MCP [%s] OAuth flow failed: %s",
                name,
                exc,
                exc_info=True,
            )
            return {"success": False, "error": str(exc)}
        finally:
            self._oauth_cancel_requested = False
            # Always cancel and await the session runner so its context
            # managers (transport, ClientSession) finalize cleanly.
            if session_task is not None and not session_task.done():
                session_task.cancel()
                await asyncio.gather(session_task, return_exceptions=True)
            try:
                await cb_server.stop()
            except Exception as exc:
                logger.warning("MCP [%s] callback server stop error: %s", name, exc)
