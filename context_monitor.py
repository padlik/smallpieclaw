"""
context_monitor.py
------------------
Lightweight, lock-free context-window profiler for the agent runtime.

Provides an immutable :class:`ContextSnapshot` dataclass that captures the
estimated input (system prompt, chat history, tool definitions grouped by
server) and the configured compaction threshold, and a :class:`ContextMonitor` that
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

    Informational only: estimated input token counts, the configured compaction
    threshold, run state and the pinned model identity. There is no capacity,
    reserve, headroom or danger information.
    """

    system_prompt_tokens: int
    chat_history_tokens: int
    tool_defs_tokens: int
    tool_defs_by_server: dict[str, int]
    compaction_threshold: int
    is_live: bool
    turn: int
    # Canonical ID of the model pinned to the run that produced the counts.
    model_id: str = ""


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


def group_tool_defs_by_server(
    tool_defs: list[dict] | None,
    tool_registry: Any | None,
    mcp_manager: Any | None,
    builtin_names: set[str] | None = None,
) -> dict[str, int]:
    """Group OpenAI-format tool definitions by owning server and estimate tokens.

    The returned dict is seeded with ``"builtin"`` and every registered MCP
    server name so that empty servers still appear with a count of zero. Each
    tool definition is cross-referenced against *tool_registry* to determine
    whether it is builtin or MCP and which server owns it. Definitions not
    present in the registry are grouped under ``"unknown"``.

    When *builtin_names* is provided, a tool whose name is not found in the
    registry but is present in *builtin_names* is classified as ``"builtin"``
    rather than ``"unknown"``. This supports classifying built-in tools that
    are intentionally not registered in the MCP-only ``ToolRegistry``.

    Token counts per group are estimated from the JSON-serialised definitions
    via :func:`token_estimator.estimate_tokens`.

    Args:
        tool_defs: OpenAI tool-definition list, each entry shaped like
            ``{"type": "function", "function": {"name": "...", ...}}``.
        tool_registry: Registry used to look up tool ownership. When ``None``,
            all definitions are classified as ``"unknown"`` (unless
            *builtin_names* matches).
        mcp_manager: Manager exposing ``list_servers()``, each result a dict
            with a ``"name"`` key. When ``None``, MCP servers are not seeded.
        builtin_names: Optional set of known built-in and pseudo-tool names.
            When the registry returns ``None`` for a tool name, the name is
            checked against this set; a match classifies the tool as
            ``"builtin"``. ``None`` preserves the legacy behaviour (fall
            through to ``"unknown"``).

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
            elif builtin_names is not None and tool_name in builtin_names:
                group_name = "builtin"
        elif builtin_names is not None and tool_name in builtin_names:
            group_name = "builtin"

        groups.setdefault(group_name, []).append(tool_def)

    return {
        name: estimate_tokens(json.dumps(group_defs)) if group_defs else 0
        for name, group_defs in groups.items()
    }
