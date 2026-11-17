"""Tests for spawn_agent functionality in BuiltinExecutor._exec_spawn_agent."""

from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import agent_controller
import agent_runtime
import outcome_utils
from builtin_executor import BuiltinExecutor
from llm_client import LLMClient
from model_registry import ModelRegistryError, RunModel
from react_loop import ReactContext
from sub_agent_registry import SOURCE_ON_DEMAND, get_registry
from tests.root_startup_harness import (
    ACME_HOST,
    GEM_HOST,
    INVALID_SAVED,
    UNCONFIGURED_ID,
    remembered_for,
    MAIN_ID,
    OTHER_ID,
    SECRETS,
    RootHarness,
)


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
# C1 shared helpers: strict factory + zero-allocation probes (unit level)
# ---------------------------------------------------------------------------

SUPPORTED_FACTORY_KWARGS = frozenset({
    "model", "context_key", "label", "notify_fn", "max_iterations",
    "on_tool_trace", "cancel_event", "trace_id", "context_payload",
    "prompt_variant", "runtime_profile",
})
LEGACY_GENERATION_KEYS = ("max_tokens", "temperature", "top_p")


class _StrictFactory:
    """Injected sub-agent factory that accepts ONLY the supported keywords.

    Any legacy generation keyword (even with a ``None`` value) raises
    ``TypeError`` exactly like a strict real signature would, and every
    successful call is recorded.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        unexpected = set(kwargs) - SUPPORTED_FACTORY_KWARGS
        if unexpected:
            raise TypeError(f"unexpected factory keyword(s): {sorted(unexpected)}")
        self.calls.append(kwargs)
        return _make_runner()


def _spawn_with_probes(make_builtin_executor, args: dict, *, caller_depth: int = 0) -> MagicMock:
    """Run ``_exec_spawn_agent`` with a valid task/depth and counting probes.

    UNIT-level only: the factory is a strict spy, so success here proves argument
    shape/forwarding, NOT catalog/active-selection model admission (later C2/F).
    Returns a namespace with ``result``, ``factory``, ``supervisor_submit``,
    ``context_builder`` and ``pool_submit`` probes.
    """
    import builtin_tools.agents as agents_mod

    factory = _StrictFactory()
    exc = _make_executor(make_builtin_executor, factory=factory)
    with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
         patch.object(exc._supervisor._pool, "submit",
                      side_effect=lambda fn, *a, **kw: MagicMock()) as pool_submit, \
         patch.object(exc._supervisor, "submit",
                      wraps=exc._supervisor.submit) as supervisor_submit, \
         patch("builtin_tools.agents._build_context_payload",
               wraps=agents_mod._build_context_payload) as context_builder:
        result = exc._exec_spawn_agent({"task": "do work", **args}, caller_depth=caller_depth)
    probes = MagicMock()
    probes.result = result
    probes.factory = factory
    probes.supervisor_submit = supervisor_submit
    probes.context_builder = context_builder
    probes.pool_submit = pool_submit
    return probes


def _assert_refused_validation(result: dict) -> None:
    from outcome_utils import read_execution

    assert result["success"] is False
    assert result["exit_code"] == -1
    assert result["error_type"] == "validation_error"
    assert read_execution(result) == "refused"


def _assert_nothing_allocated(probes: MagicMock) -> None:
    assert probes.factory.calls == []
    assert probes.supervisor_submit.call_count == 0
    assert probes.context_builder.call_count == 0
    assert probes.pool_submit.call_count == 0


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
# Obsolete generation overrides: key PRESENCE is rejected (C1, unit level)
#
# Legacy intent mapping (old TestLLMParameterOverrides -> new tests):
#   test_max_tokens_forwarded_as_int      -> rejected[max_tokens=512]
#   test_temperature_forwarded_as_float   -> rejected[temperature=0.7]
#   test_top_p_forwarded_as_float         -> rejected[top_p=0.9]
#   test_string_numbers_coerced           -> rejected[string numerics]
#   test_invalid_max_tokens_becomes_none  -> rejected[max_tokens="not_a_number"]
#   test_invalid_temperature_becomes_none -> rejected[temperature="hot"]
#   test_omitted_params_are_none          -> test_generation_kwargs_never_emitted
#                                            (no legacy key at all, not even None)
# ---------------------------------------------------------------------------

_LEAK_SENTINEL = "leak-sentinel-7f3a"

_LEGACY_CASES = [
    ("max_tokens", None),
    ("max_tokens", 512),
    ("max_tokens", "256"),
    ("max_tokens", "not_a_number"),
    ("max_tokens", _LEAK_SENTINEL),
    ("temperature", None),
    ("temperature", 0.7),
    ("temperature", "0.5"),
    ("temperature", "hot"),
    ("temperature", _LEAK_SENTINEL),
    ("top_p", None),
    ("top_p", 0.9),
    ("top_p", "0.9"),
    ("top_p", "nucleus"),
    ("top_p", _LEAK_SENTINEL),
]


class TestObsoleteGenerationKeyRejection:
    """UNIT: max_tokens/temperature/top_p presence (even None) is a validation refusal."""

    @pytest.mark.parametrize(("key", "value"), _LEGACY_CASES)
    def test_key_presence_rejected_before_any_allocation(self, make_builtin_executor, key, value):
        probes = _spawn_with_probes(make_builtin_executor, {key: value})
        _assert_refused_validation(probes.result)
        assert key in probes.result["error"]
        assert _LEAK_SENTINEL not in str(probes.result)
        assert "configured" in probes.result["error"].lower()
        _assert_nothing_allocated(probes)

    def test_all_obsolete_keys_named_together(self, make_builtin_executor):
        probes = _spawn_with_probes(
            make_builtin_executor,
            {"max_tokens": None, "temperature": None, "top_p": None},
        )
        _assert_refused_validation(probes.result)
        for key in LEGACY_GENERATION_KEYS:
            assert key in probes.result["error"]
        _assert_nothing_allocated(probes)

    @pytest.mark.parametrize("key", LEGACY_GENERATION_KEYS)
    def test_rejected_through_public_execute_entrypoint(self, make_builtin_executor, key):
        factory = _StrictFactory()
        exc = _make_executor(make_builtin_executor, factory=factory)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor, "submit") as supervisor_submit:
            result = exc.execute("spawn_agent", {"task": "do work", key: None}, caller_depth=0)
        assert result["success"] is False
        assert result["exit_code"] == -1
        assert result["error_type"] == "validation_error"
        assert factory.calls == []
        supervisor_submit.assert_not_called()

    def test_baseline_without_legacy_keys_is_admitted_to_factory(self, make_builtin_executor):
        """Isolation control: the same valid fixture without legacy keys proceeds."""
        probes = _spawn_with_probes(make_builtin_executor, {})
        assert probes.result.get("success") is not False
        assert len(probes.factory.calls) == 1
        assert probes.supervisor_submit.call_count == 1

    def test_generation_kwargs_never_emitted(self, make_builtin_executor):
        probes = _spawn_with_probes(make_builtin_executor, {})
        (kwargs,) = probes.factory.calls
        for key in LEGACY_GENERATION_KEYS:
            assert key not in kwargs
        assert set(kwargs) <= SUPPORTED_FACTORY_KWARGS

    def test_coerce_overrides_helper_removed(self):
        import builtin_tools.agents as agents_mod

        assert not hasattr(agents_mod, "_coerce_overrides")


# ---------------------------------------------------------------------------
# Optional model selector: shape validation + verbatim forwarding (C1, unit level)
# Catalog/active-selection ADMISSION is NOT proven here (strict spy factory);
# real-registry eligibility/alias/unknown-model coverage is deferred to C2/F.
# ---------------------------------------------------------------------------

class TestModelSelectorShape:
    """UNIT: only argument shape is validated and forwarded unchanged."""

    @pytest.mark.parametrize(
        "bad",
        [42, 1.5, True, False, 0, ["a/b"], [], {"id": "x"}, {}, "", " ", "\t\n  "],
        ids=["int", "float", "true", "false", "zero", "list", "empty-list",
             "dict", "empty-dict", "empty", "space", "whitespace-mix"],
    )
    def test_invalid_selector_rejected_before_any_allocation(self, make_builtin_executor, bad):
        probes = _spawn_with_probes(make_builtin_executor, {"model": bad})
        _assert_refused_validation(probes.result)
        assert "model" in probes.result["error"]
        _assert_nothing_allocated(probes)

    def test_absent_selector_forwarded_as_none(self, make_builtin_executor):
        probes = _spawn_with_probes(make_builtin_executor, {})
        (kwargs,) = probes.factory.calls
        assert "model" in kwargs
        assert kwargs["model"] is None

    def test_none_selector_forwarded_as_none(self, make_builtin_executor):
        probes = _spawn_with_probes(make_builtin_executor, {"model": None})
        (kwargs,) = probes.factory.calls
        assert kwargs["model"] is None

    @pytest.mark.parametrize(
        "selector",
        [
            "openrouter/anthropic/claude-sonnet-4",
            "provider/up/model:free",
            "  provider/up/model  ",
            "Provider/UP/Model-X",
            "default",
            "background",
        ],
        ids=["canonical", "tagged", "padded", "mixed-case", "alias-like-default", "alias-like-background"],
    )
    def test_nonempty_string_forwarded_verbatim(self, make_builtin_executor, selector):
        probes = _spawn_with_probes(make_builtin_executor, {"model": selector})
        (kwargs,) = probes.factory.calls
        assert kwargs["model"] == selector
        assert type(kwargs["model"]) is str
        assert set(kwargs) <= SUPPORTED_FACTORY_KWARGS

    def test_context_payload_model_values_remain_opaque(self, make_builtin_executor):
        payload = {"model": "", "max_tokens": 5, "note": "x"}
        probes = _spawn_with_probes(make_builtin_executor, {"context_payload": payload})
        assert probes.result.get("success") is not False
        (kwargs,) = probes.factory.calls
        assert kwargs["context_payload"] == payload
        assert kwargs["model"] is None


class TestStrictFactoryDouble:
    """UNIT: the injected double itself rejects every legacy keyword, even None."""

    @pytest.mark.parametrize("key", LEGACY_GENERATION_KEYS)
    @pytest.mark.parametrize("value", [None, 1])
    def test_legacy_keyword_rejected_by_strict_factory(self, key, value):
        factory = _StrictFactory()
        with pytest.raises(TypeError):
            factory(model=None, **{key: value})
        assert factory.calls == []

    def test_supported_keywords_accepted(self):
        factory = _StrictFactory()
        factory(**{name: None for name in SUPPORTED_FACTORY_KWARGS})
        assert len(factory.calls) == 1


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
    """Verify model_cfg shallow-copy logic (tested in isolation, not via main.py).

    C1 migration: the isolated factory model now accepts NO generation keywords.
    Legacy intent mapping:
      test_overrides_applied_to_copy              -> test_legacy_override_kwargs_rejected
      test_shared_config_not_mutated_with_overrides -> test_legacy_override_attempt_leaves_config_untouched
      test_top_p_override                         -> test_legacy_override_kwargs_rejected[top_p]
    """

    def _make_factory_fn(self, all_models: list[dict], background_cfg: dict):
        """Re-implement the factory's config copying logic (strict kwargs) for unit testing."""
        def factory(model=None, **kwargs):
            unexpected = set(kwargs) - (SUPPORTED_FACTORY_KWARGS - {"model"})
            if unexpected:
                raise TypeError(f"unexpected factory keyword(s): {sorted(unexpected)}")
            if model:
                model_cfg = next((m for m in all_models if m.get("model") == model), None)
                if model_cfg is None:
                    raise ValueError(f"Model '{model}' not found")
            else:
                model_cfg = background_cfg
            # Always copy — never mutate the shared config
            return {**model_cfg}  # return for inspection

        return factory

    @pytest.mark.parametrize("key", LEGACY_GENERATION_KEYS)
    @pytest.mark.parametrize("value", [None, 0.5])
    def test_legacy_override_kwargs_rejected(self, make_builtin_executor, key, value):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        with pytest.raises(TypeError):
            factory(model="gpt-4o", **{key: value})

    def test_legacy_override_attempt_leaves_config_untouched(self, make_builtin_executor):
        original = {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.2}
        factory = self._make_factory_fn([original], original)
        with pytest.raises(TypeError):
            factory(model="gpt-4o", max_tokens=256)
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


