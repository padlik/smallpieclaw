"""Integration tests for graph memory wiring.

Verifies that:
- When graph memory is disabled (default), ReactContext.graph_memory is None
- When graph memory is enabled and ladybug available, store/writer are wired
- BuiltinExecutor tools return graceful errors when graph memory is absent
- BuiltinExecutor tools return results when graph memory is wired
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from builtin_executor import BuiltinExecutor
from react_loop import ReactContext


# ---------------------------------------------------------------------------
# ReactContext — graph memory defaults
# ---------------------------------------------------------------------------

class TestReactContextGraphMemoryDefaults:
    def test_graph_memory_defaults_to_none(self, minimal_config):
        ctx = ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
            builtin_executor=MagicMock(),
            mcp_manager=None,
            skill_registry=None,
        )
        assert ctx.graph_memory is None
        assert ctx.graph_memory_writer is None
        assert ctx.graph_memory_max_entries == 10

    def test_graph_memory_can_be_set(self, minimal_config):
        mock_store = MagicMock()
        mock_writer = MagicMock()
        ctx = ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
            builtin_executor=MagicMock(),
            mcp_manager=None,
            skill_registry=None,
            graph_memory=mock_store,
            graph_memory_writer=mock_writer,
            graph_memory_max_entries=5,
        )
        assert ctx.graph_memory is mock_store
        assert ctx.graph_memory_writer is mock_writer
        assert ctx.graph_memory_max_entries == 5


# ---------------------------------------------------------------------------
# BuiltinExecutor — memory_graph_search / memory_graph_store without graph memory
# ---------------------------------------------------------------------------

class TestBuiltinExecutorGraphMemoryDisabled:
    @pytest.fixture
    def builtin(self, tmp_path):
        return BuiltinExecutor(data_dir=str(tmp_path))

    def test_memory_graph_search_no_store(self, builtin):
        result = builtin.execute("memory_graph_search", {"query": "test"})
        assert result["success"] is False
        assert "not enabled" in result["error"].lower() or "not available" in result["error"].lower()

    def test_memory_graph_store_no_store(self, builtin):
        result = builtin.execute("memory_graph_store", {"content": "test fact"})
        assert result["success"] is False
        assert "not enabled" in result["error"].lower() or "not available" in result["error"].lower()

    def test_memory_graph_search_missing_query(self, builtin):
        builtin._graph_memory = MagicMock()
        result = builtin.execute("memory_graph_search", {})
        assert result["success"] is False
        assert "query" in result["error"].lower()

    def test_memory_graph_store_missing_content(self, builtin):
        builtin._graph_memory = MagicMock()
        builtin._graph_memory_writer = None
        result = builtin.execute("memory_graph_store", {})
        assert result["success"] is False
        assert "content" in result["error"].lower()


# ---------------------------------------------------------------------------
# BuiltinExecutor — memory_graph_search / memory_graph_store with mock store
# ---------------------------------------------------------------------------

class TestBuiltinExecutorGraphMemoryEnabled:
    @pytest.fixture
    def builtin_with_graph_memory(self, tmp_path):
        b = BuiltinExecutor(data_dir=str(tmp_path))
        mock_store = MagicMock()
        mock_store.format_for_prompt.return_value = "KNOWLEDGE GRAPH CONTEXT:\n  • Alice (person)"
        mock_store.add_episode.return_value = "ep:123"
        b._graph_memory = mock_store
        mock_writer = MagicMock()
        b._graph_memory_writer = mock_writer
        return b, mock_store, mock_writer

    def test_memory_graph_search_returns_context(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        result = b.execute("memory_graph_search", {"query": "Alice preferences"})
        assert result["success"] is True
        assert "Alice" in result["output"]
        store.format_for_prompt.assert_called_once_with("Alice preferences")

    def test_memory_graph_search_empty_returns_not_found_message(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        store.format_for_prompt.return_value = ""
        result = b.execute("memory_graph_search", {"query": "unknown entity"})
        assert result["success"] is True
        assert "no relevant" in result["output"].lower()

    def test_memory_graph_store_enqueues_and_returns_ok(self, builtin_with_graph_memory):
        b, store, writer = builtin_with_graph_memory
        result = b.execute("memory_graph_store", {"content": "Alice prefers dark mode.", "entity_type": "preference"})
        assert result["success"] is True
        assert "ep:123" in result["output"]
        store.add_episode.assert_called_once()
        writer.enqueue.assert_called_once()
        writer.flush.assert_called_once()

    def test_memory_graph_search_store_exception_handled(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        store.format_for_prompt.side_effect = RuntimeError("DB error")
        result = b.execute("memory_graph_search", {"query": "anything"})
        assert result["success"] is False
        assert "failed" in result["error"].lower()


# ---------------------------------------------------------------------------
# is_builtin recognises graph memory tools
# ---------------------------------------------------------------------------

class TestBuiltinRecognition:
    def test_memory_graph_search_is_builtin(self, tmp_path):
        b = BuiltinExecutor(data_dir=str(tmp_path))
        assert b.is_builtin("memory_graph_search")

    def test_memory_graph_store_is_builtin(self, tmp_path):
        b = BuiltinExecutor(data_dir=str(tmp_path))
        assert b.is_builtin("memory_graph_store")

    def test_all_tools_includes_graph_memory(self, tmp_path):
        b = BuiltinExecutor(data_dir=str(tmp_path))
        names = {t.name for t in b.all_tools()}
        assert "memory_graph_search" in names
        assert "memory_graph_store" in names
