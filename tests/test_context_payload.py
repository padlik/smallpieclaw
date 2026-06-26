"""Unit tests for sub-agent context payload functionality.

Covers:
  - payload truncation in react_loop
  - payload injection into the sub-agent system prompt
  - spawn_agent forwarding + transient nature
  - prompt variant selection for sub-agents vs direct spawn
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from builtin_executor import BuiltinExecutor
from memory_store import ShortTermMemory
from react_loop import (
    ReactContext,
    _format_parent_context,
    _truncate_context_payload,
)


# ---------------------------------------------------------------------------
# Truncate context payload
# ---------------------------------------------------------------------------

class TestTruncateContextPayload:
    """Tests for ``_truncate_context_payload``."""

    def test_small_payload_unchanged(self):
        payload = {"user_goal": "check disk usage", "memory": "warn at 90%"}
        assert _truncate_context_payload(payload) == payload

    def test_large_payload_truncates_values(self):
        payload = {
            "summary": "x" * 3000,
            "detail": "y" * 3000,
        }
        result = _truncate_context_payload(payload)
        for value in result.values():
            assert len(value) < 2000
            assert value.endswith("...")

    def test_preserves_all_keys(self):
        payload = {
            "summary": "x" * 3000,
            "detail": "y" * 3000,
            "note": "z" * 3000,
        }
        result = _truncate_context_payload(payload)
        assert set(result.keys()) == set(payload.keys())

    def test_empty_payload(self):
        assert _truncate_context_payload({}) == {}


# ---------------------------------------------------------------------------
# Context payload injection into system prompt
# ---------------------------------------------------------------------------

class TestContextPayloadInjection:
    """Tests for ``_format_parent_context`` rendering."""

    def test_depth_zero_returns_empty(self):
        ctx = MagicMock()
        ctx.depth = 0
        ctx._context_payload = {"summary": "should be ignored"}
        assert _format_parent_context(ctx) == ""

    def test_subagent_with_payload_renders_section(self):
        ctx = MagicMock()
        ctx.depth = 1
        ctx._context_payload = {
            "conversation_summary": "User asked about disk space",
            "relevant_memory": ["disk_warning: 90% full"],
        }
        section = _format_parent_context(ctx)
        assert section.startswith("PARENT CONTEXT (injected by parent agent):")
        assert "conversation_summary:" in section
        assert "disk_warning: 90% full" in section

    def test_subagent_without_payload_returns_empty(self):
        ctx = MagicMock()
        ctx.depth = 1
        ctx._context_payload = {}
        assert _format_parent_context(ctx) == ""


# ---------------------------------------------------------------------------
# spawn_agent handling of context_payload
# ---------------------------------------------------------------------------

def _make_runner(agent_id: str = "sa-ctx01") -> MagicMock:
    runner = MagicMock()
    runner.agent_id = agent_id
    runner._model_id = "test-model"
    runner._cancel_event = MagicMock()
    runner._llm = MagicMock()
    runner._agent = MagicMock()
    runner._agent.max_iterations = 8
    runner._short_term = ShortTermMemory(max_turns=50)
    return runner


def _make_registry(count: int = 0) -> MagicMock:
    reg = MagicMock()
    reg.count_managed.return_value = count
    return reg


class TestSpawnAgentContextPayload:
    """Tests for ``spawn_agent`` context_payload argument handling."""

    def test_spawn_agent_accepts_context_payload(self):
        factory = MagicMock(return_value=_make_runner())
        exc = BuiltinExecutor(sub_agent_factory=factory)

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", return_value=MagicMock()):
            result = exc._exec_spawn_agent(
                {
                    "task": "do work",
                    "context_payload": {
                        "conversation_summary": "User asked about disk space",
                        "relevant_memory": ["disk_warning: 90% full"],
                    },
                },
                caller_depth=0,
            )

        assert result["success"] is True

    def test_context_payload_passed_to_factory(self):
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = BuiltinExecutor(sub_agent_factory=factory)
        payload = {"conversation_summary": "User asked about disk space"}

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", return_value=MagicMock()):
            exc._exec_spawn_agent(
                {"task": "do work", "context_payload": payload},
                caller_depth=0,
            )

        assert captured.get("context_payload") == payload

    def test_implicit_context_payload_builds_summary(self):
        """When no context_payload is given, build_spawn_context_summary is used."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        memory = MagicMock()
        memory.as_prompt_text.return_value = "persistent memory text"
        exc = BuiltinExecutor(sub_agent_factory=factory, memory=memory)
        graph_memory = MagicMock()
        graph_memory.format_for_prompt.return_value = "graph context"
        exc._graph_memory = graph_memory  # type: ignore[attr-defined]

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", return_value=MagicMock()):
            exc._exec_spawn_agent(
                {"task": "do work"},
                caller_depth=0,
            )

        payload = captured.get("context_payload")
        assert payload is not None
        assert payload.get("parent_goal") == "do work"
        assert "relevant_memory" in payload
        assert "relevant_graph" in payload

    def test_context_payload_excluded_from_persistence(self, tmp_path):
        """context_payload must not be written to the context_key persistence file."""
        factory = MagicMock(return_value=_make_runner())
        exc = BuiltinExecutor(sub_agent_factory=factory, data_dir=str(tmp_path))

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit",
                          side_effect=lambda fn, *_args, **_kwargs: fn()):
            exc._exec_spawn_agent(
                {
                    "task": "do work",
                    "context_key": "ctx-payload-test",
                    "context_payload": {"secret": "must not persist"},
                    "_notify": False,
                },
                caller_depth=0,
            )

        context_file = tmp_path / "job_contexts" / "ctx-payload-test.json"
        assert context_file.exists()
        saved = context_file.read_text(encoding="utf-8")
        assert "must not persist" not in saved
        assert "context_payload" not in saved
        assert "secret" not in saved