class TestExecutionOwnershipAdmission:
    """Spawn admission derives ownership from trusted call metadata."""

    def test_on_demand_spawn_carries_calling_turn_identity(self, make_builtin_executor):
        runner = _make_runner("sa-owned")
        exc = _make_executor(make_builtin_executor, factory=MagicMock(return_value=runner))
        captured = {}

        def capture_submit(request, options):
            captured["request"] = request
            captured["options"] = options
            return {"success": True, "agent_id": runner.agent_id}

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor, "submit", side_effect=capture_submit):
            result = exc._exec_spawn_agent(
                {"task": "owned work", "owner_turn_id": "model-injection"},
                caller_depth=0,
                trace_id="r-owner123",
            )

        assert result["success"] is True
        ownership = captured["request"].ownership
        assert ownership.origin == "on-demand"
        assert ownership.owner_turn_id == "r-owner123"

    def test_spawn_without_turn_identity_is_unowned(self, make_builtin_executor):
        captured = []
        exc = _make_executor(
            make_builtin_executor,
            factory=MagicMock(return_value=_make_runner("sa-owned")),
        )

        def capture_submit(request, _options):
            captured.append(request.ownership.owner_turn_id)
            return {"success": True, "agent_id": "sa-owned"}

        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor, "submit", side_effect=capture_submit):
            exc._exec_spawn_agent({"task": "first"}, trace_id="")
            exc._exec_spawn_agent({"task": "second"}, trace_id="")

        assert captured == [None, None]


