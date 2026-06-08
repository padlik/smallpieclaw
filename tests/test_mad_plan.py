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

from config_schema import resolve_model_id as _resolve_model_id
from mad_plan import (
    MadPlanError,
    MadPlanOrchestrator,
    _escape_prompt,
    _topo_sort_subtasks,
    _unescape_prompt,
    build_agent_capabilities_summary,
    delete_plan,
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
        def factory(model=None, label=None, temperature=None, top_p=None, max_tokens=None, on_tool_trace=None):
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

    def test_partial_revision_preserves_original_model_and_prompt(self):
        """When LLM returns subtasks but omits selections, original model/prompt must be kept."""
        partial = self._valid_revision()
        del partial["selections"]  # LLM omits selections
        llm = self._make_llm(partial)
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "some feedback", llm, [], self.CONFIGURED_MODELS)
        # The original plan has one subtask "read_ufw_config" with gpt-4 and a known prompt
        original_st = next((s for s in result["subtasks"] if s["id"] == "read_ufw_config"), None)
        if original_st:
            assert original_st["model_name"] == "gpt-4"
            assert original_st["prompt"] == "Read /etc/ufw/ufw.conf and report status."

    def test_partial_revision_no_none_params_for_preserved_subtasks(self):
        """Preserved subtasks must keep original params, not get None for all fields."""
        partial = self._valid_revision()
        partial["subtasks"] = [
            {"id": "read_ufw_config", "name": "Read UFW config",
             "description": "Parse /etc/ufw/ufw.conf", "depends_on": []}
        ]
        del partial["selections"]
        llm = self._make_llm(partial)
        orc = MadPlanOrchestrator()
        result = orc.revise_plan(self.ORIGINAL_PLAN, "keep existing", llm, [], self.CONFIGURED_MODELS)
        st = result["subtasks"][0]
        assert st["params"]["temperature"] == 0.2  # from ORIGINAL_PLAN
        assert st["params"]["max_tokens"] == 2000

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


class TestDeletePlan:
    """Tests for delete_plan()."""

    PLAN = {
        "task": "Test plan for deletion",
        "created_at": "2025-06-01T10:00:00+00:00",
        "subtasks": [],
    }

    def _save_plan(self, d):
        orc = MadPlanOrchestrator()
        slug, _ = orc.save_plan(self.PLAN, d)
        return slug

    def test_delete_removes_directory(self):
        with tempfile.TemporaryDirectory() as d:
            slug = self._save_plan(d)
            assert os.path.isdir(os.path.join(d, slug))
            delete_plan(slug, d)
            assert not os.path.exists(os.path.join(d, slug))

    def test_delete_removes_from_list(self):
        with tempfile.TemporaryDirectory() as d:
            slug = self._save_plan(d)
            assert slug in list_plans(d)
            delete_plan(slug, d)
            assert slug not in list_plans(d)

    def test_delete_not_found_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(MadPlanError, match="not found"):
                delete_plan("nonexistent_plan", d)

    def test_delete_empty_name_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(MadPlanError, match="must not be empty"):
                delete_plan("", d)

    def test_delete_path_traversal_slash_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(MadPlanError, match="path-traversal"):
                delete_plan("../some_plan", d)

    def test_delete_path_traversal_dotdot_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(MadPlanError, match="path-traversal"):
                delete_plan("valid..evil", d)

    def test_delete_does_not_remove_dir_without_plan_md(self):
        """Should not delete a directory that exists but has no plan.md."""
        with tempfile.TemporaryDirectory() as d:
            fake_dir = os.path.join(d, "not_a_plan")
            os.makedirs(fake_dir)
            with pytest.raises(MadPlanError, match="not found"):
                delete_plan("not_a_plan", d)
            assert os.path.isdir(fake_dir)


