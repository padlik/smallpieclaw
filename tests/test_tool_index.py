"""
Tests for ToolIndex: embedding, caching, build/rebuild logging.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_schema import EmbeddingsConfig
from embedding_client import EmbeddingClient, EmbeddingUnavailableError
from tool_index import ToolIndex
from tool_registry import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, description: str = "") -> Tool:
    return Tool(name=name, language="python", description=description or f"{name} description")


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


# ---------------------------------------------------------------------------
# Unavailable embedding client handling
# ---------------------------------------------------------------------------

_REASON = "embedding configuration is unavailable (test)"


def _unavailable_llm() -> MagicMock:
    llm = MagicMock()
    llm.embed.side_effect = EmbeddingUnavailableError(_REASON)
    return llm


def _prime(index_path: str, registry, builtin_executor=None) -> None:
    """Build and persist a cache through the public API with a working embedder."""
    ToolIndex(registry, _make_llm([0.5] * 8), index_path, builtin_executor).build()


def _read(index_path: str) -> dict:
    with open(index_path) as f:
        return json.load(f)


@pytest.fixture
def real_unavailable_client():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network I/O: {request.url}")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EmbeddingClient(EmbeddingsConfig(), http_client=http)
    assert client.available is False
    try:
        yield client
    finally:
        client.close()
        http.close()


class TestUnavailableEmbeddings:
    def test_build_degrades_and_logs_reason(self, tmp_path, caplog):
        llm = _unavailable_llm()
        ti = ToolIndex(_make_registry("a", "b"), llm, str(tmp_path / "index.json"))

        with caplog.at_level(logging.INFO, logger="tool_index"):
            ti.build()

        assert llm.embed.called
        assert ti._index == {}
        assert any(_REASON in r.getMessage() for r in caplog.records)

    def test_build_failed_replacement_keeps_old_entry_and_removes_stale(self, tmp_path):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a", "b")
        _prime(path, registry)
        old = _read(path)["a"]

        registry.all.return_value = [_make_tool("a", "changed description")]
        ti = ToolIndex(registry, _unavailable_llm(), path)
        ti.build()

        assert ti._index == {"a": old}
        assert _read(path) == {"a": old}

    def test_rebuild_all_failures_persist_empty_cache(self, tmp_path, caplog):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a", "b")
        _prime(path, registry)
        ti = ToolIndex(registry, _unavailable_llm(), path)

        with caplog.at_level(logging.ERROR, logger="tool_index"):
            result = ti.rebuild()

        assert result["embedded"] == 0 and result["failed"] == 2
        assert ti._index == {}
        assert _read(path) == {}
        assert any(_REASON in r.getMessage() for r in caplog.records)

    def test_rebuild_mixed_keeps_only_new_successes(self, tmp_path):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a", "b")
        _prime(path, registry)
        llm = MagicMock()

        def embed(text: str) -> list[float]:
            if text.startswith("b "):
                raise EmbeddingUnavailableError(_REASON)
            return [0.9] * 8

        llm.embed.side_effect = embed
        ti = ToolIndex(registry, llm, path)
        result = ti.rebuild()

        assert result["embedded"] == 1 and result["failed"] == 1
        assert set(ti._index) == {"a"}
        assert ti._index["a"]["vector"] == [0.9] * 8
        assert set(_read(path)) == {"a"}
        assert _read(path) == ti._index

    def test_search_uses_registry_order_with_disk_cache(self, tmp_path):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a", "b", "c")
        builtin = SimpleNamespace(name="shell", description="run shell commands")
        executor = MagicMock()
        executor.all_tools.return_value = [builtin]
        _prime(path, registry, executor)
        assert "shell" in _read(path)

        llm = _unavailable_llm()
        ti = ToolIndex(registry, llm, path, executor)
        assert ti._index
        results = ti.search("anything", top_k=2)

        assert llm.embed.called
        assert [t.name for t in results] == ["a", "b"]
        results = ti.search("anything", top_k=4)
        assert [t.name for t in results] == ["a", "b", "c"]
        assert llm.embed.call_count == 2

    @pytest.mark.parametrize("operation", ["build", "rebuild", "search"])
    def test_real_unavailable_client(self, tmp_path, real_unavailable_client, operation):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a", "b", "c")
        if operation == "search":
            _prime(path, registry)
        ti = ToolIndex(registry, real_unavailable_client, path)

        if operation == "build":
            ti.build()
            assert ti._index == {}
        elif operation == "rebuild":
            result = ti.rebuild()
            assert result["embedded"] == 0 and result["failed"] == 3
            assert _read(path) == {}
        else:
            assert [t.name for t in ti.search("q", top_k=2)] == ["a", "b"]

    @pytest.mark.parametrize("exc_type", [RuntimeError, TypeError])
    @pytest.mark.parametrize("operation", ["build", "rebuild", "search"])
    def test_unrelated_errors_propagate(self, tmp_path, operation, exc_type):
        path = str(tmp_path / "index.json")
        registry = _make_registry("a")
        if operation == "search":
            _prime(path, registry)
        exc = exc_type("unrelated failure")
        llm = MagicMock()
        llm.embed.side_effect = exc
        ti = ToolIndex(registry, llm, path)

        with pytest.raises(exc_type) as info:
            getattr(ti, operation)(*(["q"] if operation == "search" else []))

        assert info.value is exc
        assert llm.embed.called