# ---------------------------------------------------------------------------
# Trusted scheduled selector shape (task 12.10, unit level)
# ---------------------------------------------------------------------------

class TestScheduledSelectorShape:
    """Only scheduler-built options skip the strict selector-shape guard."""

    @staticmethod
    def _probes_for_source(make_builtin_executor, model, scheduled: bool) -> MagicMock:
        from sub_agent_registry import SOURCE_SCHEDULED
        from sub_agent_supervisor import SupervisionOptions

        if not scheduled:
            return _spawn_with_probes(make_builtin_executor, {"model": model})
        factory = _StrictFactory()
        exc = _make_executor(make_builtin_executor, factory=factory)
        options = SupervisionOptions(job_tag="nightly", notify=False, source=SOURCE_SCHEDULED)
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=lambda fn, *a, **kw: MagicMock()):
            result = exc._exec_spawn_agent(
                {"task": "do work", "model": model}, caller_depth=0, options=options,
            )
        probes = MagicMock()
        probes.result = result
        probes.factory = factory
        return probes

    @pytest.mark.parametrize("bad", [42, " ", ["a/b"]], ids=["int", "space", "list"])
    def test_scheduled_source_forwards_bad_shape_verbatim(self, make_builtin_executor, bad):
        probes = self._probes_for_source(make_builtin_executor, bad, scheduled=True)
        assert probes.result.get("success") is True
        (kwargs,) = probes.factory.calls
        assert kwargs["model"] is bad
        assert set(kwargs) <= SUPPORTED_FACTORY_KWARGS

    @pytest.mark.parametrize("bad", [42, " ", ["a/b"]], ids=["int", "space", "list"])
    def test_default_source_refuses_bad_shape(self, make_builtin_executor, bad):
        probes = self._probes_for_source(make_builtin_executor, bad, scheduled=False)
        _assert_refused_validation(probes.result)
        assert "model" in probes.result["error"]
        _assert_nothing_allocated(probes)


