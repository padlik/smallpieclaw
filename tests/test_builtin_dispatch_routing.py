"""Routing/dispatch-table invariants for BuiltinExecutor.

Change: split-builtin-executor-modules. These white-box tests guard the
dispatch *framework* against table drift as tool bodies move into the
``builtin_tools`` package across later phases:

  * the frozen 15-tool built-in set,
  * the 14-entry ``_exec_table`` (every tool except ``vision_query``),
  * the 6-tool ``_run_table`` (the confirmation-capable tools),
  * the ``vision_query`` seam (declared built-in, no dispatch handler), and
  * total dispatch: an unknown tool name yields an error result, never raises.
"""

from __future__ import annotations

from builtin_executor import BUILTIN_TOOLS, BuiltinExecutor

_FROZEN_TOOLS = frozenset({
    "secret_get",
    "shell",
    "file_read",
    "file_write",
    "file_diff",
    "file_send",
    "schedule",
    "spawn_agent",
    "get_agent_result",
    "memory_write",
    "vision_query",
    "file_patch",
    "memory_graph_search",
    "memory_graph_store",
    "log_query",
})

_CONFIRMATION_TOOLS = frozenset({
    "shell",
    "file_read",
    "file_write",
    "file_patch",
    "memory_graph_store",
    "secret_get",
})


def _executor() -> BuiltinExecutor:
    """Construct an executor with defaults (no external collaborators wired)."""
    return BuiltinExecutor()


def test_builtin_set_is_frozen_fifteen():
    assert len(_FROZEN_TOOLS) == 15
    assert set(BUILTIN_TOOLS) == set(_FROZEN_TOOLS)


def test_exec_table_covers_every_non_vision_tool():
    ex = _executor()
    assert set(ex._exec_table) == set(_FROZEN_TOOLS) - {"vision_query"}
    assert len(ex._exec_table) == 14


def test_run_table_is_exactly_the_six_confirmation_tools():
    ex = _executor()
    assert set(ex._run_table) == set(_CONFIRMATION_TOOLS)


def test_vision_query_is_declared_but_has_no_dispatch_handler():
    ex = _executor()
    assert ex.is_builtin("vision_query") is True
    assert "vision_query" not in ex._exec_table
    assert "vision_query" not in ex._run_table


def test_unknown_tool_returns_error_result_without_raising():
    ex = _executor()
    result = ex.execute("no_such_tool_xyz", {})
    assert result["success"] is False
    assert result["error"]
