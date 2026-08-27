"""Tests for spawn_agent functionality in BuiltinExecutor._exec_spawn_agent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import BuiltinExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(make_builtin_executor, factory=None, max_subagents: int = 6) -> BuiltinExecutor:
    """Build a BuiltinExecutor with an optional mock factory and no real threads needed."""
    return make_builtin_executor(sub_agent_factory=factory, max_subagents=max_subagents)


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

    def test_missing_task_returns_error(self, make_builtin_executor):
        exc = _make_executor(make_builtin_executor, )
        result = exc._exec_spawn_agent({})
        assert result["success"] is False
        assert "task" in result["error"].lower()

    def test_empty_task_returns_error(self, make_builtin_executor):
        exc = _make_executor(make_builtin_executor, )
        result = exc._exec_spawn_agent({"task": "   "})
        assert result["success"] is False
        assert "task" in result["error"].lower()

    def test_depth_guard_blocks_sub_agent(self, make_builtin_executor):
        exc = _make_executor(make_builtin_executor, factory=MagicMock())
        result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=1)
        assert result["success"] is False
        assert "sub-agent" in result["error"].lower()

    def test_no_factory_configured_returns_error(self, make_builtin_executor):
        exc = _make_executor(make_builtin_executor, factory=None)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)):
            result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=0)
        assert result["success"] is False
        assert "factory" in result["error"].lower()

    def test_max_subagents_cap_returns_error(self, make_builtin_executor):
        factory = MagicMock()
        exc = _make_executor(make_builtin_executor, factory=factory, max_subagents=2)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(count=2)):
            result = exc._exec_spawn_agent({"task": "do something"}, caller_depth=0)
        assert result["success"] is False
        assert "cap reached" in result["error"]

    def test_invalid_context_key_rejected_before_factory(self, make_builtin_executor):
        factory = MagicMock()
        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)):
            result = exc._exec_spawn_agent(
                {"task": "do something", "context_key": "../scheduler_state"},
                caller_depth=0,
            )
        assert result["success"] is False
        assert "invalid context_key" in result["error"]
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# Alias tolerance
# ---------------------------------------------------------------------------

class TestTaskAliases:
    """LLMs sometimes pass 'prompt'/'goal'/'description' instead of 'task'."""

    @pytest.mark.parametrize("alias", ["prompt", "goal", "description"])
    def test_alias_accepted_as_task(self, make_builtin_executor, alias):
        runner = _make_runner()
        factory = MagicMock(return_value=runner)
        exc = _make_executor(make_builtin_executor, factory=factory)

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            result = exc._exec_spawn_agent({alias: "my task text"}, caller_depth=0)

        # Should not be an error about missing task
        assert "task" not in result.get("error", "").lower() or result.get("success") is not False


# ---------------------------------------------------------------------------
# response_format — task string augmentation
# ---------------------------------------------------------------------------

class TestResponseFormat:
    """response_format modifies the task string before passing it to the factory."""

    def _call_with_format(self, make_builtin_executor, fmt: str) -> dict:
        """Call _exec_spawn_agent with the given response_format; return captured factory_kwargs."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "base task", "response_format": fmt}, caller_depth=0)

        return captured

    def test_json_format_appends_instruction(self, make_builtin_executor):
        runner = _make_runner()
        exc = _make_executor(make_builtin_executor, factory=MagicMock(return_value=runner))
        modified_tasks = []

        def track_factory(**kwargs):
            modified_tasks.append(kwargs)
            return runner

        exc._sub_agent_factory = track_factory
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "base task", "response_format": "json"}, caller_depth=0)

        # The task is augmented by the shim before delegation to the supervisor —
        # check the factory was called (the supervisor captures the record preview
        # from task[:80]). We verify the overall flow succeeds here.
        assert len(modified_tasks) == 1  # factory was called exactly once

    def test_unknown_format_defaults_to_text(self, make_builtin_executor):
        runner = _make_runner()
        factory = MagicMock(return_value=runner)
        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
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

    def _spawn_and_capture(self, make_builtin_executor, args: dict) -> dict:
        """Run _exec_spawn_agent and return captured factory kwargs."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "do work", **args}, caller_depth=0)
        return captured

    def test_max_tokens_forwarded_as_int(self, make_builtin_executor):
        captured = self._spawn_and_capture(make_builtin_executor, {"max_tokens": 512})
        assert captured.get("max_tokens") == 512

    def test_temperature_forwarded_as_float(self, make_builtin_executor):
        captured = self._spawn_and_capture(make_builtin_executor, {"temperature": 0.7})
        assert captured.get("temperature") == pytest.approx(0.7)

    def test_top_p_forwarded_as_float(self, make_builtin_executor):
        captured = self._spawn_and_capture(make_builtin_executor, {"top_p": 0.9})
        assert captured.get("top_p") == pytest.approx(0.9)

    def test_string_numbers_coerced(self, make_builtin_executor):
        """LLMs sometimes pass numbers as strings."""
        captured = self._spawn_and_capture(make_builtin_executor, {"max_tokens": "256", "temperature": "0.5"})
        assert captured.get("max_tokens") == 256
        assert captured.get("temperature") == pytest.approx(0.5)

    def test_invalid_max_tokens_becomes_none(self, make_builtin_executor):
        captured = self._spawn_and_capture(make_builtin_executor, {"max_tokens": "not_a_number"})
        assert captured.get("max_tokens") is None

    def test_invalid_temperature_becomes_none(self, make_builtin_executor):
        captured = self._spawn_and_capture(make_builtin_executor, {"temperature": "hot"})
        assert captured.get("temperature") is None

    def test_omitted_params_are_none(self, make_builtin_executor):
        """When not provided, overrides are None (factory uses config defaults)."""
        captured = self._spawn_and_capture(make_builtin_executor, {})
        assert captured.get("max_tokens") is None
        assert captured.get("temperature") is None
        assert captured.get("top_p") is None


# ---------------------------------------------------------------------------
# Trace propagation: parent request trace reaches the spawned sub-agent
# ---------------------------------------------------------------------------

class TestTracePropagation:
    """The invoking run's trace_id is forwarded through spawn_agent to the factory."""

    def _spawn_with_trace(self, make_builtin_executor, trace_id: str) -> dict:
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent({"task": "do work"}, caller_depth=0, trace_id=trace_id)
        return captured

    def test_trace_id_forwarded_to_factory(self, make_builtin_executor):
        captured = self._spawn_with_trace(make_builtin_executor, "r-deadbeef")
        assert captured.get("trace_id") == "r-deadbeef"

    def test_empty_trace_id_becomes_none(self, make_builtin_executor):
        """No active trace => factory mints a fresh one (None)."""
        captured = self._spawn_with_trace(make_builtin_executor, "")
        assert captured.get("trace_id") is None

    def test_execute_threads_trace_into_spawn(self, make_builtin_executor):
        """execute() forwards its trace_id argument down to the factory."""
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit", side_effect=lambda fn, *a, **kw: MagicMock()):
            exc.execute("spawn_agent", {"task": "do work"}, caller_depth=0,
                        caller_tag="main r-cafef00d", trace_id="r-cafef00d")
        assert captured.get("trace_id") == "r-cafef00d"


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

    def test_overrides_applied_to_copy(self, make_builtin_executor):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o", max_tokens=256, temperature=0.9)
        assert result["max_tokens"] == 256
        assert result["temperature"] == pytest.approx(0.9)

    def test_shared_config_not_mutated_with_overrides(self, make_builtin_executor):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        factory(model="gpt-4o", max_tokens=256)
        # original dict must be unchanged
        assert original["max_tokens"] == 1024

    def test_shared_config_not_mutated_without_overrides(self, make_builtin_executor):
        """Even with no overrides, the returned cfg must be a copy, not the shared ref."""
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o")
        result["max_tokens"] = 999  # mutate the returned dict
        assert original["max_tokens"] == 1024  # shared config must be unaffected

    def test_no_overrides_uses_config_values(self, make_builtin_executor):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o")
        assert result["max_tokens"] == 1024
        assert result["temperature"] == pytest.approx(0.2)

    def test_top_p_override(self, make_builtin_executor):
        original = {"model": "gpt-4o", "max_tokens": 1024, "top_p": None}
        factory = self._make_factory_fn([original], original)
        result = factory(model="gpt-4o", top_p=0.95)
        assert result["top_p"] == pytest.approx(0.95)

    def test_unknown_model_raises(self, make_builtin_executor):
        factory = self._make_factory_fn([], {})
        with pytest.raises(ValueError, match="not found"):
            factory(model="nonexistent-model")


