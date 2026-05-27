"""
mcp_client.py
-------------
MCP (Model Context Protocol) client support.

Two transport modes:
  stdio — spawn a subprocess, JSON-RPC 2.0 over stdin/stdout
  http  — JSON-RPC 2.0 POST to an HTTP(S) endpoint (streamable HTTP transport)

Usage:
    manager = MCPManager(server_configs)   # list of dicts from [[mcp_servers]] in config.toml
    manager.connect_all()                  # connect enabled servers, log errors
    tools = manager.get_tools()            # list[Tool] — register into ToolRegistry
    outcome = manager.call_tool(name, args)
    manager.close_all()
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import threading
import time
from typing import Optional

import requests

from exceptions import MCPConnectionError, MCPToolCallError
from tool_registry import Tool

logger = logging.getLogger(__name__)

_MCP_STDIO_ERRORS = (
    MCPConnectionError,
    MCPToolCallError,
    OSError,
    ValueError,
    json.JSONDecodeError,
    subprocess.SubprocessError,
)
_MCP_HTTP_ERRORS = (
    MCPConnectionError,
    MCPToolCallError,
    requests.RequestException,
    ValueError,
    json.JSONDecodeError,
)

# MCP protocol version we advertise in initialize
_MCP_PROTOCOL_VERSION = "2024-11-05"

# Client info sent in initialize
_CLIENT_INFO = {"name": "smallpieclaw", "version": "1.0"}

# Patterns for classifying MCP server stderr lines — word-boundary aware
_STDERR_ERROR_RE = re.compile(r'\b(ERROR|CRITICAL)\b', re.IGNORECASE)
_STDERR_WARN_RE  = re.compile(r'\bWARN(?:ING)?\b', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_outcome(output: str = "", error: str = "", success: bool = True) -> dict:
    return {"success": success, "output": output, "error": error, "exit_code": 0 if success else 1}


def _mcp_tools_to_registry(server_name: str, raw_tools: list[dict]) -> list[Tool]:
    """Convert MCP tools/list response entries into Tool objects."""
    result = []
    for t in raw_tools:
        name = t.get("name", "")
        if not name:
            continue
        description = t.get("description") or f"MCP tool '{name}' from {server_name}"
        schema = t.get("inputSchema") or {}
        result.append(Tool(
            name=name,
            path="",
            language="mcp",
            description=description,
            is_mcp=True,
            server_name=server_name,
            input_schema=schema,
        ))
    return result


def _extract_mcp_result(response: dict) -> str:
    """Extract text output from a tools/call MCP response."""
    result = response.get("result", {})
    is_error = result.get("isError", False)
    content = result.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "text":
                parts.append(item.get("text", ""))
            elif item_type == "error":
                parts.append(f"[error] {item.get('text', '')}")
            elif item_type == "image":
                mime = item.get("mimeType", "image")
                parts.append(f"[image: {mime}]")
            elif item_type == "resource":
                uri = (item.get("resource") or {}).get("uri", "")
                parts.append(f"[resource: {uri}]" if uri else "[resource]")
            # unknown types are silently skipped
        elif isinstance(item, str):
            parts.append(item)
    text = "\n".join(parts) if parts else json.dumps(result)
    if is_error:
        return text, False
    return text, True


# ---------------------------------------------------------------------------
# Base client interface
# ---------------------------------------------------------------------------

class MCPBaseClient:
    """Abstract base for MCP transport clients."""

    def __init__(self, cfg: dict) -> None:
        self.name: str = cfg["name"]
        self.timeout: int = int(cfg.get("timeout", 30))
        self._tools: list[Tool] = []
        self._last_error: str = ""
        self._connected: bool = False

    def connect(self) -> list[Tool]:
        raise NotImplementedError

    def call_tool(self, tool_name: str, args: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------

class MCPStdioClient(MCPBaseClient):
    """MCP client over stdin/stdout subprocess."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._command: list[str] = cfg["command"]
        # Merge os.environ with any extra env vars from config
        base_env = dict(os.environ)
        base_env.update(cfg.get("env") or {})
        self._env: dict = base_env
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0

    def connect(self) -> list[Tool]:
        """Spawn the subprocess, handshake, and discover tools."""
        self._last_error = ""
        try:
            self._start_process()
            self._initialize()
            raw_tools = self._list_tools()
            self._tools = _mcp_tools_to_registry(self.name, raw_tools)
            self._connected = True
            logger.info("MCP [%s] connected via stdio: %d tool(s)", self.name, len(self._tools))
            return self._tools
        except _MCP_STDIO_ERRORS as exc:
            self._last_error = str(exc)
            self._connected = False
            logger.error("MCP [%s] connect failed: %s", self.name, exc, exc_info=True)
            self._kill_process()
            return []

    def call_tool(self, tool_name: str, args: dict) -> dict:
        with self._lock:
            # Reconnect if process died
            if not self._is_alive():
                logger.warning("MCP [%s] process dead — restarting", self.name)
                try:
                    self._start_process()
                    self._initialize()
                except _MCP_STDIO_ERRORS as exc:
                    self._last_error = str(exc)
                    return _tool_outcome(error=f"MCP [{self.name}] restart failed: {exc}", success=False)
            try:
                req_id = self._next_id()
                request = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": args},
                }
                self._send(request)
                response = self._recv(req_id)
                if "error" in response:
                    err = response["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    self._last_error = msg
                    logger.error("MCP [%s] tool '%s' error: %s", self.name, tool_name, msg)
                    return _tool_outcome(error=msg, success=False)
                text, ok = _extract_mcp_result(response)
                if not ok:
                    self._last_error = text
                    logger.warning("MCP [%s] tool '%s' returned isError=true: %s", self.name, tool_name, text[:200])
                else:
                    logger.info("MCP [%s] tool '%s' ok (%d chars)", self.name, tool_name, len(text))
                return _tool_outcome(output=text, success=ok)
            except _MCP_STDIO_ERRORS as exc:
                self._last_error = str(exc)
                logger.error("MCP [%s] call_tool '%s' exception: %s", self.name, tool_name, exc, exc_info=True)
                return _tool_outcome(error=str(exc), success=False)

    def close(self) -> None:
        self._connected = False
        self._kill_process()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_process(self) -> None:
        self._kill_process()
        logger.debug("MCP [%s] starting: %s", self.name, self._command)
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
        )
        # Drain stderr in background thread
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name=f"mcp-stderr-{self.name}").start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                if _STDERR_ERROR_RE.search(line):
                    logger.warning("MCP [%s] stderr: %s", self.name, line)
                elif _STDERR_WARN_RE.search(line):
                    logger.info("MCP [%s] stderr: %s", self.name, line)
                else:
                    logger.debug("MCP [%s] stderr: %s", self.name, line)
        except Exception:
            pass

    def _kill_process(self) -> None:
        if self._proc is not None:
            # Close stderr explicitly to unblock the _drain_stderr daemon thread
            try:
                if self._proc.stderr:
                    self._proc.stderr.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPConnectionError(f"MCP [{self.name}] process not running")
        line = json.dumps(obj) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _recv(self, expected_id: int) -> dict:
        """Read stdout lines until we get a response for expected_id."""
        if self._proc is None or self._proc.stdout is None:
            raise MCPConnectionError(f"MCP [{self.name}] process not running")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            # Fail fast if the process already died before attempting a blocking read
            if self._proc.poll() is not None:
                raise MCPConnectionError(f"MCP [{self.name}] process exited unexpectedly")
            # Use select() so readline() never blocks longer than 0.1 s
            ready, _, _ = select.select([self._proc.stdout], [], [], 0.1)
            if not ready:
                continue
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    raise MCPConnectionError(f"MCP [{self.name}] process exited unexpectedly")
                continue
            try:
                obj = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError:
                logger.debug("MCP [%s] non-JSON stdout: %s", self.name, line[:200])
                continue
            if obj.get("id") == expected_id:
                return obj
            # Notifications or other ids — log and skip
            logger.debug("MCP [%s] skipping id=%s (want %s)", self.name, obj.get("id"), expected_id)
        raise MCPConnectionError(f"MCP [{self.name}] timeout waiting for id={expected_id}")

    def _send_notification(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        obj: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            obj["params"] = params
        self._send(obj)

    def _initialize(self) -> None:
        req_id = self._next_id()
        self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": _CLIENT_INFO,
            },
        })
        resp = self._recv(req_id)
        if "error" in resp:
            raise MCPConnectionError(f"MCP [{self.name}] initialize error: {resp['error']}")
        # Log server's protocol version; warn if it differs from what we advertised
        server_version = resp.get("result", {}).get("protocolVersion", "")
        if server_version:
            if server_version != _MCP_PROTOCOL_VERSION:
                logger.warning(
                    "MCP [%s] protocol version mismatch: advertised=%s server=%s",
                    self.name, _MCP_PROTOCOL_VERSION, server_version,
                )
            else:
                logger.info("MCP [%s] protocol version: %s", self.name, server_version)
        # MCP spec (2024-11-05 §3.1): after a successful initialize response,
        # client MUST send notifications/initialized (no params required)
        self._send_notification("notifications/initialized")

    def _list_tools(self) -> list[dict]:
        """Fetch all tools, following nextCursor pagination if present."""
        tools: list[dict] = []
        cursor: Optional[str] = None
        while True:
            req_id = self._next_id()
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            self._send({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/list",
                "params": params,
            })
            resp = self._recv(req_id)
            if "error" in resp:
                raise MCPConnectionError(f"MCP [{self.name}] tools/list error: {resp['error']}")
            result = resp.get("result", {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor") or None
            if not cursor:
                break
        return tools


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class MCPHttpClient(MCPBaseClient):
    """MCP client over HTTP(S) — JSON-RPC POST to a single endpoint."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._url: str = cfg["url"]
        # MCP 2025-03-26 Streamable HTTP requires Accept: text/event-stream.
        # Include application/json so servers that respond with plain JSON still work.
        self._headers: dict = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._headers.update(cfg.get("headers") or {})
        self._env_extra: dict = cfg.get("env") or {}
        self._session = requests.Session()
        self._req_id = 0
        self._lock = threading.Lock()
        # MCP 2025-03-26: server may issue a session ID on initialize; must be
        # included in all subsequent requests and used for DELETE on close.
        self._session_id: Optional[str] = None

    def connect(self) -> list[Tool]:
        self._last_error = ""
        try:
            self._initialize()
            raw_tools = self._list_tools()
            self._tools = _mcp_tools_to_registry(self.name, raw_tools)
            self._connected = True
            logger.info("MCP [%s] connected via http: %d tool(s)", self.name, len(self._tools))
            return self._tools
        except _MCP_HTTP_ERRORS as exc:
            self._last_error = str(exc)
            self._connected = False
            self._session.close()
            self._session = requests.Session()  # fresh session for retry
            logger.error("MCP [%s] connect failed: %s", self.name, exc, exc_info=True)
            return []

    def call_tool(self, tool_name: str, args: dict) -> dict:
        with self._lock:
            try:
                req_id = self._next_id()
                response = self._post({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": args},
                })
                if "error" in response:
                    err = response["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    self._last_error = msg
                    logger.error("MCP [%s] tool '%s' error: %s", self.name, tool_name, msg)
                    return _tool_outcome(error=msg, success=False)
                text, ok = _extract_mcp_result(response)
                if not ok:
                    self._last_error = text
                    logger.warning("MCP [%s] tool '%s' returned isError=true: %s", self.name, tool_name, text[:200])
                else:
                    logger.info("MCP [%s] tool '%s' ok (%d chars)", self.name, tool_name, len(text))
                return _tool_outcome(output=text, success=ok)
            except _MCP_HTTP_ERRORS as exc:
                self._last_error = str(exc)
                logger.error("MCP [%s] call_tool '%s' exception: %s", self.name, tool_name, exc, exc_info=True)
                return _tool_outcome(error=str(exc), success=False)

    def close(self) -> None:
        self._connected = False
        # MCP 2025-03-26: send DELETE to terminate the session cleanly
        if self._session_id:
            try:
                self._session.request(
                    "DELETE", self._url,
                    headers={**self._headers, "Mcp-Session-Id": self._session_id},
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP [%s] session DELETE failed (non-fatal): %s", self.name, exc)
        self._session.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, obj: dict) -> dict:
        # Include Mcp-Session-Id if we have one (MCP 2025-03-26)
        headers = self._headers
        if self._session_id:
            headers = {**self._headers, "Mcp-Session-Id": self._session_id}
        r = self._session.post(
            self._url,
            json=obj,
            headers=headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        # Capture session ID from response if not yet set (MCP 2025-03-26)
        if not self._session_id:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
                logger.debug("MCP [%s] session ID captured: %s", self.name, sid)
        # Handle both plain JSON and SSE-wrapped responses
        ct = r.headers.get("Content-Type", "")
        if "text/event-stream" in ct:
            return self._parse_sse(r.text)
        return r.json()

    def _post_notification(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification over HTTP (no id, no response expected)."""
        obj: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            obj["params"] = params
        try:
            r = self._session.post(
                self._url,
                json=obj,
                headers=self._headers,
                timeout=self.timeout,
            )
            # Servers may return 200 or 204; both are acceptable for notifications
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP [%s] notification '%s' post error (non-fatal): %s", self.name, method, exc)

    @staticmethod
    def _parse_sse(text: str) -> dict:
        """Extract the first JSON-RPC response from an SSE body."""
        for line in text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError:
                        continue
        raise ValueError("No JSON-RPC response found in SSE stream")

    def _initialize(self) -> None:
        req_id = self._next_id()
        resp = self._post({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": _CLIENT_INFO,
            },
        })
        if "error" in resp:
            raise MCPConnectionError(f"MCP [{self.name}] initialize error: {resp['error']}")
        # Log server's protocol version; warn if it differs from what we advertised
        server_version = resp.get("result", {}).get("protocolVersion", "")
        if server_version:
            if server_version != _MCP_PROTOCOL_VERSION:
                logger.warning(
                    "MCP [%s] protocol version mismatch: advertised=%s server=%s",
                    self.name, _MCP_PROTOCOL_VERSION, server_version,
                )
            else:
                logger.info("MCP [%s] protocol version: %s", self.name, server_version)
        # MCP spec: after a successful initialize response, client MUST send
        # notifications/initialized (no params required)
        self._post_notification("notifications/initialized")

    def _list_tools(self) -> list[dict]:
        """Fetch all tools, following nextCursor pagination if present."""
        tools: list[dict] = []
        cursor: Optional[str] = None
        while True:
            req_id = self._next_id()
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            resp = self._post({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/list",
                "params": params,
            })
            if "error" in resp:
                raise MCPConnectionError(f"MCP [{self.name}] tools/list error: {resp['error']}")
            result = resp.get("result", {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor") or None
            if not cursor:
                break
        return tools


# ---------------------------------------------------------------------------
# MCPManager
# ---------------------------------------------------------------------------

class MCPManager:
    """
    Manages multiple MCP server clients.
    Provides a unified interface for tool discovery and invocation.
    """

    def __init__(self, server_configs: list[dict]) -> None:
        self._cfgs: dict[str, dict] = {cfg["name"]: cfg for cfg in server_configs if "name" in cfg}
        self._clients: dict[str, MCPBaseClient] = {}
        self._tool_to_server: dict[str, str] = {}
        # Runtime enabled state (mirrors cfg["enabled"] but overridable without writing config)
        self._enabled: dict[str, bool] = {
            name: bool(cfg.get("enabled", True))
            for name, cfg in self._cfgs.items()
        }

    def connect_all(self) -> None:
        """Connect all enabled servers. Errors are logged but don't abort startup."""
        for name, cfg in self._cfgs.items():
            if not self._enabled.get(name, True):
                logger.info("MCP [%s] skipped (disabled)", name)
                continue
            self._connect_server(name, cfg)

    def _connect_server(self, name: str, cfg: dict) -> None:
        transport = cfg.get("transport", "stdio").lower()
        try:
            if transport == "stdio":
                client: MCPBaseClient = MCPStdioClient(cfg)
            elif transport in ("http", "sse"):
                client = MCPHttpClient(cfg)
            else:
                logger.error("MCP [%s] unknown transport '%s' — skipping", name, transport)
                return
            tools = client.connect()
            self._clients[name] = client
            for tool in tools:
                if tool.name in self._tool_to_server:
                    logger.warning(
                        "MCP tool name conflict: '%s' claimed by both '%s' and '%s' — keeping first",
                        tool.name, self._tool_to_server[tool.name], name,
                    )
                else:
                    self._tool_to_server[tool.name] = name
        except (OSError, ValueError, subprocess.SubprocessError, requests.RequestException, MCPConnectionError) as exc:
            logger.error("MCP [%s] unexpected error during connect: %s", name, exc, exc_info=True)

    def close_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                client.close()
            except Exception as exc:
                logger.warning("MCP [%s] close error: %s", name, exc)
        self._clients.clear()
        self._tool_to_server.clear()

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    def call_tool(self, tool_name: str, args: dict) -> dict:
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            return _tool_outcome(error=f"MCP tool '{tool_name}' not found", success=False)
        client = self._clients.get(server_name)
        if not client:
            return _tool_outcome(error=f"MCP server '{server_name}' not connected", success=False)
        return client.call_tool(tool_name, args)

    def get_tools(self) -> list[Tool]:
        """Return all tools from all currently connected servers."""
        tools: list[Tool] = []
        for client in self._clients.values():
            tools.extend(client.tools)
        return tools

    def set_enabled(self, name: str, on: bool) -> bool:
        """Enable or disable a server. Returns False if server name not found."""
        if name not in self._cfgs:
            return False
        self._enabled[name] = on
        if on:
            # Connect if not already connected
            if name not in self._clients:
                self._connect_server(name, self._cfgs[name])
        else:
            # Disconnect and remove tools
            client = self._clients.pop(name, None)
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            # Remove tool routing entries for this server
            for tool_name in [k for k, v in self._tool_to_server.items() if v == name]:
                del self._tool_to_server[tool_name]
            logger.info("MCP [%s] disabled and disconnected", name)
        return True

    def list_servers(self) -> list[dict]:
        """Return info about all configured servers (for /mcp list)."""
        result = []
        for name, cfg in self._cfgs.items():
            transport = cfg.get("transport", "stdio").lower()
            client = self._clients.get(name)
            enabled = self._enabled.get(name, True)
            if not enabled:
                status = "off"
            elif client and client.connected:
                status = "active"
            else:
                status = "error"
            result.append({
                "name": name,
                "transport": "web" if transport in ("http", "sse") else "stdio",
                "status": status,
                "tool_count": len(client.tools) if client else 0,
                "last_error": client.last_error if client else "",
            })
        return result

    def get_server_info(self, name: str) -> Optional[dict]:
        """Return detailed info for /mcp info."""
        cfg = self._cfgs.get(name)
        if not cfg:
            return None
        transport = cfg.get("transport", "stdio").lower()
        client = self._clients.get(name)
        enabled = self._enabled.get(name, True)
        if not enabled:
            status = "off"
        elif client and client.connected:
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
            "tools": client.tools if client else [],
            "last_error": client.last_error if client else "",
        }

    def last_error(self, name: str) -> str:
        client = self._clients.get(name)
        return client.last_error if client else ""
