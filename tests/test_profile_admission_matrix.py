"""Task 12.13: ordinary explicit-selector admission across sub-agent profiles.

Real chain: injected strict ordinary factory → real ``ModelRegistry.admit``
(exactly once) → real ``AgentRuntime.create`` → real ``SubAgentRunner`` /
``AgentController`` / ``react_loop`` → real ``LLMClient`` on
``httpx.MockTransport``. Callers are the factory interface itself (profile
matrix), the real ``BuiltinExecutor`` spawn shim with the real
``SubAgentSupervisor``, and the real ``PlanExecutor`` (which always passes
``model=None``). Root ``main.py`` wiring is out of scope (task 12.11). Only
external boundaries are substituted: file system (``tmp_path``/XDG),
discovery, credential resolution and HTTP.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx
import pytest

import agent_controller
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile
from context_monitor import ContextMonitor, ContextSnapshot
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from execution_plan import ExecutionPlan, PlanExecutor, PlanStep
from llm_client import LLMClient
from memory_store import MemoryStore
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
from outcome_utils import read_execution
from react_loop import ReactContext
from sub_agent_registry import get_registry
from sub_agent_supervisor import SupervisionOptions
from token_usage import TokenUsageRegistry

_BOUND = 10.0
RESULT = "matrix-done"
TURN = "r-13c0ffee"
AGENT = AgentConfig(max_retries=0, retry_delay=0)
URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}
ORDINARY = (
    RuntimeProfile.ON_DEMAND_SUBAGENT,
    RuntimeProfile.PLAN_STEP_AGENT,
    RuntimeProfile.DIAGNOSTIC_AGENT,
)


def _record(provider: str, upstream: str) -> ModelRecord:
    """Return one catalog record with complete provider-sourced metadata."""
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            200_000, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a")
MODEL_B = _record("pb", "up/b")
OPAQUE_ARGS = {"model": MODEL_B.id, "temperature": 0.3, "note": "opaque"}


class _NoDiscovery:
    """Discovery boundary that must never be reached during admission."""

    def fetch(self, connection: object) -> object:
        """Fail loudly: admission never refreshes the catalog."""
        raise AssertionError("discovery must not run during ordinary admission")


class _Registry(ModelRegistry):
    """Real registry that records every admission around the real implementation."""

    def __init__(self, *args: object) -> None:
        """Build the real registry and empty admission records."""
        super().__init__(*args)
        self.admits: list[object] = []
        self.errors: list[str] = []
        self.scheduled_calls = 0

    def admit(self, selector: str | None = None) -> RunModel:
        """Record *selector* and any typed refusal code, then defer to ``super``."""
        self.admits.append(selector)
        try:
            return super().admit(selector)
        except ModelRegistryError as exc:
            self.errors.append(exc.code)
            raise

    def admit_scheduled(self, raw_selector: object = None) -> RunModel:
        """Count scheduled admissions; ordinary callers must never reach this."""
        self.scheduled_calls += 1
        return super().admit_scheduled(raw_selector)


class _TrackedLLM(LLMClient):
    """Real chat client counting ``close`` calls."""

    close_count = 0

    def close(self) -> None:
        """Count, then close the real client."""
        self.close_count += 1
        super().close()


class _TrackedEmbedding(EmbeddingClient):
    """Real embedding client counting ``close`` calls."""

    close_count = 0

    def close(self) -> None:
        """Count, then close the real client."""
        self.close_count += 1
        super().close()


class _NullToolIndex:
    """Tool index with no indexed tools."""

    def search(self, *_a: object, **_k: object) -> list:
        """Return no matches."""
        return []

    def all_tools(self) -> list:
        """Return no tools."""
        return []


class _OrdinaryFactory:
    """Injected strict ordinary factory: one real ``admit``, then the real runtime.

    The keyword-only signature is exactly the union of the real spawn,
    plan-step and diagnostic call contracts, so an unexpected keyword raises
    ``TypeError``. The admitted pin reaches ``AgentRuntime.create`` unchanged
    and is never re-admitted.
    """

    def __init__(self, registry: _Registry) -> None:
        """Bind the registry; the runtime is attached once the executor exists."""
        self.registry = registry
        self.runtime: AgentRuntime | None = None
        self.calls: list[dict] = []
        self.pins: list[RunModel] = []
        self.runners: list[agent_controller.SubAgentRunner] = []

    def __call__(
        self, *, model: object, runtime_profile: RuntimeProfile,
        context_key: Optional[str] = None, label: Optional[str] = None,
        notify_fn: Optional[Callable[[str], None]] = None, trace_id: Optional[str] = None,
        context_payload: Optional[dict] = None, max_iterations: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None, prompt_variant: Optional[str] = None,
        on_tool_trace: Optional[Callable] = None,
    ) -> agent_controller.SubAgentRunner:
        """Admit *model* once and build the runner for *runtime_profile*."""
        self.calls.append({"model": model, "profile": runtime_profile, "label": label})
        if runtime_profile not in ORDINARY:
            raise TypeError(f"not an ordinary profile: {runtime_profile!r}")
        pin = self.registry.admit(model)
        self.pins.append(pin)
        runner = self.runtime.create(
            runtime_profile,
            RuntimeOptions(
                run_model=pin, max_iterations=max_iterations, context_key=context_key,
                context_payload=context_payload, prompt_variant=prompt_variant,
                trace_id=trace_id, cancel_event=cancel_event, label=label,
            ),
            notify_fn=notify_fn, on_tool_trace=on_tool_trace,
        )
        self.runners.append(runner)
        return runner


class _World:
    """Real registry, executor and runtime under ``tmp_path`` with scripted HTTP."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                 make_builtin_executor: Callable, *, select: bool) -> None:
        """Persist an A/B catalog, optionally select Main A, and wire the factory."""
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            monkeypatch.setenv(var, str(tmp_path / var.lower()))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        self.resolved: list[str] = []
        self.chat_requests: list[httpx.Request] = []
        self.embed_requests: list[httpx.Request] = []
        self.chats: list[_TrackedLLM] = []
        self.embeddings: list[_TrackedEmbedding] = []
        self.embed_http: list[httpx.Client] = []
        self.usage = TokenUsageRegistry()
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        self.settings_path = tmp_path / "models-config.toml"
        settings = ModelSettings(self.settings_path)
        settings.write(CatalogDocument(
            1, 2, None,
            tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (MODEL_A, MODEL_B),
        ))
        self.registry = _Registry(providers, settings, _NoDiscovery(), self._resolve)
        if select:
            self.registry.select(MODEL_A.id)
        self.document = self.settings_path.read_bytes()
        monkeypatch.setattr(agent_controller, "LLMClient", self._make_llm)

        self.factory = _OrdinaryFactory(self.registry)
        self.executor = make_builtin_executor(sub_agent_factory=self.factory)
        self.factory.runtime = AgentRuntime(
            agent_cfg=AGENT, embedding_factory=self._embedding,
            paths=ExecutorPaths(tmp_dir=str(tmp_path / "tmp"),
                                downloads_dir=str(tmp_path / "dl"),
                                workspace_dir=str(tmp_path / "ws")),
            tool_index=_NullToolIndex(), base_memory=MemoryStore(str(tmp_path / "memory.json")),
            builtin_executor=self.executor, usage_registry=self.usage,
            data_dir=str(tmp_path / "data"), scheduled_max_iterations=4,
        )

    def _resolve(self, config: ProviderConfig) -> ResolvedProviderConnection:
        """Credential boundary: record the provider and return a test secret."""
        self.resolved.append(config.name)
        return ResolvedProviderConnection(
            provider_id=config.name, adapter=config.type, base_url=config.base_url,
            request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
        )

    def _chat(self, request: httpx.Request) -> httpx.Response:
        """HTTP boundary: answer every chat request with a finish action."""
        self.chat_requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"action": "finish", "result": RESULT})}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    def _embed(self, request: httpx.Request) -> httpx.Response:
        """HTTP boundary for embeddings."""
        self.embed_requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.5]}]})

    def _make_llm(self, run_model: RunModel, **kwargs: object) -> _TrackedLLM:
        """Build the real pinned client on a mock transport."""
        client = _TrackedLLM(run_model, http_transport=httpx.MockTransport(self._chat), **kwargs)
        self.chats.append(client)
        return client

    def _embedding(self, event: threading.Event) -> EmbeddingClient:
        """Build a runner-owned embedding client over a borrowed test HTTP client."""
        http_client = httpx.Client(transport=httpx.MockTransport(self._embed))
        self.embed_http.append(http_client)
        client = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key",
                base_url="https://embed.example.test/v1"),
            http_client=http_client, cancel_event=event,
        )
        self.embeddings.append(client)
        return client

    def parent_ctx(self) -> ReactContext:
        """Parent context whose real executor supplies the plan factory."""
        return ReactContext(llm=None, tool_index=None, memory=None,
                            builtin_executor=self.executor, mcp_manager=None,
                            skill_registry=None, trace_id="r-0b1a2c3d")

    def teardown(self) -> None:
        """Drain the sub-agent pool and close every test-owned handle."""
        try:
            self.executor.shutdown(graceful_timeout=_BOUND)
        finally:
            for runner in self.factory.runners:
                runner.close()
            for http_client in self.embed_http:
                http_client.close()
            for client in self.chats:
                LLMClient.close(client)


