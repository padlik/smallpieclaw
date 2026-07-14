"""
interfaces.py
-------------
Protocol definitions for the major component boundaries.

These protocols define the contracts between modules, enabling:
- Type-safe dependency injection
- Easy mocking in tests (protocol-conforming fakes, not MagicMock)
- Potential future substitution of implementations

Usage in type hints:
    def __init__(self, llm: LLMProvider, tools: ToolBackend, ...): ...

Python's structural typing means existing classes conform to these protocols
without explicit inheritance — no code changes needed to existing classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Native Tool Calling Types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool call from a native tool-calling response."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """Unified response from chat_with_tools — either text or tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] | None = None

    @property
    def is_tool_call(self) -> bool:
        """True when the response contains structured tool calls."""
        return bool(self.tool_calls)


# ---------------------------------------------------------------------------
# LLM Provider
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMProvider(Protocol):
    """Contract for LLM communication."""

    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        progress_cb=None,
        json_mode: bool = False,
    ) -> str:
        """Send a chat request and return the response text."""
        ...

    def chat_with_fallback(
        self,
        messages: list[dict],
        system: str | None = None,
        progress_cb=None,
        json_mode: bool = False,
    ) -> str:
        """Chat with automatic fallback to alternative models on failure."""
        ...

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        progress_cb=None,
    ) -> ChatResponse:
        """
        Send a chat request with tool definitions and return a structured
        response (text or tool calls). Providers that do not support native
        tool calling should raise NotImplementedError.
        """
        ...

    def chat_with_tools_fallback(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        progress_cb=None,
    ) -> ChatResponse:
        """Chat with tools and automatic fallback to alternative models on failure."""
        ...

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text string."""
        ...


# ---------------------------------------------------------------------------
# Tool Execution
# ---------------------------------------------------------------------------

@runtime_checkable
class ToolBackend(Protocol):
    """Contract for executing a tool and returning structured output."""

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Execute a tool by name with the given arguments.

        Returns a dict with at least:
            {"output": str, "success": bool}
        May also include: {"requires_confirmation": True, "token": str, ...}
        """
        ...


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryBackend(Protocol):
    """Contract for persistent key-value memory."""

    def get(self, key: str) -> Optional[str]:
        """Retrieve a value by key, or None if not found."""
        ...

    def set(self, key: str, value: str) -> None:
        """Store a key-value pair persistently."""
        ...

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        ...

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Semantic search over stored memory. Returns (key, score) pairs."""
        ...


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

@runtime_checkable
class NotifyFn(Protocol):
    """Callable that sends a notification to the operator (Telegram)."""

    def __call__(self, message: str, *, parse_mode: str = "HTML") -> None:
        """Send a message to the operator."""
        ...


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

@runtime_checkable
class ToolRegistryProtocol(Protocol):
    """Contract for discovering and querying available tools."""

    def list_tools(self) -> list[Any]:
        """Return all registered tools."""
        ...

    def get_tool(self, name: str) -> Optional[Any]:
        """Get a specific tool by name, or None."""
        ...

    def refresh(self) -> None:
        """Re-scan tool directories and update the registry."""
        ...


# ---------------------------------------------------------------------------
# MCP Manager
# ---------------------------------------------------------------------------

@runtime_checkable
class MCPManagerProtocol(Protocol):
    """Contract for MCP server management."""

    def has_tool(self, tool_name: str) -> bool:
        """Check if any connected MCP server provides this tool."""
        ...

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool on the appropriate MCP server."""
        ...

    def list_servers(self) -> list[dict[str, Any]]:
        """List all configured MCP servers with status."""
        ...

    def get_tools(self) -> list[Any]:
        """Return all tools from all connected MCP servers."""
        ...
