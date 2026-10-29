"""Wave 2A contract tests: controller-owned, run-local client admission.

``AgentController(llm=None | client, run_client_factory=...)`` obtains a pinned client
per run from an injected factory. Admission is performed by a real ``ModelRegistry``
inside the factory (never by the controller). Chat traffic uses real ``LLMClient``
objects on ``httpx.MockTransport`` with synthetic credentials. Only
``agent_controller.react_loop`` (and, for one failure case, context assembly) is
patched. Every new-API use happens inside test bodies so collection works while the
API is absent.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

import agent_controller
import react_loop as react_loop_module
from agent_controller import AgentController
from builtin_executor import PendingConfirmations
from call_attempt_state import CallAttemptState
from checkpoint_store import CheckpointStore
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from confirmation import GrantLifetime
from embedding_client import EmbeddingClient
from exceptions import ConfigError
from llm_client import LLMClient
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    CatalogView,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings

TRACE = "r-0a1b2c3d"
STOPPED = "⚠️ Task stopped by operator."
AGENT = AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0)


def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            context, None, 2048, False, True, None,
            (("context_tokens", "provider"), ("vision", "provider"),
             ("output_tokens", "provider"), ("tool_calling", "provider")),
        ),
    )


MODEL_A = _record("pa", "up/a", 50_000)
MODEL_B = _record("pb", "up/b", 70_000)
URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during admission")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


class _TrackedLLM(LLMClient):
    """Real LLMClient that counts close() calls and can fail them."""

    close_count = 0
    close_error: Exception | None = None

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error
        super().close()


class _TrackedEmbedding(EmbeddingClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _Supervisor:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register_active_turn(self, turn_id):
        self.registered.append(turn_id)

    def unregister_active_turn(self, turn_id):
        self.unregistered.append(turn_id)

    def unaccounted_children(self, turn_id):
        return []

    def draining_children(self, turn_id):
        return []

    def drain_owned_children(self, turn_id, cancel=False):
        return []

    def mark_result_retrieved(self, *_a):
        pass


class _Executor:
    scheduled = False
    path_policy = None
    conversation_id = "conv"

    def __init__(self) -> None:
        self.coordinator_log: list[object] = []
        self._pending_confirmations = PendingConfirmations()
        self._supervisor = _Supervisor()
        self._current_prompt_id = None
        self._coordinator = None

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_coordinator" and "coordinator_log" in self.__dict__:
            self.coordinator_log.append(value)
        object.__setattr__(self, name, value)

    def is_builtin(self, name: str) -> bool:
        return True

    def execute(self, tool_name, args, **_kwargs):  # pragma: no cover - loop is stubbed
        raise AssertionError("tools must not run")


class _GraphWriter:
    """Recording stand-in for the graph writer; any accessed method call is counted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args, kwargs))

        return record


class _NullToolIndex:
    def search(self, *_a, **_k):
        return []

    def all_tools(self):
        return []


class _Memory:
    def record_event(self, *_a, **_k):
        pass

    def as_prompt_text(self, *_a, **_k):
        return ""


class _Loop:
    """Stand-in for agent_controller.react_loop recording the context it receives."""

    def __init__(self, error: Exception | None = None, on_call=None) -> None:
        self.error = error
        self.on_call = on_call
        self.calls: list[dict] = []

    def __call__(self, ctx, user_goal, progress_callback=None, images=None, **kwargs):
        self.calls.append({
            "ctx": ctx, "llm": ctx.llm, "event": ctx.cancel_event,
            "trace": ctx.llm._trace_id,
        })
        if self.on_call is not None:
            self.on_call(ctx)
        if self.error is not None:
            raise self.error
        return "loop-result"


class _Factory:
    """Injected run_client_factory: admits through the real registry, outside the controller."""

    def __init__(self, world: "_World", *, model_id: str | None = None,
                 entered: threading.Event | None = None,
                 release: threading.Event | None = None,
                 fail: Exception | None = None, close_error: Exception | None = None,
                 on_event=None) -> None:
        self.world = world
        self.model_id = model_id
        self.entered = entered
        self.release = release
        self.fail = fail
        self.close_error = close_error
        self.on_event = on_event
        self.events: list[threading.Event] = []
        self.clients: list[_TrackedLLM] = []

    def __call__(self, event: threading.Event) -> LLMClient:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(5), "factory was never released"
        if self.fail is not None:
            raise self.fail
        run_model = self.world.registry.admit(self.model_id)
        client = self.world.make_client(
            run_model, event=event, embedding=self.world.shared_embedding
        )
        client.close_error = self.close_error
        self.clients.append(client)
        return client


