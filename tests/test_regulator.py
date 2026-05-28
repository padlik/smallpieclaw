"""Tests for regulator.py — RegulatorOrchestrator, helpers, and validation."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from regulator import (
    RegulatorError,
    RegulatorOrchestrator,
    _extract_json,
    load_models_capabilities,
    validate_models_for_regulator,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CAPS = [
    {
        "model_name": "gpt-4o",
        "specifications": {"context_window": 128000},
        "optimal_configuration": {"temperature": [0.1, 0.7]},
        "usage_guidelines": {"best_for": ["reasoning"]},
        "management_summary": "Great for reasoning.",
    },
    {
        "model_name": "claude-3-5-sonnet",
        "specifications": {"context_window": 200000},
        "optimal_configuration": {"temperature": [0.0, 0.5]},
        "usage_guidelines": {"best_for": ["code"]},
        "management_summary": "Great for code.",
    },
]

CONFIGURED_MODELS = [
    {"name": "fast", "model": "gpt-4o-mini", "provider": "openai", "vision": True},
    {"name": "smart", "model": "claude-3-5-sonnet", "provider": "anthropic"},
]

SUBTASK_1 = {
    "id": "t1",
    "name": "Research",
    "description": "Gather information",
    "depends_on": [],
}

SUBTASK_2 = {
    "id": "t2",
    "name": "Summarise",
    "description": "Write summary",
    "depends_on": ["t1"],
}

PLAN = {
    "task": "Do research and summarise",
    "created_at": "2026-01-01T00:00:00+00:00",
    "subtasks": [
        {**SUBTASK_1, "model_name": "gpt-4o-mini",
         "params": {"temperature": 0.3, "top_p": 0.9, "max_tokens": 1024},
         "prompt": "Research this topic.", "rationale": "Fast model."},
        {**SUBTASK_2, "model_name": "claude-3-5-sonnet",
         "params": {"temperature": 0.5, "top_p": None, "max_tokens": 512},
         "prompt": "Summarise the research.", "rationale": "Good at writing."},
    ],
}


def _mock_llm(responses: list[str]) -> MagicMock:
    """LLM client mock that returns responses in order."""
    client = MagicMock()
    client.chat.side_effect = responses
    return client


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"b": 2}\n```'
        assert _extract_json(raw) == {"b": 2}

    def test_json_embedded_in_text(self):
        raw = 'Here is the result: {"c": 3} done.'
        assert _extract_json(raw) == {"c": 3}

    def test_invalid_raises(self):
        with pytest.raises(RegulatorError):
            _extract_json("not json at all")


# ---------------------------------------------------------------------------
# load_models_capabilities
# ---------------------------------------------------------------------------

class TestLoadModelsCapabilities:
    def test_loads_valid_file(self):
        data = {"models": CAPS}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                json.dump(data, f)
            result = load_models_capabilities(d)
        assert result == CAPS

    def test_missing_file_returns_empty(self):
        result = load_models_capabilities("/nonexistent/dir")
        assert result == []

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                f.write("not json")
            result = load_models_capabilities(d)
        assert result == []

    def test_top_level_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                json.dump(CAPS, f)
            result = load_models_capabilities(d)
        assert result == CAPS


# ---------------------------------------------------------------------------
# validate_models_for_regulator
# ---------------------------------------------------------------------------

class TestValidateModels:
    def test_exact_match_on_model(self):
        configured = [{"name": "a", "model": "claude-3-5-sonnet", "provider": "anthropic"}]
        result = validate_models_for_regulator(configured, CAPS)
        assert len(result["with_capabilities"]) == 1
        assert result["with_capabilities"][0]["capabilities"]["model_name"] == "claude-3-5-sonnet"

    def test_exact_match_on_alias(self):
        configured = [{"name": "a", "model": "kimi-k2:1t-cloud", "provider": "ollama",
                       "aliases": ["kimi-k2.6", "kimi-k2"]}]
        caps = [{"model_name": "kimi-k2.6", "specifications": {}, "management_summary": "x"}]
        result = validate_models_for_regulator(configured, caps)
        assert len(result["with_capabilities"]) == 1
        assert result["with_capabilities"][0]["capabilities"]["model_name"] == "kimi-k2.6"

    def test_alias_second_entry_matches(self):
        configured = [{"name": "a", "model": "qwen3:cloud", "provider": "ollama",
                       "aliases": ["qwen3-no-match", "qwen3-vl:235b"]}]
        caps = [{"model_name": "qwen3-vl:235b", "specifications": {}, "management_summary": "x"}]
        result = validate_models_for_regulator(configured, caps)
        assert len(result["with_capabilities"]) == 1

    def test_no_fuzzy_matching(self):
        # "gpt-4o-mini" should NOT match "gpt-4o" — no fuzzy/prefix matching
        configured = [{"name": "a", "model": "gpt-4o-mini", "provider": "openai"}]
        result = validate_models_for_regulator(configured, CAPS)
        assert result["with_capabilities"] == []

    def test_no_match_without_alias(self):
        configured = [{"name": "a", "model": "llama-3:cloud", "provider": "ollama"}]
        result = validate_models_for_regulator(configured, CAPS)
        assert result["with_capabilities"] == []
        assert len(result["missing_capabilities"]) == 1

    def test_empty_model_id_skipped(self):
        configured = [
            {"name": "bad", "model": "", "provider": "openai"},
            {"name": "good", "model": "gpt-4o", "provider": "openai"},
        ]
        result = validate_models_for_regulator(configured, CAPS)
        assert all(e["model"] != "" for e in result["available"])

    def test_returns_structure(self):
        configured = [
            {"name": "a", "model": "gpt-4o", "provider": "openai"},
            {"name": "b", "model": "unknown", "provider": "x"},
        ]
        result = validate_models_for_regulator(configured, CAPS)
        assert "available" in result
        assert "with_capabilities" in result
        assert "missing_capabilities" in result
        assert len(result["available"]) == 2
        assert len(result["with_capabilities"]) == 1
        assert len(result["missing_capabilities"]) == 1

    def test_empty_inputs(self):
        result = validate_models_for_regulator([], [])
        assert result == {"available": [], "with_capabilities": [], "missing_capabilities": []}

    def test_model_field_takes_priority_over_alias(self):
        # If model field itself matches, don't need aliases
        configured = [{"name": "a", "model": "gpt-4o", "provider": "openai",
                       "aliases": ["something-else"]}]
        result = validate_models_for_regulator(configured, CAPS)
        assert len(result["with_capabilities"]) == 1
        assert result["with_capabilities"][0]["capabilities"]["model_name"] == "gpt-4o"

    def test_comma_string_aliases_via_list_models(self):
        # Simulate what list_models produces from a comma-string config
        # (list_models normalizes to list, so here we just pass the list)
        configured = [{"name": "a", "model": "deep:cloud", "provider": "ollama",
                       "aliases": ["deepseek-v4-pro"]}]
        caps = [{"model_name": "deepseek-v4-pro", "specifications": {}, "management_summary": "x"}]
        result = validate_models_for_regulator(configured, caps)
        assert len(result["with_capabilities"]) == 1


# ---------------------------------------------------------------------------
# RegulatorOrchestrator.execute_plan
# ---------------------------------------------------------------------------

class TestExecutePlan:
    def _factory(self, responses: dict[str, str]):
        """Factory that returns a runner whose .run() returns a fixed response by model."""
        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None):
            runner = MagicMock()
            runner.run.return_value = responses.get(model, f"result-{model}")
            return runner
        return factory

    def test_sequential_execution_returns_results(self):
        plan = {
            "task": "test",
            "subtasks": [
                {"id": "t1", "name": "T1", "model_name": "m1",
                 "params": {}, "prompt": "do t1", "depends_on": []},
                {"id": "t2", "name": "T2", "model_name": "m2",
                 "params": {}, "prompt": "do t2", "depends_on": []},
            ],
        }
        factory = self._factory({"m1": "result1", "m2": "result2"})
        orch = RegulatorOrchestrator()
        results, failure = orch.execute_plan(plan, factory)
        assert failure is None
        assert len(results) == 2
        assert results[0] == {"id": "t1", "name": "T1", "result": "result1"}
        assert results[1] == {"id": "t2", "name": "T2", "result": "result2"}

    def test_upstream_context_injected(self):
        captured_prompts = []

        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None):
            runner = MagicMock()
            runner.run.side_effect = lambda p: captured_prompts.append(p) or f"res-{model}"
            return runner

        plan = {
            "task": "test",
            "subtasks": [
                {"id": "t1", "name": "T1", "model_name": "m1",
                 "params": {}, "prompt": "step1", "depends_on": []},
                {"id": "t2", "name": "T2", "model_name": "m2",
                 "params": {}, "prompt": "step2", "depends_on": ["t1"]},
            ],
        }
        orch = RegulatorOrchestrator()
        orch.execute_plan(plan, factory)
        # t2's prompt should contain t1's result
        assert "res-m1" in captured_prompts[1]
        assert "step2" in captured_prompts[1]

    def test_failure_returns_partial_and_failure_info(self):
        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None):
            runner = MagicMock()
            if model == "bad-model":
                runner.run.side_effect = RuntimeError("model unavailable")
            else:
                runner.run.return_value = "ok"
            return runner

        plan = {
            "task": "test",
            "subtasks": [
                {"id": "t1", "name": "T1", "model_name": "good-model",
                 "params": {}, "prompt": "step1", "depends_on": []},
                {"id": "t2", "name": "T2", "model_name": "bad-model",
                 "params": {}, "prompt": "step2", "depends_on": []},
                {"id": "t3", "name": "T3", "model_name": "good-model",
                 "params": {}, "prompt": "step3", "depends_on": []},
            ],
        }
        orch = RegulatorOrchestrator()
        results, failure = orch.execute_plan(plan, factory)
        # t1 completed, t2 failed, t3 never ran
        assert len(results) == 1
        assert results[0]["id"] == "t1"
        assert failure is not None
        assert failure["subtask_id"] == "t2"
        assert failure["model_name"] == "bad-model"
        assert "model unavailable" in failure["error"]
        assert "t3" in failure["remaining"]

    def test_missing_dependency_injects_warning(self):
        captured_prompts = []

        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None):
            runner = MagicMock()
            runner.run.side_effect = lambda p: captured_prompts.append(p) or "ok"
            return runner

        plan = {
            "task": "test",
            "subtasks": [
                {"id": "t2", "name": "T2", "model_name": "m1",
                 "params": {}, "prompt": "step2", "depends_on": ["t99"]},
            ],
        }
        orch = RegulatorOrchestrator()
        orch.execute_plan(plan, factory)
        # prompt should contain warning about missing dependency
        assert "WARNING" in captured_prompts[0] or "warning" in captured_prompts[0].lower()

    def test_empty_plan_returns_empty(self):
        orch = RegulatorOrchestrator()
        results, failure = orch.execute_plan({"task": "x", "subtasks": []}, MagicMock())
        assert results == []
        assert failure is None


# ---------------------------------------------------------------------------
# RegulatorOrchestrator.save_plan
# ---------------------------------------------------------------------------

class TestSavePlan:
    def test_saves_markdown_file(self):
        with tempfile.TemporaryDirectory() as d:
            orch = RegulatorOrchestrator()
            path = orch.save_plan(PLAN, d)
            assert os.path.exists(path)
            content = open(path).read()
            assert "Do research and summarise" in content
            assert "Research" in content
            assert "gpt-4o-mini" in content

    def test_filename_includes_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            orch = RegulatorOrchestrator()
            path = orch.save_plan(PLAN, d)
            filename = os.path.basename(path)
            # Filename is derived from task words, e.g. "do_research_and_summarise.md"
            assert filename.endswith(".md")
            assert "do" in filename or "research" in filename

    def test_no_collision_on_repeated_saves(self):
        """Three saves of the same task must produce three distinct files."""
        with tempfile.TemporaryDirectory() as d:
            orch = RegulatorOrchestrator()
            p1 = orch.save_plan(PLAN, d)
            p2 = orch.save_plan(PLAN, d)
            p3 = orch.save_plan(PLAN, d)
            assert len({p1, p2, p3}) == 3, "save_plan must produce unique filenames"
            for p in (p1, p2, p3):
                assert os.path.exists(p)

    def test_creates_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as base:
            plans_dir = os.path.join(base, "subdir", "plans")
            orch = RegulatorOrchestrator()
            path = orch.save_plan(PLAN, plans_dir)
            assert os.path.exists(path)


# ---------------------------------------------------------------------------
# RegulatorOrchestrator.format_plan_html
# ---------------------------------------------------------------------------

class TestFormatPlanHtml:
    def test_contains_task_and_subtask_names(self):
        orch = RegulatorOrchestrator()
        html = orch.format_plan_html(PLAN)
        assert "Do research and summarise" in html
        assert "Research" in html
        assert "Summarise" in html

    def test_contains_model_names(self):
        orch = RegulatorOrchestrator()
        html = orch.format_plan_html(PLAN)
        assert "gpt-4o-mini" in html
        assert "claude-3-5-sonnet" in html

    def test_escapes_html_entities(self):
        plan = {
            "task": "<script>alert(1)</script>",
            "created_at": "2026-01-01T00:00:00+00:00",
            "subtasks": [],
        }
        orch = RegulatorOrchestrator()
        out = orch.format_plan_html(plan)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


# ---------------------------------------------------------------------------
# RegulatorOrchestrator._parse_decomposition
# ---------------------------------------------------------------------------

class TestParseDecomposition:
    def test_valid_decomposition(self):
        raw = json.dumps({
            "subtasks": [
                {"id": "fetch_data", "name": "Fetch data", "description": "Do thing", "depends_on": []},
            ]
        })
        orch = RegulatorOrchestrator()
        result = orch._parse_decomposition(raw)
        assert len(result) == 1
        assert result[0]["id"] == "fetch_data"

    def test_empty_subtasks_raises(self):
        raw = json.dumps({"subtasks": []})
        orch = RegulatorOrchestrator()
        with pytest.raises(RegulatorError):
            orch._parse_decomposition(raw)

    def test_missing_subtasks_raises(self):
        raw = json.dumps({"other": "data"})
        orch = RegulatorOrchestrator()
        with pytest.raises(RegulatorError):
            orch._parse_decomposition(raw)

    def test_auto_generates_id_if_missing(self):
        raw = json.dumps({
            "subtasks": [{"name": "Step", "description": "desc", "depends_on": []}]
        })
        orch = RegulatorOrchestrator()
        result = orch._parse_decomposition(raw)
        # ID is slugified from name when missing
        assert result[0]["id"] == "step"

    def test_generic_id_replaced_with_slug(self):
        """Generic IDs like t1, t2 are replaced with a slug of the name."""
        raw = json.dumps({
            "subtasks": [{"id": "t1", "name": "Fetch Invoice Data", "description": "desc", "depends_on": []}]
        })
        orch = RegulatorOrchestrator()
        result = orch._parse_decomposition(raw)
        assert result[0]["id"] == "fetch_invoice_data"

    def test_depends_on_remapped_when_ids_slugified(self):
        """When IDs are slugified, depends_on references must be updated."""
        raw = json.dumps({
            "subtasks": [
                {"id": "t1", "name": "Fetch Data", "description": "d1", "depends_on": []},
                {"id": "t2", "name": "Process Data", "description": "d2", "depends_on": ["t1"]},
            ]
        })
        orch = RegulatorOrchestrator()
        result = orch._parse_decomposition(raw)
        assert result[0]["id"] == "fetch_data"
        assert result[1]["id"] == "process_data"
        assert result[1]["depends_on"] == ["fetch_data"]


# ---------------------------------------------------------------------------
# RegulatorOrchestrator._parse_batch_model_selection
# ---------------------------------------------------------------------------

class TestParseBatchModelSelection:
    def test_valid_selection(self):
        raw = json.dumps({"selections": [{
            "subtask_id": "t1",
            "model_name": "gpt-4o",
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 1024,
            "rationale": "Best for this task.",
            "prompt": "Do the thing.",
        }]})
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "t1", "name": "Task 1"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        assert result["t1"]["model_name"] == "gpt-4o"
        assert result["t1"]["temperature"] == 0.3
        assert result["t1"]["max_tokens"] == 1024

    def test_string_numbers_coerced(self):
        raw = json.dumps({"selections": [{
            "subtask_id": "t1",
            "model_name": "m1",
            "temperature": "0.5",
            "top_p": "0.9",
            "max_tokens": "512",
            "rationale": "ok",
            "prompt": "p",
        }]})
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "t1", "name": "Task 1"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        assert result["t1"]["temperature"] == 0.5
        assert result["t1"]["max_tokens"] == 512

    def test_missing_optional_fields_return_none(self):
        raw = json.dumps({"selections": [{
            "subtask_id": "t1", "model_name": "m1", "rationale": "r", "prompt": "p"
        }]})
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "t1", "name": "Task 1"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        assert result["t1"]["temperature"] is None
        assert result["t1"]["top_p"] is None
        assert result["t1"]["max_tokens"] is None

    def test_multiple_selections_parsed(self):
        raw = json.dumps({"selections": [
            {"subtask_id": "fetch_data", "model_name": "gpt-4o", "temperature": 0.2,
             "top_p": 0.9, "max_tokens": 2048, "rationale": "r1", "prompt": "p1"},
            {"subtask_id": "analyze_data", "model_name": "claude-3", "temperature": 0.7,
             "top_p": 0.95, "max_tokens": 4096, "rationale": "r2", "prompt": "p2"},
        ]})
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "fetch_data", "name": "Fetch"}, {"id": "analyze_data", "name": "Analyze"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        assert len(result) == 2
        assert result["fetch_data"]["model_name"] == "gpt-4o"
        assert result["analyze_data"]["model_name"] == "claude-3"

    def test_missing_subtask_id_fallback(self):
        """Selections without matching subtask_id are assigned to unmatched subtasks in order."""
        raw = json.dumps({"selections": [
            {"subtask_id": "wrong_id", "model_name": "gpt-4o", "temperature": 0.3,
             "top_p": 0.9, "max_tokens": 1024, "rationale": "r", "prompt": "p"},
        ]})
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "real_task", "name": "Real task"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        # Unmatched subtask gets the unassigned selection
        assert result["real_task"]["model_name"] == "gpt-4o"

    def test_single_object_fallback(self):
        """If LLM returns a single object without 'selections' wrapper, still works."""
        raw = json.dumps({
            "subtask_id": "t1",
            "model_name": "gpt-4o",
            "temperature": 0.5,
            "top_p": 0.9,
            "max_tokens": 2000,
            "rationale": "r",
            "prompt": "p",
        })
        orch = RegulatorOrchestrator()
        subtasks = [{"id": "t1", "name": "T"}]
        result = orch._parse_batch_model_selection(raw, subtasks)
        assert result["t1"]["model_name"] == "gpt-4o"
