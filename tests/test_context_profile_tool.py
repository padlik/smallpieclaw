"""Tests for the context_profile built-in tool.

Covers OpenSpec change ``context-profile-command`` tasks 5.1-5.6:
registration in descriptors/schemas, dispatch wiring, and the handler's
result shape.
"""

from __future__ import annotations

from builtin_executor import BuiltinExecutor
from builtin_tools.context_profile import exec_context_profile
from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS, build_tool_definitions
from context_monitor import ContextMonitor, ContextSnapshot


def _make_snapshot(**overrides) -> ContextSnapshot:
    defaults = {
        "system_prompt_tokens": 10,
        "chat_history_tokens": 20,
        "tool_defs_tokens": 30,
        "tool_defs_by_server": {"builtin": 30},
        "completion_reserve": 512,
        "effective_window": 4096,
        "compaction_threshold": 3000,
        "headroom_nominal": 100,
        "headroom_real": 70,
        "danger_level": "approaching",
        "is_live": True,
        "turn": 2,
    }
    defaults.update(overrides)
    return ContextSnapshot(**defaults)


def test_exec_context_profile_returns_expected_fields() -> None:
    """When a snapshot exists, the handler returns a success dict with all fields."""
    monitor = ContextMonitor()
    monitor.publish(_make_snapshot())

    result = exec_context_profile(monitor)

    assert result["success"] is True
    assert result["danger_level"] == "approaching"
    assert result["total_tokens"] == 10 + 20 + 30
    assert result["system_prompt_tokens"] == 10
    assert result["chat_history_tokens"] == 20
    assert result["tool_defs_tokens"] == 30
    assert result["tool_defs_by_server"] == {"builtin": 30}
    assert result["completion_reserve"] == 512
    assert result["effective_window"] == 4096
    assert result["compaction_threshold"] == 3000
    assert result["headroom_nominal"] == 100
    assert result["headroom_real"] == 70
    assert result["is_live"] is True
    assert result["turn"] == 2


def test_exec_context_profile_no_snapshot_returns_failure() -> None:
    """A fresh monitor with no snapshot yields a graceful error."""
    monitor = ContextMonitor()
    result = exec_context_profile(monitor)
    assert result["success"] is False
    assert "No context snapshot" in result["error"]


def test_exec_context_profile_no_monitor_returns_failure() -> None:
    """Passing None yields a clear error without raising."""
    result = exec_context_profile(None)
    assert result["success"] is False
    assert "Context monitor not available" in result["error"]


def test_context_profile_in_builtin_tools() -> None:
    """The descriptor registry includes context_profile."""
    assert "context_profile" in BUILTIN_TOOLS
    assert "context_profile" in BUILTIN_TOOL_SCHEMAS
    assert BUILTIN_TOOL_SCHEMAS["context_profile"]["parameters"]["properties"] == {}
    assert BUILTIN_TOOL_SCHEMAS["context_profile"]["parameters"]["required"] == []


def test_build_tool_definitions_includes_context_profile() -> None:
    """The generated OpenAI-format definitions include context_profile."""
    defs = build_tool_definitions(mcp_manager=None)
    names = {d["function"]["name"] for d in defs}
    assert "context_profile" in names


def test_builtin_executor_is_builtin_recognizes_context_profile() -> None:
    """BuiltinExecutor.is_builtin returns True for context_profile."""
    builtin = BuiltinExecutor(
        default_timeout=10,
        data_dir="data",
        memory=None,
    )
    assert builtin.is_builtin("context_profile") is True


def test_context_profile_not_confirmation_capable() -> None:
    """context_profile is not in the dangerous-tool confirmation table."""
    builtin = BuiltinExecutor(
        default_timeout=10,
        data_dir="data",
        memory=None,
    )
    assert "context_profile" not in builtin._run_table