@pytest.fixture
def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
               make_builtin_executor: Callable) -> Iterator[Callable[..., _World]]:
    """Build isolated worlds and tear each down even when assertions fail."""
    worlds: list[_World] = []

    def build(*, select: bool) -> _World:
        world = _World(tmp_path, monkeypatch, make_builtin_executor, select=select)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.teardown()


def _assert_nothing_allocated(world: _World, active_before: int) -> None:
    """No pin, credential, client, request, registration or settings write."""
    assert world.factory.pins == [] and world.factory.runners == []
    assert world.resolved == []
    assert world.chats == [] and world.embeddings == [] and world.embed_http == []
    assert world.chat_requests == [] and world.embed_requests == []
    assert len(get_registry().list_active()) == active_before
    assert world.registry.scheduled_calls == 0
    assert world.settings_path.read_bytes() == world.document


def _assert_one_closed_run_on(
    world: _World, expected: ModelRecord, main: str | None = MODEL_A.id,
) -> httpx.Request:
    """One pinned run on *expected*: wire, usage, closed handles, *main* unchanged."""
    (pin,) = world.factory.pins
    assert pin.model == expected and world.resolved == [expected.provider_id]
    (runner,) = world.factory.runners
    assert runner.model_id == expected.id and runner._llm._run_model is pin
    (request,) = world.chat_requests
    assert str(request.url) == f"{URLS[expected.provider_id]}/chat/completions"
    assert request.headers["authorization"] == f"Bearer secret-{expected.provider_id}"
    assert json.loads(request.content)["model"] == expected.upstream_id
    usage = world.usage.get_today()
    assert usage == {expected.id: {"prompt": 3, "completion": 2, "total": 5}}, usage
    (client,) = world.chats
    assert client.close_count == 1 and client._closed
    (embedding,) = world.embeddings
    assert embedding.close_count == 1
    assert not world.embed_http[0].is_closed, "borrowed embedding HTTP closed by runner"
    assert world.embed_requests == []
    assert get_registry().get(runner.agent_id) is None
    assert world.registry.view().active_model_id == main
    assert world.registry.scheduled_calls == 0
    assert world.settings_path.read_bytes() == world.document
    return request