class TestLoadPlanSecurity:
    """Path-traversal guard tests for load_plan()."""

    def test_load_plan_empty_name_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="must not be empty"):
                orc.load_plan("", d)

    def test_load_plan_slash_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="path-traversal"):
                orc.load_plan("../etc/passwd", d)

    def test_load_plan_backslash_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="path-traversal"):
                orc.load_plan("evil\\plan", d)

    def test_load_plan_dotdot_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="path-traversal"):
                orc.load_plan("valid..evil", d)

    def test_load_plan_valid_name_not_found_raises(self):
        with tempfile.TemporaryDirectory() as d:
            orc = MadPlanOrchestrator()
            with pytest.raises(MadPlanError, match="not found"):
                orc.load_plan("nonexistent_plan", d)


# ---------------------------------------------------------------------------
# _extract_json (brace-counting)
# ---------------------------------------------------------------------------

class TestExtractJson:
    """Tests for _extract_json() — specifically the brace-counting fallback."""

    def _call(self, raw: str) -> dict:
        from mad_plan import _extract_json
        return _extract_json(raw)

    def test_clean_json(self):
        assert self._call('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert self._call('```json\n{"a": 1}\n```') == {"a": 1}

    def test_trailing_text_after_json(self):
        """Greedy regex would have matched too much; brace-counting finds the right end."""
        assert self._call('{"a": 1} some trailing text {"b": 2}') == {"a": 1}

    def test_embedded_in_prose(self):
        raw = 'Here is the result:\n{"x": "hello"}\nDone.'
        assert self._call(raw) == {"x": "hello"}

    def test_nested_objects(self):
        raw = 'prefix {"outer": {"inner": 42}} suffix}'
        assert self._call(raw) == {"outer": {"inner": 42}}

    def test_invalid_json_raises(self):
        from mad_plan import MadPlanError
        with pytest.raises(MadPlanError):
            self._call("this is not json at all")

    def test_string_with_braces_inside_value(self):
        """Braces inside string literals must not confuse the counter."""
        assert self._call('{"key": "value with } brace"}') == {"key": "value with } brace"}


# ---------------------------------------------------------------------------
# _parse_decomposition — ID deduplication
# ---------------------------------------------------------------------------

class TestSubtaskIdDeduplication:
    """Tests for ID collision handling in _parse_decomposition."""

    def _orc(self):
        return MadPlanOrchestrator()

    def test_duplicate_slugs_get_suffix(self):
        """Two subtasks whose names slugify identically should get unique IDs."""
        raw = json.dumps({"subtasks": [
            {"id": "t1", "name": "Fetch user data", "description": "a", "depends_on": []},
            {"id": "t2", "name": "Fetch user-data", "description": "b", "depends_on": []},
        ]})
        result = self._orc()._parse_decomposition(raw)
        ids = [st["id"] for st in result]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"
        assert ids[0] == "fetch_user_data"
        assert ids[1] == "fetch_user_data_2"

    def test_three_duplicates_get_sequential_suffixes(self):
        raw = json.dumps({"subtasks": [
            {"id": "t1", "name": "Do thing", "description": "", "depends_on": []},
            {"id": "t2", "name": "Do thing", "description": "", "depends_on": []},
            {"id": "t3", "name": "Do thing", "description": "", "depends_on": []},
        ]})
        result = self._orc()._parse_decomposition(raw)
        ids = [st["id"] for st in result]
        assert ids == ["do_thing", "do_thing_2", "do_thing_3"]

    def test_no_collision_unchanged(self):
        raw = json.dumps({"subtasks": [
            {"id": "t1", "name": "Alpha task", "description": "", "depends_on": []},
            {"id": "t2", "name": "Beta task", "description": "", "depends_on": []},
        ]})
        result = self._orc()._parse_decomposition(raw)
        assert result[0]["id"] == "alpha_task"
        assert result[1]["id"] == "beta_task"


# ---------------------------------------------------------------------------
# _resolve_model_id
# ---------------------------------------------------------------------------

class TestResolveModelId:
    """Unit tests for the _resolve_model_id helper."""

    MODELS = [
        {"model": "kimi-k2.5:cloud", "name": "kimi-k2.5", "aliases": ["kimi-k25"]},
        {"model": "deepseek-v4-pro:cloud", "name": "deepseek-v4-pro", "aliases": []},
        {"model": "gemini-3-flash:cloud", "name": "gemini-3-flash", "aliases": ["gemini-flash"]},
    ]

    def test_exact_model_id_unchanged(self):
        assert _resolve_model_id("kimi-k2.5:cloud", self.MODELS) == "kimi-k2.5:cloud"

    def test_resolves_name_to_model_id(self):
        assert _resolve_model_id("kimi-k2.5", self.MODELS) == "kimi-k2.5:cloud"

    def test_resolves_name_case_insensitive(self):
        assert _resolve_model_id("KIMI-K2.5", self.MODELS) == "kimi-k2.5:cloud"

    def test_resolves_alias(self):
        assert _resolve_model_id("kimi-k25", self.MODELS) == "kimi-k2.5:cloud"

    def test_resolves_alias_case_insensitive(self):
        assert _resolve_model_id("Gemini-Flash", self.MODELS) == "gemini-3-flash:cloud"

    def test_resolves_second_model_by_name(self):
        assert _resolve_model_id("deepseek-v4-pro", self.MODELS) == "deepseek-v4-pro:cloud"

    def test_unknown_returns_empty_string(self):
        assert _resolve_model_id("nonexistent-model", self.MODELS) == ""

    def test_empty_selection_returns_empty_string(self):
        assert _resolve_model_id("", self.MODELS) == ""

    def test_empty_model_list(self):
        assert _resolve_model_id("kimi-k2.5", []) == ""


# ---------------------------------------------------------------------------
# _topo_sort_subtasks
# ---------------------------------------------------------------------------

class TestTopoSortSubtasks:
    def _st(self, id_, depends_on=None):
        return {"id": id_, "name": id_, "depends_on": depends_on or []}

    def test_empty_list(self):
        assert _topo_sort_subtasks([]) == []

    def test_single_subtask_no_deps(self):
        st = self._st("a")
        assert _topo_sort_subtasks([st]) == [st]

    def test_already_ordered(self):
        a, b = self._st("a"), self._st("b", ["a"])
        result = _topo_sort_subtasks([a, b])
        assert [s["id"] for s in result] == ["a", "b"]

    def test_reverse_order_reordered(self):
        # b depends on a, but b is listed first — must be reordered
        a, b = self._st("a"), self._st("b", ["a"])
        result = _topo_sort_subtasks([b, a])
        assert [s["id"] for s in result] == ["a", "b"]

    def test_chain_three(self):
        a = self._st("a")
        b = self._st("b", ["a"])
        c = self._st("c", ["b"])
        result = _topo_sort_subtasks([c, b, a])
        ids = [s["id"] for s in result]
        assert ids.index("a") < ids.index("b") < ids.index("c")

    def test_diamond_dependency(self):
        a = self._st("a")
        b = self._st("b", ["a"])
        c = self._st("c", ["a"])
        d = self._st("d", ["b", "c"])
        result = _topo_sort_subtasks([d, c, b, a])
        ids = [s["id"] for s in result]
        assert ids.index("a") < ids.index("b")
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("d")
        assert ids.index("c") < ids.index("d")

    def test_independent_subtasks_all_present(self):
        a, b, c = self._st("a"), self._st("b"), self._st("c")
        result = _topo_sort_subtasks([a, b, c])
        assert {s["id"] for s in result} == {"a", "b", "c"}

    def test_cycle_raises(self):
        a = self._st("a", ["b"])
        b = self._st("b", ["a"])
        with pytest.raises(MadPlanError, match="cycle"):
            _topo_sort_subtasks([a, b])

    def test_external_dep_ignored_for_ordering(self):
        # depends_on references an ID not in the subtask list — no crash, no order change
        a = self._st("a", ["external_task"])
        b = self._st("b", ["a"])
        result = _topo_sort_subtasks([b, a])
        assert [s["id"] for s in result] == ["a", "b"]


# ---------------------------------------------------------------------------
# save_plan / load_plan multi-line prompt round-trip
# ---------------------------------------------------------------------------

class TestSavePlanLoadPlanPromptRoundTrip:
    def _orc(self):
        return MadPlanOrchestrator()

    def _make_plan(self, prompt: str) -> dict:
        return {
            "task": "Test task",
            "created_at": "2026-01-01T00:00:00+00:00",
            "strategy": {},
            "subtasks": [
                {
                    "id": "subtask_1",
                    "name": "Do something",
                    "description": "desc",
                    "model_name": "test-model",
                    "params": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 512},
                    "prompt": prompt,
                    "depends_on": [],
                }
            ],
        }

    def test_single_line_prompt_roundtrip(self, tmp_path):
        orc = self._orc()
        prompt = "Do the thing with argument X and return Y."
        plan = self._make_plan(prompt)
        name, _ = orc.save_plan(plan, str(tmp_path))
        loaded = orc.load_plan(name, str(tmp_path))
        assert loaded["subtasks"][0]["prompt"] == prompt

    def test_multiline_prompt_roundtrip(self, tmp_path):
        orc = self._orc()
        prompt = "Step 1: fetch the data.\nStep 2: transform it.\nStep 3: upload results."
        plan = self._make_plan(prompt)
        name, _ = orc.save_plan(plan, str(tmp_path))
        loaded = orc.load_plan(name, str(tmp_path))
        assert loaded["subtasks"][0]["prompt"] == prompt

    def test_empty_prompt_roundtrip(self, tmp_path):
        orc = self._orc()
        plan = self._make_plan("")
        name, _ = orc.save_plan(plan, str(tmp_path))
        loaded = orc.load_plan(name, str(tmp_path))
        assert loaded["subtasks"][0]["prompt"] == ""

    def test_prompt_with_backslash_n_literal(self, tmp_path):
        # A prompt that already contains the literal text \n (not a real newline)
        orc = self._orc()
        prompt = r"Use \n as a separator between items."
        plan = self._make_plan(prompt)
        name, _ = orc.save_plan(plan, str(tmp_path))
        loaded = orc.load_plan(name, str(tmp_path))
        assert loaded["subtasks"][0]["prompt"] == prompt