class TestOrdinarySelectorForwarding:
    """Task 12.13 (UNIT, forwarding only): a padded canonical selector reaches
    the factory byte-exact; real admission (model_not_found) is proven in
    tests/test_profile_admission_matrix.py."""

    def test_padded_canonical_selector_forwarded_byte_exact(self, make_builtin_executor):
        probes = _spawn_with_probes(make_builtin_executor, {"model": " pb/up/b "})
        (kwargs,) = probes.factory.calls
        assert kwargs["model"].encode() == b" pb/up/b "
        assert set(kwargs) <= SUPPORTED_FACTORY_KWARGS


# ---------------------------------------------------------------------------
# Task 7.2: production-root ordinary admission through the actual spawn route
#
# Drives the real ``main._run`` via ``tests/root_startup_harness.py`` and calls
# the actual root ``BuiltinExecutor._exec_spawn_agent`` with its real
# ``SubAgentSupervisor`` and the main-owned factory. Replaced boundaries:
# the harness's external effects, ``agent_controller.react_loop`` and (positive
# control only) the supervisor's worker pool, run inline. Admission,
# credential resolution, runtime construction and run registration are
# observed by delegating spies installed after startup.
# ---------------------------------------------------------------------------

ROOT_TURN = "r-7a2c0de1"
ROOT_SPAWN_RESULT = "root-spawn-done"


