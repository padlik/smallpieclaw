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


class ToolError(AgentError):
    """Tool execution failure — timeout, crash, invalid output."""


class MCPError(AgentError):
    """MCP server communication failure."""


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class SecurityError(AgentError):
    """Security violation — unauthorized access, dangerous operation blocked."""
