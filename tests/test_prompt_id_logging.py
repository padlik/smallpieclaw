"""Tests for prompt_id propagation into structured logs.

These tests verify that the run entry points (main agent and sub-agent
supervisor) call ``bind_run_context`` with the correct ``prompt_id`` so every
log line carries the operator-facing prompt handle.
"""

from __future__ import annotations

import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
import structlog

import agent_logging as al


@pytest.fixture(autouse=True)
def _reset_logging(tmp_path):
    """Use a fresh JSONL sink and clear structlog context for each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    structlog.contextvars.clear_contextvars()

    json_file = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)

    yield tmp_path, json_file

    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def _last_json(json_file: str) -> dict:
    with open(json_file, encoding="utf-8") as f:
        return json.loads(f.read().strip().splitlines()[-1])


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


class TestMainAgentPromptIdBinding:
    """AgentController.run() must bind prompt_id into the log context."""

    def test_run_calls_bind_run_context_with_prompt_id(self):
        from agent_controller import AgentController

        mock_llm = MagicMock()
        mock_llm._active_idx = 0
        mock_llm._models = []
        mock_llm.llm_cfg = {"model": "test"}

        controller = AgentController(
            llm=mock_llm,
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
        )
        controller.tool_index.search.return_value = []
        controller.memory.as_prompt_text.return_value = ""

        with patch("agent_controller.bind_run_context") as mock_bind, \
             patch("agent_controller.react_loop", return_value="done"):
            controller.run("hello", prompt_id=42)

        mock_bind.assert_called_once()
        _, kwargs = mock_bind.call_args
        assert kwargs.get("prompt_id") == "42"
        assert kwargs.get("agent") == "main"
        assert kwargs.get("trace", "").startswith("r-")


class TestSubAgentPromptIdBinding:
    """SubAgentSupervisor._run_and_notify must bind the parent's prompt_id."""

    def test_supervisor_binds_parent_prompt_id_before_run(self, tmp_path):
        from builtin_executor import BuiltinExecutor
        from sub_agent_supervisor import SupervisionOptions

        class FakeRunner:
            def __init__(self):
                self.agent_id = "sa-test"
                self._model_id = "test-model"
                self._cancel_event = threading.Event()
                self._llm = MagicMock()
                self._agent = MagicMock()
                self._agent._trace_id = "r-parent"
                self.closed = False
                self.prompt_id_seen = None

            def run(self, task, prompt_id=None):
                self.prompt_id_seen = prompt_id
                return "done"

            def close(self):
                self.closed = True

        runner = FakeRunner()

        exc = BuiltinExecutor(
            sub_agent_factory=lambda **_kw: runner,
            data_dir=str(tmp_path),
        )

        with patch("sub_agent_supervisor.bind_run_context") as mock_bind:
            res = exc._exec_spawn_agent(
                {"task": "subtask"},
                caller_depth=0,
                options=SupervisionOptions(prompt_id=7, notify=False),
            )

        assert res.get("success") is True
        assert runner.prompt_id_seen == 7
        mock_bind.assert_called_once()
        _, kwargs = mock_bind.call_args
        assert kwargs.get("prompt_id") == "7"
        assert kwargs.get("agent") == "sa-test"

    def test_supervisor_clears_run_context_after_run(self, tmp_path):
        from builtin_executor import BuiltinExecutor
        from sub_agent_supervisor import SupervisionOptions

        class FakeRunner:
            def __init__(self):
                self.agent_id = "sa-c"
                self._model_id = "test-model"
                self._cancel_event = threading.Event()
                self._llm = MagicMock()
                self._agent = MagicMock()
                self._agent._trace_id = None
                self.run_done = threading.Event()

            def run(self, task, prompt_id=None):
                self.run_done.set()
                return "done"

            def close(self):
                pass

        exc = BuiltinExecutor(
            sub_agent_factory=lambda **_kw: FakeRunner(),
            data_dir=str(tmp_path),
        )

        cleared = threading.Event()

        def _patched_clear():
            al.clear_run_context()
            cleared.set()

        with patch("sub_agent_supervisor.clear_run_context", side_effect=_patched_clear) as mock_clear:
            exc._exec_spawn_agent(
                {"task": "subtask"},
                options=SupervisionOptions(notify=False),
            )
            assert cleared.wait(timeout=5.0), "sub-agent task did not clear run context"

        mock_clear.assert_called_once()

    def test_supervisor_uses_legacy_run_signature_without_prompt_id(self, tmp_path):
        """A runner that does not accept prompt_id must still run without a
        bare TypeError being swallowed and retried."""
        from builtin_executor import BuiltinExecutor
        from sub_agent_supervisor import SupervisionOptions

        class LegacyRunner:
            def __init__(self):
                self.agent_id = "sa-legacy"
                self._model_id = "test-model"
                self._cancel_event = threading.Event()
                self._llm = MagicMock()
                self._agent = MagicMock()
                self._agent._trace_id = None
                self.prompt_id_seen = "not-called"

            def run(self, task):
                self.prompt_id_seen = None
                return "legacy-done"

            def close(self):
                pass

        runner = LegacyRunner()
        exc = BuiltinExecutor(
            sub_agent_factory=lambda **_kw: runner,
            data_dir=str(tmp_path),
        )

        with patch("sub_agent_supervisor.bind_run_context"):
            res = exc._exec_spawn_agent(
                {"task": "subtask"},
                options=SupervisionOptions(prompt_id=7, notify=False),
            )

        assert res.get("success") is True
        assert runner.prompt_id_seen is None


class TestBindRunContextField:
    """Directly verify bind_run_context emits prompt_id into JSONL records."""

    def test_prompt_id_appears_as_structured_field(self, tmp_path):
        al.bind_run_context(trace="r-1234", agent="main", prompt_id="7")
        logging.getLogger("test").info("hello")
        _flush()
        obj = _last_json(str(tmp_path / "agent.jsonl"))
        assert obj.get("prompt_id") == "7"
        assert obj.get("trace") == "r-1234"
        assert obj.get("agent") == "main"

    def test_unbound_log_has_no_prompt_id(self, tmp_path):
        logging.getLogger("startup").info("boot")
        _flush()
        obj = _last_json(str(tmp_path / "agent.jsonl"))
        assert "prompt_id" not in obj


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