class _World:
    def __init__(self, tmp_path: Path, stack: ExitStack, *, history: bool = True) -> None:
        self.tmp_path = tmp_path
        self.stack = stack
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        self.settings_path = tmp_path / "models-config.toml"
        settings = ModelSettings(self.settings_path)
        settings.write(
            CatalogDocument(
                1, 2, None,
                tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
                (MODEL_A, MODEL_B), (),
            )
        )
        self.registry = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
        self.registry.select(MODEL_A.id)
        self.requests: list[httpx.Request] = []
        self.clients: list[_TrackedLLM] = []
        self.short_term = ShortTermMemory(max_turns=80)
        if history:
            self.short_term.add("user", "hello")
            self.short_term.add("assistant", "hi")
        self.working = WorkingMemory()
        self.working.start_task("previous goal")
        self.executor = _Executor()
        self.store = CheckpointStore(str(tmp_path / "ckpt"))
        self.graph_writer = _GraphWriter()
        self.embed_requests: list[httpx.Request] = []
        self.shared_embedding: EmbeddingClient | None = None
        self.controller: AgentController | None = None

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return httpx.Response(200, json=body)

    def make_client(
        self, run_model, *, event: threading.Event | None = None,
        embedding: EmbeddingClient | None = None,
    ) -> _TrackedLLM:
        client = _TrackedLLM(
            run_model, agent_config=AGENT, http_transport=httpx.MockTransport(self._chat),
            cancel_event=event, embedding_client=embedding,
        )
        self.clients.append(client)
        # Register teardown immediately; bypass instrumented close so failures can't leak.
        self.stack.callback(lambda c=client: LLMClient.close(c))
        return client

    def _embed(self, request: httpx.Request) -> httpx.Response:
        self.embed_requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.5]}]})

    def make_embedding(self) -> _TrackedEmbedding:
        """Shared real embedding client; the injected HTTP client stays test-owned."""
        http_client = httpx.Client(transport=httpx.MockTransport(self._embed))
        self.stack.callback(http_client.close)
        embedding = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key"),
            {}, http_client=http_client,
        )
        assert embedding.available
        return embedding

    def controller_with(self, **kwargs: object) -> AgentController:
        controller = AgentController(
            AGENT,
            ExecutorPaths(
                tmp_dir=str(self.tmp_path / "tmp"), downloads_dir=str(self.tmp_path / "dl"),
                workspace_dir=str(self.tmp_path / "ws"),
            ),
            tool_index=_NullToolIndex(),
            memory=_Memory(),
            short_term=self.short_term,
            working=self.working,
            builtin_executor=self.executor,
            checkpoint_store=self.store,
            **kwargs,
        )
        controller._graph_memory_writer = self.graph_writer
        self.controller = controller
        return controller

    def save_checkpoint(self, trace: str, model_id: str) -> Path:
        ckpt = {
            "format_version": 2, "trace_id": trace, "user_goal": "resume goal",
            "messages": [{"role": "user", "content": "resume goal"}],
            "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
            "model_id": model_id, "attempt_state": CallAttemptState().to_dict(),
            "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
            "created_at": "2026-08-21T14:30:00Z",
        }
        owner = self.store.acquire_owner(trace)
        assert owner is not None
        try:
            assert self.store.save(trace, ckpt, owner=owner) is True
        finally:
            owner.release()
        path = Path(self.store._checkpoint_dir) / f"{trace}.json"
        assert path.is_file()
        return path

    def snapshot(self) -> dict:
        ledger = self.controller._confirmation.grant_ledger
        return {
            "short": json.dumps(self.short_term.to_dict(), sort_keys=True),
            "working": json.dumps(self.working.to_dict(), sort_keys=True),
            "grant": ledger.check("file_write", "/tmp"),
            "graph": (
                self.controller._graph_memory_writer is self.graph_writer,
                list(self.graph_writer.calls),
            ),
            "settings": self.settings_path.read_bytes(),
            "ckpt": {p.name: p.read_bytes() for p in sorted(Path(self.store._checkpoint_dir).glob("*"))},
            "armed": self.controller.selection_guard_armed,
            "registered": list(self.executor._supervisor.registered),
        }

    def assert_owner_free(self, trace: str) -> None:
        owner = self.store.acquire_owner(trace)
        assert owner is not None, "checkpoint owner lease leaked"
        owner.release()

    def assert_no_events_registered(self) -> None:
        assert self.controller._cancel_registry._events == set(), "run-private event leaked"

    def prime_grants(self) -> None:
        self.controller._confirmation.grant_ledger.add("file_write", "/tmp", GrantLifetime.SESSION)


