"""Characterization / golden tests for current agent construction paths.

These tests freeze the observable construction behavior of the *current*
legacy construction paths so the upcoming ``AgentRuntime`` refactor
(OpenSpec change ``introduce-agent-runtime``) can be proven behavior-preserving.

They target surviving entry points that will remain stable across the refactor:

- ``AgentController.run`` — ``ReactContext`` assembly.
- ``SubAgentRunner.__init__`` / ``.run`` — construction + product surface.
- ``react_loop`` — cancel-event ownership semantics.
- ``sub_agent_registry.register_run`` — ``_on_step`` wiring survives construction.

Assertions freeze *expected field values* (not only runtime-vs-legacy A/B
comparisons) so they remain meaningful once construction moves behind the
runtime builder.
"""

from __future__ import annotations

import copy
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent_controller import AgentController, SubAgentRunner
from agent_runtime import AgentRuntime
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from llm_client import LLMClient
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings

from tests.execution_harness import RecordingExecutor, ScriptedLLM, run_react
from tests.root_startup_harness import MAIN_ID as ROOT_MAIN_ID
from tests.root_startup_harness import OTHER_ID as ROOT_OTHER_ID
from tests.root_startup_harness import RootHarness


_TRACE_RE = re.compile(r"^r-[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _make_controller(**overrides) -> AgentController:
    """Construct an AgentController with frozen, inspectable knob values."""
    from config_schema import AgentConfig, ExecutorPaths

    builtin = MagicMock()
    builtin._max_subagents = 5  # frozen expected value for ctx.max_subagents
    agent_cfg = AgentConfig(
        max_iterations=11,
        top_tools=4,
        ctx_max_tokens=12345,
        plan_max_iterations=42,
        inactivity_warn_minutes=7,
    )
    paths = ExecutorPaths()
    kwargs = dict(
        agent_cfg=agent_cfg,
        paths=paths,
        llm=MagicMock(),
        tool_index=MagicMock(),
        memory=MagicMock(),
        short_term=MagicMock(),
        working=MagicMock(),
        results=MagicMock(),
        builtin_executor=builtin,
        skill_registry=MagicMock(),
        mcp_manager=MagicMock(),
        depth=0,
        label="main",
    )
    kwargs.update(overrides)
    return AgentController(**kwargs)  # type: ignore[arg-type]


def _run_capture(ctrl: AgentController, goal: str = "do it"):
    """Run the controller with react_loop patched to capture the ReactContext."""
    captured: dict = {}

    def _fake_loop(ctx, _goal, _progress, _images):
        captured["ctx"] = ctx
        return "ok"

    with patch("agent_controller.react_loop", side_effect=_fake_loop):
        result = ctrl.run(goal)
    return captured["ctx"], result


_URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}


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


def _registry(tmp_path: Path) -> ModelRegistry:
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
    registry = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
    registry.select(MODEL_A.id)
    return registry


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    return _registry(tmp_path)


@pytest.fixture
def pin_a(registry: ModelRegistry) -> RunModel:
    return registry.admit()


@pytest.fixture
def pin_b(registry: ModelRegistry) -> RunModel:
    return registry.admit(MODEL_B.id)


def _unavailable_embedding(event: threading.Event) -> EmbeddingClient:
    """Real independent EmbeddingClient with no configuration (unavailable)."""
    return EmbeddingClient(EmbeddingsConfig(), cancel_event=event)


def _runner_kwargs(run_model: object, **overrides) -> dict:
    kwargs: dict = dict(
        run_model=run_model,
        agent_cfg=AgentConfig(),
        embedding_factory=_unavailable_embedding,
        paths=ExecutorPaths(),
        tool_index=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=None,
        results=MagicMock(),
        notify_fn=None,
        label="on-demand",
    )
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def make_runner(pin_a: RunModel) -> Iterator[Callable[..., SubAgentRunner]]:
    """Construct real SubAgentRunners on the admitted pin; close them on teardown."""
    runners: list[SubAgentRunner] = []

    def _make(run_model: object = None, **overrides) -> SubAgentRunner:
        pin = pin_a if run_model is None else run_model
        runner = SubAgentRunner(**_runner_kwargs(pin, **overrides))
        runners.append(runner)
        return runner

    yield _make
    for runner in runners:
        runner.close()


# ===========================================================================
# 1.1 — AgentController.run ReactContext assembly
# ===========================================================================