# ---------------------------------------------------------------------------
# _escape_prompt / _unescape_prompt
# ---------------------------------------------------------------------------

class TestEscapeUnescapePrompt:
    def test_roundtrip_plain(self):
        s = "Hello world"
        assert _unescape_prompt(_escape_prompt(s)) == s

    def test_roundtrip_newline(self):
        s = "Step 1\nStep 2\nStep 3"
        assert _unescape_prompt(_escape_prompt(s)) == s

    def test_roundtrip_backslash(self):
        s = "Use \\ as escape"
        assert _unescape_prompt(_escape_prompt(s)) == s

    def test_roundtrip_literal_backslash_n(self):
        s = r"Use \n as separator"
        assert _unescape_prompt(_escape_prompt(s)) == s

    def test_roundtrip_mixed(self):
        s = "Line 1\nLine 2 with \\n literal and \\ backslash"
        assert _unescape_prompt(_escape_prompt(s)) == s

    def test_escape_is_single_line(self):
        s = "a\nb\nc"
        assert "\n" not in _escape_prompt(s)


class TestMadPlanSession:
    """Tests for MadPlanSession state machine."""

    def test_initial_state_is_off(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession()
        assert s.state == MadPlanState.OFF
        assert not s.is_on
        assert not s.is_executing

    def test_transition_off_to_planning(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession()
        s.transition(MadPlanState.PLANNING)
        assert s.state == MadPlanState.PLANNING
        assert s.is_on

    def test_transition_planning_to_executing(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.PLANNING)
        s.transition(MadPlanState.EXECUTING)
        assert s.is_executing

    def test_illegal_transition_raises(self):
        from mad_plan import MadPlanSession, MadPlanState, MadPlanError
        s = MadPlanSession()  # Off
        with pytest.raises(MadPlanError, match="Cannot transition"):
            s.transition(MadPlanState.EXECUTING)

    def test_can_transition(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.PLANNING)
        assert s.can_transition(MadPlanState.EXECUTING)
        assert s.can_transition(MadPlanState.OFF)
        assert not s.can_transition(MadPlanState.PLANNING)

    def test_to_json_roundtrip(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(
            state=MadPlanState.PLANNING,
            plan_name="test_plan",
            dirty=True,
            last_run="2026-01-01T00:00:00Z",
            last_run_success=True,
        )
        data = s.to_json()
        s2 = MadPlanSession.from_json(data)
        assert s2.state == MadPlanState.PLANNING
        assert s2.plan_name == "test_plan"
        assert s2.dirty is True
        assert s2.last_run == "2026-01-01T00:00:00Z"

    def test_persist_and_load(self, tmp_path):
        from mad_plan import MadPlanSession, MadPlanState, load_session
        s = MadPlanSession(
            state=MadPlanState.PLANNING,
            plan_name="my_plan",
            dirty=True,
        )
        s.persist(str(tmp_path))
        loaded = load_session(str(tmp_path), "my_plan")
        assert loaded.state == MadPlanState.PLANNING
        assert loaded.plan_name == "my_plan"
        assert loaded.dirty is True

    def test_persist_noop_without_plan_name(self, tmp_path):
        from mad_plan import MadPlanSession
        s = MadPlanSession()  # no plan_name
        s.persist(str(tmp_path))
        # No file created
        assert not (tmp_path / "session.json").exists()

    def test_load_session_missing_returns_default(self, tmp_path):
        from mad_plan import MadPlanState, load_session
        s = load_session(str(tmp_path), "nonexistent")
        assert s.state == MadPlanState.PLANNING
        assert s.plan_name == "nonexistent"

    def test_executing_to_planning_transition(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.EXECUTING)
        s.transition(MadPlanState.PLANNING)
        assert s.state == MadPlanState.PLANNING

    def test_in_review_to_planning_transition(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.IN_REVIEW)
        s.transition(MadPlanState.PLANNING)
        assert s.state == MadPlanState.PLANNING

    def test_in_review_to_off_transition(self):
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.IN_REVIEW)
        s.transition(MadPlanState.OFF)
        assert s.state == MadPlanState.OFF

    def test_session_does_not_have_cancel_event(self, tmp_path):
        """cancel_event lives on _UserState, not MadPlanSession (runtime-only)."""
        from mad_plan import MadPlanSession, MadPlanState
        s = MadPlanSession(state=MadPlanState.PLANNING, plan_name="ce_test")
        assert not hasattr(s, "cancel_event")


# ---------------------------------------------------------------------------
# execute_plan: cancellation and resume
# ---------------------------------------------------------------------------

class TestExecutePlanCancelResume:
    """Tests for cancel_event and skip_completed/resume_from_dir."""

    PLAN = {
        "task": "cancel-resume test",
        "subtasks": [
            {"id": "a", "name": "Task A", "prompt": "do A", "model_name": "m1",
             "params": {}, "depends_on": []},
            {"id": "b", "name": "Task B", "prompt": "do B", "model_name": "m1",
             "params": {}, "depends_on": ["a"]},
            {"id": "c", "name": "Task C", "prompt": "do C", "model_name": "m1",
             "params": {}, "depends_on": ["b"]},
        ],
    }

    def _make_factory(self, results=None):
        results = results or {}

        def factory(model=None, label=None, **kw):
            runner = MagicMock()
            runner.run.return_value = results.get(label, "ok")
            return runner
        return factory

    def test_cancel_before_first_subtask(self):
        """Cancel event set before execution starts → no subtasks run."""
        import threading
        cancel = threading.Event()
        cancel.set()

        factory = self._make_factory()
        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, cancel_event=cancel,
        )
        assert failure is not None
        assert failure["error"] == "Cancelled by user"
        assert "a" in failure["remaining"]
        assert len(output) == 0

    def test_cancel_after_first_subtask(self):
        """Cancel event set after first subtask → only first subtask result returned."""
        import threading
        cancel = threading.Event()
        call_count = [0]

        def factory(model=None, label=None, **kw):
            runner = MagicMock()
            def _run(prompt):
                call_count[0] += 1
                if call_count[0] == 1:
                    cancel.set()  # cancel after first task runs
                return f"result-{call_count[0]}"
            runner.run = _run
            return runner

        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, cancel_event=cancel,
        )
        assert len(output) == 1
        assert output[0]["id"] == "a"
        assert failure is not None
        assert failure["error"] == "Cancelled by user"
        assert "b" in failure["remaining"]

    def test_skip_completed_skips_subtasks(self):
        """skip_completed prevents matching subtasks from running."""
        call_labels = []

        def factory(model=None, label=None, **kw):
            call_labels.append(label)
            runner = MagicMock()
            runner.run.return_value = "ok"
            return runner

        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, skip_completed={"a", "b"},
        )
        assert failure is None
        # Only "c" should have been executed
        assert len(call_labels) == 1
        assert call_labels[0] == "madplan-c"

    def test_resume_from_dir_loads_results(self, tmp_path):
        """resume_from_dir loads previous results for dependency injection."""
        # Write a previous results.json
        prev_results = [
            {"id": "a", "name": "Task A", "result": "previous-A-result", "traces": []},
            {"id": "b", "name": "Task B", "result": "previous-B-result", "traces": []},
        ]
        results_path = tmp_path / "results.json"
        results_path.write_text(json.dumps(prev_results))

        # Track what prompt subtask C receives
        received_prompts = []

        def factory(model=None, label=None, **kw):
            runner = MagicMock()
            def _run(prompt):
                received_prompts.append(prompt)
                return "result-c"
            runner.run = _run
            return runner

        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory,
            skip_completed={"a", "b"},
            resume_from_dir=str(tmp_path),
        )
        assert failure is None
        assert len(output) == 3  # 2 loaded + 1 new
        # Verify subtask C got the upstream result injected
        assert len(received_prompts) == 1
        assert "previous-B-result" in received_prompts[0]

    def test_resume_with_run_dir_writes_incrementally(self, tmp_path):
        """New results are written to run_dir incrementally."""
        factory = self._make_factory({"madplan-a": "r1", "madplan-b": "r2", "madplan-c": "r3"})
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir)

        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, run_dir=run_dir,
        )
        assert failure is None
        # results.json should exist with all 3 results
        results_file = os.path.join(run_dir, "results.json")
        assert os.path.exists(results_file)
        with open(results_file) as f:
            saved = json.load(f)
        assert len(saved) == 3
        assert saved[0]["id"] == "a"
        assert saved[2]["id"] == "c"