@pytest.fixture
def make_world(tmp_path: Path):
    with ExitStack() as stack:
        counter = iter(range(100))

        def build(**kwargs: object) -> _World:
            root = tmp_path / f"w{next(counter)}"
            root.mkdir()
            return _World(root, stack, **kwargs)

        yield build


def _run(world: _World, **kwargs: object):
    """Run once; return (result, exception) without assuming how errors surface."""
    kwargs.setdefault("trace_id", TRACE)
    try:
        return world.controller.run("q", **kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch) -> _Loop:
    stub = _Loop()
    monkeypatch.setattr(agent_controller, "react_loop", stub)
    return stub


# ---------------------------------------------------------------------------
# Construction and selection precedence
# ---------------------------------------------------------------------------

def test_nullable_construction_performs_no_admission(make_world) -> None:
    w = make_world()
    factory = _Factory(w)
    controller = w.controller_with(llm=None, run_client_factory=factory)
    assert controller.llm is None
    assert factory.events == [] and w.clients == []
    w2 = make_world()
    assert w2.controller_with(llm=None).llm is None


def test_missing_every_client_source_is_an_actionable_config_error(make_world, loop) -> None:
    w = make_world()
    w.controller_with(llm=None)
    result, exc = _run(w)
    assert isinstance(exc, ConfigError), (result, exc)
    assert str(exc).strip()
    assert loop.calls == []
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_factory_is_called_once_with_run_private_event_and_real_pin(make_world, loop) -> None:
    w = make_world()
    embedding = w.shared_embedding = w.make_embedding()
    factory = _Factory(w)
    controller = w.controller_with(llm=None, run_client_factory=factory)
    result, exc = _run(w)
    assert exc is None and result == "loop-result"
    assert len(factory.events) == 1 and len(factory.clients) == 1
    event, client = factory.events[0], factory.clients[0]
    assert event is not controller._cancel_event
    seen = loop.calls[0]
    assert seen["llm"] is client and seen["event"] is event
    assert client._cancel_event is event
    assert client._embedding_client is embedding
    assert client.llm_cfg["id"] == MODEL_A.id
    assert seen["ctx"].owns_cancel_event is False
    assert controller.llm is None
    assert client.close_count == 1
    assert embedding.close_count == 0
    assert embedding.embed("probe") == [0.5] and len(w.embed_requests) == 1
    assert w.requests == []
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_explicit_admitted_client_bypasses_factory_and_stays_open(make_world, loop) -> None:
    w = make_world()
    factory = _Factory(w)
    injected = w.make_client(w.registry.admit(MODEL_A.id))
    explicit = w.make_client(w.registry.admit(MODEL_B.id))
    w.controller_with(llm=injected, run_client_factory=factory)
    result, exc = _run(w, admitted_llm=explicit)
    assert exc is None and result == "loop-result"
    assert factory.events == []
    assert loop.calls[0]["llm"] is explicit
    assert explicit.close_count == 0 and injected.close_count == 0


def test_injected_llm_is_the_caller_owned_fallback(make_world, loop) -> None:
    w = make_world()
    injected = w.make_client(w.registry.admit(MODEL_A.id))
    controller = w.controller_with(llm=injected)
    result, exc = _run(w)
    assert exc is None and result == "loop-result"
    assert loop.calls[0]["llm"] is injected
    assert controller.llm is injected
    assert injected.close_count == 0


