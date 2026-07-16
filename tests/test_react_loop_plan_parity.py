"""Tests for json_mode plan parity: on_tool_trace and working.add_step emitted after plan."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests.execution_harness import ScriptedLLM, RecordingExecutor, run_react

_PLAN_ACTION = json.dumps({
    "action": "plan",
    "plan": {
        "description": "test plan",
        "steps": [{"id": "s1", "tool": "shell", "args": {"command": "echo hi"}}],
        "timeout": 30,
    },
})
_FINISH_ACTION = '{"action": "finish", "result": "done"}'


class TestJsonModePlanParity:
    def test_json_mode_plan_emits_tool_trace(self):
        """Plan action in json_mode emits a tool trace with tool_name == 'plan'."""
        traces = []
        llm = ScriptedLLM([_PLAN_ACTION, _FINISH_ACTION])
        ex = RecordingExecutor()
        with (
            patch("execution_plan.PlanExecutor") as mock_pe,
            patch("execution_plan.PlanStep"),
            patch("execution_plan.ExecutionPlan"),
        ):
            mock_pe.return_value.execute.return_value = {"results": {}, "summary": "ok"}
            run_react(llm, ex, "run a plan", on_tool_trace=traces.append)
        assert any(t.tool_name == "plan" for t in traces)

    def test_json_mode_plan_emits_working_add_step(self):
        """Plan action in json_mode calls working.add_step('plan', ...)."""
        llm = ScriptedLLM([_PLAN_ACTION, _FINISH_ACTION])
        ex = RecordingExecutor()
        working = MagicMock()
        working.has_content.return_value = False
        with (
            patch("execution_plan.PlanExecutor") as mock_pe,
            patch("execution_plan.PlanStep"),
            patch("execution_plan.ExecutionPlan"),
        ):
            mock_pe.return_value.execute.return_value = {"results": {}, "summary": "ok"}
            run_react(llm, ex, "run a plan", working=working)
        plan_calls = [c for c in working.add_step.call_args_list if c.args[0] == "plan"]
        assert plan_calls
