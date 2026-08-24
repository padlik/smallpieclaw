"""Tests for :mod:`context_monitor`.

Covers OpenSpec change ``context-profile-command`` tasks 1.1-1.6:

- immutable snapshot publishing and reading
- concurrent read-while-publish safety
- default empty state before first publish
- danger-level classification
- real headroom computation
- tool-definition grouping by server with builtin/MCP/unknown classification
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from context_monitor import (
    ContextMonitor,
    ContextSnapshot,
    compute_danger_level,
    compute_headroom_real,
    group_tool_defs_by_server,
)
from token_estimator import estimate_tokens


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeTool:
    """Lightweight stand-in for :class:`tool_registry.Tool`."""

    name: str
    is_mcp: bool = False
    server_name: str = ""


class FakeToolRegistry:
    """Stand-in for :class:`tool_registry.ToolRegistry`."""

    def __init__(self, tools: list[FakeTool] | None = None) -> None:
        self._tools = {t.name: t for t in (tools or [])}

    def get(self, name: str) -> FakeTool | None:
        return self._tools.get(name)


class FakeMcpManager:
    """Stand-in for the MCP manager providing ``list_servers()``."""

    def __init__(self, servers: list[dict[str, Any]] | None = None) -> None:
        self._servers = servers or []

    def list_servers(self) -> list[dict[str, Any]]:
        return list(self._servers)


# ---------------------------------------------------------------------------
# ContextMonitor publish/read
# ---------------------------------------------------------------------------


def test_publish_read_returns_snapshot() -> None:
    """Publishing a snapshot makes it available via read()."""
    monitor = ContextMonitor()
    snapshot = ContextSnapshot(
        system_prompt_tokens=10,
        chat_history_tokens=20,
        tool_defs_tokens=30,
        tool_defs_by_server={"builtin": 30},
        completion_reserve=512,
        effective_window=4096,
        compaction_threshold=3072,
        headroom_nominal=1000,
        headroom_real=900,
        danger_level="safe",
        is_live=True,
        turn=1,
    )
    monitor.publish(snapshot)
    assert monitor.read() is snapshot


def test_read_before_publish_returns_none() -> None:
    """A fresh monitor returns None until the first snapshot is published."""
    monitor = ContextMonitor()
    assert monitor.read() is None


def test_concurrent_read_during_publish_does_not_crash() -> None:
    """Reference-swap publishing is safe while readers call read() concurrently."""
    monitor = ContextMonitor()
    base = ContextSnapshot(
        system_prompt_tokens=1,
        chat_history_tokens=1,
        tool_defs_tokens=1,
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

    publish_iterations = 200
    errors: list[Exception] = []

    def publisher() -> None:
        for i in range(publish_iterations):
            try:
                monitor.publish(
                    ContextSnapshot(
                        system_prompt_tokens=base.system_prompt_tokens + i,
                        chat_history_tokens=base.chat_history_tokens,
                        tool_defs_tokens=base.tool_defs_tokens,
                        tool_defs_by_server={"builtin": i},
                        completion_reserve=base.completion_reserve,
                        effective_window=base.effective_window,
                        compaction_threshold=base.compaction_threshold,
                        headroom_nominal=base.headroom_nominal,
                        headroom_real=base.headroom_real,
                        danger_level=base.danger_level,
                        is_live=base.is_live,
                        turn=i,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — we only care that it never crashes
                errors.append(exc)

    def reader() -> None:
        for _ in range(publish_iterations):
            try:
                snapshot = monitor.read()
                if snapshot is not None:
                    _ = snapshot.turn
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    t1 = threading.Thread(target=publisher)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    final = monitor.read()
    assert final is not None
    assert final.turn == publish_iterations - 1


# ---------------------------------------------------------------------------
# Danger level
# ---------------------------------------------------------------------------


def test_compute_danger_level_safe() -> None:
    """Below 70% of threshold is safe."""
    assert compute_danger_level(68, 100) == "safe"
    assert compute_danger_level(699, 1000) == "safe"


def test_compute_danger_level_approaching() -> None:
    """70% to below 90% of threshold is approaching."""
    assert compute_danger_level(70, 100) == "approaching"
    assert compute_danger_level(89, 100) == "approaching"
    assert compute_danger_level(899, 1000) == "approaching"


def test_compute_danger_level_danger() -> None:
    """90% or above is danger."""
    assert compute_danger_level(90, 100) == "danger"
    assert compute_danger_level(100, 100) == "danger"
    assert compute_danger_level(200, 100) == "danger"


def test_compute_danger_level_non_positive_threshold() -> None:
    """A zero or negative threshold is treated as danger."""
    assert compute_danger_level(0, 0) == "danger"
    assert compute_danger_level(1, -5) == "danger"


# ---------------------------------------------------------------------------
# Headroom
# ---------------------------------------------------------------------------


def test_compute_headroom_real_basic() -> None:
    """Headroom subtracts system, history, and tool-def token counts."""
    assert compute_headroom_real(1000, 100, 200, 300) == 400


def test_compute_headroom_real_negative() -> None:
    """Headroom may be negative when usage exceeds threshold."""
    assert compute_headroom_real(500, 200, 200, 200) == -100


# ---------------------------------------------------------------------------
# Tool-definition grouping
# ---------------------------------------------------------------------------


def make_tool_def(name: str) -> dict[str, Any]:
    """Return an OpenAI-format function tool definition."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Does {name}.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_group_tool_defs_by_server_builtin_mcp_unknown() -> None:
    """Groups are split between builtin, MCP server, and unknown."""
    builtin_def = make_tool_def("shell")
    mcp_def = make_tool_def("weather")
    unknown_def = make_tool_def("orphan")

    registry = FakeToolRegistry(
        [
            FakeTool("shell", is_mcp=False),
            FakeTool("weather", is_mcp=True, server_name="mcp-weather"),
        ]
    )
    manager = FakeMcpManager([{"name": "mcp-weather"}, {"name": "mcp-unused"}])

    result = group_tool_defs_by_server(
        [builtin_def, mcp_def, unknown_def],
        registry,
        manager,
    )

    assert set(result.keys()) == {"builtin", "mcp-weather", "mcp-unused", "unknown"}
    assert result["mcp-unused"] == 0
    shell = registry.get("shell")
    assert shell is not None
    assert result["builtin"] == estimate_tokens(
        __import__("json").dumps([builtin_def])
    )
    assert result["mcp-weather"] == estimate_tokens(
        __import__("json").dumps([mcp_def])
    )
    assert result["unknown"] == estimate_tokens(
        __import__("json").dumps([unknown_def])
    )