def test_factory_client_beats_injected_llm_and_self_llm_never_swaps(make_world, monkeypatch) -> None:
    w = make_world()
    injected = w.make_client(w.registry.admit(MODEL_A.id))
    factory = _Factory(w, model_id=MODEL_B.id)
    during: list[object] = []
    stub = _Loop(on_call=lambda ctx: during.append(w.controller.llm))
    monkeypatch.setattr(agent_controller, "react_loop", stub)
    controller = w.controller_with(llm=injected, run_client_factory=factory)
    result, exc = _run(w)
    assert exc is None and result == "loop-result"
    assert stub.calls[0]["llm"] is factory.clients[0] is not injected
    assert during == [injected] and controller.llm is injected
    assert injected.close_count == 0 and factory.clients[0].close_count == 1


# ---------------------------------------------------------------------------
# Lifecycle cleanup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("history", [True, False], ids=["guarded", "unguarded"])
@pytest.mark.parametrize("fails", [False, True], ids=["loop-ok", "loop-raises"])
def test_trace_restored_and_only_owned_client_is_closed(
    make_world, monkeypatch, fails, history
) -> None:
    w = make_world(history=history)
    loop_error = RuntimeError("loop boom") if fails else None
    stub = _Loop(error=loop_error)
    monkeypatch.setattr(agent_controller, "react_loop", stub)
    caller = w.make_client(w.registry.admit(MODEL_A.id))
    caller.set_trace_id("r-previous1")
    factory = _Factory(w)
    w.controller_with(llm=None, run_client_factory=factory)

    for kwargs in ({"admitted_llm": caller}, {}):
        result, exc = _run(w, **kwargs)
        if fails:
            assert exc is loop_error
        else:
            assert exc is None and result == "loop-result"
    assert [c["trace"] for c in stub.calls] == [TRACE, TRACE]
    assert caller._trace_id == "r-previous1" and caller.close_count == 0
    assert stub.calls[0]["llm"] is caller and stub.calls[1]["llm"] is factory.clients[0]
    assert len(factory.clients) == 1 and factory.clients[0].close_count == 1
    assert factory.clients[0]._trace_id == ""
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_close_failure_of_owned_client_still_releases_lease_and_event(make_world, loop) -> None:
    w = make_world()
    factory = _Factory(w, close_error=RuntimeError("close boom"))
    w.controller_with(llm=None, run_client_factory=factory)
    _run(w)
    assert factory.clients[0].close_count == 1
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


@pytest.mark.parametrize("injected", [True, False], ids=["injected-error", "real-registry-error"])
def test_factory_admission_failure_preserves_protected_state(make_world, loop, injected) -> None:
    w = make_world()
    expected = ModelRegistryError("model_not_found") if injected else None
    factory = _Factory(w, model_id="pa/not-in-catalog", fail=expected)
    w.controller_with(llm=None, run_client_factory=factory)
    w.prime_grants()
    path = w.save_checkpoint(TRACE, MODEL_A.id)
    saved = path.read_bytes()
    before = w.snapshot()
    assert before["ckpt"][path.name] == saved
    result, exc = _run(w, resume_from=TRACE, trace_id=None)
    assert result is None
    assert isinstance(exc, ModelRegistryError) and exc.code == "model_not_found"
    if injected:
        assert exc is expected
    assert len(factory.events) == 1 and loop.calls == []
    assert w.clients == []
    assert path.read_bytes() == saved
    assert w.snapshot() == before
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_context_build_failure_closes_owned_client_and_releases(make_world, monkeypatch, loop) -> None:
    w = make_world()
    factory = _Factory(w)
    w.controller_with(llm=None, run_client_factory=factory)

    boom_error = RuntimeError("context boom")

    def boom(*_a, **_k):
        raise boom_error

    monkeypatch.setattr(agent_controller.AgentRuntime, "build_react_context", staticmethod(boom))
    # Default restored-history world is guarded: nothing is activated before the loop,
    # so the whole protected snapshot (incl. executor registrations) must be unchanged.
    before = w.snapshot()
    _result, exc = _run(w)
    assert exc is boom_error
    assert loop.calls == []
    assert factory.clients[0].close_count == 1
    assert w.snapshot() == before
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_rejected_resume_closes_owned_client_and_keeps_checkpoint(make_world, loop) -> None:
    w = make_world()
    factory = _Factory(w)  # admits the selected model A
    w.controller_with(llm=None, run_client_factory=factory)
    w.save_checkpoint("r-resume001", MODEL_B.id)
    before = w.snapshot()
    result, exc = _run(w, resume_from="r-resume001", trace_id=None)
    assert exc is None and "Cannot resume" in result
    assert len(factory.events) == 1 and loop.calls == []
    assert factory.clients[0].close_count == 1
    assert w.snapshot() == before
    w.assert_owner_free("r-resume001")
    w.assert_no_events_registered()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("history", [True, False])
