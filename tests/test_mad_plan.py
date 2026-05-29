"""
tests/test_mad_plan.py
Tests for the MadPlanOrchestrator and helper functions.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from mad_plan import (
    MadPlanError,
    MadPlanOrchestrator,
    build_agent_capabilities_summary,
    list_plans,
    load_models_capabilities,
    validate_models_for_mad_plan,
)


# ---------------------------------------------------------------------------
# _parse_decomposition
# ---------------------------------------------------------------------------

class TestParseDecomposition:
    def _orc(self):
        return MadPlanOrchestrator()

    def test_basic_parse(self):
        raw = json.dumps({"subtasks": [
            {"id": "fetch_data", "name": "Fetch data", "description": "Download CSV", "depends_on": []},
        ]})
        result = self._orc()._parse_decomposition(raw)
        assert len(result) == 1
        assert result[0]["id"] == "fetch_data"
        assert result[0]["name"] == "Fetch data"
        assert result[0]["depends_on"] == []

    def test_generic_id_slugified(self):
        """t1/task1 IDs are replaced with slugified names."""
        raw = json.dumps({"subtasks": [
            {"id": "t1", "name": "Fetch Invoice Data", "description": "...", "depends_on": []},
            {"id": "t2", "name": "Send email", "description": "...", "depends_on": ["t1"]},
        ]})
        result = self._orc()._parse_decomposition(raw)
        assert result[0]["id"] == "fetch_invoice_data"
        assert result[1]["id"] == "send_email"
        # depends_on references are remapped
        assert result[1]["depends_on"] == ["fetch_invoice_data"]

    def test_task_number_id_slugified(self):
        raw = json.dumps({"subtasks": [
            {"id": "task_1", "name": "Do thing", "description": "x", "depends_on": []},
        ]})
        result = self._orc()._parse_decomposition(raw)
        assert result[0]["id"] == "do_thing"

    def test_empty_subtasks_raises(self):
        raw = json.dumps({"subtasks": []})
        with pytest.raises(MadPlanError):
            self._orc()._parse_decomposition(raw)

    def test_no_subtasks_key_raises(self):
        raw = json.dumps({"something": "else"})
        with pytest.raises(MadPlanError):
            self._orc()._parse_decomposition(raw)

    def test_fenced_json_ignored(self):
        raw = "```json\n" + json.dumps({"subtasks": [
            {"id": "step_one", "name": "Step one", "description": "desc", "depends_on": []}
        ]}) + "\n```"
        result = self._orc()._parse_decomposition(raw)
        assert result[0]["id"] == "step_one"


# ---------------------------------------------------------------------------
# _parse_batch_model_selection
# ---------------------------------------------------------------------------

class TestParseBatchModelSelection:
    def _orc(self):
        return MadPlanOrchestrator()

    def _subtasks(self, ids):
        return [{"id": sid, "name": sid, "description": ""} for sid in ids]

    def test_matched_by_subtask_id(self):
        raw = json.dumps({"selections": [
            {
                "subtask_id": "fetch_data",
                "model_name": "gpt-4",
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": 500,
                "rationale": "good choice",
                "prompt": "do the thing",
            }
        ]})
        result = self._orc()._parse_batch_model_selection(raw, self._subtasks(["fetch_data"]))
        assert result["fetch_data"]["model_name"] == "gpt-4"
        assert result["fetch_data"]["temperature"] == 0.3

    def test_unmatched_assigned_in_order(self):
        """If subtask_id is missing, entries are assigned in order."""
        raw = json.dumps({"selections": [
            {"model_name": "claude-3", "temperature": 0.5, "top_p": 1.0, "max_tokens": 1000,
             "rationale": "r", "prompt": "p"},
        ]})
        result = self._orc()._parse_batch_model_selection(
            raw, self._subtasks(["alpha", "beta"])
        )
        assert result["alpha"]["model_name"] == "claude-3"

    def test_flat_object_fallback(self):
        """A flat object (not selections list) is treated as one entry."""
        raw = json.dumps({
            "subtask_id": "solo",
            "model_name": "gpt-3.5",
            "temperature": 0.7,
            "top_p": 1.0,
            "max_tokens": 200,
            "rationale": "r",
            "prompt": "p",
        })
        result = self._orc()._parse_batch_model_selection(raw, self._subtasks(["solo"]))
        assert result["solo"]["model_name"] == "gpt-3.5"

    def test_numeric_coercion(self):
        """String numbers are coerced to float/int."""
        raw = json.dumps({"selections": [
            {"subtask_id": "x", "model_name": "m", "temperature": "0.2", "top_p": "0.8",
             "max_tokens": "1024", "rationale": "", "prompt": ""}
        ]})
        result = self._orc()._parse_batch_model_selection(raw, self._subtasks(["x"]))
        assert result["x"]["temperature"] == pytest.approx(0.2)
        assert result["x"]["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# save_plan / load_plan / list_plans
# ---------------------------------------------------------------------------

class TestSaveLoadPlan:
    SAMPLE_PLAN = {
        "task": "Fetch invoices and send a summary email",
        "created_at": "2025-01-01T12:00:00+00:00",
        "subtasks": [
            {
                "id": "fetch_invoices",
                "name": "Fetch Invoices",
                "description": "Download invoice data",
                "model_name": "gpt-4",
                "params": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 500},
                "prompt": "Fetch all invoices from the API.",
                "rationale": "Needs structured output.",
                "depends_on": [],
            },
            {
                "id": "send_summary_email",
                "name": "Send Summary Email",
                "description": "Compose and send email",
                "model_name": "claude-3",
                "params": {"temperature": 0.5, "top_p": 1.0, "max_tokens": 800},
                "prompt": "Write a summary email.",
                "rationale": "Creative writing task.",
                "depends_on": ["fetch_invoices"],
            },
        ],
    }

    def test_save_creates_plan_md(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug, path = orc.save_plan(self.SAMPLE_PLAN, d)
            assert os.path.exists(path)
            assert path.endswith("plan.md")
            assert slug in path

    def test_save_unique_collision(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug1, _ = orc.save_plan(self.SAMPLE_PLAN, d)
            # Same plan saved again — should get a different slug
            slug2, _ = orc.save_plan(self.SAMPLE_PLAN, d)
            assert slug1 != slug2

    def test_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug, _ = orc.save_plan(self.SAMPLE_PLAN, d)
            loaded = orc.load_plan(slug, d)
            assert loaded["task"] == self.SAMPLE_PLAN["task"]
            assert len(loaded["subtasks"]) == 2
            assert loaded["subtasks"][0]["id"] == "fetch_invoices"
            assert loaded["subtasks"][1]["id"] == "send_summary_email"
            assert loaded["subtasks"][1]["depends_on"] == ["fetch_invoices"]

    def test_load_not_found_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="not found"):
                orc.load_plan("nonexistent", d)

    def test_list_plans_empty(self):
        with tempfile.TemporaryDirectory() as d:
            assert list_plans(d) == []

    def test_list_plans_after_save(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            orc.save_plan(self.SAMPLE_PLAN, d)
            names = list_plans(d)
            assert len(names) == 1

    def test_list_plans_nonexistent_dir(self):
        assert list_plans("/tmp/definitely_does_not_exist_abc123") == []


# ---------------------------------------------------------------------------
# load_models_capabilities
# ---------------------------------------------------------------------------

class TestLoadModelsCapabilities:
    def test_loads_valid_file(self):
        caps_data = {"models": [{"model_name": "gpt-4", "strengths": ["reasoning"]}]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                json.dump(caps_data, f)
            result = load_models_capabilities(d)
        assert len(result) == 1
        assert result[0]["model_name"] == "gpt-4"

    def test_returns_empty_on_missing_file(self):
        result = load_models_capabilities("/tmp/no_such_dir_999")
        assert result == []

    def test_returns_empty_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                f.write("not json {{{")
            result = load_models_capabilities(d)
        assert result == []

    def test_handles_list_format(self):
        """Top-level list (not dict) is returned as-is."""
        caps_data = [{"model_name": "m1"}, {"model_name": "m2"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "models_capabilities.json")
            with open(path, "w") as f:
                json.dump(caps_data, f)
            result = load_models_capabilities(d)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# validate_models_for_mad_plan
# ---------------------------------------------------------------------------

class TestValidateModels:
    CAPS = [
        {"model_name": "gpt-4", "strengths": ["reasoning"]},
        {"model_name": "claude-3-opus", "strengths": ["code"]},
    ]

    def test_matched_by_model_field(self):
        configured = [{"model": "gpt-4", "name": "GPT-4"}]
        result = validate_models_for_mad_plan(configured, self.CAPS)
        assert len(result["with_capabilities"]) == 1
        assert result["with_capabilities"][0]["model"] == "gpt-4"
        assert result["missing_capabilities"] == []

    def test_unmatched_in_missing(self):
        configured = [{"model": "gpt-3.5-turbo", "name": "GPT-3.5"}]
        result = validate_models_for_mad_plan(configured, self.CAPS)
        assert len(result["missing_capabilities"]) == 1
        assert result["with_capabilities"] == []

    def test_matched_by_alias(self):
        configured = [{"model": "gemini-flash:cloud", "name": "Gemini", "aliases": ["claude-3-opus"]}]
        result = validate_models_for_mad_plan(configured, self.CAPS)
        assert len(result["with_capabilities"]) == 1

    def test_empty_configured(self):
        result = validate_models_for_mad_plan([], self.CAPS)
        assert result["available"] == []
        assert result["with_capabilities"] == []

    def test_skips_model_without_model_field(self):
        configured = [{"name": "no_model_field"}]
        result = validate_models_for_mad_plan(configured, self.CAPS)
        assert result["available"] == []


# ---------------------------------------------------------------------------
# build_agent_capabilities_summary
# ---------------------------------------------------------------------------

class TestBuildCapabilitiesSummary:
    def test_no_registries(self):
        result = build_agent_capabilities_summary()
        assert "Built-in tools:" in result

    def test_builtin_override(self):
        result = build_agent_capabilities_summary(builtin_tool_names=["shell", "web_fetch"])
        assert "shell" in result
        assert "web_fetch" in result

    def test_with_tool_registry(self):
        mock_tool = MagicMock()
        mock_tool.name = "my_tool"
        registry = MagicMock()
        registry.all.return_value = [mock_tool]
        result = build_agent_capabilities_summary(tool_registry=registry)
        assert "my_tool" in result

    def test_with_skill_registry(self):
        mock_skill = MagicMock()
        mock_skill.name = "web_scraper"
        mock_skill.description = "Scrape websites"
        skills = MagicMock()
        skills.all.return_value = [mock_skill]
        result = build_agent_capabilities_summary(skill_registry=skills)
        assert "web_scraper" in result

    def test_with_mcp_manager_list_tools(self):
        mcp = MagicMock()
        mcp.list_tools.return_value = [{"name": "mcp_search"}, {"name": "mcp_db"}]
        result = build_agent_capabilities_summary(mcp_manager=mcp)
        assert "mcp_search" in result
        assert "mcp_db" in result

    def test_registry_exception_handled(self):
        registry = MagicMock()
        registry.all.side_effect = RuntimeError("boom")
        # Should not raise
        result = build_agent_capabilities_summary(tool_registry=registry)
        assert "Built-in tools:" in result


# ---------------------------------------------------------------------------
# format_plan_html
# ---------------------------------------------------------------------------

class TestFormatPlanHtml:
    PLAN = {
        "task": "Build a report",
        "created_at": "2025-06-01T10:00:00+00:00",
        "subtasks": [
            {
                "id": "gather_data",
                "name": "Gather data",
                "description": "Collect metrics",
                "model_name": "gpt-4",
                "params": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 500},
                "prompt": "Gather all metrics",
                "rationale": "Needs structured output",
                "depends_on": [],
            },
            {
                "id": "write_report",
                "name": "Write report",
                "description": "Draft the final report",
                "model_name": "claude-3",
                "params": {"temperature": 0.7, "top_p": 1.0, "max_tokens": 2000},
                "prompt": "Write the report",
                "rationale": "Creative task",
                "depends_on": ["gather_data"],
            },
        ],
    }

    def test_contains_task(self):
        html = MadPlanOrchestrator().format_plan_html(self.PLAN)
        assert "Build a report" in html

    def test_contains_model_names(self):
        html = MadPlanOrchestrator().format_plan_html(self.PLAN)
        assert "gpt-4" in html
        assert "claude-3" in html

    def test_contains_sub_task_ids(self):
        html = MadPlanOrchestrator().format_plan_html(self.PLAN)
        assert "gather_data" in html
        assert "write_report" in html

    def test_dependency_shown(self):
        html = MadPlanOrchestrator().format_plan_html(self.PLAN)
        assert "gather_data" in html  # dependency reference

    def test_params_shown(self):
        html = MadPlanOrchestrator().format_plan_html(self.PLAN)
        assert "t=0.2" in html or "0.2" in html

    def test_empty_subtasks(self):
        plan = {"task": "Empty", "created_at": "2025-01-01T00:00:00+00:00", "subtasks": []}
        html = MadPlanOrchestrator().format_plan_html(plan)
        assert "0 sub-task" in html


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------

class TestExecutePlan:
    PLAN = {
        "task": "test execution",
        "created_at": "2025-01-01T00:00:00+00:00",
        "subtasks": [
            {
                "id": "step_one", "name": "Step one", "description": "do step 1",
                "model_name": "m1", "params": {}, "prompt": "execute step 1",
                "rationale": "", "depends_on": [],
            },
            {
                "id": "step_two", "name": "Step two", "description": "do step 2",
                "model_name": "m2", "params": {}, "prompt": "execute step 2",
                "rationale": "", "depends_on": ["step_one"],
            },
        ],
    }

    def _make_factory(self, results: dict):
        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None):
            runner = MagicMock()
            runner.run.return_value = results.get(label, "ok")
            return runner
        return factory

    def test_successful_execution(self):
        factory = self._make_factory({
            "madplan-step_one": "result1",
            "madplan-step_two": "result2",
        })
        output, failure = MadPlanOrchestrator().execute_plan(self.PLAN, factory)
        assert failure is None
        assert len(output) == 2
        assert output[0]["id"] == "step_one"
        assert output[0]["result"] == "result1"
        assert output[1]["id"] == "step_two"

    def test_failure_returns_info(self):
        def bad_factory(model=None, label=None, **kw):
            runner = MagicMock()
            if label == "madplan-step_one":
                runner.run.side_effect = RuntimeError("connection error")
            else:
                runner.run.return_value = "ok"
            return runner

        output, failure = MadPlanOrchestrator().execute_plan(self.PLAN, bad_factory)
        assert failure is not None
        assert failure["subtask_id"] == "step_one"
        assert "connection error" in failure["error"]
        assert "step_two" in failure["remaining"]
        assert len(output) == 0

    def test_notify_called(self):
        notifications = []
        factory = self._make_factory({})
        MadPlanOrchestrator().execute_plan(self.PLAN, factory, notify_fn=notifications.append)
        assert any("step_one" in n for n in notifications)
