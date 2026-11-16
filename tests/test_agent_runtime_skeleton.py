"""Tests for the Phase 2 AgentRuntime skeleton and runtime types.

Covers OpenSpec change ``introduce-agent-runtime`` tasks 2.1-2.4:

- ``RuntimeProfile`` members exist (2.1).
- ``RuntimeOptions`` fields and defaults (2.2).
- ``AgentRuntime`` construction boundary skeleton API (2.3).
- Profile-to-source mapping proving profiles stay separate from
  ``SubAgentRecord.source`` visibility/capacity categories (2.4).

Models are admitted by a real ``ModelRegistry`` outside the runtime; the runtime
and runner only consume the immutable ``RunModel`` pin.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_controller import AgentController, SubAgentRunner
from agent_runtime import (
    AgentRuntime,
    RuntimeOptions,
    RuntimeProfile,
    profile_to_source,
)
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings
from sub_agent_registry import (
    CAPACITY_COUNTED_SOURCES,
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
    VISIBLE_SOURCES,
)


# ---------------------------------------------------------------------------
# Real registry admission + AgentRuntime.create builders
# ---------------------------------------------------------------------------

_SUB_AGENT_PROFILES = [
    RuntimeProfile.ON_DEMAND_SUBAGENT,
    RuntimeProfile.SCHEDULED_AGENT,
    RuntimeProfile.PLAN_STEP_AGENT,
    RuntimeProfile.DIAGNOSTIC_AGENT,
]

_URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}
AGENT_CFG = AgentConfig(top_tools=4, ctx_max_tokens=12345)


def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            context, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a", 5000)
MODEL_B = _record("pb", "up/b", 7000)


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during admission")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    """Real registry with models A (active) and B (inactive)."""
    providers = {
        name: ProviderConfig(name, "openai", f"env:KEY_{name}", url, 9)
        for name, url in _URLS.items()
    }
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(CatalogDocument(
        1, 2, None,
        tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
        (MODEL_A, MODEL_B),
    ))
    reg = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
    reg.select(MODEL_A.id)
    return reg


@pytest.fixture
def pin_a(registry: ModelRegistry) -> RunModel:
    return registry.admit()


@pytest.fixture
def pin_b(registry: ModelRegistry) -> RunModel:
    return registry.admit(MODEL_B.id)


def _unavailable_embedding(event: threading.Event) -> EmbeddingClient:
    """Real independent EmbeddingClient with no configuration (unavailable)."""
    return EmbeddingClient(EmbeddingsConfig(), cancel_event=event)


def _runtime(*, notify_fn=None, scheduled_max_iterations: int = 100,
             usage_registry=None, **overrides) -> AgentRuntime:
    kwargs: dict = dict(
        agent_cfg=AGENT_CFG,
        embedding_factory=_unavailable_embedding,
        paths=ExecutorPaths(tmp_dir="/tmp/agent", downloads_dir="downloads",
                            workspace_dir="~/Documents", log_file="agent.log",
                            log_backup_count=30),
        tool_index=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=None,
        results=MagicMock(),
        usage_registry=usage_registry,
        notify_fn=notify_fn,
        scheduled_max_iterations=scheduled_max_iterations,
    )
    kwargs.update(overrides)
    return AgentRuntime(**kwargs)


@pytest.fixture
def build() -> Iterator[Callable[..., SubAgentRunner]]:
    """Create runners through ``AgentRuntime.create`` and close them on teardown."""
    runners: list[SubAgentRunner] = []

    def _build(runtime: AgentRuntime, profile: RuntimeProfile, *,
               notify_fn=None, on_tool_trace=None, **option_kwargs) -> SubAgentRunner:
        runner = runtime.create(
            profile, RuntimeOptions(**option_kwargs),
            notify_fn=notify_fn, on_tool_trace=on_tool_trace,
        )
        runners.append(runner)
        return runner

    yield _build
    for runner in runners:
        runner.close()


def _counting_embedding_factory() -> tuple[list[threading.Event], Callable]:
    events: list[threading.Event] = []

    def factory(event: threading.Event) -> EmbeddingClient:
        events.append(event)
        return _unavailable_embedding(event)

    return events, factory


# ===========================================================================
# 2.1 — RuntimeProfile members
# ===========================================================================

class TestRuntimeProfile:
    """The four supported sub-agent construction profiles exist."""

    def test_all_profiles_present(self):
        names = {p.name for p in RuntimeProfile}
        assert names == {
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
        assert opts.run_model is None
        assert opts.max_iterations is None
        assert opts.context_key is None
        assert opts.context_payload is None
        assert opts.prompt_variant is None
        assert opts.trace_id is None
        assert opts.cancel_event is None
        assert opts.label is None

    def test_all_documented_fields_exist(self):
        # Freeze the option surface: the admitted pin replaces selector and
        # generation-tuning knobs.
        expected = {
            "run_model",
            "max_iterations",
            "context_key",
            "context_payload",
            "prompt_variant",
            "trace_id",
            "cancel_event",
            "label",
        }
        assert set(RuntimeOptions.__dataclass_fields__) == expected

    def test_fields_are_assignable(self, pin_a):
        cancel = threading.Event()
        opts = RuntimeOptions(
            run_model=pin_a,
            max_iterations=12,
            context_key="ctx-key",
            context_payload={"parent": "state"},
            prompt_variant="sub-agent",
            trace_id="r-abc12345",
            cancel_event=cancel,
            label="on-demand",
        )
        assert opts.run_model is pin_a
        assert opts.max_iterations == 12
        assert opts.context_key == "ctx-key"
        assert opts.context_payload == {"parent": "state"}
        assert opts.prompt_variant == "sub-agent"
        assert opts.trace_id == "r-abc12345"
        assert opts.cancel_event is cancel
        assert opts.label == "on-demand"

    @pytest.mark.parametrize(
        "obsolete",
        [{"model": "pa/up/a"}, {"max_tokens": 256}, {"temperature": 0.9}, {"top_p": 0.95}],
        ids=["model", "max_tokens", "temperature", "top_p"],
    )
    def test_removed_selector_and_generation_options_rejected_before_allocation(
        self, obsolete, pin_a
    ):
        events, factory = _counting_embedding_factory()
        runtime = _runtime(embedding_factory=factory)
        with pytest.raises((TypeError, ValueError)):
            runtime.create(
                RuntimeProfile.ON_DEMAND_SUBAGENT,
                RuntimeOptions(run_model=pin_a, **obsolete),
            )
        assert events == []


# ===========================================================================
# 2.4 — Profile-to-source mapping (profiles vs visibility sources)
# ===========================================================================

class TestProfileToSourceMapping:
    """Profiles are construction policy; sources are visibility/capacity policy."""

    def test_main_profile_removed(self):
        assert not hasattr(RuntimeProfile, "MAIN")

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
            # Does not raise KeyError; every sub-agent profile maps to a source.
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
        base_memory = object()
        runtime = AgentRuntime(
            agent_cfg=AgentConfig(top_tools=4, ctx_max_tokens=12345),
            embedding_factory=_unavailable_embedding,
            tool_index=tool_index,
            base_memory=base_memory,
        )
        assert runtime._tool_index is tool_index
        assert runtime._base_memory is base_memory
        assert runtime._embedding_factory is _unavailable_embedding
        assert runtime._agent_cfg.top_tools == 4
        assert runtime._agent_cfg.ctx_max_tokens == 12345

    def test_source_for_profile_matches_module_helper(self):
        runtime = AgentRuntime()
        for profile in RuntimeProfile:
            assert runtime.source_for_profile(profile) == profile_to_source(profile)

    def test_create_subagent_without_admitted_pin_raises(self):
        # No fallback model exists: unbound options cannot build a sub-agent.
        runtime = AgentRuntime()
        with pytest.raises(ValueError, match="run_model"):
            runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())

    def test_create_without_typed_agent_config_raises(self, pin_a):
        runtime = AgentRuntime(embedding_factory=_unavailable_embedding)
        with pytest.raises(ValueError, match="AgentConfig"):
            runtime.create(
                RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(run_model=pin_a),
            )

    def test_create_without_embedding_factory_raises(self, pin_a):
        runtime = AgentRuntime(agent_cfg=AGENT_CFG)
        with pytest.raises(ValueError, match="embedding_factory"):
            runtime.create(
                RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(run_model=pin_a),
            )


# ===========================================================================
# 3.1-3.4 — AgentRuntime.create builds sub-agent products
# ===========================================================================

class TestAgentRuntimeCreateSubAgent:
    """create() builds current sub-agent runner products for all sub-agent
    profiles, preserving construction behavior and the product surface."""

    @pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
    def test_builds_runner_with_product_surface(self, profile, build, pin_a):
        runner = build(_runtime(), profile, run_model=pin_a)
        assert isinstance(runner, SubAgentRunner)
        # Runner-shaped product surface (3.4).
        assert callable(runner.run)
        assert runner.agent_id.startswith("sa-")
        assert runner._model_id == MODEL_A.id
        assert runner.model_id == MODEL_A.id
        assert isinstance(runner._cancel_event, threading.Event)
        from llm_client import LLMClient
        assert isinstance(runner._llm, LLMClient)
        assert runner._llm._run_model is pin_a
        assert isinstance(runner._agent, AgentController)
        assert isinstance(runner._short_term, ShortTermMemory)
        assert callable(runner.close)
        assert callable(runner.notify_fn)

    @pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
    def test_subagent_depth_is_one(self, profile, build, pin_a):
        runner = build(_runtime(), profile, run_model=pin_a)
        assert runner._agent._depth == 1

    def test_all_subagent_profiles_build_equivalent_products(self, build, pin_a):
        # Construction is uniform across sub-agent profiles (source differs later).
        runtime = _runtime()
        runners = [build(runtime, p, run_model=pin_a) for p in _SUB_AGENT_PROFILES]
        depths = {r._agent._depth for r in runners}
        models = {r._model_id for r in runners}
        iters = {r._agent.max_iterations for r in runners}
        working = {type(r._working) for r in runners}
        assert depths == {1}
        assert models == {MODEL_A.id}
        assert iters == {100}
        assert working == {WorkingMemory}

    def test_explicit_pin_b_is_used_while_registry_active_model_is_a(
        self, registry, pin_b, build
    ):
        runner = build(
            _runtime(), RuntimeProfile.ON_DEMAND_SUBAGENT, run_model=pin_b,
        )
        assert runner._model_id == MODEL_B.id
        assert runner._llm._run_model is pin_b
        assert registry.view().active_model_id == MODEL_A.id

    def test_registry_selection_never_retargets_existing_runner(
        self, registry, pin_a, build
    ):
        runner = build(_runtime(), RuntimeProfile.ON_DEMAND_SUBAGENT, run_model=pin_a)
        registry.select(MODEL_B.id)
        assert runner._model_id == MODEL_A.id
        assert runner._llm._run_model is pin_a

    def test_unknown_selector_fails_registry_admission_before_runtime_allocation(
        self, registry, monkeypatch
    ):
        import agent_controller

        chat_allocations: list[tuple] = []

        def forbidden_llm(*args, **kwargs):
            chat_allocations.append(args)
            raise AssertionError("chat client allocated after failed admission")

        monkeypatch.setattr(agent_controller, "LLMClient", forbidden_llm)
        events, factory = _counting_embedding_factory()
        runtime = _runtime(embedding_factory=factory)

        # Admission happens outside the runtime; a failed admission yields no pin,
        # so nothing can reach create() and nothing is allocated.
        with pytest.raises(ModelRegistryError) as excinfo:
            registry.admit("not-a-model")  # no connection part: malformed, never an uncached send
        assert excinfo.value.code == "malformed_selector"
        assert events == []
        assert chat_allocations == []
        assert registry.view().active_model_id == MODEL_A.id

        # The runtime itself still refuses an unbound call without allocating.
        with pytest.raises(ValueError, match="run_model"):
            runtime.create(RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions())
        assert events == []
        assert chat_allocations == []

    @pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
    def test_unbound_options_have_no_background_fallback_and_allocate_nothing(
        self, profile
    ):
        events, factory = _counting_embedding_factory()
        runtime = _runtime(embedding_factory=factory)
        with pytest.raises(ValueError, match="run_model"):
            runtime.create(profile, RuntimeOptions())
        assert events == []

    def test_max_iterations_replaces_only_that_typed_config_field(self, build, pin_a):
        # Caller-owned, fully non-default config (incl. a mutable list field).
        caller_cfg = AgentConfig(
            agent_name="caller-agent", max_iterations=3, scheduled_max_iterations=55,
            tool_timeout=21, max_output_size=1234, top_tools=9,
            ctx_max_tokens=23456, compaction_threshold_tokens=12321,
            required_output_cap_tokens=617, max_retries=7, retry_delay=0.25, max_subagents=2,
            subagent_result_timeout=45, long_run_warn_minutes=12,
            diagnose_empty_responses=True, default_model="pa/up/a",
            background_model="pb/up/b", fallback_models=["pb/up/b", "pa/up/a"],
            shell_backend="pty", shell_pty_cols=100, shell_pty_rows=30,
            shell_streaming=True, shell_nsjail_confirm_mode="adaptive",
            shell_nsjail_memory_mb=512, shell_nsjail_pids_max=32,
            shell_nsjail_cpu_percent=25, shell_nsjail_dump_config_on_error=True,
            allow_net=True, dns_nameserver="1.1.1.1", session_logs_retention_days=3,
            creativity_mode="planner", step_notify_interval=5,
            inactivity_warn_minutes=4, plan_max_iterations=17,
        )
        # Independent snapshot taken BEFORE runtime/runner construction.
        snapshot = copy.deepcopy(asdict(caller_cfg))
        expected = {**copy.deepcopy(snapshot), "max_iterations": 7}
        assert snapshot["max_iterations"] == 3 and snapshot["top_tools"] == 9

        runner = build(
            _runtime(agent_cfg=caller_cfg), RuntimeProfile.ON_DEMAND_SUBAGENT,
            run_model=pin_a, max_iterations=7,
        )

        # Caller-owned config unchanged in every field (incl. nested list).
        assert asdict(caller_cfg) == snapshot
        assert caller_cfg.fallback_models == ["pb/up/b", "pa/up/a"]
        # Effective controller config == snapshot except max_iterations only.
        assert asdict(runner._agent._agent_cfg) == expected
        assert runner._agent.max_iterations == 7
        assert runner._agent.top_tools == 9
        assert runner._agent.ctx_max_tokens == 23456
        assert runner._agent.creativity_mode == "planner"
        assert runner._agent.plan_max_iterations == 17
        assert runner._agent.inactivity_warn_minutes == 4
        # Effective client settings come from the snapshot values.
        assert runner._agent.compaction_threshold_tokens == snapshot["compaction_threshold_tokens"]
        assert runner._llm._required_output_cap == snapshot["required_output_cap_tokens"]
        assert runner._llm._max_retries == snapshot["max_retries"]
        assert runner._llm._retry_delay == snapshot["retry_delay"]
        assert runner._llm._diagnose_empty is True

    def test_usage_registry_propagated(self, build, pin_a):
        usage = MagicMock()
        runner = build(
            _runtime(usage_registry=usage), RuntimeProfile.ON_DEMAND_SUBAGENT,
            run_model=pin_a,
        )
        assert runner._llm._usage_registry is usage

    def test_caller_tag_is_agent_id(self, build, pin_a):
        runner = build(_runtime(), RuntimeProfile.ON_DEMAND_SUBAGENT, run_model=pin_a)
        assert runner._llm._base_caller_tag == runner.agent_id

    def test_scheduled_max_iterations_default(self, build, pin_a):
        runner = build(
            _runtime(scheduled_max_iterations=42), RuntimeProfile.SCHEDULED_AGENT,
            run_model=pin_a,
        )
        assert runner._agent.max_iterations == 42

    def test_explicit_max_iterations_overrides_default(self, build, pin_a):
        runner = build(
            _runtime(scheduled_max_iterations=42), RuntimeProfile.PLAN_STEP_AGENT,
            run_model=pin_a, max_iterations=7,
        )
        assert runner._agent.max_iterations == 7

    def test_label_defaults_to_on_demand(self, build, pin_a):
        runner = build(_runtime(), RuntimeProfile.ON_DEMAND_SUBAGENT, run_model=pin_a)
        assert runner.label == "on-demand"

    def test_label_passthrough(self, build, pin_a):
        runner = build(
            _runtime(), RuntimeProfile.PLAN_STEP_AGENT,
            run_model=pin_a, label="plan-s1",
        )
        assert runner.label == "plan-s1"

    def test_prompt_variant_and_context_payload_propagated(self, build, pin_a):
        payload = {"parent_working_summary": "state"}
        runner = build(
            _runtime(), RuntimeProfile.PLAN_STEP_AGENT,
            run_model=pin_a, prompt_variant="sub-agent", context_payload=payload,
        )
        assert runner.prompt_variant == "sub-agent"
        assert runner._agent._prompt_variant == "sub-agent"
        assert runner.context_payload == payload
        assert runner._agent._context_payload == payload

    def test_trace_and_cancel_event_propagated(self, build, pin_a):
        cancel = threading.Event()
        runner = build(
            _runtime(), RuntimeProfile.PLAN_STEP_AGENT,
            run_model=pin_a, trace_id="r-abc12345", cancel_event=cancel,
        )
        assert runner._cancel_event is cancel
        assert runner._agent._cancel_event is cancel
        assert runner._agent._trace_id == "r-abc12345"

    def test_notify_fn_override_threaded_through(self, build, pin_a):
        sentinel = MagicMock()
        runner = build(
            _runtime(notify_fn=lambda _m: None), RuntimeProfile.ON_DEMAND_SUBAGENT,
            run_model=pin_a, notify_fn=sentinel,
        )
        assert runner.notify_fn is sentinel

    def test_notify_fn_defaults_to_runtime_notify(self, build, pin_a):
        default_notify = MagicMock()
        runner = build(
            _runtime(notify_fn=default_notify), RuntimeProfile.ON_DEMAND_SUBAGENT,
            run_model=pin_a,
        )
        assert runner.notify_fn is default_notify

    def test_on_tool_trace_threaded_through(self, build, pin_a):
        trace_cb = MagicMock()
        runner = build(
            _runtime(), RuntimeProfile.PLAN_STEP_AGENT,
            run_model=pin_a, on_tool_trace=trace_cb,
        )
        assert runner._agent._on_tool_trace is trace_cb

    def test_on_step_not_set_by_construction(self, build, pin_a):
        # Registry helpers install _on_step after construction (Phase 1 invariant).
        runner = build(_runtime(), RuntimeProfile.PLAN_STEP_AGENT, run_model=pin_a)
        assert runner._agent._on_step is None

    def test_context_key_preloads_short_term(self, tmp_path, build, pin_a):
        # context_key routes through _load_context; a missing key yields fresh memory.
        runner = build(
            _runtime(data_dir=str(tmp_path), agent_cfg=AgentConfig(),
                     paths=ExecutorPaths()),
            RuntimeProfile.SCHEDULED_AGENT,
            run_model=pin_a, context_key="never-saved",
        )
        assert runner.context_key == "never-saved"
        assert isinstance(runner._short_term, ShortTermMemory)