def test_cancel_while_factory_is_blocked_survives_admission(make_world, loop, history) -> None:
    w = make_world(history=history)
    entered, release = threading.Event(), threading.Event()
    factory = _Factory(w, entered=entered, release=release)
    w.controller_with(llm=None, run_client_factory=factory)
    w.prime_grants()
    before = w.snapshot()
    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.update(zip(("result", "exc"), _run(w))), daemon=True
    )
    worker.start()
    try:
        assert entered.wait(5), "factory never started"
        w.controller.cancel()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert outcome["exc"] is None, outcome
    assert outcome["result"] == STOPPED
    assert factory.events[0].is_set()
    assert loop.calls == []
    assert w.requests == []
    assert [v for v in w.executor.coordinator_log if v is not None] == []
    assert w.snapshot() == before
    assert factory.clients[0].close_count == 1
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_run_private_event_is_registered_before_factory(make_world, loop) -> None:
    w = make_world()
    observed: list[bool] = []

    def cancel_from_inside(event: threading.Event) -> None:
        w.controller.cancel()
        observed.append(event.is_set())

    factory = _Factory(w, on_event=cancel_from_inside)
    w.controller_with(llm=None, run_client_factory=factory)
    result, exc = _run(w)
    assert exc is None and result == STOPPED
    assert observed == [True]
    assert loop.calls == []
    assert factory.clients[0].close_count == 1
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_external_event_identity_and_state_are_preserved(make_world, loop) -> None:
    external = threading.Event()
    w = make_world()
    factory = _Factory(w)
    controller = w.controller_with(llm=None, run_client_factory=factory, cancel_event=external)
    result, exc = _run(w)
    assert exc is None and result == "loop-result"
    assert factory.events == [external] and factory.events[0] is external
    assert loop.calls[0]["event"] is external and not external.is_set()

    external.set()
    calls_before = len(loop.calls)
    result, exc = _run(w)
    assert exc is None and result == STOPPED
    assert len(loop.calls) == calls_before
    assert external.is_set() and controller._cancel_event is external
    assert all(f is external for f in factory.events)
    assert all(c.close_count == 1 for c in factory.clients)
    w.assert_owner_free(TRACE)


# ---------------------------------------------------------------------------
# Independent controllers
# ---------------------------------------------------------------------------

def test_independent_controllers_keep_distinct_immutable_pins(make_world, monkeypatch) -> None:
    w1, w2 = make_world(), make_world()
    f1, f2 = _Factory(w1), _Factory(w2)
    w2.registry = w1.registry  # shared registry, separate stores/histories
    c1 = w1.controller_with(llm=None, run_client_factory=f1)
    w2.controller_with(llm=None, run_client_factory=f2)
    observed: dict[str, str] = {}

    inner = _Loop(on_call=lambda ctx: observed.update(c2=ctx.llm.llm_cfg["id"]))
    depth: list[int] = []

    def first_run(ctx) -> None:
        w1.registry.select(MODEL_B.id)
        result, exc = _run(w2)
        assert exc is None and result == "loop-result"
        observed["c1_after_reselect"] = ctx.llm.llm_cfg["id"]

    outer = _Loop(on_call=first_run)

    def dispatch(ctx, *a, **k):
        depth.append(1)
        return (outer if len(depth) == 1 else inner)(ctx, *a, **k)

    monkeypatch.setattr(agent_controller, "react_loop", dispatch)
    result, exc = _run(w1)
    assert exc is None and result == "loop-result"
    assert observed["c1_after_reselect"] == MODEL_A.id
    assert observed["c2"] == MODEL_B.id
    assert f1.clients[0] is not f2.clients[0]
    assert c1.llm is None


