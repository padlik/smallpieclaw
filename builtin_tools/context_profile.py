"""Context-window consumption profiling for the ``context_profile`` built-in tool.

This module exposes ``exec_context_profile``, which reads the most recently
published :class:`~context_monitor.ContextSnapshot` and returns a JSON-serializable
summary of context-window usage (token counts, headroom, danger level, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from context_monitor import ContextMonitor


def exec_context_profile(context_monitor: Optional["ContextMonitor"]) -> "dict[str, Any]":
    """Return a JSON snapshot of context-window consumption from the monitor.

    Reads the most recently published :class:`~context_monitor.ContextSnapshot`
    from *context_monitor* and returns a result dict suitable for the LLM tool
    response contract (``success``, token counts, headroom, danger level, etc.).
    """
    if context_monitor is None:
        return {"success": False, "error": "Context monitor not available"}
    snapshot = context_monitor.read()
    if snapshot is None:
        return {"success": False, "error": "No context snapshot available yet"}
    return {
        "success": True,
        "danger_level": snapshot.danger_level,
        "total_tokens": snapshot.system_prompt_tokens + snapshot.chat_history_tokens + snapshot.tool_defs_tokens,
        "system_prompt_tokens": snapshot.system_prompt_tokens,
        "chat_history_tokens": snapshot.chat_history_tokens,
        "tool_defs_tokens": snapshot.tool_defs_tokens,
        "tool_defs_by_server": snapshot.tool_defs_by_server,
        "completion_reserve": snapshot.completion_reserve,
        "effective_window": snapshot.effective_window,
        "compaction_threshold": snapshot.compaction_threshold,
        "headroom_nominal": snapshot.headroom_nominal,
        "headroom_real": snapshot.headroom_real,
        "is_live": snapshot.is_live,
        "turn": snapshot.turn,
    }