# ---------------------------------------------------------------------------
# A. Profile matrix directly through the injected factory interface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ORDINARY, ids=[p.name for p in ORDINARY])
def test_factory_explicit_b_without_main_runs_pinned_b(
    make_world: Callable[..., _World], profile: RuntimeProfile,
) -> None:
    """Task 4.1: an explicit selector needs no active Main; Main stays unset."""
    world = make_world(select=False)

    runner = world.factory(model=MODEL_B.id, runtime_profile=profile, label=f"matrix-{profile.name}")
    try:
        assert runner.run("matrix task") == RESULT
    finally:
        runner.close()

    assert world.registry.admits == [MODEL_B.id] and world.registry.errors == []
    _assert_one_closed_run_on(world, MODEL_B, main=None)


@pytest.mark.parametrize("profile", ORDINARY, ids=[p.name for p in ORDINARY])
def test_factory_explicit_b_with_main_a_runs_pinned_b(
    make_world: Callable[..., _World], profile: RuntimeProfile,
) -> None:
    world = make_world(select=True)
    cancel = threading.Event()

    runner = world.factory(
        model=MODEL_B.id, runtime_profile=profile, label=f"matrix-{profile.name}",
        trace_id="r-0a1b2c3d", cancel_event=cancel, max_iterations=3,
    )
    try:
        assert runner.label == f"matrix-{profile.name}"
        assert runner._cancel_event is cancel and not runner.is_cancelled
        assert runner.run("matrix task") == RESULT
    finally:
        runner.close()

    assert world.registry.admits == [MODEL_B.id] and world.registry.errors == []
    _assert_one_closed_run_on(world, MODEL_B)