# ---------------------------------------------------------------------------
# _sanitize_plan_name
# ---------------------------------------------------------------------------

class TestSanitizePlanName:
    def test_basic_lowercasing(self):
        from mad_plan import _sanitize_plan_name
        assert _sanitize_plan_name("MyPlan") == "myplan"

    def test_special_chars_replaced(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("hello world!")
        assert " " not in result
        assert "!" not in result

    def test_consecutive_underscores_collapsed(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("foo   bar")
        assert "__" not in result
        assert "foo" in result and "bar" in result

    def test_trailing_underscores_stripped(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("foo!!!")
        assert not result.endswith("_")
        assert result.startswith("foo")

    def test_max_length_50(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("a" * 100)
        assert len(result) <= 50

    def test_empty_after_sanitize_returns_fallback(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("!!!")
        assert len(result) > 0  # Should have some fallback

    def test_hyphens_preserved(self):
        from mad_plan import _sanitize_plan_name
        assert _sanitize_plan_name("my-plan") == "my-plan"

    def test_digits_preserved(self):
        from mad_plan import _sanitize_plan_name
        assert _sanitize_plan_name("plan123") == "plan123"

    def test_empty_string(self):
        from mad_plan import _sanitize_plan_name
        result = _sanitize_plan_name("")
        assert len(result) > 0


# ---------------------------------------------------------------------------
# load_plan module-level wrapper
# ---------------------------------------------------------------------------

class TestLoadPlanModuleWrapper:
    def test_loads_saved_plan(self, tmp_path):
        from mad_plan import load_plan as mod_load_plan
        orc = MadPlanOrchestrator()
        plan = {
            "task": "wrapper test",
            "subtasks": [
                {"id": "s1", "name": "Step One", "description": "do it",
                 "model_name": "m1", "params": {}, "prompt": "go",
                 "rationale": "", "depends_on": []},
            ],
        }
        slug, _ = orc.save_plan(plan, str(tmp_path))
        loaded = mod_load_plan(str(tmp_path), slug)
        assert loaded["task"] == "wrapper test"
        assert len(loaded["subtasks"]) == 1

    def test_raises_on_missing(self, tmp_path):
        from mad_plan import load_plan as mod_load_plan
        with pytest.raises(MadPlanError, match="not found"):
            mod_load_plan(str(tmp_path), "nonexistent")


# ---------------------------------------------------------------------------
# _write_results
# ---------------------------------------------------------------------------

class TestWriteResults:
    def test_creates_dir_and_writes_json(self, tmp_path):
        run_dir = str(tmp_path / "newdir")
        output = [{"id": "a", "result": "ok"}]
        MadPlanOrchestrator._write_results(run_dir, output)
        with open(os.path.join(run_dir, "results.json")) as f:
            assert json.load(f) == output

    def test_atomic_overwrite(self, tmp_path):
        run_dir = str(tmp_path)
        MadPlanOrchestrator._write_results(run_dir, [{"id": "a"}])
        MadPlanOrchestrator._write_results(run_dir, [{"id": "a"}, {"id": "b"}])
        with open(os.path.join(run_dir, "results.json")) as f:
            assert len(json.load(f)) == 2

    def test_cleanup_on_failure(self, tmp_path, monkeypatch):
        run_dir = str(tmp_path / "faildir")
        os.makedirs(run_dir)
        monkeypatch.setattr(json, "dump", lambda *a, **k: (_ for _ in ()).throw(IOError("full")))
        with pytest.raises(IOError):
            MadPlanOrchestrator._write_results(run_dir, [{"id": "x"}])
        remaining = [f for f in os.listdir(run_dir) if f.startswith(".results_")]
        assert remaining == []


# ---------------------------------------------------------------------------
# execute_plan: cancel + skip interaction
# ---------------------------------------------------------------------------

class TestExecutePlanCancelSkipInteraction:
    PLAN = {
        "task": "cancel-skip",
        "subtasks": [
            {"id": "a", "name": "A", "prompt": "A", "model_name": "m", "params": {}, "depends_on": []},
            {"id": "b", "name": "B", "prompt": "B", "model_name": "m", "params": {}, "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt": "C", "model_name": "m", "params": {}, "depends_on": ["b"]},
        ],
    }

    def test_cancel_with_skip_reports_remaining_correctly(self):
        import threading
        cancel = threading.Event()
        cancel.set()
        factory = MagicMock()
        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, cancel_event=cancel, skip_completed={"a"},
        )
        assert failure is not None
        assert failure["error"] == "Cancelled by user"
        # 'a' is in skip so excluded from remaining
        assert "a" not in failure["remaining"]
        factory.assert_not_called()

    def test_skip_all_succeeds(self):
        factory = MagicMock()
        output, failure = MadPlanOrchestrator().execute_plan(
            self.PLAN, factory, skip_completed={"a", "b", "c"},
        )
        assert failure is None
        factory.assert_not_called()
