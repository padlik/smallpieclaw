"""Tests for ResultsMemory recall control in build_system_prompt (memory item D).

When graph memory supplies semantic context for a turn, react_loop suppresses
ResultsMemory recall by passing ``results_top_k=0`` to avoid redundant/overlapping
recall. When graph context is absent, ResultsMemory recall is preserved at
``top_k=2`` (the default).
"""

from __future__ import annotations

from prompt_builder import build_system_prompt


class _FakeToolIndex:
    def search(self, _goal, top_k=3):
        return []


class _FakeMemory:
    def as_prompt_text(self):
        return "No persistent memory entries."


class _FakeResults:
    def __init__(self):
        self.calls = []

    def as_prompt_text(self, query, top_k=3):
        self.calls.append((query, top_k))
        return f"PAST_RESULT_FOR::{query}::k={top_k}"


class _FakeLLM:
    _models: list = []
    llm_cfg: dict = {}


def _build(results, *, graph_context_section="", results_top_k=2):
    return build_system_prompt(
        tool_index=_FakeToolIndex(),
        memory=_FakeMemory(),
        results=results,
        skill_registry=None,
        llm=_FakeLLM(),
        tmp_dir="/tmp/agent",
        downloads_dir="downloads",
        log_file="agent.log",
        log_backup_count=30,
        top_tools=3,
        user_goal="do the thing",
        graph_context_section=graph_context_section,
        results_top_k=results_top_k,
    )


class TestResultsRecallControl:
    def test_default_queries_results_top_k_2(self):
        results = _FakeResults()
        prompt, _ = _build(results, results_top_k=2)
        assert results.calls == [("do the thing", 2)]
        assert "PAST_RESULT_FOR::do the thing::k=2" in prompt

    def test_suppressed_when_results_top_k_zero(self):
        results = _FakeResults()
        prompt, _ = _build(results, results_top_k=0)
        # ResultsMemory must not be queried at all when suppressed.
        assert results.calls == []
        assert "PAST_RESULT_FOR" not in prompt
        # A neutral marker points the model at graph recall instead.
        assert "graph memory" in prompt

    def test_no_results_store_uses_default_message(self):
        prompt, _ = _build(None, results_top_k=2)
        assert "No past results." in prompt

    def test_graph_context_block_injected(self):
        results = _FakeResults()
        graph = "RECALLED CONTEXT (graph memory):\n  - alice uses postgres"
        prompt, _ = _build(results, graph_context_section=graph, results_top_k=0)
        assert "alice uses postgres" in prompt
        assert results.calls == []