# ---------------------------------------------------------------------------
# Cancellation during setup on non-factory paths (real react_loop)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["explicit", "fallback"])
def test_setup_cancel_not_cleared_for_(make_world, monkeypatch, source) -> None:
    w = make_world()
    if source == "explicit":
        injected = w.make_client(w.registry.admit(MODEL_B.id))
        chosen = w.make_client(w.registry.admit(MODEL_A.id))
        w.controller_with(llm=injected)
        kwargs: dict = {"admitted_llm": chosen}
    else:
        chosen = injected = w.make_client(w.registry.admit(MODEL_A.id))
        w.controller_with(llm=injected)
        kwargs = {}
    w.prime_grants()
    path = w.save_checkpoint(TRACE, MODEL_A.id)
    assert w.controller.selection_guard_armed
    before = w.snapshot()
    assert before["ckpt"][path.name] == path.read_bytes()

    hook_events: list[threading.Event] = []
    original = agent_controller.AgentRuntime.build_react_context

    def cancel_after_registration(controller, trace_id, *, cancel_event=None):
        ctx = original(controller, trace_id, cancel_event=cancel_event)
        assert cancel_event is not None and cancel_event in controller._cancel_registry._events
        controller.cancel()
        assert cancel_event.is_set()
        hook_events.append(cancel_event)
        return ctx

    monkeypatch.setattr(
        agent_controller.AgentRuntime, "build_react_context", staticmethod(cancel_after_registration)
    )
    prepare_calls: list[tuple] = []

    def guarded_prepare_spy(*args, **kwargs):
        prepare_calls.append(args)
        raise AssertionError("guarded first-request preparation must not start after a cancel")

    monkeypatch.setattr(react_loop_module, "_stage_guarded_first_request", guarded_prepare_spy)

    result, exc = _run(w, resume_from=TRACE, trace_id=None, **kwargs)
    assert len(hook_events) == 1, "setup cancel hook was never reached"
    assert exc is None and result == STOPPED
    assert prepare_calls == []
    assert w.requests == []
    assert [v for v in w.executor.coordinator_log if v is not None] == []
    assert w.snapshot() == before
    assert path.read_bytes() == before["ckpt"][path.name]
    assert chosen.close_count == 0 and injected.close_count == 0
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


# ---------------------------------------------------------------------------
# RunClientBinding: pin-aware presentation carried with the run client (F2)
#
# ``agent_controller.RunClientBinding`` does not exist yet; it is imported
# inside each test body so the legacy cases above still collect and run.
# Model identity is compared against the registry's canonical records, never
# against the hand-built MODEL_A/MODEL_B fixtures (provenance order differs).
# ---------------------------------------------------------------------------

def _canonical(w: _World, model_id: str) -> ModelRecord:
    """Return the registry's own current record for *model_id*."""
    return {model.id: model for model in w.registry.view().models}[model_id]


def _binding_factory(
    w: _World,
    binding_cls: type,
    *,
    view_log: list[str],
    clients: list[_TrackedLLM],
    pins: list[RunModel],
    view_error: Exception | None = None,
    embedding: EmbeddingClient | None = None,
) -> Callable[[threading.Event], object]:
    """Return a run_client_factory admitting once and returning a binding.

    Mirrors the ruled main factory: one ``registry.admit()``, one real client
    for that pin, and a view callback closing over the pin that calls
    ``registry.view(pinned=pin)`` (or raises *view_error*).
    """

    def factory(event: threading.Event) -> object:
        """Admit one pin, build its tracked client and bind the pin's view."""
        pin = w.registry.admit()
        client = w.make_client(pin, event=event, embedding=embedding)
        pins.append(pin)
        clients.append(client)

        def catalog_view() -> CatalogView:
            """Present the current catalog with this run's pin."""
            view_log.append("view")
            if view_error is not None:
                raise view_error
            return w.registry.view(pinned=pin)

        return binding_cls(client, catalog_view)

    return factory