def test_group_tool_defs_empty_mcp_server_appears_with_zero() -> None:
    """A registered MCP server with no tools still appears in the result."""
    manager = FakeMcpManager([{"name": "empty-server"}])
    result = group_tool_defs_by_server([], None, manager)
    assert result == {"builtin": 0, "empty-server": 0}


def test_group_tool_defs_no_mcp_manager_seeds_builtin_only() -> None:
    """Without an MCP manager only the builtin group is seeded."""
    result = group_tool_defs_by_server(None, None, None)
    assert result == {"builtin": 0}


def test_group_tool_defs_no_registry_all_unknown() -> None:
    """Tools without a registry all fall into the unknown bucket."""
    defs = [make_tool_def("a"), make_tool_def("b")]
    manager = FakeMcpManager([{"name": "srv"}])
    result = group_tool_defs_by_server(defs, None, manager)
    assert set(result.keys()) == {"builtin", "srv", "unknown"}
    assert result["builtin"] == 0
    assert result["srv"] == 0
    assert result["unknown"] == estimate_tokens(
        __import__("json").dumps(defs)
    )


def test_group_tool_defs_none_or_empty_returns_seeded_zeros() -> None:
    """None or empty tool_defs returns the seeded dict with all zero counts."""
    manager = FakeMcpManager([{"name": "srv1"}, {"name": "srv2"}])
    expected = {"builtin": 0, "srv1": 0, "srv2": 0}
    assert group_tool_defs_by_server(None, None, manager) == expected
    assert group_tool_defs_by_server([], None, manager) == expected


def test_group_tool_defs_builtin_name_not_in_registry_classified_builtin() -> None:
    """A builtin name in *builtin_names* but absent from the registry is builtin."""
    shell_def = make_tool_def("shell")
    registry = FakeToolRegistry([FakeTool("weather", is_mcp=True, server_name="mcp-weather")])
    manager = FakeMcpManager()

    result = group_tool_defs_by_server(
        [shell_def],
        registry,
        manager,
        builtin_names={"shell"},
    )

    assert "builtin" in result
    assert "unknown" not in result or result["unknown"] == 0
    assert result["builtin"] == estimate_tokens(
        __import__("json").dumps([shell_def])
    )


def test_group_tool_defs_builtin_name_no_registry_classified_builtin() -> None:
    """A builtin name in *builtin_names* with no registry at all is builtin."""
    shell_def = make_tool_def("shell")
    manager = FakeMcpManager()

    result = group_tool_defs_by_server(
        [shell_def],
        None,
        manager,
        builtin_names={"shell"},
    )

    assert result == {
        "builtin": estimate_tokens(__import__("json").dumps([shell_def])),
    }


def test_group_tool_defs_unknown_name_not_in_builtin_names() -> None:
    """A tool not in the registry and not in *builtin_names* stays unknown."""
    orphan_def = make_tool_def("orphan")
    registry = FakeToolRegistry([FakeTool("shell", is_mcp=False)])
    manager = FakeMcpManager()

    result = group_tool_defs_by_server(
        [orphan_def],
        registry,
        manager,
        builtin_names={"shell"},
    )

    assert result == {
        "builtin": 0,
        "unknown": estimate_tokens(__import__("json").dumps([orphan_def])),
    }
