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

import re
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_controller import AgentController, SubAgentRunner
from agent_runtime import AgentRuntime
from memory_store import ShortTermMemory, WorkingMemory

from tests.execution_harness import RecordingExecutor, ScriptedLLM, run_react


_TRACE_RE = re.compile(r"^r-[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _make_controller(**overrides) -> AgentController:
    """Construct an AgentController with frozen, inspectable knob values."""
    builtin = MagicMock()
    builtin._max_subagents = 5  # frozen expected value for ctx.max_subagents
    kwargs = dict(
        llm=MagicMock(),
        tool_index=MagicMock(),
        memory=MagicMock(),
        max_iterations=11,
        top_tools=4,
        ctx_max_tokens=12345,
        short_term=MagicMock(),
        working=MagicMock(),
        results=MagicMock(),
        builtin_executor=builtin,
        skill_registry=MagicMock(),
        mcp_manager=MagicMock(),
        depth=0,
        label="main",
        plan_max_iterations=42,
        inactivity_warn_minutes=7,
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


def _cfg(fallback=None) -> dict:
    """A real multi-model config usable to build a real LLMClient."""
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


def _make_runner(config: dict, *, model_cfg=None, fallback_models=None,
                 short_term=None, usage_registry=None, cancel_event=None,
                 context_payload=None, prompt_variant=None, on_step=None,
                 label: str = "on-demand") -> SubAgentRunner:
    """Construct a real SubAgentRunner (builds a real isolated LLMClient)."""
    model_cfg = model_cfg if model_cfg is not None else config["models"][0]
    return SubAgentRunner(
        model_cfg=model_cfg,
        config=config,
        tool_index=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=None,
        results=MagicMock(),
        short_term=short_term,
        notify_fn=None,
        label=label,
        usage_registry=usage_registry,
        fallback_models=fallback_models,
        cancel_event=cancel_event,
        context_payload=context_payload,
        prompt_variant=prompt_variant,
        on_step=on_step,
    )


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

    def test_cancel_ownership_owned_by_default(self):
        """When the controller owns its cancellation (no external event supplied),
        run() mints a run-private event via CancelEventRegistry rather than
        reusing ctrl._cancel_event, so concurrent runs on one shared controller
        (e.g. two different users on the MAIN agent) never share an event and
        can't race on its clear()/set() state. ctrl._cancel_event remains only
        as the fallback for callers that bypass run() (e.g. a bare
        build_react_context() call)."""
        ctrl = _make_controller()  # cancel_event defaults to None -> owned
        ctx, _ = _run_capture(ctrl)
        assert ctx.owns_cancel_event is True
        assert isinstance(ctx.cancel_event, threading.Event)
        assert ctx.cancel_event is not ctrl._cancel_event
        # run() releases the per-run event from the registry once it returns.
        assert ctx.cancel_event not in ctrl._cancel_registry._events

    def test_cancel_ownership_forwarded_not_owned(self):
        shared = threading.Event()
        ctrl = _make_controller(cancel_event=shared)
        ctx, _ = _run_capture(ctrl)
        assert ctx.owns_cancel_event is False
        assert ctx.cancel_event is shared

    def test_callbacks_and_context_payload_propagated(self):
        on_step = MagicMock()
        on_tool_trace = MagicMock()
        job_history_fn = MagicMock()
        ctrl = _make_controller(
            on_step=on_step,
            on_tool_trace=on_tool_trace,
            job_history_fn=job_history_fn,
        )
        ctrl._context_payload = {"parent": "ctx"}
        ctrl._prompt_variant = "sub-agent"
        ctx, _ = _run_capture(ctrl)
        assert ctx.on_step is on_step
        assert ctx.on_tool_trace is on_tool_trace
        assert ctx.job_history_fn is job_history_fn
        assert ctx._context_payload == {"parent": "ctx"}
        assert ctx._prompt_variant == "sub-agent"


# ===========================================================================
# 1.2 — SubAgentRunner construction + product surface
# ===========================================================================

class TestSubAgentRunnerConstruction:
    """Freeze SubAgentRunner construction and the consumer-facing surface."""

    def test_isolated_llm_client_provisioning(self):
        config = _cfg()
        runner = _make_runner(config, model_cfg=config["models"][1])  # model-b
        # A distinct LLMClient (not the shared main client).
        from llm_client import LLMClient
        assert isinstance(runner._llm, LLMClient)
        # Chosen model is placed first and is the active/default model.
        assert runner._llm._models[0].get("model") == "model-b"
        assert runner._llm.llm_cfg.get("model") == "model-b"
        assert runner._model_id == "model-b"

    def test_short_term_memory_source_provided(self):
        config = _cfg()
        preloaded = ShortTermMemory()
        runner = _make_runner(config, short_term=preloaded)
        assert runner._short_term is preloaded
        assert runner._agent.short_term is preloaded

    def test_short_term_memory_fresh_when_absent(self):
        config = _cfg()
        runner = _make_runner(config, short_term=None)
        assert isinstance(runner._short_term, ShortTermMemory)
        assert runner._agent.short_term is runner._short_term

    def test_working_memory_is_fresh(self):
        config = _cfg()
        runner = _make_runner(config)
        assert isinstance(runner._working, WorkingMemory)
        assert runner._agent.working is runner._working

    def test_prompt_variant_propagated(self):
        config = _cfg()
        runner = _make_runner(config, prompt_variant="sub-agent")
        assert runner.prompt_variant == "sub-agent"
        assert runner._agent._prompt_variant == "sub-agent"

    def test_context_payload_propagated(self):
        config = _cfg()
        payload = {"summary": "parent state"}
        runner = _make_runner(config, context_payload=payload)
        assert runner.context_payload == payload
        assert runner._agent._context_payload == payload

    def test_runner_product_surface(self):
        config = _cfg()
        runner = _make_runner(config)
        # Runner-shaped product surface consumed by supervisor/plan/registry.
        assert callable(runner.run)
        assert isinstance(runner.agent_id, str) and runner.agent_id.startswith("sa-")
        assert runner._model_id == "model-a"
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

    def test_fallback_none_inherits_config(self):
        # config declares two inheritable fallbacks
        config = _cfg(fallback=["model-b", "model-c"])
        runner = _make_runner(config, fallback_models=None)
        assert len(runner._llm._fallback_indices) == 2

    def test_fallback_empty_disables(self):
        config = _cfg(fallback=["model-b", "model-c"])
        runner = _make_runner(config, fallback_models=[])
        assert runner._llm._fallback_indices == []

    def test_fallback_explicit_list(self):
        config = _cfg(fallback=["model-b", "model-c"])
        runner = _make_runner(config, fallback_models=["model-b"])
        assert len(runner._llm._fallback_indices) == 1

    def test_per_call_overrides_preserved_in_isolated_client(self):
        config = _cfg()
        # Simulate factory-merged per-call overrides baked into model_cfg.
        model_cfg = {**config["models"][0], "max_tokens": 256,
                     "temperature": 0.9, "top_p": 0.95}
        runner = _make_runner(config, model_cfg=model_cfg)
        active = runner._llm.llm_cfg
        assert active["max_tokens"] == 256
        assert active["temperature"] == pytest.approx(0.9)
        assert active["top_p"] == pytest.approx(0.95)

    def test_per_call_overrides_do_not_mutate_shared_config(self):
        config = _cfg()
        original_first = config["models"][0]
        assert original_first.get("max_tokens") == 1024
        model_cfg = {**original_first, "max_tokens": 256}
        _make_runner(config, model_cfg=model_cfg)
        # Shared config entry is unchanged.
        assert config["models"][0]["max_tokens"] == 1024

    def test_usage_registry_propagated(self):
        config = _cfg()
        registry = MagicMock()
        runner = _make_runner(config, usage_registry=registry)
        assert runner._llm._usage_registry is registry

    def test_caller_tag_preserved(self):
        config = _cfg()
        runner = _make_runner(config)
        assert runner._llm._base_caller_tag == runner.agent_id

    def test_active_index_restored_after_run(self):
        config = _cfg()
        runner = _make_runner(config)
        assert runner._llm._active_idx == 0

        def _fake_agent_run(_task, prompt_id=None):
            runner._llm._active_idx = 2  # simulate a mid-run fallback
            return "sub result"

        runner._agent.run = _fake_agent_run  # type: ignore[assignment]
        result = runner.run("task")
        assert result == "sub result"
        assert runner._llm._active_idx == 0

    def test_active_index_restored_after_run_exception(self):
        config = _cfg()
        runner = _make_runner(config)

        def _boom(_task, prompt_id=None):
            runner._llm._active_idx = 1
            raise RuntimeError("crash")

        runner._agent.run = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            runner.run("task")
        assert runner._llm._active_idx == 0


# ===========================================================================
# Forwarded / shared cancel_event ownership
# ===========================================================================

class TestForwardedCancelEvent:
    """owns_cancel_event=False must not clear a forwarded/shared cancel signal."""

    def test_subagent_forwarded_cancel_event_is_shared_not_owned(self):
        config = _cfg()
        shared = threading.Event()
        runner = _make_runner(config, cancel_event=shared)
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
        assert result == "[Cancelled]"

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

    def test_construction_leaves_on_step_unset(self):
        config = _cfg()
        runner = _make_runner(config, on_step=None)
        assert runner._agent._on_step is None

    def test_register_run_wires_on_step_and_survives(self):
        from sub_agent_registry import get_registry, register_run

        config = _cfg()
        runner = _make_runner(config, on_step=None)
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
# MAIN _active_idx save/restore (frontend responsibility, post-migration)
# ===========================================================================

class TestMainActiveIndexRestore:
    """AgentController.run keeps model _active_idx save/restore in the frontend
    after ReactContext assembly moved to the runtime builder."""

    def _make_llm(self, idx: int = 0):
        llm = MagicMock()
        llm._active_idx = idx  # real int, mirrors LLMClient
        return llm

    def test_active_idx_restored_on_success(self):
        llm = self._make_llm(0)
        ctrl = _make_controller(llm=llm)

        def fake(ctx, *a, **k):
            ctx.llm._active_idx = 2  # transient fallback mid-run
            return "done"

        with patch("agent_controller.react_loop", side_effect=fake):
            assert ctrl.run("goal") == "done"
        assert llm._active_idx == 0

    def test_active_idx_restored_on_exception(self):
        llm = self._make_llm(0)
        ctrl = _make_controller(llm=llm)

        def boom(ctx, *a, **k):
            ctx.llm._active_idx = 3
            raise RuntimeError("crash")

        with patch("agent_controller.react_loop", side_effect=boom):
            with pytest.raises(RuntimeError):
                ctrl.run("goal")
        assert llm._active_idx == 0
