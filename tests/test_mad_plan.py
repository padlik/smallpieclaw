"""
tests/test_mad_plan.py
Tests for the MadPlanOrchestrator and helper functions.
"""

from __future__ import annotations

import json
import os
import re
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


# ---------------------------------------------------------------------------
# _parse_strategy
# ---------------------------------------------------------------------------

class TestParseStrategy:
    def _orc(self):
        return MadPlanOrchestrator()

    def _valid_strategy(self):
        return {
            "task_summary": "Check firewall status without root",
            "constraints": ["no sudo", "read-only access"],
            "primary_approach": "Use systemctl status ufw",
            "fallback_approaches": ["check /proc/net/ip_tables_names"],
            "discovery_needed": ["which commands work without root"],
            "execution_phases": [
                {"phase": "explore", "goal": "Find working method", "depends_on_discovery": False, "can_run_independently": True},
                {"phase": "report", "goal": "Output result", "depends_on_discovery": True, "can_run_independently": False},
            ],
            "notes": "iptables requires root",
        }

    def test_valid_strategy_parsed(self):
        raw = json.dumps(self._valid_strategy())
        result = self._orc()._parse_strategy(raw)
        assert result["task_summary"] == "Check firewall status without root"
        assert result["constraints"] == ["no sudo", "read-only access"]
        assert len(result["execution_phases"]) == 2

    def test_missing_keys_filled_with_defaults(self):
        raw = json.dumps({"task_summary": "Do something"})
        result = self._orc()._parse_strategy(raw)
        assert result["task_summary"] == "Do something"
        assert result["constraints"] == []
        assert result["fallback_approaches"] == []
        assert result["discovery_needed"] == []
        assert result["execution_phases"] == []
        assert result["primary_approach"] == ""
        assert result["notes"] == ""

    def test_non_dict_json_array_falls_back(self):
        """LLM returns a JSON array instead of object — must not raise TypeError."""
        raw = json.dumps([{"task_summary": "should be dict not list"}])
        result = self._orc()._parse_strategy(raw)
        assert isinstance(result, dict)
        assert result["constraints"] == []

    def test_null_json_falls_back(self):
        """LLM returns JSON null — must not raise."""
        result = self._orc()._parse_strategy("null")
        assert isinstance(result, dict)
        assert result["primary_approach"] == ""

    def test_invalid_json_falls_back(self):
        """Completely broken JSON — must return empty-but-valid dict."""
        result = self._orc()._parse_strategy("not json at all {{{")
        assert isinstance(result, dict)
        assert result["notes"] == ""

    def test_fenced_json_parsed(self):
        raw = "```json\n" + json.dumps(self._valid_strategy()) + "\n```"
        result = self._orc()._parse_strategy(raw)
        assert result["task_summary"] == "Check firewall status without root"


# ---------------------------------------------------------------------------
# save_plan / load_plan — strategy round-trip
# ---------------------------------------------------------------------------