class TestAgentControllerReactContextAssembly:
    """Freeze how AgentController.run assembles the ReactContext."""

    def test_core_service_identity(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.llm is ctrl.llm
        assert ctx.tool_index is ctrl.tool_index
        assert ctx.memory is ctrl.memory
        assert ctx.builtin_executor is ctrl.builtin_executor
        assert ctx.mcp_manager is ctrl.mcp_manager
        assert ctx.skill_registry is ctrl.skill_registry

    def test_memory_fields_identity(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.short_term is ctrl.short_term
        assert ctx.working is ctrl.working
        assert ctx.results is ctrl.results

    def test_iteration_limits_frozen(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.max_iterations == 11
        assert ctx.top_tools == 4
        assert ctx.ctx_max_tokens == 12345
        assert ctx.plan_max_iterations == 42
        assert ctx.inactivity_warn_minutes == 7
        assert ctx.max_subagents == 5
        assert ctx.depth == 0
        assert ctx.label == "main"

    def test_graph_memory_post_init_wiring_preserved(self):
        ctrl = _make_controller()
        gm = object()
        gw = object()
        ctrl._graph_memory = gm  # type: ignore[assignment]
        ctrl._graph_memory_writer = gw  # type: ignore[assignment]
        ctrl._graph_memory_max_entries = 15
        ctx, _ = _run_capture(ctrl)
        assert ctx.graph_memory is gm
        assert ctx.graph_memory_writer is gw
        assert ctx.graph_memory_max_entries == 15

    def test_graph_memory_absent_defaults(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.graph_memory is None
        assert ctx.graph_memory_writer is None
        assert ctx.graph_memory_max_entries == 10

    def test_strategy_memory_post_init_wiring_preserved(self):
        ctrl = _make_controller()
        sm = object()
        ctrl.strategy_memory = sm  # type: ignore[attr-defined]
        ctx, _ = _run_capture(ctrl)
        assert ctx.strategy_memory is sm

    def test_strategy_memory_absent_is_none(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.strategy_memory is None

    def test_confirmation_manager_instance_shared(self):
        ctrl = _make_controller()
        ctx, _ = _run_capture(ctrl)
        assert ctx.confirmation is ctrl._confirmation

    def test_trace_fresh_when_not_propagated(self):
        ctrl = _make_controller(trace_id=None)
        ctx, _ = _run_capture(ctrl)
        assert _TRACE_RE.match(ctx.trace_id), ctx.trace_id
        # set at run start (also restored in finally, hence assert_any_call)
        ctrl.llm.set_trace_id.assert_any_call(ctx.trace_id)  # type: ignore[attr-defined]

    def test_trace_propagated_from_parent(self):
        ctrl = _make_controller(trace_id="r-abc12345")
        ctx, _ = _run_capture(ctrl)
        assert ctx.trace_id == "r-abc12345"
        ctrl.llm.set_trace_id.assert_any_call("r-abc12345")  # type: ignore[attr-defined]

    def test_controller_owns_private_event_without_loop_clearing(self):
        """The controller owns allocation and lifetime of the run-private event
        (CancelEventRegistry), but the loop must NOT clear it: the event is
        registered before setup finishes, so a cancel() arriving during setup
        would be erased by react_loop's legacy ``owns_cancel_event`` clear.
        Hence run() contexts carry owns_cancel_event=False, while a direct
        (static) build_react_context() bypass keeps the legacy owned semantics
        on ctrl._cancel_event."""
        ctrl = _make_controller()  # cancel_event defaults to None -> owned
        assert ctrl._owns_cancel_event is True
        during: dict = {}

        def _fake_loop(ctx, _goal, _progress, _images):
            during["ctx"] = ctx
            during["registered"] = ctx.cancel_event in ctrl._cancel_registry._events
            during["owns"] = ctx.owns_cancel_event
            return "ok"

        with patch("agent_controller.react_loop", side_effect=_fake_loop):
            result = ctrl.run("do it")
        ctx = during["ctx"]
        assert result == "ok"
        assert ctrl._cancel_registry is not None
        assert during["registered"] is True
        assert during["owns"] is False
        assert isinstance(ctx.cancel_event, threading.Event)
        assert ctx.cancel_event is not ctrl._cancel_event
        assert ctx.owns_cancel_event is False
        # run() releases the per-run event from the registry once it returns.
        assert ctx.cancel_event not in ctrl._cancel_registry._events
        assert ctrl._cancel_registry._events == set()

        direct = AgentRuntime.build_react_context(ctrl, "r-abc12345")
        assert direct.cancel_event is ctrl._cancel_event
        assert direct.owns_cancel_event is True

    def test_cancel_ownership_forwarded_not_owned(self):
        shared = threading.Event()
        ctrl = _make_controller(cancel_event=shared)
        assert ctrl._owns_cancel_event is False
        assert ctrl._cancel_registry is None
        ctx, _ = _run_capture(ctrl)
        assert ctx.owns_cancel_event is False
        assert ctx.cancel_event is shared
        direct = AgentRuntime.build_react_context(ctrl, "r-abc12345")
        assert direct.cancel_event is shared
        assert direct.owns_cancel_event is False

    def test_callbacks_and_context_payload_propagated(self):
        on_step = MagicMock()
        on_tool_trace = MagicMock()
        ctrl = _make_controller(
            on_step=on_step,
            on_tool_trace=on_tool_trace,
        )
        ctrl._context_payload = {"parent": "ctx"}
        ctrl._prompt_variant = "sub-agent"
        ctx, _ = _run_capture(ctrl)
        assert ctx.on_step is on_step
        assert ctx.on_tool_trace is on_tool_trace
        assert ctx._context_payload == {"parent": "ctx"}
        assert ctx._prompt_variant == "sub-agent"

    def test_controller_rejects_removed_job_history_fn(self):
        """The dormant operator job-history seam is gone (isolate-job-execution DRIFT-01)."""
        with pytest.raises(TypeError):
            _make_controller(job_history_fn=MagicMock())

    def test_react_context_has_no_job_history_field(self):
        from dataclasses import fields as dc_fields

        from agent_runtime import ControllerDeps
        from react_loop import ReactContext

        names = {f.name for f in dc_fields(ReactContext)} | {f.name for f in dc_fields(ControllerDeps)}
        assert "job_history_fn" not in names


# ===========================================================================
# 1.2 — SubAgentRunner construction + product surface
# ===========================================================================

class TestSubAgentRunnerConstruction:
    """Freeze SubAgentRunner construction and the consumer-facing surface."""

    def test_isolated_llm_client_provisioning(self, make_runner, pin_b):
        runner = make_runner(pin_b)
        # A distinct LLMClient (not the shared main client) pinned to B.
        from llm_client import LLMClient
        assert isinstance(runner._llm, LLMClient)
        assert runner._llm._run_model is pin_b
        assert runner._model_id == MODEL_B.id
        assert runner.model_id == MODEL_B.id
        # Each runner owns its own client even when built from the same pin.
        sibling = make_runner(pin_b)
        assert sibling._llm is not runner._llm
        assert sibling._llm._run_model is pin_b

    def test_short_term_memory_source_provided(self, make_runner):
        preloaded = ShortTermMemory()
        runner = make_runner(short_term=preloaded)
        assert runner._short_term is preloaded
        assert runner._agent.short_term is preloaded

    def test_short_term_memory_fresh_when_absent(self, make_runner):
        runner = make_runner(short_term=None)
        assert isinstance(runner._short_term, ShortTermMemory)
        assert runner._agent.short_term is runner._short_term

    def test_working_memory_is_fresh(self, make_runner):
        runner = make_runner()
        assert isinstance(runner._working, WorkingMemory)
        assert runner._agent.working is runner._working

    def test_prompt_variant_propagated(self, make_runner):
        runner = make_runner(prompt_variant="sub-agent")
        assert runner.prompt_variant == "sub-agent"
        assert runner._agent._prompt_variant == "sub-agent"

    def test_context_payload_propagated(self, make_runner):
        payload = {"summary": "parent state"}
        runner = make_runner(context_payload=payload)
        assert runner.context_payload == payload
        assert runner._agent._context_payload == payload

    def test_runner_product_surface(self, make_runner, pin_a):
        runner = make_runner()
        # Runner-shaped product surface consumed by supervisor/plan/registry.
        assert callable(runner.run)
        assert isinstance(runner.agent_id, str) and runner.agent_id.startswith("sa-")
        assert runner.model_id == MODEL_A.id
        assert runner._model_id == MODEL_A.id
        assert runner._llm._run_model is pin_a
        assert isinstance(runner._cancel_event, threading.Event)
        from llm_client import LLMClient
        assert isinstance(runner._llm, LLMClient)
        assert isinstance(runner._agent, AgentController)
        assert isinstance(runner._short_term, ShortTermMemory)
        assert callable(runner.close)
        assert callable(runner.notify_fn)


# ===========================================================================
# 1.3 — fallback trichotomy, overrides, usage registry, caller tag, restore
# ===========================================================================

class TestSubAgentModelConfiguration:
    """Freeze model-configuration construction invariants."""

    @pytest.mark.parametrize(
        "obsolete",
        [{"max_tokens": 256}, {"temperature": 0.9}, {"top_p": 0.95},
         {"model_cfg": {"model": "up/a"}}, {"config": {"models": []}}],
        ids=["max_tokens", "temperature", "top_p", "model_cfg", "config"],
    )
    def test_removed_model_and_generation_kwargs_rejected_before_allocation(
        self, obsolete, pin_a
    ):
        events: list[threading.Event] = []

        def factory(event: threading.Event) -> EmbeddingClient:
            events.append(event)
            return _unavailable_embedding(event)

        with pytest.raises(TypeError):
            SubAgentRunner(**_runner_kwargs(pin_a, embedding_factory=factory, **obsolete))
        assert events == []

    def test_typed_agent_config_reaches_controller_unchanged(self, make_runner):
        runner = make_runner()
        assert runner._agent._agent_cfg == AgentConfig()

    def test_nondefault_caller_config_reaches_controller_and_client_unmutated(
        self, pin_a
    ):
        caller_cfg = AgentConfig(
            agent_name="caller-agent", max_iterations=3, scheduled_max_iterations=55,
            tool_timeout=21, max_output_size=1234, top_tools=9,
            ctx_max_tokens=23456, compaction_threshold_tokens=33333, max_retries=7, retry_delay=0.25, max_subagents=2,
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
        # Independent snapshot taken BEFORE SubAgentRunner construction.
        snapshot = copy.deepcopy(asdict(caller_cfg))
        assert snapshot != asdict(AgentConfig())

        with ExitStack() as stack:
            runner = SubAgentRunner(**_runner_kwargs(pin_a, agent_cfg=caller_cfg))
            stack.callback(runner.close)

            assert asdict(caller_cfg) == snapshot
            assert asdict(runner._agent._agent_cfg) == snapshot
            assert runner._agent.max_iterations == 3
            assert runner._agent.top_tools == 9
            assert runner._agent.ctx_max_tokens == 23456
            assert runner._agent.compaction_threshold_tokens == 33333
            assert runner._agent.creativity_mode == "planner"
            assert runner._agent.plan_max_iterations == 17
            assert runner._agent.inactivity_warn_minutes == 4
            assert runner._llm._max_retries == snapshot["max_retries"]
            assert runner._llm._retry_delay == snapshot["retry_delay"]
            assert runner._llm._diagnose_empty is True

        assert asdict(caller_cfg) == snapshot

    def test_usage_registry_propagated(self, make_runner):
        registry = MagicMock()
        runner = make_runner(usage_registry=registry)
        assert runner._llm._usage_registry is registry

    def test_caller_tag_preserved(self, make_runner):
        runner = make_runner()
        assert runner._llm._base_caller_tag == runner.agent_id

    def test_pin_is_immutable_across_successful_run(
        self, make_runner, registry, pin_a
    ):
        runner = make_runner()
        assert runner._llm._run_model is pin_a
        assert not hasattr(runner._llm, "_active_idx")

        def _fake_agent_run(_task, prompt_id=None):
            registry.select(MODEL_B.id)  # selection change mid-run
            return "sub result"

        runner._agent.run = _fake_agent_run  # type: ignore[assignment]
        assert runner.run("task") == "sub result"
        assert runner._llm._run_model is pin_a
        assert runner._model_id == MODEL_A.id
        assert not hasattr(runner._llm, "_active_idx")

    def test_pin_is_immutable_across_run_exception(
        self, make_runner, registry, pin_a
    ):
        runner = make_runner()

        def _boom(_task, prompt_id=None):
            registry.select(MODEL_B.id)  # selection change before the crash
            raise RuntimeError("crash")

        runner._agent.run = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            runner.run("task")
        assert runner._llm._run_model is pin_a
        assert runner._model_id == MODEL_A.id
        assert not hasattr(runner._llm, "_active_idx")


# ===========================================================================
# Forwarded / shared cancel_event ownership
# ===========================================================================

class TestForwardedCancelEvent:
    """owns_cancel_event=False must not clear a forwarded/shared cancel signal."""

    def test_subagent_forwarded_cancel_event_is_shared_not_owned(self, make_runner):
        shared = threading.Event()
        runner = make_runner(cancel_event=shared)
        assert runner._cancel_event is shared
        assert runner._agent._cancel_event is shared
        # SubAgentRunner always hands the controller a non-None event, so the
        # controller never owns (and never clears) a sub-agent cancel signal.
        assert runner._agent._owns_cancel_event is False

    def test_react_loop_does_not_clear_forwarded_cancel(self):
        cancel = threading.Event()
        cancel.set()  # a stop request arrived just before the run started
        llm = ScriptedLLM(['{"action": "finish", "result": "done"}'])
        ex = RecordingExecutor()
        result, _calls, _progress = run_react(
            llm, ex, "goal", cancel_event=cancel, owns_cancel_event=False,
        )
        # Forwarded signal survives -> loop honors it and short-circuits.
        assert cancel.is_set()
        assert result == "⚠️ Task stopped by operator."

    def test_react_loop_clears_owned_cancel(self):
        cancel = threading.Event()
        cancel.set()  # stale set on an owned event must be cleared at start
        llm = ScriptedLLM(['{"action": "finish", "result": "done"}'])
        ex = RecordingExecutor()
        result, _calls, _progress = run_react(
            llm, ex, "goal", cancel_event=cancel, owns_cancel_event=True,
        )
        assert not cancel.is_set()
        assert result == "done"


# ===========================================================================
# 1.4 — registry-installed _on_step is not clobbered after construction
# ===========================================================================

class TestOnStepNotClobbered:
    """register_run() installs _on_step after construction; it must survive."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from sub_agent_registry import get_registry
        yield
        reg = get_registry()
        for rec in reg.list_active():
            reg.unregister(rec.agent_id)

    def test_construction_leaves_on_step_unset(self, make_runner):
        runner = make_runner(on_step=None)
        assert runner._agent._on_step is None

    def test_register_run_wires_on_step_and_survives(self, make_runner):
        from sub_agent_registry import get_registry, register_run

        runner = make_runner(on_step=None)
        assert runner._agent._on_step is None

        record = register_run(
            runner,
            source="plan-step",
            label="plan-x",
            task_preview="a task",
        )
        # Construction did not clobber the registry-installed callback.
        assert callable(runner._agent._on_step)

        runner._agent._on_step(4)
        registered = get_registry().get(runner.agent_id)
        assert registered is not None
        assert registered.iteration == 4
        assert record.iteration == 4


# ===========================================================================
# 4.1 — Runtime-owned ReactContext builder (assembly centralized in runtime)
# ===========================================================================

class TestRuntimeContextBuilder:
    """AgentRuntime.build_react_context is the per-run assembly path, and
    AgentController.run delegates to it while keeping per-run trace/model concerns."""

    def test_run_delegates_to_runtime_builder(self):
        ctrl = _make_controller()
        with patch.object(
            AgentRuntime, "build_react_context",
            wraps=AgentRuntime.build_react_context,
        ) as spy, patch("agent_controller.react_loop", side_effect=lambda ctx, *a, **k: "ok"):
            ctrl.run("goal")
        spy.assert_called_once()
        called_ctrl, called_trace = spy.call_args.args
        assert called_ctrl is ctrl
        assert _TRACE_RE.match(called_trace), called_trace

    def test_builder_freezes_core_fields(self):
        ctrl = _make_controller()
        ctx = AgentRuntime.build_react_context(ctrl, "r-deadbeef")
        # Frozen field values built by the runtime, independent of run().
        assert ctx.trace_id == "r-deadbeef"
        assert ctx.llm is ctrl.llm
        assert ctx.tool_index is ctrl.tool_index
        assert ctx.confirmation is ctrl._confirmation
        assert ctx.max_iterations == 11
        assert ctx.top_tools == 4
        assert ctx.ctx_max_tokens == 12345
        assert ctx.plan_max_iterations == 42
        assert ctx.inactivity_warn_minutes == 7
        assert ctx.max_subagents == 5
        assert ctx.depth == 0
        assert ctx.label == "main"

    def test_builder_preserves_post_init_memory_and_cancel(self):
        gm, gw, sm = object(), object(), object()
        shared = threading.Event()
        ctrl = _make_controller(cancel_event=shared)
        ctrl._graph_memory = gm  # type: ignore[assignment]
        ctrl._graph_memory_writer = gw  # type: ignore[assignment]
        ctrl._graph_memory_max_entries = 15
        ctrl.strategy_memory = sm  # type: ignore[attr-defined]
        ctrl._context_payload = {"parent": "ctx"}
        ctrl._prompt_variant = "sub-agent"
        ctx = AgentRuntime.build_react_context(ctrl, "r-abcd1234")
        assert ctx.graph_memory is gm
        assert ctx.graph_memory_writer is gw
        assert ctx.graph_memory_max_entries == 15
        assert ctx.strategy_memory is sm
        assert ctx.owns_cancel_event is False
        assert ctx.cancel_event is shared
        assert ctx._context_payload == {"parent": "ctx"}
        assert ctx._prompt_variant == "sub-agent"

    def test_builder_reads_on_step_after_registry_wiring(self):
        # Ordering: a registry-installed _on_step set AFTER construction must be
        # what the builder threads into the context (not clobbered/pre-read).
        ctrl = _make_controller()
        marker = object()
        ctrl._on_step = marker  # simulate register_run wiring post-construction
        ctx = AgentRuntime.build_react_context(ctrl, "r-11112222")
        assert ctx.on_step is marker


# ===========================================================================
# MAIN run-local pin and trace isolation (frontend responsibility)
# ===========================================================================

_TRACE_A = "r-a0a0a0a0"
_TRACE_B = "r-b0b0b0b0"
_TRACE_RUN = "r-12345678"


def _no_io_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected provider I/O: {request.url}")

    return httpx.MockTransport(handler)


class TestMainRunLocalPinIsolation:
    """A run uses its admitted client without leaking model or trace changes
    into later runs or the shared controller."""

    @pytest.mark.parametrize("explicit", [True, False], ids=["explicit-B", "default-A"])
    @pytest.mark.parametrize("outcome", ["success", "error"])
    def test_run_local_pin_and_trace_isolated(
        self, registry, pin_a, pin_b, explicit, outcome
    ):
        with ExitStack() as stack:
            clients: list[LLMClient] = []
            for pin in (pin_a, pin_b):
                event = threading.Event()
                embedding = _unavailable_embedding(event)
                stack.callback(embedding.close)
                client = LLMClient(
                    pin,
                    agent_config=AgentConfig(),
                    embedding_client=embedding,
                    http_transport=_no_io_transport(),
                )
                stack.callback(client.close)
                clients.append(client)
            client_a, client_b = clients
            client_a.set_trace_id(_TRACE_A)
            client_b.set_trace_id(_TRACE_B)

            ctrl = _make_controller(llm=client_a)
            expected, expected_pin, chosen = (
                (client_b, pin_b, MODEL_B) if explicit else (client_a, pin_a, MODEL_A)
            )
            other = MODEL_A if chosen is MODEL_B else MODEL_B
            registry.select(chosen.id)
            captured: list = []

            def fake_loop(ctx, _goal, _progress, _images, **_kwargs):
                captured.append(ctx)
                assert ctx.llm is expected
                assert expected._run_model is expected_pin
                assert ctrl.llm is client_a  # no temporary swap during execution
                assert ctx.trace_id == _TRACE_RUN
                assert expected._trace_id == _TRACE_RUN
                registry.select(other.id)  # selection change mid-run
                assert registry.view().active_model_id == other.id
                assert ctx.llm is expected
                assert expected._run_model is expected_pin
                assert ctrl.llm is client_a
                if outcome == "error":
                    raise RuntimeError("crash")
                return "done"

            run_kwargs: dict = {"trace_id": _TRACE_RUN}
            if explicit:
                run_kwargs["admitted_llm"] = client_b
            with patch("agent_controller.react_loop", side_effect=fake_loop):
                if outcome == "error":
                    with pytest.raises(RuntimeError, match="crash"):
                        ctrl.run("goal", **run_kwargs)
                else:
                    assert ctrl.run("goal", **run_kwargs) == "done"

            assert len(captured) == 1
            assert client_a._run_model is pin_a
            assert client_b._run_model is pin_b
            assert client_a._trace_id == _TRACE_A
            assert client_b._trace_id == _TRACE_B
            assert ctrl.llm is client_a
            assert captured[0].cancel_event not in ctrl._cancel_registry._events

            # A later default run still uses the shared controller's client A.
            later, result = _run_capture(ctrl)
            assert result == "ok"
            assert later.llm is client_a
            assert client_a._run_model is pin_a
            assert client_a._trace_id == _TRACE_A
            assert client_b._trace_id == _TRACE_B


# ===========================================================================
# isolate-job-execution 2.1 — scheduled runs use only job-owned context
# ===========================================================================

_SCHEDULED_TRIGGERS = ("cron", "once", "run_now")
_JOB_TAG = "nightly_report"


def _operator_collaborators() -> dict:
    """Operator-owned memory collaborators populated with recognisable content."""
    memory = MagicMock(name="operator_memory")
    memory.as_prompt_text.return_value = "operator KV memory"
    results = MagicMock(name="operator_results")
    results.as_prompt_text.return_value = "operator result recall"
    working = WorkingMemory()
    working.start_task("operator goal")
    working.add_step("tool", {"tool": "shell", "output": "operator tool result 1"})
    working.add_step("tool", {"tool": "file_read", "output": "operator tool result 2"})
    graph_memory = MagicMock(name="operator_graph")
    graph_memory.format_for_prompt.return_value = "operator graph memory"
    return {
        "memory": memory,
        "results": results,
        "working": working,
        "graph_memory": graph_memory,
    }


def _runtime_factory(
    runtime: AgentRuntime, registry: ModelRegistry, owned: list[SubAgentRunner],
) -> Callable[..., SubAgentRunner]:
    """Test-injected strict factory adapter over ``AgentRuntime.create``.

    Not the production root factory. Each call admits its own pin from
    *registry* — ``admit_scheduled(model)`` for ``SCHEDULED_AGENT``,
    ``admit(model)`` otherwise — and builds the runner for *runtime_profile*.
    Created runners are appended to *owned* so the caller can close them.
    """
    from agent_runtime import RuntimeOptions, RuntimeProfile

    def factory(model=None, context_key=None, label="on-demand", notify_fn=None,
                max_iterations=None, on_tool_trace=None, cancel_event=None,
                trace_id=None, context_payload=None, prompt_variant=None,
                runtime_profile=RuntimeProfile.ON_DEMAND_SUBAGENT):
        if runtime_profile is RuntimeProfile.SCHEDULED_AGENT:
            run_model = registry.admit_scheduled(model)
        else:
            run_model = registry.admit(model)
        options = RuntimeOptions(
            run_model=run_model, max_iterations=max_iterations,
            context_key=context_key, context_payload=context_payload,
            prompt_variant=prompt_variant, trace_id=trace_id,
            cancel_event=cancel_event, label=label,
        )
        runner = runtime.create(
            runtime_profile, options,
            notify_fn=notify_fn, on_tool_trace=on_tool_trace,
        )
        owned.append(runner)
        return runner

    return factory


def _build_operator_stack(make_builtin_executor, tmp_path):
    """Build a real executor + runtime wired to operator-owned collaborators.

    Returns ``(executor, operator_collaborators, owned_runners)``; the caller
    closes every runner in ``owned_runners``.
    """
    operator = _operator_collaborators()
    executor = make_builtin_executor(
        memory=operator["memory"],
        results=operator["results"],
        working=operator["working"],
        data_dir=str(tmp_path),
    )
    executor._graph_memory = operator["graph_memory"]
    runtime = AgentRuntime(
        agent_cfg=AgentConfig(),
        embedding_factory=_unavailable_embedding,
        tool_index=MagicMock(),
        base_memory=operator["memory"],
        builtin_executor=executor,
        skill_registry=MagicMock(),
        results=operator["results"],
        data_dir=str(tmp_path),
    )
    owned: list[SubAgentRunner] = []
    executor._sub_agent_factory = _runtime_factory(runtime, _registry(tmp_path), owned)
    return executor, operator, owned


def _make_scheduler(tmp_path, monkeypatch, executor):
    """A real Scheduler routing agentic jobs through ``executor.spawn_agent``."""
    from scheduler import Scheduler
    from xdg import xdg_paths

    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Scheduler(
        config={"scheduler": {"enabled": True}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
        builtin_executor=executor,
    )


def _fire_trigger(scheduler, trigger: str) -> None:
    """Fire *trigger* through the scheduler's own route (loop or run_now)."""
    import datetime as _dt

    import schedule

    if trigger == "run_now":
        assert scheduler.run_now(_JOB_TAG)["success"] is True
        return
    scheduler._register_job(_JOB_TAG, scheduler._jobs_meta[_JOB_TAG])
    past = _dt.datetime.now() - _dt.timedelta(seconds=5)
    if trigger == "cron":
        scheduler._jobs_meta[_JOB_TAG]["_next_run"] = past.isoformat()
    else:
        for job in schedule.get_jobs(_JOB_TAG):
            job.next_run = past
    loop = threading.Thread(target=scheduler._run_loop, daemon=True)
    scheduler._thread = loop
    loop.start()


def drive_scheduled_trigger(
    trigger: str, make_builtin_executor, tmp_path, monkeypatch,
    *, preserve_context: bool = False, legacy_flat_context: bool = False,
):
    """Run one scheduled job via a real trigger route; return its ReactContext.

    The job flows Scheduler -> ``BuiltinExecutor.spawn_agent`` -> supervisor
    admission -> the test-local strict ``_runtime_factory`` adapter (which
    admits a fresh scheduled pin per call) -> ``AgentRuntime.create`` ->
    ``SubAgentRunner.run``; only the ReAct loop itself is replaced so the
    constructed context can be inspected. These tests prove job-owned
    isolation through that adapter, not through the production factory or the
    not-yet-migrated root wiring.
    Returns ``(ctx, operator_collaborators)``.
    """
    import schedule

    from builtin_tools.context_io import _save_context, scheduled_context_key
    from sub_agent_registry import get_registry

    executor, operator, owned = _build_operator_stack(make_builtin_executor, tmp_path)
    scheduler = _make_scheduler(tmp_path, monkeypatch, executor)
    schedule_kwargs = (
        {"schedule_type": "once", "run_at": "23:59"}
        if trigger == "once"
        else {"schedule_type": "cron", "cron": "0 3 * * *"}
    )
    added = scheduler.add_job(
        _JOB_TAG, task="compile the nightly report", notify=False,
        preserve_context=preserve_context, **schedule_kwargs,
    )
    assert added["success"] is True
    if preserve_context:
        saved = ShortTermMemory(max_turns=50)
        saved.add("user", "job-owned earlier request")
        saved.add("assistant", "job-owned earlier answer")
        _save_context(
            _JOB_TAG if legacy_flat_context else scheduled_context_key(_JOB_TAG),
            saved, str(tmp_path),
        )

    captured: dict = {}
    ran = threading.Event()

    def _fake_loop(ctx, goal, _progress, _images):
        captured["ctx"] = ctx
        captured["goal"] = goal
        ran.set()
        return "job done"

    try:
        with patch("agent_controller.react_loop", side_effect=_fake_loop):
            _fire_trigger(scheduler, trigger)
            assert ran.wait(timeout=10), f"{trigger} trigger never ran the job"
    finally:
        scheduler._stop_event.set()
        if scheduler._thread is not None:
            scheduler._thread.join(timeout=5)
        schedule.clear(_JOB_TAG)
        executor._supervisor.shutdown(graceful_timeout=2)
        reg = get_registry()
        for rec in reg.list_active():
            reg.unregister(rec.agent_id)
        for runner in owned:
            runner.close()
    return captured["ctx"], operator


@pytest.mark.parametrize("trigger", _SCHEDULED_TRIGGERS)
class TestScheduledRunJobOwnedContext:
    """Each scheduled run constructs only job-owned context (design decision 2)."""

    def test_operator_kv_memory_not_wired(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
        )
        assert ctx.memory is not operator["memory"]
        assert "operator KV memory" not in ctx.memory.as_prompt_text()

    def test_operator_result_recall_not_wired(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, _operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
        )
        assert ctx.results is None

    def test_operator_working_memory_not_wired(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
        )
        assert ctx.working is not operator["working"]
        assert not ctx.working.has_content()

    def test_graph_memory_references_remain_none(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, _operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
        )
        assert ctx.graph_memory is None
        assert ctx.graph_memory_writer is None

    def test_cross_job_execution_history_not_wired(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, _operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
        )
        assert not hasattr(ctx, "job_history_fn")

    def test_preserve_context_retains_only_job_conversation(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, _operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
            preserve_context=True,
        )
        assert ctx.short_term.get_messages() == [
            {"role": "user", "content": "job-owned earlier request"},
            {"role": "assistant", "content": "job-owned earlier answer"},
        ]

    def test_legacy_flat_context_is_never_imported(
        self, trigger, make_builtin_executor, tmp_path, monkeypatch,
    ):
        ctx, _operator = drive_scheduled_trigger(
            trigger, make_builtin_executor, tmp_path, monkeypatch,
            preserve_context=True, legacy_flat_context=True,
        )
        assert ctx.short_term.get_messages() == []


class TestScheduledTriggerRoutesAgree:
    """Cron, once, and run_now produce the same job-owned isolation outcome."""

    def test_all_trigger_routes_yield_identical_isolation(
        self, make_builtin_executor, tmp_path, monkeypatch,
    ):
        outcomes = {}
        for trigger in _SCHEDULED_TRIGGERS:
            run_dir = tmp_path / trigger
            run_dir.mkdir()
            ctx, operator = drive_scheduled_trigger(
                trigger, make_builtin_executor, run_dir, monkeypatch,
                preserve_context=True,
            )
            outcomes[trigger] = {
                "own_kv_memory": ctx.memory is not operator["memory"],
                "own_working_memory": ctx.working is not operator["working"],
                "no_result_recall": ctx.results is None,
                "no_graph_memory": ctx.graph_memory is None
                and ctx.graph_memory_writer is None,
                "job_messages": [m["content"] for m in ctx.short_term.get_messages()],
            }

        assert set(outcomes) == set(_SCHEDULED_TRIGGERS)
        assert outcomes["cron"] == outcomes["once"] == outcomes["run_now"]
        assert outcomes["cron"] == {
            "own_kv_memory": True,
            "own_working_memory": True,
            "no_result_recall": True,
            "no_graph_memory": True,
            "job_messages": ["job-owned earlier request", "job-owned earlier answer"],
        }


class TestScheduledMemoryIsolatedPerRun:
    """Scheduled memory objects are private to one run, never shared across runs."""

    def test_each_scheduled_product_gets_distinct_memory(self, pin_a):
        from agent_runtime import RuntimeOptions, RuntimeProfile

        runtime = AgentRuntime(
            agent_cfg=AgentConfig(), embedding_factory=_unavailable_embedding,
            base_memory=MagicMock(name="operator_memory"),
            builtin_executor=MagicMock(), results=MagicMock(),
        )
        with ExitStack() as stack:
            first = runtime.create(
                RuntimeProfile.SCHEDULED_AGENT, RuntimeOptions(run_model=pin_a),
            )
            stack.callback(first.close)
            second = runtime.create(
                RuntimeProfile.SCHEDULED_AGENT, RuntimeOptions(run_model=pin_a),
            )
            stack.callback(second.close)
            assert first._agent.memory is not second._agent.memory


class TestOnDemandMemoryWiringUnchanged:
    """On-demand construction keeps sharing operator memory and result recall."""

    def test_on_demand_keeps_operator_memory_wiring(self, pin_a):
        from agent_runtime import RuntimeOptions, RuntimeProfile

        memory, results = MagicMock(name="operator_memory"), MagicMock()
        runtime = AgentRuntime(
            agent_cfg=AgentConfig(), embedding_factory=_unavailable_embedding,
            base_memory=memory, builtin_executor=MagicMock(), results=results,
        )
        runner = runtime.create(
            RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeOptions(run_model=pin_a),
        )
        try:
            assert runner._agent.memory is memory
            assert runner._agent.results is results
        finally:
            runner.close()


class TestRuntimeFactoryAdapterAdmission:
    """The strict test adapter admits once per call on the profile's own branch."""

    @pytest.mark.parametrize("scheduled", [False, True], ids=["on-demand", "scheduled"])
    @pytest.mark.parametrize("selector,expected", [(None, MODEL_A), (MODEL_B.id, MODEL_B)],
                             ids=["inherit-main", "explicit-B"])
    def test_each_call_admits_fresh_pin_on_profile_branch(
        self, registry, scheduled, selector, expected,
    ):
        from agent_runtime import RuntimeProfile

        profile = (RuntimeProfile.SCHEDULED_AGENT if scheduled
                   else RuntimeProfile.ON_DEMAND_SUBAGENT)
        method, opposite = (("admit_scheduled", "admit") if scheduled
                            else ("admit", "admit_scheduled"))
        runtime = AgentRuntime(
            agent_cfg=AgentConfig(), embedding_factory=_unavailable_embedding,
            tool_index=MagicMock(), base_memory=MagicMock(),
            builtin_executor=MagicMock(), skill_registry=MagicMock(),
            results=MagicMock(),
        )
        owned: list[SubAgentRunner] = []
        factory = _runtime_factory(runtime, registry, owned)
        admitted: list[RunModel] = []
        real_admit = getattr(registry, method)

        def _observe(raw=None):
            pin = real_admit(raw)
            admitted.append(pin)
            return pin

        try:
            with patch.object(registry, method, side_effect=_observe) as primary, \
                    patch.object(registry, opposite) as other, \
                    patch.object(registry, "_credential_resolver",
                                 wraps=registry._credential_resolver) as resolver:
                runners = [
                    factory(model=selector, label="nightly", runtime_profile=profile)
                    for _ in range(2)
                ]
            assert [c.args for c in primary.call_args_list] == [(selector,), (selector,)]
            other.assert_not_called()
            assert resolver.call_count == 2
            assert runners == owned
            assert admitted[0] is not admitted[1]
            for runner, pin in zip(runners, admitted):
                assert isinstance(pin, RunModel)
                assert runner._llm._run_model is pin
                assert runner.model_id == expected.id
                assert runner.label == "nightly"
            assert registry.view().active_model_id == MODEL_A.id
        finally:
            for runner in owned:
                runner.close()


# ===========================================================================
# 7.1 / 12.11 — production root factory/profile wiring through actual main._run
# ===========================================================================

@pytest.fixture
def root_harness(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Yield a root harness; close production leftovers only after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch)
    yield harness
    harness.release_leftovers()


class TestRootSubAgentFactoryWiring:
    """The real root installs the helper's exact factory; each call admits once.

    Unlike ``TestRuntimeFactoryAdapterAdmission`` (a strict injected adapter),
    this drives the actual ``main._run`` via ``tests/root_startup_harness.py``
    and invokes the factory while the fake Telegram ``run()`` holds startup.
    Runners are constructed on real runtime products and closed, never run.
    """

    @pytest.mark.parametrize(
        "profile_name,selector,expected,method",
        [
            ("ON_DEMAND_SUBAGENT", None, ROOT_MAIN_ID, "admit"),
            ("PLAN_STEP_AGENT", ROOT_OTHER_ID, ROOT_OTHER_ID, "admit"),
            ("DIAGNOSTIC_AGENT", None, ROOT_MAIN_ID, "admit"),
            ("SCHEDULED_AGENT", ROOT_OTHER_ID, ROOT_OTHER_ID, "admit_scheduled"),
        ],
        ids=["on-demand", "plan-step", "diagnostic", "scheduled"],
    )
    def test_builtin_installs_exact_helper_factory_and_forwards_one_pin(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        profile_name: str, selector: str | None, expected: str, method: str,
    ) -> None:
        """The installed root factory admits once on its profile branch and forwards that pin."""
        from agent_runtime import RuntimeOptions, RuntimeProfile

        profile = RuntimeProfile[profile_name]
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Build one runner through the installed factory, inspect it, close it."""
            runtime_arg, registry_arg, factory = root.factory_binding()
            outcome["binding"] = (
                runtime_arg is root.runtime, registry_arg is root.registry,
                root.builtin._sub_agent_factory is factory,
            )
            admitted: dict[str, list[object]] = {"admit": [], "admit_scheduled": []}
            pins: list[RunModel] = []
            for name in admitted:
                original = getattr(root.registry, name)

                def spy(
                    raw: object = None,
                    *,
                    _name: str = name,
                    _original: Callable[[object], RunModel] = original,
                ) -> RunModel:
                    """Record the selector and pin, delegating to the real admission."""
                    admitted[_name].append(raw)
                    pin = _original(raw)
                    pins.append(pin)
                    return pin

                monkeypatch.setattr(root.registry, name, spy)
            creates: list[tuple[RuntimeProfile, RuntimeOptions]] = []
            create = root.runtime.create

            def create_spy(
                profile_arg: RuntimeProfile, options: RuntimeOptions, **kwargs: Any,
            ) -> SubAgentRunner:
                """Record the profile and options, delegating to the real runtime."""
                creates.append((profile_arg, options))
                return create(profile_arg, options, **kwargs)

            monkeypatch.setattr(root.runtime, "create", create_spy)
            llm_before = len(root.llm_clients)
            runner = factory(model=selector, label="root-probe", runtime_profile=profile)
            try:
                outcome["admitted"] = admitted
                outcome["forwarded"] = [
                    (p is profile, o.run_model is pins[0], o.label) for p, o in creates
                ]
                outcome["runner"] = (
                    isinstance(runner, SubAgentRunner), runner._llm._run_model is pins[0],
                    runner.model_id, runner.label,
                )
                outcome["new_llm_clients"] = root.llm_clients[llm_before:] == [runner._llm]
                outcome["embedding"] = (
                    isinstance(runner._embedding, EmbeddingClient),
                    runner._embedding is not root.root_embedding,
                    runner._embedding._cancel_event is runner._cancel_event,
                )
            finally:
                runner.close()
            outcome["closed"] = runner._llm._closed

        root_harness.start(saved_default=ROOT_MAIN_ID, hold=hold)

        assert outcome["binding"] == (True, True, True)
        opposite = "admit_scheduled" if method == "admit" else "admit"
        assert outcome["admitted"] == {method: [selector], opposite: []}
        assert outcome["forwarded"] == [(True, True, "root-probe")]
        assert outcome["runner"] == (True, True, expected, "root-probe")
        assert outcome["new_llm_clients"] is True
        assert outcome["embedding"] == (True, True, True)
        assert outcome["closed"] is True
        assert root_harness.unexpected == []