def test_binding_request_unwraps_client_and_presents_view_at_context_assembly(
    make_world: Callable[..., _World], loop: _Loop, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binding run uses its real client and the callback view built after assembly."""
    from agent_controller import RunClientBinding

    w = make_world()
    embedding = w.shared_embedding = w.make_embedding()
    order: list[str] = []
    clients: list[_TrackedLLM] = []
    pins: list[RunModel] = []
    factory = _binding_factory(
        w, RunClientBinding, view_log=order, clients=clients, pins=pins, embedding=embedding,
    )
    controller = w.controller_with(llm=None, run_client_factory=factory)
    original_build = agent_controller.AgentRuntime.build_react_context

    def build_spy(
        ctrl: AgentController, trace_id: str, *, cancel_event: threading.Event | None = None,
    ) -> react_loop_module.ReactContext:
        """Change Main after pin A was admitted, then delegate the real assembly."""
        order.append("build")
        w.registry.select(MODEL_B.id)
        return original_build(ctrl, trace_id, cancel_event=cancel_event)

    monkeypatch.setattr(
        agent_controller.AgentRuntime, "build_react_context", staticmethod(build_spy),
    )
    result, exc = _run(w)

    assert exc is None and result == "loop-result"
    assert order == ["build", "view"]
    assert len(clients) == 1 and len(pins) == 1 and w.clients == clients
    client, pin = clients[0], pins[0]
    ctx = loop.calls[0]["ctx"]
    assert loop.calls[0]["llm"] is client and isinstance(client, LLMClient)
    assert pin.model == _canonical(w, MODEL_A.id)
    view = ctx.model_catalog_view
    assert isinstance(view, CatalogView)
    assert view.active_model_id == MODEL_B.id
    assert view.pinned_model == pin.model
    assert client.close_count == 1
    assert embedding.close_count == 0
    assert controller.llm is None
    assert controller._model_catalog_view is None
    assert controller.runtime_deps.model_catalog_view is None
    assert w.requests == []
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_binding_view_failure_closes_client_once_and_skips_loop(
    make_world: Callable[..., _World], loop: _Loop,
) -> None:
    """A view callback failure after the client exists propagates with full cleanup."""
    from agent_controller import RunClientBinding

    w = make_world()
    boom = RuntimeError("view boom")
    order: list[str] = []
    clients: list[_TrackedLLM] = []
    pins: list[RunModel] = []
    factory = _binding_factory(
        w, RunClientBinding, view_log=order, clients=clients, pins=pins, view_error=boom,
    )
    controller = w.controller_with(llm=None, run_client_factory=factory)

    result, exc = _run(w)

    assert result is None and exc is boom
    assert order == ["view"]
    assert loop.calls == []
    assert len(clients) == 1 and clients[0].close_count == 1
    assert controller._model_catalog_view is None
    assert w.requests == []
    w.assert_owner_free(TRACE)
    w.assert_no_events_registered()


def test_summary_unwraps_binding_client_without_presenting_a_view(
    make_world: Callable[..., _World],
) -> None:
    """Compression summarises on the binding's real client; the view is never built."""
    from agent_controller import RunClientBinding

    w = make_world()
    order: list[str] = []
    clients: list[_TrackedLLM] = []
    pins: list[RunModel] = []
    factory = _binding_factory(
        w, RunClientBinding, view_log=order, clients=clients, pins=pins,
        view_error=AssertionError("summary must not build a catalog view"),
    )
    controller = w.controller_with(llm=None, run_client_factory=factory)

    report = controller.compress_context()

    assert report.startswith("✅ Context compressed."), report
    assert order == []
    assert len(clients) == 1 and clients[0].close_count == 1
    assert pins[0].model == _canonical(w, MODEL_A.id)
    assert len(w.requests) == 1
    (summary,) = w.short_term.get_messages()
    assert summary["content"].startswith("[Compressed context summary]\n")
    assert controller.llm is None
    assert controller._model_catalog_view is None
    w.assert_no_events_registered()


def test_view_provider_presents_fresh_unpinned_catalog_outside_runs(
    make_world: Callable[..., _World],
) -> None:
    """``model_catalog_view_provider`` feeds build_system_prompt with no admission."""
    w = make_world()
    tripwire = _Factory(w, fail=AssertionError("prompt building must not admit"))
    provided: list[CatalogView] = []

    def provider() -> CatalogView:
        """Return the registry's current unpinned view, recording each call."""
        view = w.registry.view()
        provided.append(view)
        return view

    controller = w.controller_with(
        llm=None, run_client_factory=tripwire, model_catalog_view_provider=provider,
    )

    first, _ = controller.build_system_prompt()
    w.registry.select(MODEL_B.id)
    second, _ = controller.build_system_prompt()

    assert [view.active_model_id for view in provided] == [MODEL_A.id, MODEL_B.id]
    assert all(view.pinned_model is None for view in provided)
    assert f"Active selection for future runs (status active): {MODEL_A.id}" in first
    assert f"Active selection for future runs (status active): {MODEL_B.id}" in second
    assert "Pinned execution model for this run: none" in second
    assert tripwire.events == [] and w.clients == []
    assert controller._model_catalog_view is None
    assert w.requests == []