class TestSaveLoadPlanWithStrategy:
    SAMPLE_PLAN_WITH_STRATEGY = {
        "task": "Check ufw status without root",
        "created_at": "2025-06-01T10:00:00+00:00",
        "strategy": {
            "task_summary": "Determine firewall state using non-root methods",
            "constraints": ["no sudo", "Linux environment"],
            "primary_approach": "Try systemctl status ufw first",
            "fallback_approaches": ["check /proc/net/ip_tables_names", "use ss -tuln"],
            "discovery_needed": ["which method is accessible without root"],
            "execution_phases": [
                {"phase": "explore", "goal": "Find accessible method", "depends_on_discovery": False, "can_run_independently": True},
                {"phase": "report", "goal": "Output firewall state", "depends_on_discovery": True, "can_run_independently": False},
            ],
            "notes": "iptables -L always requires root",
        },
        "subtasks": [
            {
                "id": "explore_ufw_access",
                "name": "Explore accessible methods",
                "description": "Try all methods without root",
                "model_name": "gpt-4",
                "params": {"temperature": 0.1, "top_p": 0.9, "max_tokens": 2000},
                "prompt": "Try these methods in order: ...",
                "rationale": "Needs tool use.",
                "depends_on": [],
            },
        ],
    }

    def test_strategy_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug, _ = orc.save_plan(self.SAMPLE_PLAN_WITH_STRATEGY, d)
            loaded = orc.load_plan(slug, d)
            assert loaded["strategy"]["task_summary"] == "Determine firewall state using non-root methods"
            assert loaded["strategy"]["constraints"] == ["no sudo", "Linux environment"]
            assert len(loaded["strategy"]["fallback_approaches"]) == 2
            assert len(loaded["strategy"]["execution_phases"]) == 2

    def test_strategy_json_file_created(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug, _ = orc.save_plan(self.SAMPLE_PLAN_WITH_STRATEGY, d)
            strategy_path = os.path.join(d, slug, "strategy.json")
            assert os.path.exists(strategy_path)
            with open(strategy_path) as fh:
                data = json.load(fh)
            assert data["task_summary"] == "Determine firewall state using non-root methods"

    def test_load_without_strategy_json_returns_empty_strategy(self):
        """Plans saved before strategy feature have no strategy.json — load gracefully."""
        plan_no_strategy = {
            "task": "Old plan without strategy",
            "created_at": "2025-01-01T00:00:00+00:00",
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug, _ = orc.save_plan(plan_no_strategy, d)
            loaded = orc.load_plan(slug, d)
            assert loaded["strategy"] == {}

    def test_format_plan_html_shows_strategy(self):
        orc = MadPlanOrchestrator()
        html_out = orc.format_plan_html(self.SAMPLE_PLAN_WITH_STRATEGY)
        assert "Try systemctl status ufw first" in html_out
        assert "no sudo" in html_out


# ---------------------------------------------------------------------------
# revise_plan / _parse_revision
# ---------------------------------------------------------------------------

class TestRevisePlan:
    """Tests for MadPlanOrchestrator.revise_plan() and _parse_revision()."""

    ORIGINAL_PLAN = {
        "task": "Write a tool to check UFW status without root",
        "created_at": "2025-06-01T10:00:00+00:00",
        "strategy": {
            "task_summary": "Read UFW config files directly",
            "constraints": ["no sudo"],
            "primary_approach": "Parse /etc/ufw/ufw.conf",
            "fallback_approaches": [],
            "discovery_needed": [],
            "execution_phases": [],
            "notes": "",
        },
        "subtasks": [
            {
                "id": "read_ufw_config",
                "name": "Read UFW config",
                "description": "Parse /etc/ufw/ufw.conf",
                "model_name": "gpt-4",
                "params": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 2000},
                "prompt": "Read /etc/ufw/ufw.conf and report status.",
                "rationale": "File parsing task.",
                "depends_on": [],
            },
        ],
    }

    CONFIGURED_MODELS = [
        {"model": "gpt-4", "name": "GPT-4", "provider": "openai"},
        {"model": "claude-3", "name": "Claude 3", "provider": "anthropic"},
    ]

    def _make_llm(self, response: dict):
        m = MagicMock()
        m.chat.return_value = json.dumps(response)
        m.list_models.return_value = self.CONFIGURED_MODELS
        return m

    def _valid_revision(self):
        return {
            "strategy": {
                "task_summary": "Use Docker privileged container to query iptables",
                "constraints": ["no sudo on host", "Docker must be available"],
                "primary_approach": "Run privileged Docker container to execute iptables -L",
                "fallback_approaches": ["Parse /etc/ufw/ufw.conf if Docker unavailable"],
                "discovery_needed": ["docker daemon accessibility"],
                "execution_phases": [
                    {"phase": "explore_docker", "goal": "Verify Docker access", "depends_on_discovery": False, "can_run_independently": True},
                    {"phase": "query_iptables", "goal": "Run iptables inside container", "depends_on_discovery": True, "can_run_independently": False},
                ],
                "notes": "Requires Docker socket access",
            },
            "subtasks": [
                {"id": "explore_docker_access", "name": "Explore Docker access", "description": "Verify Docker is available and privileged mode works", "depends_on": []},
                {"id": "query_ufw_via_docker", "name": "Query UFW via Docker", "description": "Run iptables -L inside privileged container", "depends_on": ["explore_docker_access"]},
            ],
            "selections": [
                {"subtask_id": "explore_docker_access", "model_name": "gpt-4", "temperature": 0.1, "top_p": 0.9, "max_tokens": 1500, "rationale": "Tool use needed", "prompt": "Verify docker run --privileged works..."},
                {"subtask_id": "query_ufw_via_docker", "model_name": "gpt-4", "temperature": 0.1, "top_p": 0.9, "max_tokens": 2000, "rationale": "Execution task", "prompt": "Run: docker run --rm --privileged ..."},
            ],
        }

    def test_revision_updates_strategy(self):
        llm = self._make_llm(self._valid_revision())
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "What if I can use Docker in privileged mode", llm, [], self.CONFIGURED_MODELS)
        assert "Docker" in result["strategy"]["primary_approach"]
        assert result["strategy"]["task_summary"] != self.ORIGINAL_PLAN["strategy"]["task_summary"]

    def test_revision_produces_new_subtasks(self):
        llm = self._make_llm(self._valid_revision())
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "use Docker privileged", llm, [], self.CONFIGURED_MODELS)
        assert len(result["subtasks"]) == 2
        ids = [st["id"] for st in result["subtasks"]]
        assert "explore_docker_access" in ids
        assert "query_ufw_via_docker" in ids

    def test_revision_respects_dependencies(self):
        llm = self._make_llm(self._valid_revision())
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "use Docker privileged", llm, [], self.CONFIGURED_MODELS)
        second = next(st for st in result["subtasks"] if st["id"] == "query_ufw_via_docker")
        assert "explore_docker_access" in second["depends_on"]

    def test_revision_preserves_metadata_keys(self):
        plan_with_meta = dict(self.ORIGINAL_PLAN)
        plan_with_meta["_plan_name"] = "write_a_tool_to"
        plan_with_meta["_saved_path"] = "/tmp/plans/write_a_tool_to/plan.md"
        llm = self._make_llm(self._valid_revision())
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(plan_with_meta, "use Docker", llm, [], self.CONFIGURED_MODELS)
        assert result["_plan_name"] == "write_a_tool_to"
        assert result["_saved_path"] == "/tmp/plans/write_a_tool_to/plan.md"

    def test_revision_invalid_json_returns_original(self):
        llm = MagicMock()
        llm.chat.return_value = "not json at all"
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "some feedback", llm, [], self.CONFIGURED_MODELS)
        assert result["task"] == self.ORIGINAL_PLAN["task"]
        assert len(result["subtasks"]) == len(self.ORIGINAL_PLAN["subtasks"])

    def test_revision_model_not_in_configured_falls_back(self):
        revision = self._valid_revision()
        revision["selections"][0]["model_name"] = "hallucinated-model-xyz"
        revision["selections"][1]["model_name"] = "also-not-real"
        llm = self._make_llm(revision)
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "use Docker", llm, [], self.CONFIGURED_MODELS)
        for st in result["subtasks"]:
            assert st["model_name"] == "gpt-4"  # first configured model

    def test_agent_capabilities_injected_into_system_prompt(self):
        """agent_capabilities text must appear in the system prompt sent to the LLM."""
        llm = self._make_llm(self._valid_revision())
        orc = MadPlanOrchestrator()
        caps = "TOOL: bash_execute — run shell commands\nSKILL: web_search — search the internet"
        orc.revise_plan(
            self.ORIGINAL_PLAN, "some feedback", llm, [], self.CONFIGURED_MODELS,
            agent_capabilities=caps,
        )
        # The system prompt is passed as the `system` kwarg to llm.chat
        call_kwargs = llm.chat.call_args
        system_prompt = call_kwargs.kwargs.get("system") or call_kwargs[1].get("system", "")
        assert "bash_execute" in system_prompt
        assert "web_search" in system_prompt

    def test_no_double_braces_in_revise_system_prompt(self):
        """The _REVISE_SYSTEM prompt must not contain literal {{ after .format() call."""
        from mad_plan import _REVISE_SYSTEM
        formatted = _REVISE_SYSTEM.format(agent_capabilities="test-caps")
        assert "{{" not in formatted
        assert "}}" not in formatted