# ---------------------------------------------------------------------------
# B. Actual ordinary spawn path: real executor shim + real supervisor
# ---------------------------------------------------------------------------

def test_spawn_explicit_b_without_main_completes_on_its_own_pin(
    make_world: Callable[..., _World],
) -> None:
    """Task 4.1: real spawn path admits an explicit selector with no Main selected."""
    world = make_world(select=False)
    finished = threading.Event()

    spawned = world.executor._exec_spawn_agent(
        {"task": "do work", "model": MODEL_B.id}, caller_depth=0, trace_id=TURN,
        options=SupervisionOptions(finish_cb=lambda _tag: finished.set()),
    )
    assert spawned["success"] is True and read_execution(spawned) == "executed", spawned
    assert finished.wait(_BOUND), "finish_cb never fired"
    assert world.executor._supervisor.drain_owned_children(TURN, cancel=False, timeout=_BOUND) == []

    (call,) = world.factory.calls
    assert call["model"] == MODEL_B.id and call["profile"] is RuntimeProfile.ON_DEMAND_SUBAGENT
    assert world.executor._supervisor.unaccounted_children(TURN) == []
    assert world.registry.admits == [MODEL_B.id] and world.registry.errors == []
    _assert_one_closed_run_on(world, MODEL_B, main=None)


