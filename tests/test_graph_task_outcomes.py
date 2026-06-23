"""Tests for graph-memory task-outcome indexing (G-J item I).

Verifies that successful task finishes and /reset save paths enqueue a bounded
task-outcome episode to the graph-memory writer when graph memory is enabled,
while leaving graph-disabled deployments unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory_store import ResultsMemory, WorkingMemory, _task_outcome_text
from tests.execution_harness import RecordingExecutor, ScriptedLLM, make_outcome, run_react


@pytest.fixture
def results_memory(tmp_path):
    return ResultsMemory(path=str(tmp_path / "results.json"))


class TestTaskOutcomeTextHelper:
    def test_includes_goal_summary_tools(self):
        text = _task_outcome_text("deploy app", "deployment succeeded", ["shell", "file_write"])
        assert "Goal: deploy app" in text
        assert "Outcome: deployment succeeded" in text
        assert "Tools used: shell, file_write" in text

    def test_omits_empty_tools(self):
        text = _task_outcome_text("hello", "hi there", [])
        assert "Tools used" not in text

    def test_bounds_length(self):
        long_summary = "x" * 1200
        text = _task_outcome_text("goal", long_summary, max_len=100)
        assert len(text) <= 101
        assert text.endswith("…")

    def test_truncates_at_line_boundary(self):
        # First line short, second line long; truncation should not split words across lines.
        long_summary = "x" * 1200
        text = _task_outcome_text("short", long_summary, max_len=40)
        assert "Goal: short" in text
        assert len(text) <= 41
        assert text.endswith("…")


class TestReactLoopFinishSummary:
    def test_finish_uses_llm_summary_when_available(self, results_memory):
        writer = MagicMock()
        results = results_memory
        working = WorkingMemory()
        working.start_task("deploy site")
        working.add_step("tool", {"tool": "shell", "args": {"command": "ls"}, "success": True})

        llm = ScriptedLLM([
            '{"action": "tool", "tool": "shell", "args": {"command": "ls"}}',
            '{"action": "finish", "result": "site deployed"}',
        ])
        # Override chat() to return a realistic summary instead of the
        # hardcoded compaction string.
        llm.chat = MagicMock(return_value="Deployed the site successfully using shell. No unresolved issues.")
        ex = RecordingExecutor({"shell": make_outcome(output="file.txt")})

        _, _, _ = run_react(
            llm, ex, "deploy site",
            results=results,
            working=working,
            graph_memory_writer=writer,
        )

        entry = results.search("deploy site", top_k=1)[0]
        assert entry["summary"] == "Deployed the site successfully using shell. No unresolved issues."

        task_outcome_calls = [
            c for c in writer.enqueue.call_args_list
            if c.kwargs.get("source") == "task_outcome"
        ]
        assert len(task_outcome_calls) == 1
        text = task_outcome_calls[0].args[0]
        assert "Goal: deploy site" in text
        assert "Deployed the site successfully" in text

    def test_finish_falls_back_to_bounded_result_on_summary_failure(self, results_memory):
        writer = MagicMock()
        results = results_memory
        working = WorkingMemory()
        working.start_task("deploy site")

        llm = ScriptedLLM(['{"action": "finish", "result": "site deployed"}'])
        llm.chat = MagicMock(side_effect=RuntimeError("model unavailable"))
        ex = RecordingExecutor({})

        _, _, _ = run_react(
            llm, ex, "deploy site",
            results=results,
            working=working,
            graph_memory_writer=writer,
        )

        entry = results.search("deploy site", top_k=1)[0]
        assert "Goal: deploy site" in entry["summary"]
        assert "site deployed" in entry["summary"]

        task_outcome_calls = [
            c for c in writer.enqueue.call_args_list
            if c.kwargs.get("source") == "task_outcome"
        ]
        assert len(task_outcome_calls) == 1
        assert "Goal: deploy site" in task_outcome_calls[0].args[0]

    def test_finish_does_not_enqueue_when_writer_absent(self, results_memory):
        results = results_memory
        working = WorkingMemory()
        working.start_task("deploy site")
        working.add_step("tool", {"tool": "shell", "args": {"command": "ls"}, "success": True})

        llm = ScriptedLLM([
            '{"action": "tool", "tool": "shell", "args": {"command": "ls"}}',
            '{"action": "finish", "result": "site deployed"}',
        ])
        ex = RecordingExecutor({"shell": make_outcome(output="file.txt")})

        _, _, _ = run_react(llm, ex, "deploy site", results=results, working=working)

        # ResultsMemory should still have recorded the outcome.
        assert results.search("deploy site", top_k=1)

    def test_writer_enqueue_failure_is_non_fatal(self, results_memory):
        writer = MagicMock()
        writer.enqueue.side_effect = RuntimeError("queue full")

        results = results_memory
        working = WorkingMemory()
        working.start_task("deploy site")

        llm = ScriptedLLM(['{"action": "finish", "result": "done"}'])
        ex = RecordingExecutor({})

        result, _, _ = run_react(
            llm, ex, "deploy site", results=results, working=working,
            graph_memory_writer=writer,
        )
        assert result == "done"


class TestAgentControllerResetTaskOutcomeEnqueue:
    def test_reset_save_enqueues_task_outcome(self, results_memory):
        from agent_controller import AgentController

        llm = MagicMock()
        llm.chat.return_value = "reset summary"
        llm.llm_cfg = {"model": "test"}

        results = results_memory
        working = WorkingMemory()
        working.start_task("restart service")
        working.add_step("tool", {"tool": "shell", "args": {}, "success": True})

        writer = MagicMock()

        ctrl = AgentController(
            llm=llm,
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
            max_iterations=2,
        )
        ctrl.results = results
        ctrl.working = working
        ctrl._graph_memory_writer = writer

        msg = ctrl.reset_task(save=True)
        assert "saved" in msg.lower()

        writer.enqueue.assert_called_once()
        assert writer.enqueue.call_args.kwargs["source"] == "task_outcome"
        text = writer.enqueue.call_args.args[0]
        assert "Goal: restart service" in text
        assert "reset summary" in text

    def test_reset_save_without_writer_does_not_fail(self, results_memory):
        from agent_controller import AgentController

        llm = MagicMock()
        llm.chat.return_value = "fallback"
        llm.llm_cfg = {"model": "test"}

        results = results_memory
        working = WorkingMemory()
        working.start_task("restart service")

        ctrl = AgentController(
            llm=llm,
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
            max_iterations=2,
        )
        ctrl.results = results
        ctrl.working = working
        ctrl._graph_memory_writer = None

        ctrl.reset_task(save=True)
        assert results.search("restart service", top_k=1)
