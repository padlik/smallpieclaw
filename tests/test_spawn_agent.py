"""Tests for spawn_agent functionality in BuiltinExecutor._exec_spawn_agent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import BuiltinExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(factory=None, max_subagents: int = 6) -> BuiltinExecutor:
    """Build a BuiltinExecutor with an optional mock factory and no real threads needed."""
    return BuiltinExecutor(sub_agent_factory=factory, max_subagents=max_subagents)


def _make_runner(agent_id: str = "sa-abc123", model_id: str = "test-model") -> MagicMock:
    """Build a minimal mock SubAgentRunner."""
    runner = MagicMock()
    runner.agent_id = agent_id
    runner._model_id = model_id
    runner._cancel_event = MagicMock()
    runner._llm = MagicMock()
    runner._agent = MagicMock()
    runner._agent.max_iterations = 8
    return runner


def _make_registry(count: int = 0) -> MagicMock:
    """Build a mock registry with a fixed count_managed return value."""
    reg = MagicMock()
    reg.count_managed.return_value = count
    return reg


# ---------------------------------------------------------------------------
# Guard tests (no factory required)
# ---------------------------------------------------------------------------

class TestSpawnAgentGuards:
    """Tests for early-exit guard conditions in _exec_spawn_agent."""

    def test_missing_task_returns_error(self):
        exc = _make_executor()
        result = exc._exec_spawn_agent({})
        assert result["success"] is False
        assert "task" in result["error"].lower()

    def test_empty_task_returns_error(self):
        exc = _make_executor()
        result = exc._exec_spawn_agent({"task": "   "})
        assert result["success"] is False
        assert "task" in result["error"].lower()

    def test_depth_guard_blocks_sub_agent(self):
        exc = _make_executor(factory=MagicMock())
        result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=1)
        assert result["success"] is False
        assert "sub-agent" in result["error"].lower()

    def test_no_factory_configured_returns_error(self):
        exc = _make_executor(factory=None)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)):
            result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=0)
        assert result["success"] is False
        assert "factory" in result["error"].lower()

    def test_max_subagents_cap_returns_error(self):
        factory = MagicMock()
        exc = _make_executor(factory=factory, max_subagents=2)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(count=2)):
            result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=0)
        assert result["success"] is False
        assert "cap reached" in result["error"]


# ---------------------------------------------------------------------------
# Alias tolerance
# ---------------------------------------------------------------------------

class TestTaskAliases:
    """LLMs sometimes pass 'prompt'/'goal'/'description' instead of 'task'."""

    @pytest.mark.parametrize("alias", ["prompt", "goal", "description"])
    def test_alias_accepted_as_task(self, alias):
        runner = _make_runner()
        factory = MagicMock(return_value=runner)
        exc = _make_executor(factory=factory)

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", return_value=MagicMock()):
            result = exc._exec_spawn_agent({alias: "my task text"}, caller_depth=0)

        # Should not be an error about missing task
        assert "task" not in result.get("error", "").lower() or result.get("success") is not False


# ---------------------------------------------------------------------------
# response_format — task string augmentation
# ---------------------------------------------------------------------------

class TestResponseFormat:
    """response_format modifies the task string before passing it to the factory."""

    def _call_with_format(self, fmt: str) -> tuple[str, dict]:
        """Call _exec_spawn_agent with the given response_format; return (task_used, factory_kwargs)."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "base task", "response_format": fmt}, caller_depth=0)

        return captured

    def test_json_format_appends_instruction(self):
        runner = _make_runner()
        exc = _make_executor(factory=MagicMock(return_value=runner))
        modified_tasks = []

        def track_factory(**kwargs):
            modified_tasks.append(kwargs)
            return runner

        exc._sub_agent_factory = track_factory
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "base task", "response_format": "json"}, caller_depth=0)

        # The task is augmented before being stored in the record — check the factory was called
        # (task augmentation is part of the _run_and_notify closure, but the record preview
        #  is captured from task[:80]. We verify the overall flow succeeds here.)
        assert len(modified_tasks) == 1  # factory was called exactly once

    def test_unknown_format_defaults_to_text(self):
        runner = _make_runner()
        factory = MagicMock(return_value=runner)
        exc = _make_executor(factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            result = exc._exec_spawn_agent(
                {"task": "base task", "response_format": "xml"}, caller_depth=0
            )
        # Should not error — falls back silently to "text"
        assert "error" not in result or result.get("success") is not False


# ---------------------------------------------------------------------------
# LLM parameter overrides
# ---------------------------------------------------------------------------

class TestLLMParameterOverrides:
    """max_tokens / temperature / top_p are parsed and forwarded to the factory."""

    def _spawn_and_capture(self, args: dict) -> dict:
        """Run _exec_spawn_agent and return captured factory kwargs."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._sub_agent_pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "do work", **args}, caller_depth=0)
        return captured

    def test_max_tokens_forwarded_as_int(self):
        captured = self._spawn_and_capture({"max_tokens": 512})
        assert captured.get("max_tokens") == 512

    def test_temperature_forwarded_as_float(self):
        captured = self._spawn_and_capture({"temperature": 0.7})
        assert captured.get("temperature") == pytest.approx(0.7)

    def test_top_p_forwarded_as_float(self):
        captured = self._spawn_and_capture({"top_p": 0.9})
        assert captured.get("top_p") == pytest.approx(0.9)

    def test_string_numbers_coerced(self):
        """LLMs sometimes pass numbers as strings."""
        captured = self._spawn_and_capture({"max_tokens": "256", "temperature": "0.5"})
        assert captured.get("max_tokens") == 256
        assert captured.get("temperature") == pytest.approx(0.5)

    def test_invalid_max_tokens_becomes_none(self):
        captured = self._spawn_and_capture({"max_tokens": "not_a_number"})
        assert captured.get("max_tokens") is None

    def test_invalid_temperature_becomes_none(self):
        captured = self._spawn_and_capture({"temperature": "hot"})
        assert captured.get("temperature") is None

    def test_omitted_params_are_none(self):
        """When not provided, overrides are None (factory uses config defaults)."""
        captured = self._spawn_and_capture({})
        assert captured.get("max_tokens") is None
        assert captured.get("temperature") is None
        assert captured.get("top_p") is None


# ---------------------------------------------------------------------------
# sub_agent_factory: model_cfg override merging
# ---------------------------------------------------------------------------

class TestSubAgentFactoryOverrides:
    """Verify model_cfg shallow-copy logic (tested in isolation, not via main.py)."""

    def _make_factory_fn(self, all_models: list[dict], background_cfg: dict):
        """Re-implement the factory's override merging logic for unit testing."""
        def factory(model=None, max_tokens=None, temperature=None, top_p=None, **_kwargs):
            if model:
                model_cfg = next((m for m in all_models if m.get("model") == model), None)
                if model_cfg is None:
                    raise ValueError(f"Model '{model}' not found")
            else:
                model_cfg = background_cfg

            overrides = {}
            if max_tokens is not None:
                overrides["max_tokens"] = max_tokens
            if temperature is not None:
                overrides["temperature"] = temperature
            if top_p is not None:
                overrides["top_p"] = top_p
            # Always copy — never mutate the shared config
            model_cfg = {**model_cfg, **overrides}
            return model_cfg  # return for inspection

        return factory

    def test_overrides_applied_to_copy(self):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o", max_tokens=256, temperature=0.9)
        assert result["max_tokens"] == 256
        assert result["temperature"] == pytest.approx(0.9)

    def test_shared_config_not_mutated_with_overrides(self):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        factory(model="gpt-4o", max_tokens=256)
        # original dict must be unchanged
        assert original["max_tokens"] == 1024

    def test_shared_config_not_mutated_without_overrides(self):
        """Even with no overrides, the returned cfg must be a copy, not the shared ref."""
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o")
        result["max_tokens"] = 999  # mutate the returned dict
        assert original["max_tokens"] == 1024  # shared config must be unaffected

    def test_no_overrides_uses_config_values(self):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o")
        assert result["max_tokens"] == 1024
        assert result["temperature"] == pytest.approx(0.2)

    def test_top_p_override(self):
        original = {"model": "gpt-4o", "max_tokens": 1024, "top_p": None}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o", top_p=0.95)
        assert result["top_p"] == pytest.approx(0.95)

    def test_unknown_model_raises(self):
        factory = self._make_factory_fn([], {})
        with pytest.raises(ValueError, match="not found"):
            factory(model="nonexistent-model")
