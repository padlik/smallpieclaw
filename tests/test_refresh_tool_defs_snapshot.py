"""Tests for :func:`telegram_commands._refresh_tool_defs_snapshot`."""

from __future__ import annotations

from unittest.mock import MagicMock

from context_monitor import ContextSnapshot, compute_danger_level, compute_headroom_real
from telegram_commands import _refresh_tool_defs_snapshot


def _make_iface(last_snapshot: ContextSnapshot | None) -> MagicMock:
    """Return a MagicMock iface wired for a successful refresh call."""
    iface = MagicMock()
    iface.agent.context_monitor.read.return_value = last_snapshot
    iface.tool_registry = MagicMock()
    iface.tool_registry.get.return_value = None
    iface.mcp_manager.get_tools.return_value = []
    iface.mcp_manager.list_servers.return_value = []
    return iface


def test_refresh_publishes_updated_snapshot() -> None:
    """An updated snapshot is published with recomputed fields and is_live False."""
    last = ContextSnapshot(
        system_prompt_tokens=100,
        chat_history_tokens=200,
        tool_defs_tokens=0,
        tool_defs_by_server={},
        completion_reserve=512,
        effective_window=4096,
        compaction_threshold=3072,
        headroom_nominal=1000,
        headroom_real=0,
        danger_level="safe",
        is_live=True,
        turn=7,
    )
    iface = _make_iface(last)

    _refresh_tool_defs_snapshot(iface)

    iface.agent.context_monitor.publish.assert_called_once()
    updated: ContextSnapshot = iface.agent.context_monitor.publish.call_args[0][0]

    assert "builtin" in updated.tool_defs_by_server
    assert updated.tool_defs_tokens == sum(updated.tool_defs_by_server.values())
    assert updated.is_live is False

    assert updated.system_prompt_tokens == last.system_prompt_tokens
    assert updated.chat_history_tokens == last.chat_history_tokens
    assert updated.completion_reserve == last.completion_reserve
    assert updated.effective_window == last.effective_window
    assert updated.compaction_threshold == last.compaction_threshold
    assert updated.turn == last.turn

    total = last.system_prompt_tokens + last.chat_history_tokens + updated.tool_defs_tokens
    assert updated.danger_level == compute_danger_level(total, last.compaction_threshold)
    assert updated.headroom_real == compute_headroom_real(
        last.compaction_threshold,
        last.system_prompt_tokens,
        last.chat_history_tokens,
        updated.tool_defs_tokens,
    )


def test_refresh_no_snapshot_no_publish() -> None:
    """When the monitor has no snapshot, publish is not called and no crash occurs."""
    iface = _make_iface(None)

    _refresh_tool_defs_snapshot(iface)

    iface.agent.context_monitor.publish.assert_not_called()


def test_refresh_guard_agent_none() -> None:
    """Returns early when the agent attribute itself is None."""
    iface = MagicMock()
    iface.agent = None

    _refresh_tool_defs_snapshot(iface)

    # No crash; no further attributes are accessed.


def test_refresh_guard_context_monitor_none() -> None:
    """Returns early when the agent has no context monitor."""
    iface = MagicMock()
    monitor = iface.agent.context_monitor
    iface.agent.context_monitor = None
    iface.tool_registry = MagicMock()
    iface.mcp_manager = MagicMock()

    _refresh_tool_defs_snapshot(iface)

    monitor.publish.assert_not_called()


def test_refresh_guard_tool_registry_none() -> None:
    """Returns early when the tool registry is missing."""
    iface = MagicMock()
    iface.agent.context_monitor = MagicMock()
    iface.tool_registry = None
    iface.mcp_manager = MagicMock()

    _refresh_tool_defs_snapshot(iface)

    iface.agent.context_monitor.read.assert_not_called()


def test_refresh_guard_mcp_manager_none() -> None:
    """Returns early when the MCP manager is missing."""
    last = ContextSnapshot(
        system_prompt_tokens=10,
        chat_history_tokens=10,
        tool_defs_tokens=0,
        tool_defs_by_server={},
        completion_reserve=1,
        effective_window=100,
        compaction_threshold=80,
        headroom_nominal=10,
        headroom_real=10,
        danger_level="safe",
        is_live=True,
        turn=0,
    )
    iface = MagicMock()
    iface.agent.context_monitor.read.return_value = last
    iface.tool_registry = MagicMock()
    iface.mcp_manager = None

    _refresh_tool_defs_snapshot(iface)

    iface.agent.context_monitor.publish.assert_not_called()


def test_refresh_attributes_mcp_server_tools() -> None:
    """Refresh correctly attributes MCP server tools to their server name."""
    from dataclasses import dataclass

    @dataclass
    class FakeMcpTool:
        name: str
        description: str
        input_schema: dict | None = None

    github_tools = [
        FakeMcpTool(name="gh_create_issue", description="Create an issue"),
        FakeMcpTool(name="gh_list_prs", description="List pull requests"),
    ]

    last = ContextSnapshot(
        system_prompt_tokens=100,
        chat_history_tokens=200,
        tool_defs_tokens=0,
        tool_defs_by_server={"builtin": 0, "github": 0},
        completion_reserve=512,
        effective_window=4096,
        compaction_threshold=3072,
        headroom_nominal=2772,
        headroom_real=2772,
        danger_level="safe",
        is_live=True,
        turn=5,
    )
    iface = _make_iface(last)
    iface.mcp_manager.get_tools.return_value = github_tools
    iface.mcp_manager.list_servers.return_value = [{"name": "github"}]

    def registry_get(name: str) -> MagicMock | None:
        if name in ("gh_create_issue", "gh_list_prs"):
            return MagicMock(is_mcp=True, server_name="github")
        return None

    iface.tool_registry.get.side_effect = registry_get

    _refresh_tool_defs_snapshot(iface)

    iface.agent.context_monitor.publish.assert_called_once()
    updated: ContextSnapshot = iface.agent.context_monitor.publish.call_args[0][0]

    assert "github" in updated.tool_defs_by_server
    assert updated.tool_defs_by_server["github"] > 0
    assert updated.tool_defs_tokens == sum(updated.tool_defs_by_server.values())
    assert updated.is_live is False