def test_spawn_explicit_b_with_main_a_completes_through_supervisor(
    make_world: Callable[..., _World],
) -> None:
    world = make_world(select=True)
    seen: list[dict] = []
    callback_errors: list[BaseException] = []
    finished = threading.Event()

    def main_state() -> tuple[str | None, bool]:
        return (world.registry.view().active_model_id,
                world.settings_path.read_bytes() == world.document)

    def result_log(**fields: object) -> None:
        try:
            seen.append({"cb": "result_log", **fields, "main": main_state()})
        except BaseException as exc:  # noqa: BLE001 - surfaced in the parent below
            callback_errors.append(exc)

    def finish(tag: str) -> None:
        try:
            seen.append({"cb": "finish", "tag": tag, "main": main_state(),
                         "chat_closed": [c._closed for c in world.chats]})
        except BaseException as exc:  # noqa: BLE001 - surfaced in the parent below
            callback_errors.append(exc)
        finally:
            finished.set()

    spawned = world.executor._exec_spawn_agent(
        {"task": "do work", "model": MODEL_B.id}, caller_depth=0, trace_id=TURN,
        options=SupervisionOptions(result_log_cb=result_log, finish_cb=finish),
    )
    assert spawned["success"] is True and read_execution(spawned) == "executed", spawned
    agent_id = spawned["agent_id"]
    assert f"model: {MODEL_B.id}" in spawned["output"]
    assert finished.wait(_BOUND), "finish_cb never fired"

    fetched = world.executor._exec_get_agent_result(
        {"agent_id": agent_id, "timeout": int(_BOUND)}, caller_tag=f"main {TURN}",
    )
    assert fetched["status"] == "done" and fetched["result"] == RESULT, fetched
    assert world.executor._supervisor.drain_owned_children(
        TURN, cancel=False, timeout=_BOUND) == []
    assert callback_errors == []

    (call,) = world.factory.calls
    assert call["model"] == MODEL_B.id and call["label"] == "on-demand"
    assert call["profile"] is RuntimeProfile.ON_DEMAND_SUBAGENT
    assert world.registry.admits == [MODEL_B.id] and world.registry.errors == []
    assert [s["cb"] for s in seen] == ["result_log", "finish"], seen
    logged, done = seen
    assert logged["model"] == MODEL_B.id and logged["success"] is True
    assert logged["result"] == RESULT and logged["tag"] == "on-demand"
    assert logged["main"] == (MODEL_A.id, True)
    assert done["tag"] == "on-demand" and done["main"] == (MODEL_A.id, True)
    assert done["chat_closed"] == [True], "runner not closed before finish_cb"
    (runner,) = world.factory.runners
    assert runner.agent_id == agent_id
    _assert_one_closed_run_on(world, MODEL_B)


def test_spawn_padded_connection_selector_is_rejected_without_normalizing(
    make_world: Callable[..., _World],
) -> None:
    world = make_world(select=True)
    padded = f" {MODEL_B.id} "
    active_before = len(get_registry().list_active())

    result = world.executor._exec_spawn_agent(
        {"task": "do work", "model": padded}, caller_depth=0, trace_id=TURN,
    )

    assert result["error"] == f"spawn_agent: {ModelRegistryError('unknown_connection')}"
    assert result["success"] is False and read_execution(result) == "refused"
    (call,) = world.factory.calls
    assert call["model"] == padded
    assert world.registry.admits == [padded]
    assert world.registry.errors == ["unknown_connection"]
    _assert_nothing_allocated(world, active_before)
    assert world.registry.view().active_model_id == MODEL_A.id


# ---------------------------------------------------------------------------
# C. Actual PlanExecutor: factory model is always None; step.args stay opaque
# ---------------------------------------------------------------------------

def test_plan_step_without_main_fails_step_before_allocation(
    make_world: Callable[..., _World],
) -> None:
    world = make_world(select=False)
    args = dict(OPAQUE_ARGS)
    plan = ExecutionPlan(description="matrix plan",
                         steps=[PlanStep(id="a", tool="shell", args=args)])
    active_before = len(get_registry().list_active())

    result = PlanExecutor(max_concurrent=1).execute(plan, world.parent_ctx())

    error = "Failed to create sub-agent: model registry no active selection"
    assert result["success"] is False
    step = result["results"]["a"]
    assert step["success"] is False and step["error"] == error
    assert "diagnosis" not in step
    assert result["errors"] == [f"Step 'a' failed: {error}"]
    (call,) = world.factory.calls
    assert call["model"] is None
    assert call["profile"] is RuntimeProfile.PLAN_STEP_AGENT
    assert world.registry.admits == [None]
    assert world.registry.errors == ["no_active_selection"]
    assert plan.steps[0].args is args and args == OPAQUE_ARGS
    _assert_nothing_allocated(world, active_before)


