"""Tests for the Phase 2 AgentRuntime skeleton and runtime types.

Covers OpenSpec change ``introduce-agent-runtime`` tasks 2.1-2.4:

- ``RuntimeProfile`` members exist (2.1).
- ``RuntimeOptions`` fields and defaults (2.2).
- ``AgentRuntime`` construction boundary skeleton API (2.3).
- Profile-to-source mapping proving profiles stay separate from
  ``SubAgentRecord.source`` visibility/capacity categories (2.4).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from agent_controller import AgentController, SubAgentRunner
from agent_runtime import (
    AgentRuntime,
    RuntimeOptions,
    RuntimeProfile,
    profile_to_source,
)
from memory_store import ShortTermMemory, WorkingMemory
from sub_agent_registry import (
    CAPACITY_COUNTED_SOURCES,
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
    VISIBLE_SOURCES,
)


# ---------------------------------------------------------------------------
# Builders for AgentRuntime.create sub-agent construction tests
# ---------------------------------------------------------------------------

_SUB_AGENT_PROFILES = [
    RuntimeProfile.ON_DEMAND_SUBAGENT,
    RuntimeProfile.SCHEDULED_AGENT,
    RuntimeProfile.PLAN_STEP_AGENT,
    RuntimeProfile.DIAGNOSTIC_AGENT,
]


def _cfg(fallback=None) -> dict:
    models = [
        {"name": "a", "provider": "openai", "model": "model-a",
         "api_key": "k", "base_url": "http://x", "max_tokens": 1024,
         "temperature": 0.2},
        {"name": "b", "provider": "openai", "model": "model-b",
         "api_key": "k", "base_url": "http://x"},
        {"name": "c", "provider": "openai", "model": "model-c",
         "api_key": "k", "base_url": "http://x"},
    ]
    agent = {"default_model": "model-a"}
    if fallback is not None:
        agent["fallback_models"] = fallback
    return {"models": models, "agent": agent}


def _runtime(config: dict, *, notify_fn=None, scheduled_max_iterations: int = 100,
             usage_registry=None) -> AgentRuntime:
    return AgentRuntime(
        config=config,
        all_models=config["models"],
        background_model_cfg=config["models"][0],
        tool_index=MagicMock(),
        executor=MagicMock(),
        creator=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=None,
        results=MagicMock(),
        usage_registry=usage_registry,
        notify_fn=notify_fn,
        scheduled_max_iterations=scheduled_max_iterations,
        top_tools=4,
        ctx_max_tokens=12345,
    )


# ===========================================================================
# 2.1 — RuntimeProfile members
# ===========================================================================

class TestRuntimeProfile:
    """The five supported construction profiles exist."""

    def test_all_profiles_present(self):
        names = {p.name for p in RuntimeProfile}
        assert names == {
            "MAIN",
            "ON_DEMAND_SUBAGENT",
            "SCHEDULED_AGENT",
            "PLAN_STEP_AGENT",
            "DIAGNOSTIC_AGENT",
        }

    def test_profiles_have_distinct_values(self):
        values = [p.value for p in RuntimeProfile]
        assert len(values) == len(set(values))


# ===========================================================================
# 2.2 — RuntimeOptions fields and defaults
# ===========================================================================

class TestRuntimeOptions:
    """RuntimeOptions carries construction knobs with inherit-by-default None."""

    def test_defaults_are_inherit_none(self):
        opts = RuntimeOptions()
        assert opts.model is None
        assert opts.fallback_models is None
        assert opts.max_iterations is None
        assert opts.max_tokens is None
        assert opts.temperature is None
        assert opts.top_p is None
        assert opts.context_key is None
        assert opts.context_payload is None
        assert opts.prompt_variant is None
        assert opts.trace_id is None
        assert opts.cancel_event is None
        assert opts.label is None

    def test_all_documented_fields_exist(self):
        # Freeze the documented option surface (design.md decision 3).
        expected = {
            "model",
            "fallback_models",
            "max_iterations",
            "max_tokens",
            "temperature",
            "top_p",
            "context_key",
            "context_payload",
            "prompt_variant",
            "trace_id",
            "cancel_event",
            "label",
        }
        assert set(RuntimeOptions.__dataclass_fields__) == expected

    def test_fields_are_assignable(self):
        cancel = threading.Event()
        opts = RuntimeOptions(
            model="model-a",
            fallback_models=["model-b"],
            max_iterations=12,
            max_tokens=256,
            temperature=0.9,
            top_p=0.95,
            context_key="ctx-key",
            context_payload={"parent": "state"},
            prompt_variant="sub-agent",
            trace_id="r-abc12345",
            cancel_event=cancel,
            label="on-demand",
        )
        assert opts.model == "model-a"
        assert opts.fallback_models == ["model-b"]
        assert opts.max_iterations == 12
        assert opts.max_tokens == 256
        assert opts.temperature == pytest.approx(0.9)
        assert opts.top_p == pytest.approx(0.95)
        assert opts.context_key == "ctx-key"
        assert opts.context_payload == {"parent": "state"}
        assert opts.prompt_variant == "sub-agent"
        assert opts.trace_id == "r-abc12345"
        assert opts.cancel_event is cancel
        assert opts.label == "on-demand"

    def test_fallback_models_preserves_trichotomy_shapes(self):
        # None inherits, [] disables, list is explicit — all representable.
        assert RuntimeOptions(fallback_models=None).fallback_models is None
        assert RuntimeOptions(fallback_models=[]).fallback_models == []
        assert RuntimeOptions(
            fallback_models=["m1", "m2"]
        ).fallback_models == ["m1", "m2"]


# ===========================================================================
# 2.4 — Profile-to-source mapping (profiles vs visibility sources)
# ===========================================================================

class TestProfileToSourceMapping:
    """Profiles are construction policy; sources are visibility/capacity policy."""

    def test_main_has_no_source(self):
        assert profile_to_source(RuntimeProfile.MAIN) is None

    def test_subagent_profiles_map_to_source_constants(self):
        assert profile_to_source(RuntimeProfile.ON_DEMAND_SUBAGENT) == SOURCE_ON_DEMAND
        assert profile_to_source(RuntimeProfile.SCHEDULED_AGENT) == SOURCE_SCHEDULED
        assert profile_to_source(RuntimeProfile.PLAN_STEP_AGENT) == SOURCE_PLAN_STEP
        assert profile_to_source(RuntimeProfile.DIAGNOSTIC_AGENT) == SOURCE_DIAGNOSTIC

    def test_mapped_sources_are_visible_sources(self):
        for profile in RuntimeProfile:
            source = profile_to_source(profile)
            if source is not None:
                assert source in VISIBLE_SOURCES

    def test_profile_values_are_not_source_strings(self):
        # Profiles must not be identity-equal to source categories, so
        # construction policy cannot leak into visibility/capacity semantics.
        source_set = set(VISIBLE_SOURCES)
        for profile in RuntimeProfile:
            assert profile.value not in source_set

    def test_profile_does_not_dictate_capacity_category(self):
        # Capacity counting is a source concern; profiles alone don't decide it.
        # plan-step/diagnostic are visible but not capacity-counted.
        assert SOURCE_ON_DEMAND in CAPACITY_COUNTED_SOURCES
        assert SOURCE_SCHEDULED in CAPACITY_COUNTED_SOURCES
        assert SOURCE_PLAN_STEP not in CAPACITY_COUNTED_SOURCES
        assert SOURCE_DIAGNOSTIC not in CAPACITY_COUNTED_SOURCES

    def test_every_profile_has_a_mapping(self):
        for profile in RuntimeProfile:
            # Does not raise KeyError; MAIN maps to None, others to a string.
            source = profile_to_source(profile)
            assert source is None or isinstance(source, str)


# ===========================================================================
# 2.3 — AgentRuntime skeleton API
# ===========================================================================

class TestAgentRuntimeSkeleton:
    """The construction boundary exists with the intended API but no migration."""

    def test_constructs_with_no_arguments(self):
        runtime = AgentRuntime()
        assert isinstance(runtime, AgentRuntime)

    def test_holds_construction_dependencies(self):
        tool_index = object()
        executor = object()
        base_memory = object()
        runtime = AgentRuntime(
            config={"models": []},
            all_models=[{"model": "model-a"}],
            background_model_cfg={"model": "model-a"},
            tool_index=tool_index,
            executor=executor,
            base_memory=base_memory,
            top_tools=4,
            ctx_max_tokens=12345,
        )
        assert runtime._tool_index is tool_index
        assert runtime._executor is executor
        assert runtime._base_memory is base_memory
        assert runtime._all_models == [{"model": "model-a"}]
        assert runtime._top_tools == 4
        assert runtime._ctx_max_tokens == 12345

    def test_source_for_profile_matches_module_helper(self):
        runtime = AgentRuntime()
        for profile in RuntimeProfile:
            assert runtime.source_for_profile(profile) == profile_to_source(profile)

    def test_create_main_is_deferred(self):
        # MAIN top-level construction stays in main.py for this change.
        runtime = AgentRuntime()
        with pytest.raises(NotImplementedError):
            runtime.create(RuntimeProfile.MAIN, RuntimeOptions())

    def test_create_subagent_without_config_raises(self):
        # A runtime with no config cannot build a sub-agent product.
        runtime = AgentRuntime()
        with pytest.raises(ValueError, match="config"):
            runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())


# ===========================================================================
# 3.1-3.4 — AgentRuntime.create builds sub-agent products
# ===========================================================================

class TestAgentRuntimeCreateSubAgent:
    """create() builds current sub-agent runner products for all sub-agent
    profiles, preserving construction behavior and the product surface."""

    @pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
    def test_builds_runner_with_product_surface(self, profile):
        runtime = _runtime(_cfg())
        runner = runtime.create(profile, RuntimeOptions())
        assert isinstance(runner, SubAgentRunner)
        # Runner-shaped product surface (3.4).
        assert callable(runner.run)
        assert runner.agent_id.startswith("sa-")
        assert runner._model_id == "model-a"
        assert isinstance(runner._cancel_event, threading.Event)
        from llm_client import LLMClient
        assert isinstance(runner._llm, LLMClient)
        assert isinstance(runner._agent, AgentController)
        assert isinstance(runner._short_term, ShortTermMemory)
        assert callable(runner.close)
        assert callable(runner.notify_fn)

    @pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
    def test_subagent_depth_is_one(self, profile):
        runtime = _runtime(_cfg())
        runner = runtime.create(profile, RuntimeOptions())
        assert runner._agent._depth == 1

    def test_all_subagent_profiles_build_equivalent_products(self):
        # Construction is uniform across sub-agent profiles (source differs later).
        runtime = _runtime(_cfg())
        runners = [runtime.create(p, RuntimeOptions()) for p in _SUB_AGENT_PROFILES]
        depths = {r._agent._depth for r in runners}
        models = {r._model_id for r in runners}
        iters = {r._agent.max_iterations for r in runners}
        working = {type(r._working) for r in runners}
        assert depths == {1}
        assert models == {"model-a"}
        assert iters == {100}
        assert working == {WorkingMemory}

    def test_model_override_resolves_and_activates(self):
        runtime = _runtime(_cfg())
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(model="model-b"),
        )
        assert runner._model_id == "model-b"
        assert runner._llm.llm_cfg.get("model") == "model-b"

    def test_unknown_model_raises(self):
        runtime = _runtime(_cfg())
        with pytest.raises(ValueError, match="not found"):
            runtime.create(
                RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(model="nope"),
            )

    def test_default_model_is_background_model(self):
        # No model override -> background_model_cfg (models[0]).
        runtime = _runtime(_cfg())
        runner = runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert runner._model_id == "model-a"

    def test_fallback_none_inherits(self):
        runtime = _runtime(_cfg(fallback=["model-b", "model-c"]))
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT,
            RuntimeOptions(fallback_models=None),
        )
        assert len(runner._llm._fallback_indices) == 2

    def test_fallback_empty_disables(self):
        runtime = _runtime(_cfg(fallback=["model-b", "model-c"]))
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT,
            RuntimeOptions(fallback_models=[]),
        )
        assert runner._llm._fallback_indices == []

    def test_fallback_explicit_list(self):
        runtime = _runtime(_cfg(fallback=["model-b", "model-c"]))
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT,
            RuntimeOptions(fallback_models=["model-b"]),
        )
        assert len(runner._llm._fallback_indices) == 1

    def test_per_call_overrides_applied_without_mutating_shared_config(self):
        config = _cfg()
        runtime = _runtime(config)
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT,
            RuntimeOptions(max_tokens=256, temperature=0.9, top_p=0.95),
        )
        active = runner._llm.llm_cfg
        assert active["max_tokens"] == 256
        assert active["temperature"] == pytest.approx(0.9)
        assert active["top_p"] == pytest.approx(0.95)
        # Shared config entry unchanged.
        assert config["models"][0]["max_tokens"] == 1024
        assert config["models"][0]["temperature"] == pytest.approx(0.2)

    def test_usage_registry_propagated(self):
        registry = MagicMock()
        runtime = _runtime(_cfg(), usage_registry=registry)
        runner = runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert runner._llm._usage_registry is registry

    def test_caller_tag_is_agent_id(self):
        runtime = _runtime(_cfg())
        runner = runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert runner._llm._base_caller_tag == runner.agent_id

    def test_scheduled_max_iterations_default(self):
        runtime = _runtime(_cfg(), scheduled_max_iterations=42)
        runner = runtime.create(RuntimeProfile.SCHEDULED_AGENT, RuntimeOptions())
        assert runner._agent.max_iterations == 42

    def test_explicit_max_iterations_overrides_default(self):
        runtime = _runtime(_cfg(), scheduled_max_iterations=42)
        runner = runtime.create(
            RuntimeProfile.PLAN_STEP_AGENT, RuntimeOptions(max_iterations=7),
        )
        assert runner._agent.max_iterations == 7

    def test_label_defaults_to_on_demand(self):
        runtime = _runtime(_cfg())
        runner = runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert runner.label == "on-demand"

    def test_label_passthrough(self):
        runtime = _runtime(_cfg())
        runner = runtime.create(
            RuntimeProfile.PLAN_STEP_AGENT, RuntimeOptions(label="plan-s1"),
        )
        assert runner.label == "plan-s1"

    def test_prompt_variant_and_context_payload_propagated(self):
        runtime = _runtime(_cfg())
        payload = {"parent_working_summary": "state"}
        runner = runtime.create(
            RuntimeProfile.PLAN_STEP_AGENT,
            RuntimeOptions(prompt_variant="sub-agent", context_payload=payload),
        )
        assert runner.prompt_variant == "sub-agent"
        assert runner._agent._prompt_variant == "sub-agent"
        assert runner.context_payload == payload
        assert runner._agent._context_payload == payload

    def test_trace_and_cancel_event_propagated(self):
        runtime = _runtime(_cfg())
        cancel = threading.Event()
        runner = runtime.create(
            RuntimeProfile.PLAN_STEP_AGENT,
            RuntimeOptions(trace_id="r-abc12345", cancel_event=cancel),
        )
        assert runner._cancel_event is cancel
        assert runner._agent._cancel_event is cancel
        assert runner._agent._trace_id == "r-abc12345"

    def test_notify_fn_override_threaded_through(self):
        runtime = _runtime(_cfg(), notify_fn=lambda _m: None)
        sentinel = MagicMock()
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(), notify_fn=sentinel,
        )
        assert runner.notify_fn is sentinel

    def test_notify_fn_defaults_to_runtime_notify(self):
        default_notify = MagicMock()
        runtime = _runtime(_cfg(), notify_fn=default_notify)
        runner = runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert runner.notify_fn is default_notify

    def test_on_tool_trace_threaded_through(self):
        runtime = _runtime(_cfg())
        trace_cb = MagicMock()
        runner = runtime.create(
            RuntimeProfile.PLAN_STEP_AGENT, RuntimeOptions(), on_tool_trace=trace_cb,
        )
        assert runner._agent._on_tool_trace is trace_cb

    def test_on_step_not_set_by_construction(self):
        # Registry helpers install _on_step after construction (Phase 1 invariant).
        runtime = _runtime(_cfg())
        runner = runtime.create(RuntimeProfile.PLAN_STEP_AGENT, RuntimeOptions())
        assert runner._agent._on_step is None

    def test_context_key_preloads_short_term(self, tmp_path):
        # context_key routes through _load_context; a missing key yields fresh memory.
        runtime = AgentRuntime(
            config=_cfg(),
            all_models=_cfg()["models"],
            background_model_cfg=_cfg()["models"][0],
            tool_index=MagicMock(),
            executor=MagicMock(),
            creator=MagicMock(),
            base_memory=MagicMock(),
            builtin_executor=MagicMock(),
            skill_registry=MagicMock(),
            results=MagicMock(),
            data_dir=str(tmp_path),
        )
        runner = runtime.create(
            RuntimeProfile.SCHEDULED_AGENT, RuntimeOptions(context_key="never-saved"),
        )
        assert runner.context_key == "never-saved"
        assert isinstance(runner._short_term, ShortTermMemory)
