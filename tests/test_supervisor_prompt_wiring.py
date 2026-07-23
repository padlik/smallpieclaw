"""Tests for sub-agent supervisor prompt-registry wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from builtin_executor import BuiltinExecutor
from prompt_registry import PromptRegistry


class FakeRunner:
    """Minimal SubAgentRunner stand-in."""

    def __init__(self, agent_id: str = "sa-test"):
        self.agent_id = agent_id
        self._model_id = "test-model"
        self._cancel_event = MagicMock()
        self._llm = MagicMock()
        self._agent = MagicMock()
        self._short_term = MagicMock()
        self.notify_fn = MagicMock()
        self.closed = False

    def run(self, task):
        return "done"

    def close(self):
        self.closed = True


def _seq_factory(runners):
    it = iter(runners)

    def factory(**_kwargs):
        return next(it)

    return factory


@pytest.fixture
def registry(tmp_path):
    return PromptRegistry(data_dir=str(tmp_path))


class TestPromptRegistryWiring:
    def test_spawned_sub_agent_recorded_against_active_prompt(self, tmp_path, registry):
        runner = FakeRunner(agent_id="sa-prompt")
        exc = BuiltinExecutor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._prompt_registry = registry
        exc._current_prompt_id = registry.start("r-active", "active task").prompt_id

        res = exc._exec_spawn_agent({"task": "do work"}, caller_depth=0)
        assert res["success"] is True
        assert res["agent_id"] == "sa-prompt"

        record = registry.get(exc._current_prompt_id)
        assert record is not None
        assert record.sub_agent_ids == ["sa-prompt"]

    def test_no_recording_when_current_prompt_id_is_none(self, tmp_path, registry):
        runner = FakeRunner(agent_id="sa-no-prompt")
        exc = BuiltinExecutor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._prompt_registry = registry
        # _current_prompt_id remains None (e.g. scheduled run without prompt context)

        res = exc._exec_spawn_agent({"task": "do work"}, caller_depth=0)
        assert res["success"] is True

        # No prompt should have recorded a sub-agent.
        recent = registry.list_recent()
        assert recent == []


class TestDepthGuardProtectsExecutorFields:
    """C2 regression: sub-agent AgentController.run() (depth=1) must not clobber
    the shared executor's _prompt_approval_set / _current_prompt_id."""

    def test_sub_agent_run_preserves_parent_executor_fields(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from agent_controller import AgentController
        from builtin_executor import BuiltinExecutor

        executor = BuiltinExecutor(data_dir=str(tmp_path))
        sentinel_set: set = {"file_read"}
        executor._prompt_approval_set = sentinel_set
        executor._current_prompt_id = "01JARYN6R0ABCDEFGHJKMNPQRS"

        llm = MagicMock()
        llm._active_idx = 0

        ctrl = AgentController(
            llm=llm,
            tool_index=MagicMock(),
            memory=MagicMock(),
            builtin_executor=executor,
            depth=1,  # sub-agent
        )

        with patch("agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()):
            with patch("agent_controller.react_loop", return_value="sub done"):
                with patch("agent_controller.bind_run_context"):
                    result = ctrl.run("sub-task", prompt_id="01JARYN6R0ABCDEFGHJKMNPQRS")

        assert result == "sub done"
        # Parent's fields survive the sub-agent run unchanged
        assert executor._prompt_approval_set is sentinel_set
        assert executor._current_prompt_id == "01JARYN6R0ABCDEFGHJKMNPQRS"