@pytest.fixture
def production_root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Keep production ownership assertions ahead of fallback fixture hygiene."""
    root = RootHarness(tmp_xdg, monkeypatch)
    try:
        yield root
    finally:
        root.release_leftovers()


class _InlineExecutor(concurrent.futures.ThreadPoolExecutor):
    """Worker-pool boundary that runs submitted work inline: no thread starts."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        """Run *fn* now and return its completed future."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — delivered through the future
            future.set_exception(exc)
        return future


class _RootReactBoundary:
    """Replaces only ``agent_controller.react_loop``; each run sends one real pinned request."""

    def __init__(self, answer: str) -> None:
        """Record runs and return *answer* from each."""
        self.answer = answer
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self, ctx: ReactContext, goal: str, progress_callback: Any = None,
        images: Any = None, **kwargs: Any,
    ) -> str:
        """Capture the run's pinned client, then send one request through it."""
        call = SimpleNamespace(goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.calls.append(call)
        call.reply = ctx.llm.chat([{"role": "user", "content": "ping"}])
        return self.answer


class _RootAttemptSpies:
    """Delegating observers on the started root; counters exclude startup work."""

    def __init__(self, root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        """Snapshot root state, then wrap the root's real collaborators."""
        self._root = root
        self.view_before = root.registry.view()
        self.settings_before = root.paths.models_cache_file.read_bytes()
        self._requests = len(root.requests)
        self._llm_clients = len(root.llm_clients)
        self._http_clients = len(root.http_clients)
        self.admits: dict[str, list[object]] = {"admit": [], "admit_scheduled": []}
        self.codes: list[str] = []
        self.pins: list[RunModel] = []
        self.resolved: list[str] = []
        self.creates: list[tuple[object, object]] = []
        self.registered: list[tuple[str, str]] = []
        registry = root.registry
        for name in self.admits:
            monkeypatch.setattr(registry, name, self._admission_spy(name, getattr(registry, name)))
        resolve = registry._credential_resolver

        def resolve_spy(config: Any) -> Any:
            """Record the provider whose credentials are resolved, then delegate."""
            self.resolved.append(config.name)
            return resolve(config)

        monkeypatch.setattr(registry, "_credential_resolver", resolve_spy)
        create = root.runtime.create

        def create_spy(profile: object, options: object, **kwargs: Any) -> object:
            """Record the runtime construction request, then delegate."""
            self.creates.append((profile, options))
            return create(profile, options, **kwargs)

        monkeypatch.setattr(root.runtime, "create", create_spy)
        runs = get_registry()
        register = runs.register

        def register_spy(record: Any) -> None:
            """Record the child run registration, then delegate."""
            self.registered.append((record.agent_id, record.source))
            register(record)

        monkeypatch.setattr(runs, "register", register_spy)

    def _admission_spy(self, name: str, original: Callable[[object], RunModel]) -> Callable[..., RunModel]:
        """Return a spy recording selector, pin or typed refusal code around *original*."""

        def spy(selector: object = None) -> RunModel:
            """Record, then delegate to the real registry admission."""
            self.admits[name].append(selector)
            try:
                pin = original(selector)
            except ModelRegistryError as exc:
                self.codes.append(exc.code)
                raise
            self.pins.append(pin)
            return pin

        return spy

    def provider_requests(self) -> list[httpx.Request]:
        """Requests to either catalog provider since the snapshot."""
        return [r for r in self._root.requests[self._requests:] if r.url.host in (ACME_HOST, GEM_HOST)]

    def new_llm_clients(self) -> list[LLMClient]:
        """Inference clients constructed since the snapshot."""
        return self._root.llm_clients[self._llm_clients:]

    def new_http_clients(self) -> list[httpx.Client]:
        """HTTP clients constructed since the snapshot."""
        return self._root.http_clients[self._http_clients:]


def _serve_inference(root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extend the harness HTTP boundary with canned Anthropic/Google chat replies."""
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Serve pinned chat endpoints; delegate everything else to the harness."""
        host, path = request.url.host, request.url.path
        if request.method == "POST" and host == ACME_HOST and path == "/v1/messages":
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            })
        if request.method == "POST" and host == GEM_HOST and path.endswith(":generateContent"):
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "pong"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
            })
        return original(request)

    monkeypatch.setattr(root, "_route", route)


