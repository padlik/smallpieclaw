"""
Tests for MadPlanService — the service layer for MadPlan operations.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mad_plan_service import MadPlanService, PlanResult, ExecutionResult


@pytest.fixture
def service(tmp_path):
    """Create a MadPlanService with mocked dependencies."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    llm_client = MagicMock()
    llm_client.list_models.return_value = [{"model": "gpt-4o", "name": "GPT-4o"}]

    sub_agent_factory = MagicMock()

    return MadPlanService(
        plans_dir=str(plans_dir),
        llm_client=llm_client,
        sub_agent_factory=sub_agent_factory,
        tool_registry=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=MagicMock(),
        data_dir=str(data_dir),
    )


class TestCreatePlan:
    def test_returns_plan_result(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.plan_task.return_value = {"task": "test", "subtasks": []}
            orch.save_plan.return_value = ("test_plan", "/tmp/plans/test_plan/plan.md")
            orch.format_plan_html.return_value = "<b>Plan</b>"

            result = service.create_plan("Build a thing")

            assert isinstance(result, PlanResult)
            assert result.plan == {"task": "test", "subtasks": []}
            assert result.plan_name == "test_plan"
            assert result.saved_path == "/tmp/plans/test_plan/plan.md"
            assert len(result.plan_id) == 8
            assert result.html == "<b>Plan</b>"

    def test_applies_name_override(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.plan_task.return_value = {"task": "original", "subtasks": []}
            orch.save_plan.return_value = ("custom", "/tmp/custom/plan.md")
            orch.format_plan_html.return_value = ""

            result = service.create_plan("Build it", name_override="MyProject")

            assert result.plan["task"] == "MyProject - original"


class TestRevisePlan:
    def test_returns_plan_result(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.revise_plan.return_value = {"task": "revised", "subtasks": []}
            orch.save_plan.return_value = ("existing", "/tmp/existing/plan.md")
            orch.format_plan_html.return_value = "<b>Revised</b>"

            original = {"task": "old", "subtasks": []}
            result = service.revise_plan(original, "make it better", plan_name="existing")

            assert isinstance(result, PlanResult)
            assert result.plan["task"] == "revised"
            orch.save_plan.assert_called_once()
            # target_slug passed when plan_name provided
            call_kwargs = orch.save_plan.call_args[1]
            assert call_kwargs["target_slug"] == "existing"

    def test_generates_new_name_when_no_plan_name(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.revise_plan.return_value = {"task": "new", "subtasks": []}
            orch.save_plan.return_value = ("auto_generated", "/tmp/auto/plan.md")
            orch.format_plan_html.return_value = ""

            result = service.revise_plan({"task": "x"}, "feedback", plan_name="")

            assert result.plan_name == "auto_generated"


class TestExecutePlan:
    def test_returns_execution_result_success(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.execute_plan.return_value = (
                [{"id": "a", "name": "Task A"}, {"id": "b", "name": "Task B"}],
                None,  # no failure
            )

            plan = {"subtasks": [{"id": "a"}, {"id": "b"}]}
            result = service.execute_plan(plan, plan_name="my_plan")

            assert isinstance(result, ExecutionResult)
            assert result.total == 2
            assert result.success is True
            assert result.failure is None
            assert result.run_ts  # non-empty timestamp
            assert "my_plan" in result.run_dir

    def test_returns_execution_result_failure(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            failure = {"subtask_id": "b", "error": "timeout", "remaining": ["c"]}
            orch.execute_plan.return_value = (
                [{"id": "a", "name": "Task A"}],
                failure,
            )

            plan = {"subtasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
            result = service.execute_plan(plan, plan_name="fail_plan")

            assert result.success is False
            assert result.failure == failure
            assert result.total == 1

    def test_writes_trace_json_when_traced(self, service, tmp_path):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.execute_plan.return_value = (
                [{"id": "a", "name": "A", "traces": [{"tool": "shell"}]}],
                None,
            )

            # Point plans_dir to tmp_path so trace.json is written there
            service._plans_dir = str(tmp_path / "plans")

            plan = {"subtasks": [{"id": "a"}]}
            result = service.execute_plan(plan, plan_name="trace_test", traced=True)

            trace_path = os.path.join(result.run_dir, "trace.json")
            assert os.path.exists(trace_path)
            with open(trace_path) as f:
                trace = json.load(f)
            assert trace["traced"] is True
            assert trace["plan_name"] == "trace_test"
            assert len(trace["subtasks"]) == 1

    def test_passes_cancel_event(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.execute_plan.return_value = ([], None)

            cancel = threading.Event()
            service.execute_plan(
                {"subtasks": []}, plan_name="x", cancel_event=cancel,
            )

            call_kwargs = orch.execute_plan.call_args[1]
            assert call_kwargs["cancel_event"] is cancel

    def test_no_run_dir_when_no_plan_name(self, service):
        with patch("mad_plan_service.MadPlanOrchestrator") as MockOrch:
            orch = MockOrch.return_value
            orch.execute_plan.return_value = ([], None)

            result = service.execute_plan({"subtasks": []}, plan_name="")

            assert result.run_dir == ""