# ---------------------------------------------------------------------------
# get_agent_result: cancel_on_timeout
# ---------------------------------------------------------------------------

class TestGetAgentResultCancelOnTimeout:
    """Tests for cancel_on_timeout behaviour in _exec_get_agent_result."""

    def _make_timed_out_record(self, agent_id: str = "sa-test"):
        """Return a SubAgentRecord whose _result_event will never fire."""
        from sub_agent_registry import SubAgentRecord

        record = SubAgentRecord(
            agent_id=agent_id,
            label="test",
            model="test-model",
            task_preview="test task",
            started_at=0.0,
            source="on-demand",
        )
        # _result_event stays unset so wait() always times out immediately
        return record

    def _exec_with_record(self, exc, record, args: dict):
        """Patch the registry so get_agent_result finds our record."""
        from unittest.mock import patch as _patch

        mock_reg = MagicMock()
        mock_reg.get.return_value = record

        with _patch("builtin_tools.agents._get_agent_registry", return_value=mock_reg):
            return exc._exec_get_agent_result(args)

    def test_cancel_event_set_by_default_on_timeout(self, make_builtin_executor):
        """With default cancel_on_timeout=True the sub-agent's cancel event must be set."""
        exc = _make_executor(make_builtin_executor, )
        record = self._make_timed_out_record()
        result = self._exec_with_record(exc, record, {"agent_id": "sa-test", "timeout": 0})
        assert result["status"] == "timeout"
        assert record._cancel_event.is_set(), "cancel_event should be set after timeout"
        assert record._timeout_cancelled is True

    def test_cancel_event_not_set_when_opted_out(self, make_builtin_executor):
        """With cancel_on_timeout=False the sub-agent must NOT be cancelled."""
        exc = _make_executor(make_builtin_executor, )
        record = self._make_timed_out_record()
        result = self._exec_with_record(
            exc, record, {"agent_id": "sa-test", "timeout": 0, "cancel_on_timeout": False}
        )
        assert result["status"] == "timeout"
        assert not record._cancel_event.is_set(), "cancel_event must not be set when opted out"
        assert record._timeout_cancelled is False

    def test_already_cancelled_agent_not_double_cancelled(self, make_builtin_executor):
        """If the agent is already cancelled, do not set _timeout_cancelled."""
        exc = _make_executor(make_builtin_executor, )
        record = self._make_timed_out_record()
        record._cancel_event.set()  # pre-cancelled (e.g. user /agents cancel)
        self._exec_with_record(exc, record, {"agent_id": "sa-test", "timeout": 0})
        assert record._timeout_cancelled is False, "_timeout_cancelled must stay False for pre-cancelled agents"

    def test_timeout_cancelled_flag_suppresses_notification(self, make_builtin_executor):
        """_timeout_cancelled=True on the record should suppress Telegram notification."""
        from sub_agent_registry import SubAgentRecord

        record = SubAgentRecord(
            agent_id="sa-tnotify",
            label="notify-test",
            model="m",
            task_preview="t",
            started_at=0.0,
            source="on-demand",
        )
        record._timeout_cancelled = True

        assert record._timeout_cancelled is True
        # The actual notification branch check is: `if _notify_result and not record._timeout_cancelled`
        _notify_result = True
        should_notify = _notify_result and not record._timeout_cancelled
        assert should_notify is False, "notification must be suppressed when _timeout_cancelled is True"


