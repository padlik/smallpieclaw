"""
Tests for ToolIndex: embedding, caching, build/rebuild logging.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tool_index import ToolIndex
from tool_registry import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, description: str = "") -> Tool:
    return Tool(name=name, path="", language="python", description=description or f"{name} description")


def _make_registry(*tool_names: str) -> ToolRegistry:
    registry = MagicMock(spec=ToolRegistry)
    registry.all.return_value = [_make_tool(n) for n in tool_names]
    return registry


def _make_llm(vector: list[float] | None = None) -> MagicMock:
    llm = MagicMock()
    llm.embed.return_value = vector or [0.1] * 128
    return llm


# ---------------------------------------------------------------------------
# ToolIndex._load() logging
# ---------------------------------------------------------------------------

class TestLoadLogging:
    def test_load_logs_info_on_success(self, tmp_path, caplog):
        index_path = str(tmp_path / "index.json")
        data = {"tool_a": {"description": "desc", "vector": [0.1] * 128}}
        with open(index_path, "w") as f:
            json.dump(data, f)

        with caplog.at_level(logging.INFO, logger="tool_index"):
            ToolIndex(_make_registry(), _make_llm(), index_path)

        assert any(
            "Tool index loaded" in r.message and "1" in r.message
            for r in caplog.records
        ), f"Expected 'Tool index loaded' INFO; got: {[r.message for r in caplog.records]}"

    def test_load_warns_on_bad_json(self, tmp_path, caplog):
        index_path = str(tmp_path / "index.json")
        with open(index_path, "w") as f:
            f.write("not valid json{")

        with caplog.at_level(logging.WARNING, logger="tool_index"):
            ti = ToolIndex(_make_registry(), _make_llm(), index_path)

        assert any("Could not load tool index" in r.message for r in caplog.records)
        assert ti._index == {}


# ---------------------------------------------------------------------------
# ToolIndex.build() logging
# ---------------------------------------------------------------------------

class TestBuildLogging:
    def test_build_logs_start_and_summary(self, tmp_path, caplog):
        registry = _make_registry("tool_a", "tool_b")
        ti = ToolIndex(registry, _make_llm(), str(tmp_path / "index.json"))

        with caplog.at_level(logging.INFO, logger="tool_index"):
            ti.build()

        messages = [r.message for r in caplog.records]
        assert any("Building tool index" in m for m in messages), messages
        assert any("build complete" in m for m in messages), messages

    def test_build_summary_shows_embedded_count(self, tmp_path, caplog):
        registry = _make_registry("alpha", "beta", "gamma")
        ti = ToolIndex(registry, _make_llm(), str(tmp_path / "index.json"))

        with caplog.at_level(logging.INFO, logger="tool_index"):
            ti.build()

        summary = next(r.message for r in caplog.records if "build complete" in r.message)
        # 3 tools embedded, 0 cached on first build
        assert "3" in summary, f"Expected embedded count in summary: {summary}"

    def test_build_cached_tools_not_re_embedded(self, tmp_path, caplog):
        llm = _make_llm()
        registry = _make_registry("tool_x")
        index_path = str(tmp_path / "index.json")
        ti = ToolIndex(registry, llm, index_path)

        # First build embeds the tool
        ti.build()
        first_call_count = llm.embed.call_count

        # Second build should skip (cached)
        with caplog.at_level(logging.DEBUG, logger="tool_index"):
            ti.build()

        assert llm.embed.call_count == first_call_count, "embed() should not be called on second build"
        assert any("already indexed" in r.message for r in caplog.records)

    def test_build_logs_stale_removal(self, tmp_path, caplog):
        """Tools removed from registry should be reported in the build summary."""
        registry = _make_registry("tool_a", "tool_b")
        index_path = str(tmp_path / "index.json")
        ti = ToolIndex(registry, _make_llm(), index_path)
        ti.build()

        # Remove one tool from registry
        registry.all.return_value = [_make_tool("tool_a")]

        with caplog.at_level(logging.INFO, logger="tool_index"):
            ti.build()

        summaries = [r.message for r in caplog.records if "build complete" in r.message]
        assert summaries, "No build summary found in logs"
        summary = summaries[-1]
        # 1 stale removed
        assert "1" in summary, f"Expected stale count in summary: {summary}"


# ---------------------------------------------------------------------------
# ToolIndex._embed_if_needed() debug skip log
# ---------------------------------------------------------------------------

class TestEmbedIfNeededLogging:
    def test_skip_debug_logged(self, tmp_path, caplog):
        llm = _make_llm()
        registry = _make_registry("my_tool")
        ti = ToolIndex(registry, llm, str(tmp_path / "index.json"))
        ti.build()  # first build embeds

        with caplog.at_level(logging.DEBUG, logger="tool_index"):
            ti.build()  # second build should skip

        assert any(
            "already indexed" in r.message and "my_tool" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# ToolIndex.rebuild() logging (existing, just smoke-test)
# ---------------------------------------------------------------------------

class TestRebuildLogging:
    def test_rebuild_logs_summary(self, tmp_path, caplog):
        registry = _make_registry("t1", "t2")
        ti = ToolIndex(registry, _make_llm(), str(tmp_path / "index.json"))

        with caplog.at_level(logging.INFO, logger="tool_index"):
            result = ti.rebuild()

        assert result["embedded"] == 2
        assert any("rebuilt" in r.message for r in caplog.records)