def _root_binding(root: RootHarness) -> tuple[bool, bool, bool]:
    """Whether the root builtin holds the helper's factory over the root runtime/registry."""
    runtime, registry, factory = root.factory_binding()
    return runtime is root.runtime, registry is root.registry, root.builtin._sub_agent_factory is factory


class TestProductionRootSpawnAdmission:
    """Actual root spawn route; explicit selectors need no Main (task 4.1), implicit ones do."""

    @pytest.mark.parametrize(
        "saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["no-saved-selection", "saved-model-missing"],
    )
    def test_explicit_selector_runs_exact_pin_without_active_main(
        self, production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
        saved_default: str | None,
    ) -> None:
        """Task 4.1: a valid explicit OTHER_ID needs no active Main; it admits once and reaches only gem."""
        _serve_inference(production_root, monkeypatch)
        boundary = _RootReactBoundary(ROOT_SPAWN_RESULT)
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Spawn with an explicit known selector while Main is inactive."""
            outcome["binding"] = _root_binding(root)
            spies = _RootAttemptSpies(root, monkeypatch)
            outcome["spies"] = spies
            supervisor = root.builtin._supervisor
            monkeypatch.setattr(supervisor, "_pool", _InlineExecutor())
            spawned = root.builtin._exec_spawn_agent(
                {"task": "explicit work", "model": OTHER_ID}, caller_depth=0, trace_id=ROOT_TURN,
            )
            outcome["spawned"] = spawned
            outcome["fetched"] = root.builtin._exec_get_agent_result(
                {"agent_id": spawned.get("agent_id", ""), "timeout": 5}, caller_tag=f"main {ROOT_TURN}",
            )
            outcome["drain_errors"] = supervisor.drain_owned_children(ROOT_TURN, cancel=False, timeout=5)
            outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
            outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
            outcome["view_after"] = root.registry.view()
            outcome["settings_after"] = root.paths.models_cache_file.read_bytes()

        production_root.start(saved_default=saved_default, hold=hold)
        root = production_root
        spies: _RootAttemptSpies = outcome["spies"]

        assert outcome["binding"] == (True, True, True)
        before = spies.view_before
        assert before.active_model_id is None and before.remembered_model_id == remembered_for(saved_default)
        assert before.selection_status == (
            "no_selection" if remembered_for(saved_default) is None else "saved_selection_unavailable"
        )
        assert OTHER_ID in {m.id for m in before.models}
        spawned = outcome["spawned"]
        assert all(secret not in json.dumps(spawned, default=str) for secret in SECRETS.values())
        assert spawned["success"] is True and outcome_utils.read_execution(spawned) == "executed", spawned
        assert outcome["fetched"]["status"] == "done" and outcome["fetched"]["result"] == ROOT_SPAWN_RESULT
        assert spies.admits == {"admit": [OTHER_ID], "admit_scheduled": []} and spies.codes == []
        (pin,) = spies.pins
        assert pin.model.id == OTHER_ID and pin.connection.provider_id == "gem"
        assert spies.resolved == ["gem"] and len(spies.creates) == 1
        (post,) = [r for r in spies.provider_requests() if r.method == "POST"]
        assert str(post.url) == f"https://{GEM_HOST}/v1beta/models/gem-1:generateContent"
        assert post.headers["x-goog-api-key"] == SECRETS["KEY_GEM"]
        assert outcome["drain_errors"] == [] and outcome["llm_closed"] == [True] and outcome["http_open"] == []
        assert outcome["view_after"] == before and outcome["settings_after"] == spies.settings_before
        assert root.unexpected == [] and root.open_clients_at_exit == []

    def test_explicit_selector_under_active_main_runs_exact_pin_once(
        self, production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Positive control: explicit OTHER_ID under Main A admits once and reaches only gem."""
        _serve_inference(production_root, monkeypatch)
        boundary = _RootReactBoundary(ROOT_SPAWN_RESULT)
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Spawn, retrieve the result, and drain while startup is held."""
            outcome["binding"] = _root_binding(root)
            spies = _RootAttemptSpies(root, monkeypatch)
            outcome["spies"] = spies
            supervisor = root.builtin._supervisor
            monkeypatch.setattr(supervisor, "_pool", _InlineExecutor())
            spawned = root.builtin._exec_spawn_agent(
                {"task": "explicit work", "model": OTHER_ID}, caller_depth=0, trace_id=ROOT_TURN,
            )
            outcome["spawned"] = spawned
            outcome["fetched"] = root.builtin._exec_get_agent_result(
                {"agent_id": spawned.get("agent_id", ""), "timeout": 5}, caller_tag=f"main {ROOT_TURN}",
            )
            outcome["unaccounted"] = supervisor.unaccounted_children(ROOT_TURN)
            outcome["drain_errors"] = supervisor.drain_owned_children(ROOT_TURN, cancel=False, timeout=5)
            outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
            outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
            outcome["view_after"] = root.registry.view()
            outcome["settings_after"] = root.paths.models_cache_file.read_bytes()

        production_root.start(saved_default=MAIN_ID, hold=hold)
        root = production_root
        spies: _RootAttemptSpies = outcome["spies"]

        assert outcome["binding"] == (True, True, True)
        assert spies.view_before.active_model_id == MAIN_ID
        spawned = outcome["spawned"]
        assert spawned["success"] is True and outcome_utils.read_execution(spawned) == "executed", spawned
        agent_id = spawned["agent_id"]
        assert f"model: {OTHER_ID}" in spawned["output"]
        fetched = outcome["fetched"]
        assert fetched["status"] == "done" and fetched["result"] == ROOT_SPAWN_RESULT, fetched
        # One ordinary admission of the explicit selector; no scheduled path, no substitution.
        assert spies.admits == {"admit": [OTHER_ID], "admit_scheduled": []} and spies.codes == []
        (pin,) = spies.pins
        assert pin.model.id == OTHER_ID and pin.connection.provider_id == "gem"
        assert pin.connection.base_url == f"https://{GEM_HOST}"
        assert spies.resolved == ["gem"]
        # The admitted pin reaches the real runtime unchanged, exactly once.
        ((profile, options),) = spies.creates
        assert profile is agent_runtime.RuntimeProfile.ON_DEMAND_SUBAGENT
        assert isinstance(options, agent_runtime.RuntimeOptions)
        assert options.run_model is pin and options.trace_id == ROOT_TURN
        (call,) = boundary.calls
        assert call.pin is pin and call.reply == "pong"
        assert spies.new_llm_clients() == [call.llm]
        # The only model request went to the pinned endpoint with its own credential.
        (post,) = [r for r in spies.provider_requests() if r.method == "POST"]
        assert str(post.url) == f"https://{GEM_HOST}/v1beta/models/gem-1:generateContent"
        assert post.headers["x-goog-api-key"] == SECRETS["KEY_GEM"]
        assert "x-api-key" not in post.headers
        # Registered once as an on-demand child, then deregistered and accounted for.
        assert spies.registered == [(agent_id, SOURCE_ON_DEMAND)]
        assert get_registry().get(agent_id) is None
        assert outcome["unaccounted"] == [] and outcome["drain_errors"] == []
        # Run-owned clients were closed by production before any fixture hygiene.
        assert outcome["llm_closed"] == [True] and outcome["http_open"] == []
        # Main A and the persisted settings are untouched by the explicit run.
        assert outcome["view_after"] == spies.view_before
        assert outcome["settings_after"] == spies.settings_before
        assert root.unexpected == [] and root.open_clients_at_exit == []
