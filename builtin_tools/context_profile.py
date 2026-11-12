"""Context-window consumption profiling for the ``context_profile`` built-in tool.

This module exposes ``exec_context_profile``, which reads the most recently
published :class:`~context_monitor.ContextSnapshot` and returns a JSON-serializable
summary of estimated input tokens (per category and per MCP server), the
configured compaction threshold, model identity, turn and liveness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from outcome_utils import with_execution

if TYPE_CHECKING:
    from context_monitor import ContextMonitor


def exec_context_profile(context_monitor: Optional["ContextMonitor"]) -> "dict[str, Any]":
    """Return a JSON snapshot of context-window consumption from the monitor.

    Reads the most recently published :class:`~context_monitor.ContextSnapshot`
    from *context_monitor* and returns a result dict suitable for the LLM tool
    response contract (``success``, token counts, threshold, identity, etc.).
    """
    if context_monitor is None:
        return with_execution({"success": False, "error": "Context monitor not available"}, "refused")
    snapshot = context_monitor.read()
    if snapshot is None:
        return with_execution({"success": False, "error": "No context snapshot available yet"}, "refused")
    return with_execution({
        "success": True,
        "total_tokens": snapshot.system_prompt_tokens + snapshot.chat_history_tokens + snapshot.tool_defs_tokens,
        "system_prompt_tokens": snapshot.system_prompt_tokens,
        "chat_history_tokens": snapshot.chat_history_tokens,
        "tool_defs_tokens": snapshot.tool_defs_tokens,
        "tool_defs_by_server": snapshot.tool_defs_by_server,
        "compaction_threshold": snapshot.compaction_threshold,
        "model_id": snapshot.model_id,
        "is_live": snapshot.is_live,
        "turn": snapshot.turn,
    }, "executed")
