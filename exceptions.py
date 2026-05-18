"""
exceptions.py
-------------
Application exception hierarchy.

Use specific exceptions instead of broad ``except Exception`` to distinguish
recoverable operational failures from programming bugs.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base exception for all agent errors."""


class LLMError(AgentError):
    """LLM communication failure — network, timeout, rate-limit, empty response."""


class LLMEmptyResponseError(LLMError):
    """LLM returned an empty or whitespace-only response."""


class LLMRateLimitError(LLMError):
    """LLM rate-limited the request (HTTP 429 or equivalent)."""


class LLMContextOverflowError(LLMError):
    """Request exceeded the model's context window."""


class ToolError(AgentError):
    """Tool execution failure — timeout, crash, invalid output."""


class ToolTimeoutError(ToolError):
    """Tool exceeded its allowed execution time."""


class MCPError(AgentError):
    """MCP server communication failure."""


class MCPConnectionError(MCPError):
    """Failed to connect to or initialise an MCP server."""


class MCPToolCallError(MCPError):
    """MCP tool invocation failed (server returned error or isError=true)."""


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class SecurityError(AgentError):
    """Security violation — unauthorized access, dangerous operation blocked."""
