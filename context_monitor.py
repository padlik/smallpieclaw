"""
context_monitor.py
------------------
Lightweight, lock-free context-window profiler for the agent runtime.

Provides an immutable :class:`ContextSnapshot` dataclass that captures the
current token budget (system prompt, chat history, tool definitions grouped by
server, headroom, danger level, etc.) and a :class:`ContextMonitor` that
publishes and serves these snapshots via atomic reference swaps.

The module is intentionally decoupled from the rest of the agent code so it can
be constructed in ``main.py`` and injected into the controller, the ReAct loop,
and the built-in tool executor without introducing circular dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from token_estimator import estimate_tokens


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable snapshot of context-window consumption at a single turn.

    Attributes are ordered to match the dashboard layout: token counts first,
    then budget/threshold values, then derived status, and finally run state.
    """

    system_prompt_tokens: int
    chat_history_tokens: int
    tool_defs_tokens: int
    tool_defs_by_server: dict[str, int]
    completion_reserve: int
    effective_window: int
    compaction_threshold: int
    headroom_nominal: int
    headroom_real: int
    danger_level: str
    is_live: bool
    turn: int


class ContextMonitor:
    """Lock-free publisher/reader for the most recent :class:`ContextSnapshot`.

    Because :class:`ContextSnapshot` is frozen (immutable) and CPython's GIL
    makes the reference assignment atomic, no lock is required. Readers either
    see the previous snapshot or the new one; partial mutations are impossible.
    """

    def __init__(self) -> None:
        """Create a monitor with no snapshot published yet."""
        self._snapshot: ContextSnapshot | None = None

    def publish(self, snapshot: ContextSnapshot) -> None:
        """Publish *snapshot* by atomic reference swap.

        Args:
            snapshot: The immutable snapshot to expose to readers.
        """
        self._snapshot = snapshot

    def read(self) -> ContextSnapshot | None:
        """Return the current snapshot, or ``None`` if none has been published.

        Returns:
            The most recently published :class:`ContextSnapshot`, or ``None``.
        """
        return self._snapshot


def compute_danger_level(total_tokens: int, compaction_threshold: int) -> str:
    """Classify *total_tokens* relative to *compaction_threshold*.

    Buckets:
      - ``"safe"`` when total is below 70% of the threshold.
      - ``"approaching"`` when total is 70% or above but below 90%.
      - ``"danger"`` when total is 90% or above, or when the threshold is not
        positive.

    Args:
        total_tokens: Estimated consumed tokens.
        compaction_threshold: Token budget that triggers compaction.

    Returns:
        One of ``"safe"``, ``"approaching"``, or ``"danger"``.
    """
    if compaction_threshold <= 0:
        return "danger"
    ratio = total_tokens / compaction_threshold
    if ratio < 0.7:
        return "safe"
    if ratio < 0.9:
        return "approaching"
    return "danger"


def compute_headroom_real(
    threshold: int,
    system_tokens: int,
    history_tokens: int,
    tool_defs_tokens: int,
) -> int:
    """Compute remaining headroom after fixed-size context pieces.

    Args:
        threshold: Effective compaction threshold.
        system_tokens: Tokens consumed by the system prompt.
        history_tokens: Tokens consumed by chat history.
        tool_defs_tokens: Tokens consumed by tool definitions.

    Returns:
        ``threshold - system_tokens - history_tokens - tool_defs_tokens``.
        May be negative.
    """
    return threshold - system_tokens - history_tokens - tool_defs_tokens


def group_tool_defs_by_server(
    tool_defs: list[dict] | None,
    tool_registry: Any | None,
    mcp_manager: Any | None,
) -> dict[str, int]:
    """Group OpenAI-format tool definitions by owning server and estimate tokens.

    The returned dict is seeded with ``"builtin"`` and every registered MCP
    server name so that empty servers still appear with a count of zero. Each
    tool definition is cross-referenced against *tool_registry* to determine
    whether it is builtin or MCP and which server owns it. Definitions not
    present in the registry are grouped under ``"unknown"``.

    Token counts per group are estimated from the JSON-serialised definitions
    via :func:`token_estimator.estimate_tokens`.

    Args:
        tool_defs: OpenAI tool-definition list, each entry shaped like
            ``{"type": "function", "function": {"name": "...", ...}}``.
        tool_registry: Registry used to look up tool ownership. When ``None``,
            all definitions are classified as ``"unknown"``.
        mcp_manager: Manager exposing ``list_servers()``, each result a dict
            with a ``"name"`` key. When ``None``, MCP servers are not seeded.

    Returns:
        Mapping from server name (or ``"builtin"``/``"unknown"``) to estimated
        token count. If *tool_defs* is ``None`` or empty, all seeded groups
        still appear with count ``0``.
    """
    groups: dict[str, list[dict]] = {}

    # Seed builtin group.
    groups["builtin"] = []

    # Seed MCP server groups when a manager is available.
    if mcp_manager is not None:
        try:
            servers = mcp_manager.list_servers()
        except Exception:  # noqa: BLE001 — defensive against misbehaving manager
            servers = []
        for server in servers or []:
            if isinstance(server, dict):
                name = server.get("name")
                if name and isinstance(name, str):
                    groups.setdefault(name, [])

    if not tool_defs:
        return {name: 0 for name in groups}

    for tool_def in tool_defs:
        if not isinstance(tool_def, dict):
            continue
        function = tool_def.get("function")
        if not isinstance(function, dict):
            continue
        tool_name = function.get("name")
        if not isinstance(tool_name, str):
            continue

        group_name = "unknown"
        if tool_registry is not None:
            tool = tool_registry.get(tool_name)
            if tool is not None:
                if tool.is_mcp and tool.server_name:
                    group_name = tool.server_name
                else:
                    group_name = "builtin"

        groups.setdefault(group_name, []).append(tool_def)

    return {
        name: estimate_tokens(json.dumps(group_defs)) if group_defs else 0
        for name, group_defs in groups.items()
    }