def test_plan_step_inherits_main_a_with_opaque_model_args(
    make_world: Callable[..., _World],
) -> None:
    world = make_world(select=True)
    plan = ExecutionPlan(description="matrix plan",
                         steps=[PlanStep(id="a", tool="shell", args=dict(OPAQUE_ARGS))])
    progress: list[tuple[str, str | None]] = []

    result = PlanExecutor(max_concurrent=1).execute(
        plan, world.parent_ctx(),
        progress_cb=lambda msg: progress.append((msg, world.registry.view().active_model_id)),
    )

    assert result["success"] is True, result
    step = result["results"]["a"]
    assert step["success"] is True and step["output"] == RESULT
    assert ("Step 'a' completed successfully", MODEL_A.id) in progress
    (call,) = world.factory.calls
    assert call["model"] is None and call["label"] == "plan-a"
    assert call["profile"] is RuntimeProfile.PLAN_STEP_AGENT
    assert world.registry.admits == [None] and world.registry.errors == []
    request = _assert_one_closed_run_on(world, MODEL_A)
    arguments = f"Arguments: {json.dumps(OPAQUE_ARGS, ensure_ascii=False)}"
    messages = json.loads(request.content)["messages"]
    assert any(arguments in str(m.get("content")) for m in messages), messages
    assert plan.steps[0].args == OPAQUE_ARGS


# ---------------------------------------------------------------------------
# D. Context monitoring: a child run never publishes to the Main monitor
# ---------------------------------------------------------------------------

def test_child_run_publishes_only_to_its_own_monitor_and_leaves_main_snapshot_unchanged(
    make_world: Callable[..., _World], tmp_path: Path,
) -> None:
    """Spec ``context-monitoring``: sub-agent turns never replace the Main snapshot.

    A real Main ``AgentController`` owns the monitor that ``main.py`` also injects
    into the shared executor. A real sub-agent runs through the factory, runtime
    and ``react_loop`` on a mock transport; its turns must land on the runner's
    own monitor while the Main sentinel snapshot stays the same object with
    every field unchanged.
    """
    world = make_world(select=False)
    sentinel = ContextSnapshot(
        system_prompt_tokens=11111, chat_history_tokens=22222, tool_defs_tokens=33333,
        tool_defs_by_server={"sentinel-server": 33333}, compaction_threshold=44444,
        is_live=True, turn=77, model_id="sentinel/main-model",
    )
    main_monitor = ContextMonitor()
    main_monitor.publish(sentinel)
    world.executor._context_monitor = main_monitor
    main = agent_controller.AgentController(
        AGENT,
        ExecutorPaths(tmp_dir=str(tmp_path / "main-tmp"), downloads_dir=str(tmp_path / "main-dl"),
                      workspace_dir=str(tmp_path / "main-ws")),
        llm=None, tool_index=_NullToolIndex(), memory=MemoryStore(str(tmp_path / "main-memory.json")),
        builtin_executor=world.executor, context_monitor=main_monitor,
    )

    runner = world.factory(
        model=MODEL_B.id, runtime_profile=RuntimeProfile.ON_DEMAND_SUBAGENT, label="monitor-isolation",
    )
    try:
        assert runner.run("monitor task") == RESULT
        child_monitor = runner._agent.context_monitor
        child = child_monitor.read()
    finally:
        runner.close()

    # The child really ran and published to its own monitor.
    assert len(world.chat_requests) == 1
    assert child_monitor is not main.context_monitor and child_monitor is not main_monitor
    assert child is not None and child is not sentinel
    assert child.model_id == MODEL_B.id and child.compaction_threshold == AGENT.compaction_threshold_tokens
    assert child.turn != sentinel.turn and child.system_prompt_tokens != sentinel.system_prompt_tokens
    assert child.is_live is False and child.tool_defs_by_server != sentinel.tool_defs_by_server
    # The Main monitor still serves the very same sentinel, field by field.
    assert main.context_monitor is main_monitor and main_monitor.read() is sentinel
    after = main_monitor.read()
    assert (after.system_prompt_tokens, after.chat_history_tokens, after.tool_defs_tokens) == (11111, 22222, 33333)
    assert after.tool_defs_by_server == {"sentinel-server": 33333} and after.compaction_threshold == 44444
    assert (after.is_live, after.turn, after.model_id) == (True, 77, "sentinel/main-model")
    _assert_one_closed_run_on(world, MODEL_B, main=None)
