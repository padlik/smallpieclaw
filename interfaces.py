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

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

import httpx
from model_registry import ModelRecord


# ---------------------------------------------------------------------------
# Provider Backend Context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderContext:
    """Narrow pinned-model context passed to a concrete provider adapter."""

    model: ModelRecord
    adapter: str
    mode: str
    send_json: Callable[[dict[str, Any]], httpx.Response]
    track_usage: Callable[[int, int], None]
    caller_tag: str = ""
    cancel_event: Optional[threading.Event] = None
    ollama_client: Any = field(default=None, repr=False)
    graph_extraction: bool = False


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

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text string."""
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for multiple text strings.

        Default implementation loops over :meth:`embed` for backward
        compatibility with Protocol implementors that do not provide a native
        batch endpoint.
        """
        return [self.embed(text) for text in texts]


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
    """Contract for querying available tools registered via MCP."""

    def list_tools(self) -> list[Any]:
        """Return all registered tools."""
        ...

    def get_tool(self, name: str) -> Optional[Any]:
        """Get a specific tool by name, or None."""
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


# ---------------------------------------------------------------------------
# Graph preparation
# ---------------------------------------------------------------------------

@runtime_checkable
class PreparedGraphContext(Protocol):
    """Read-only graph recall whose usage counters are committed explicitly."""

    @property
    def text(self) -> str:
        """Return the prompt section that was recalled (may be empty)."""
        ...

    def commit_metrics(self) -> None:
        """Record this recall in the store's usage counters, exactly once."""
        ...


@runtime_checkable
class GraphPromptPreparer(Protocol):
    """Graph store capability: recall prompt context without counting it yet."""

    def prepare_for_prompt(self, query: str, max_entries: int = 10) -> PreparedGraphContext:
        """Retrieve context for ``query`` without mutating usage counters."""
        ...