class TestSavePlanOverwrite:
    """Test save_plan overwrite=True and target_slug behaviours."""

    PLAN = {
        "task": "Check UFW status",
        "created_at": "2025-06-01T10:00:00+00:00",
        "subtasks": [],
    }

    def test_overwrite_replaces_existing(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug1, path1 = orc.save_plan(self.PLAN, d)
            # Overwrite with updated plan
            updated = dict(self.PLAN)
            updated["task"] = "Check UFW status"  # same task → same slug
            slug2, path2 = orc.save_plan(updated, d, overwrite=True)
            assert slug1 == slug2
            assert path1 == path2

    def test_no_overwrite_creates_new_slug(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            slug1, _ = orc.save_plan(self.PLAN, d)
            slug2, _ = orc.save_plan(self.PLAN, d)  # overwrite=False by default
            assert slug1 != slug2

    def test_target_slug_writes_to_correct_directory(self):
        """target_slug overrides task-derived slug — even if task text would produce a different name."""
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            # First save produces a timestamped slug because the base slug dir already exists
            slug1, _ = orc.save_plan(self.PLAN, d)
            # Simulate a revision using the original slug as target_slug
            updated = dict(self.PLAN)
            updated["task"] = "Check UFW status"
            slug2, path2 = orc.save_plan(updated, d, target_slug=slug1)
            assert slug2 == slug1
            assert os.path.join(d, slug1, "plan.md") == path2

    def test_target_slug_overrides_timestamped_slug(self):
        """When original slug was timestamped, target_slug writes to that exact dir."""
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            # Force a timestamped slug by saving twice
            slug1, _ = orc.save_plan(self.PLAN, d)
            slug_ts, _ = orc.save_plan(self.PLAN, d)  # slug_ts will be timestamped
            assert slug_ts != slug1
            # Revise using slug_ts as target — must not accidentally use slug1
            updated = dict(self.PLAN)
            slug_rev, path_rev = orc.save_plan(updated, d, target_slug=slug_ts)
            assert slug_rev == slug_ts
            assert os.path.dirname(path_rev) == os.path.join(d, slug_ts)


class TestShortNamePlanNaming:
    """Test that save_plan uses strategy.short_name when available."""

    def test_short_name_used_as_slug(self):
        plan = {
            "task": "Write a tool that can check UFW status without root permissions",
            "created_at": "2025-06-01T10:00:00+00:00",
            "strategy": {"short_name": "ufw_check_without_root"},
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            slug, path = MadPlanOrchestrator().save_plan(plan, d)
            assert slug == "ufw_check_without_root"
            assert os.path.dirname(path) == os.path.join(d, "ufw_check_without_root")

    def test_short_name_preferred_over_task_words(self):
        """short_name beats 5-word task derivation."""
        plan = {
            "task": "task check ufw status in current environment",
            "created_at": "2025-06-01T10:00:00+00:00",
            "strategy": {"short_name": "ufw_status_probe"},
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            slug, _ = MadPlanOrchestrator().save_plan(plan, d)
            assert slug == "ufw_status_probe"
            assert "task_check_ufw" not in slug

    def test_fallback_to_task_words_when_short_name_absent(self):
        """No strategy → falls back to first-5-words derivation."""
        plan = {
            "task": "Check UFW status",
            "created_at": "2025-06-01T10:00:00+00:00",
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            slug, _ = MadPlanOrchestrator().save_plan(plan, d)
            assert slug == "check_ufw_status"

    def test_fallback_to_task_words_when_short_name_empty(self):
        """Empty short_name → falls back to first-5-words derivation."""
        plan = {
            "task": "Check UFW status",
            "created_at": "2025-06-01T10:00:00+00:00",
            "strategy": {"short_name": ""},
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            slug, _ = MadPlanOrchestrator().save_plan(plan, d)
            assert slug == "check_ufw_status"

    def test_short_name_slugified(self):
        """short_name with spaces or special chars is slugified."""
        plan = {
            "task": "some task",
            "created_at": "2025-06-01T10:00:00+00:00",
            "strategy": {"short_name": "UFW Check — Without Root!"},
            "subtasks": [],
        }
        with tempfile.TemporaryDirectory() as d:
            slug, _ = MadPlanOrchestrator().save_plan(plan, d)
            assert re.match(r"^[a-z0-9_]+$", slug)
            assert "ufw" in slug

    def test_parse_strategy_adds_short_name_default(self):
        """_parse_strategy must always return short_name key."""
        raw = json.dumps({
            "task_summary": "Check UFW",
            "primary_approach": "Read config",
            "constraints": [],
            "fallback_approaches": [],
            "discovery_needed": [],
            "execution_phases": [],
            "notes": "",
        })
        result = MadPlanOrchestrator()._parse_strategy(raw)
        assert "short_name" in result
        assert result["short_name"] == ""

    def test_parse_strategy_preserves_short_name(self):
        """short_name from LLM is preserved and slugified."""
        raw = json.dumps({
            "task_summary": "Check UFW",
            "short_name": "UFW Status Probe",
            "primary_approach": "Read config",
            "constraints": [],
            "fallback_approaches": [],
            "discovery_needed": [],
            "execution_phases": [],
            "notes": "",
        })
        result = MadPlanOrchestrator()._parse_strategy(raw)
        assert result["short_name"] == "ufw_status_probe"