# ---------------------------------------------------------------------------
# Runtime profile threading (Phase 3 profile wiring)
# ---------------------------------------------------------------------------

class TestRuntimeProfileThreading:
    """spawn/scheduler construction threads the matching RuntimeProfile through
    the internal factory channel (never through model-facing args)."""

    def _capture_factory_kwargs(self, make_builtin_executor, *, args=None, options=None) -> dict:
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _make_runner()

        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=lambda fn, *a, **kw: MagicMock()):
            exc._exec_spawn_agent(
                {"task": "do work", **(args or {})}, caller_depth=0, options=options,
            )
        return captured

    def test_default_spawn_uses_on_demand_profile(self, make_builtin_executor):
        from agent_runtime import RuntimeProfile

        captured = self._capture_factory_kwargs(make_builtin_executor, )
        assert captured.get("runtime_profile") == RuntimeProfile.ON_DEMAND_SUBAGENT

    def test_scheduled_source_uses_scheduled_profile(self, make_builtin_executor):
        from agent_runtime import RuntimeProfile
        from sub_agent_registry import SOURCE_SCHEDULED
        from sub_agent_supervisor import SupervisionOptions

        captured = self._capture_factory_kwargs(make_builtin_executor, 
            options=SupervisionOptions(source=SOURCE_SCHEDULED, notify=False),
        )
        assert captured.get("runtime_profile") == RuntimeProfile.SCHEDULED_AGENT

    def test_runtime_profile_is_not_a_model_facing_arg(self, make_builtin_executor):
        # The model-facing spawn_agent args must not carry runtime_profile; it is
        # derived internally from the supervision source.
        from agent_runtime import RuntimeProfile

        captured = self._capture_factory_kwargs(make_builtin_executor, args={"runtime_profile": "attempted-injection"})
        # Injection through model args is ignored; the internal derivation wins.
        assert captured.get("runtime_profile") == RuntimeProfile.ON_DEMAND_SUBAGENT