# ---------------------------------------------------------------------------
# Prompt variant selection
# ---------------------------------------------------------------------------

class TestPromptVariant:
    """Tests for sub-agent prompt variant wiring."""

    def test_sub_agent_variant_loaded(self, tmp_path):
        """PlanExecutor requests the sub-agent prompt variant when creating runners."""
        from execution_plan import PlanExecutor, PlanStep, ExecutionPlan

        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            runner = _make_runner("sa-plan")
            runner.run = MagicMock(return_value='{"success": true, "output": "ok"}')
            return runner

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        ctx = MagicMock(spec=ReactContext)
        ctx.depth = 0
        ctx.trace_id = "r-plan"
        working = MagicMock()
        working.to_summary_text.return_value = "parent summary"
        ctx.working = working

        plan = ExecutionPlan(
            description="test plan",
            steps=[PlanStep(id="step-1", tool="shell", args={"command": "echo hi"})],
        )
        executor.execute(plan, ctx)

        assert captured.get("prompt_variant") == "sub-agent"

    def test_direct_spawn_uses_system_variant(self):
        """A direct spawn_agent call uses system prompts by default."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = BuiltinExecutor(sub_agent_factory=factory)

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", return_value=MagicMock()):
            exc._exec_spawn_agent(
                {"task": "do work"},
                caller_depth=0,
            )

        assert captured.get("prompt_variant") is None


# ---------------------------------------------------------------------------
# Sub-agent prompt variant rendering and ReactContext propagation
# ---------------------------------------------------------------------------

class TestSubAgentVariantRendering:
    """Tests that the sub-agent prompt variant renders without missing variables."""

    def test_sub_agent_variant_renders_task(self):
        """build_system_prompt(mode="sub-agent") renders the delegated task.

        Regression test: the sub-agent ``02-task.md`` template references
        ``{{task}}``; ``build_system_prompt`` must supply it so rendering does
        not raise ``UnresolvedVariableError``.
        """
        import os

        from prompt_loader import build_system_prompt

        prompts_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "prompts"
        )

        tool_index = MagicMock()
        tool_index.search.return_value = []
        memory = MagicMock()
        memory.as_prompt_text.return_value = "No memory."
        llm = MagicMock()
        llm._models = []
        llm.llm_cfg = {}

        with patch("prompt_builder.format_tools", return_value="No tools."), \
             patch("prompt_builder.format_skills", return_value=""), \
             patch("prompt_builder.format_models", return_value=""), \
             patch("prompt_builder.format_log_section", return_value="Log."), \
             patch("prompt_loader.estimate_tokens", return_value=1):
            prompt, _ = build_system_prompt(
                tool_index=tool_index,
                memory=memory,
                results=None,
                skill_registry=None,
                llm=llm,
                tmp_dir="/tmp/agent",
                downloads_dir="downloads",
                log_file="agent.log",
                log_backup_count=30,
                top_tools=3,
                user_goal="summarise the logs",
                prompts_dir=prompts_dir,
                mode="sub-agent",
            )

        assert "summarise the logs" in prompt
        assert "YOUR TASK:" in prompt


class TestReactContextPropagation:
    """Tests that AgentController.run propagates sub-agent context fields to ctx."""

    def test_run_propagates_context_payload_and_variant(self):
        """``_context_payload`` and ``_prompt_variant`` reach the ReactContext.

        Regression test: react_loop reads these off the ReactContext, so
        AgentController.run must copy them from the controller onto ``ctx``.
        """
        from agent_controller import AgentController

        captured = {}

        def fake_loop(ctx, *_args, **_kwargs):
            captured["context_payload"] = ctx._context_payload
            captured["prompt_variant"] = ctx._prompt_variant
            return "done"

        llm = MagicMock()
        llm._active_idx = 0
        llm._trace_id = ""

        agent = AgentController(
            llm=llm,
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            memory=MagicMock(),
            builtin_executor=MagicMock(),
        )
        agent._context_payload = {"parent_goal": "check disk"}
        agent._prompt_variant = "sub-agent"

        with patch("agent_controller.react_loop", side_effect=fake_loop):
            result = agent.run("do work")

        assert result == "done"
        assert captured["context_payload"] == {"parent_goal": "check disk"}
        assert captured["prompt_variant"] == "sub-agent"
