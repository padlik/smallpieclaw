"""Wave 2B1 contract tests: nullable, run-local admission for summary calls.

``reset_task(save=True)`` and ``compress_context()`` obtain their summary client from
an injected ``run_client_factory`` (preferred over the borrowed ``self.llm``) only when
a summary is actually needed. Admission uses a real ``ModelRegistry``; chat traffic uses
real ``LLMClient`` objects on ``httpx.MockTransport`` with synthetic credentials.
Only public methods are exercised, never private helper names.
"""

from __future__ import annotations

import copy
import logging
import threading
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from agent_controller import AgentController
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
from llm_client import LLMClient, LLMError
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
)
from model_settings import ModelSettings

AGENT = AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0)
HOST_A, HOST_B = "a.example.test", "b.example.test"
URLS = {"pa": f"https://{HOST_A}/v1", "pb": f"https://{HOST_B}/v1"}
OPS = ["reset", "compress"]
GOAL = "previous goal"
NO_CHAT_REASON = "No chat model is available; select one with /models."


class _Primary(BaseException):
    """Not an Exception: the summary chat-failure fallback must not swallow it."""


def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            context, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a", 50_000)
MODEL_B = _record("pb", "up/b", 70_000)


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during admission")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


class _TrackedLLM(LLMClient):
    close_count = 0
    close_error: Exception | None = None
    on_close = None

    def close(self) -> None:
        self.close_count += 1
        if self.on_close is not None:
            self.on_close()
        if self.close_error is not None:
            raise self.close_error
        super().close()


class _TrackedEmbedding(EmbeddingClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _Executor:
    conversation_id = "conv-old"


class _Results:
    """Public ResultsMemory stand-in; wired through save_task_outcome and recorded."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.added: list[dict] = []

    def add_result(self, goal: str, summary: str, tools_used: list | None = None) -> str:
        self.log.append("result")
        self.added.append({"goal": goal, "summary": summary})
        return "id"


class _Factory:
    def __init__(self, world: "_World", *, registry: ModelRegistry | None = None,
                 fail: Exception | None = None, close_error: Exception | None = None,
                 on_event=None) -> None:
        self.world = world
        self.registry = registry or world.registry
        self.fail = fail
        self.close_error = close_error
        self.on_event = on_event
        self.events: list[threading.Event] = []
        self.clients: list[_TrackedLLM] = []

    def __call__(self, event: threading.Event) -> LLMClient:
        self.events.append(event)
        self.world.log.append("factory")
        if self.on_event is not None:
            self.on_event(event)
        if self.fail is not None:
            raise self.fail
        client = self.world.make_client(
            self.registry.admit(), event=event, embedding=self.world.embedding
        )
        client.close_error = self.close_error
        self.clients.append(client)
        return client


class _World:
    def __init__(self, tmp_path: Path, stack: ExitStack) -> None:
        self.tmp_path = tmp_path
        self.stack = stack
        self.log: list[str] = []
        self.requests: list[httpx.Request] = []
        self.chat_error: Exception | None = None
        self.on_request = None
        self.close_snapshots: list[dict] = []
        self.registry = self.make_registry("main", select=MODEL_A.id)
        self.short_term = ShortTermMemory(max_turns=80)
        self.working = WorkingMemory()
        self.results = _Results(self.log)
        self.executor = _Executor()
        self.store = CheckpointStore(str(tmp_path / "ckpt"))
        self.embedding = self._make_embedding()
        self.controller: AgentController | None = None
        self.seed()

    def make_registry(self, name: str, *, select: str | None) -> ModelRegistry:
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        settings = ModelSettings(self.tmp_path / f"{name}-models.toml")
        settings.write(
            CatalogDocument(
                1, 2, None,
                tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
                (MODEL_A, MODEL_B),
            )
        )
        registry = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
        if select is not None:
            registry.select(select)
        return registry

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.log.append("request")
        if self.on_request is not None:
            self.on_request()
        if self.chat_error is not None:
            raise self.chat_error
        body = {"choices": [{"message": {"content": f"summary-{request.url.host}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return httpx.Response(200, json=body)

    def make_client(self, run_model, *, event: threading.Event | None = None,
                    embedding: EmbeddingClient | None = None) -> _TrackedLLM:
        client = _TrackedLLM(
            run_model, agent_config=AGENT, http_transport=httpx.MockTransport(self._chat),
            cancel_event=event, embedding_client=embedding,
        )
        def record_close() -> None:
            self.log.append("close")
            self.close_snapshots.append(self.state())

        client.on_close = record_close
        self.stack.callback(lambda c=client: LLMClient.close(c))
        return client

    def _make_embedding(self) -> _TrackedEmbedding:
        http_client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"embedding": [0.5]}]})))
        self.stack.callback(http_client.close)
        embedding = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key"),
            {}, http_client=http_client,
        )
        assert embedding.available
        return embedding

    def borrowed(self, model: ModelRecord = MODEL_B) -> _TrackedLLM:
        run_model = self.make_registry("borrowed", select=model.id).admit()
        return self.make_client(run_model, embedding=self.embedding)

    def controller_with(self, **kwargs: object) -> AgentController:
        kwargs.setdefault("llm", None)
        controller = AgentController(
            AGENT,
            ExecutorPaths(tmp_dir=str(self.tmp_path / "tmp"), downloads_dir=str(self.tmp_path / "dl"),
                          workspace_dir=str(self.tmp_path / "ws")),
            tool_index=object(), memory=object(),
            short_term=self.short_term, working=self.working, results=self.results,
            builtin_executor=self.executor, checkpoint_store=self.store, **kwargs,
        )
        controller._conversation_state_dir = str(self.tmp_path / "state")
        self.controller = controller
        return controller

    def seed(self) -> None:
        self.short_term.clear()
        self.short_term.add("user", "hello")
        self.short_term.add("assistant", "hi")
        self.working.start_task(GOAL)

    def persisted_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for root in (self.tmp_path / "state", self.tmp_path / "ckpt"):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    files[str(path.relative_to(self.tmp_path))] = path.read_bytes()
        return files

    def state(self) -> dict:
        """Independent deep snapshot: later mutation of live objects never alters it."""
        return {
            "results": len(self.results.added),
            "stm": len(self.short_term.get_messages()),
            "working": self.working.has_content(),
            "conv_id": self.executor.conversation_id,
            "saved": sorted(p.name for p in (self.tmp_path / "state").glob("conversations/*")),
            "history": copy.deepcopy(self.short_term.get_messages()),
            "working_dump": copy.deepcopy(self.working.to_dict()),
            "working_text": self.working.to_summary_text(),
            "results_content": copy.deepcopy(self.results.added),
            "files": self.persisted_files(),
        }

    def invoke(self, op: str) -> str:
        if op == "reset":
            return self.controller.reset_task(save=True)
        return self.controller.compress_context()

    def run(self, op: str) -> str:
        self.seed()
        return self.invoke(op)

    def summary(self, op: str) -> str:
        if op == "reset":
            return self.results.added[-1]["summary"]
        return self.short_term.get_messages()[0]["content"]

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def save_checkpoint(self, trace: str = "r-0a1b2c3d") -> dict[str, bytes]:
        ckpt = {
            "format_version": 2, "trace_id": trace, "user_goal": "resume goal",
            "messages": [{"role": "user", "content": "resume goal"}],
            "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
            "model_id": MODEL_A.id, "attempt_state": CallAttemptState().to_dict(),
            "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
            "created_at": "2026-08-21T14:30:00Z",
        }
        owner = self.store.acquire_owner(trace)
        assert owner is not None
        try:
            assert self.store.save(trace, ckpt, owner=owner) is True
        finally:
            owner.release()
        return self.checkpoint_bytes()

    def checkpoint_bytes(self) -> dict[str, bytes]:
        files = {p.name: p.read_bytes() for p in sorted(Path(self.store._checkpoint_dir).glob("*"))}
        assert files, "checkpoint must be actually seeded"
        return files

    def assert_no_events_registered(self) -> None:
        assert self.controller._cancel_registry._events == set(), "summary event leaked"


@pytest.fixture
def world(tmp_path: Path):
    with ExitStack() as stack:
        yield _World(tmp_path, stack)


def _fell_back(op: str, text: str) -> bool:
    return "deterministic" in text if op == "compress" else text.startswith("Goal: ")


def _assert_reason_reported(op: str, msg: str, caplog, reason: str) -> None:
    """Compress reports the reason; reset logs it as an agent_controller warning."""
    if op == "compress":
        assert msg.startswith("⚠️") and reason in msg.split("Reason:", 1)[1]
    else:
        logged = [r.getMessage() for r in caplog.records
                  if r.name == "agent_controller" and r.levelno >= logging.WARNING]
        assert any(reason in m for m in logged), logged


# ---------------------------------------------------------------------------
# Selection, precedence, pinning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", OPS)
def test_selection_is_read_per_operation_and_pinned_within_one(world, op) -> None:
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)
    world.on_request = lambda: world.registry.select(MODEL_B.id)  # mid-flight change

    world.run(op)
    assert world.hosts() == [HOST_A]
    assert factory.clients[0].llm_cfg["id"] == MODEL_A.id
    assert f"summary-{HOST_A}" in world.summary(op)

    world.run(op)
    assert world.hosts() == [HOST_A, HOST_B]
    assert len(factory.events) == 2 and len(factory.clients) == 2
    assert [c.close_count for c in factory.clients] == [1, 1]
    assert controller.llm is None


@pytest.mark.parametrize("op", OPS)
def test_factory_is_preferred_over_borrowed_client_and_leaves_it_untouched(world, op) -> None:
    borrowed = world.borrowed(MODEL_B)
    trace_before = borrowed._trace_id
    factory = _Factory(world)
    controller = world.controller_with(llm=borrowed, run_client_factory=factory)

    world.run(op)
    assert world.hosts() == [HOST_A]
    assert controller.llm is borrowed
    assert borrowed.close_count == 0 and borrowed._trace_id == trace_before
    assert factory.clients[0] is not borrowed and factory.clients[0].close_count == 1
    assert factory.clients[0]._embedding_client is world.embedding
    assert world.embedding.close_count == 0 and world.embedding.embed("probe") == [0.5]


@pytest.mark.parametrize("op", OPS)
def test_borrowed_client_without_factory_is_used_and_never_closed(world, op) -> None:
    borrowed = world.borrowed(MODEL_B)
    controller = world.controller_with(llm=borrowed)

    world.run(op)
    assert world.hosts() == [HOST_B] and f"summary-{HOST_B}" in world.summary(op)
    assert controller.llm is borrowed
    assert borrowed.close_count == 0 and world.embedding.close_count == 0
    assert world.embedding.embed("probe") == [0.5]


# ---------------------------------------------------------------------------
# Unavailability and factory failures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", OPS)
def test_missing_every_client_falls_back_with_actionable_reason(world, caplog, op) -> None:
    world.controller_with()
    with caplog.at_level(logging.WARNING):
        msg = world.run(op)
    assert _fell_back(op, world.summary(op)) and world.requests == []
    if op == "reset":
        assert world.results.added[-1]["summary"] == f"Goal: {GOAL}"
    else:
        assert "NoneType" not in msg and "AttributeError" not in msg
    _assert_reason_reported(op, msg, caplog, NO_CHAT_REASON)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("kind", ["unselected", "llm_error"])
def test_expected_factory_failure_falls_back_never_to_borrowed_client(world, caplog, op, kind) -> None:
    borrowed = world.borrowed(MODEL_B)
    if kind == "unselected":
        empty = world.make_registry("empty", select=None)
        with pytest.raises(ModelRegistryError) as unselected:
            empty.admit()
        reason = str(unselected.value)
        factory = _Factory(world, registry=empty)
    else:
        reason = "provider hiccup"
        factory = _Factory(world, fail=LLMError(reason))
    assert reason
    world.controller_with(llm=borrowed, run_client_factory=factory)

    with caplog.at_level(logging.WARNING):
        msg = world.run(op)
    assert len(factory.events) == 1
    assert world.requests == [] and borrowed.close_count == 0
    assert _fell_back(op, world.summary(op))
    _assert_reason_reported(op, msg, caplog, reason)
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("error", [RuntimeError("admission bug"), TypeError("bad factory")])
def test_unexpected_factory_error_propagates_after_release_without_mutation(world, op, error) -> None:
    borrowed = world.borrowed(MODEL_B)
    factory = _Factory(world, fail=error)
    world.controller_with(llm=borrowed, run_client_factory=factory)

    world.seed()
    before = world.state()
    with pytest.raises(type(error)) as caught:
        world.invoke(op)
    assert caught.value is error
    assert world.requests == [] and world.state() == before
    assert borrowed.close_count == 0
    world.assert_no_events_registered()


# ---------------------------------------------------------------------------
# Ownership and close
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("chat_error", [None, RuntimeError("chat boom")])
def test_owned_client_closed_exactly_once_on_success_and_chat_failure(world, caplog, op, chat_error) -> None:
    world.chat_error = chat_error
    factory = _Factory(world)
    world.controller_with(run_client_factory=factory)

    with caplog.at_level(logging.WARNING):
        msg = world.run(op)
    assert len(world.requests) == 1
    assert factory.clients[0].close_count == 1
    assert _fell_back(op, world.summary(op)) is (chat_error is not None)
    if chat_error is not None:
        _assert_reason_reported(op, msg, caplog, "chat boom")
    assert world.embedding.close_count == 0
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("chat_error", [None, RuntimeError("chat boom")])
def test_unexpected_close_error_propagates_exactly_before_any_mutation(world, op, chat_error) -> None:
    world.chat_error = chat_error
    error = OSError("close boom")
    factory = _Factory(world, close_error=error)
    world.controller_with(run_client_factory=factory)
    world.seed()
    before = world.state()
    assert before["history"] and before["working_dump"]["goal"] == GOAL

    with pytest.raises(OSError) as caught:
        world.invoke(op)

    assert caught.value is error
    assert factory.clients[0].close_count == 1 and len(world.requests) == 1
    assert world.close_snapshots == [before]
    assert world.state() == before
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
def test_close_failure_does_not_replace_propagating_primary_exception(world, caplog, op) -> None:
    primary, error = _Primary("primary"), OSError("close boom")
    world.chat_error = primary
    factory = _Factory(world, close_error=error)
    world.controller_with(run_client_factory=factory)
    world.seed()
    before = world.state()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_Primary) as caught:
            world.invoke(op)

    assert caught.value is primary
    assert factory.clients[0].close_count == 1 and len(world.requests) == 1
    assert world.state() == before
    assert any(r.name == "agent_controller" and r.exc_info and r.exc_info[1] is error
               for r in caplog.records), "secondary close failure must be logged"
    world.assert_no_events_registered()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", OPS)
def test_controller_owned_event_is_registered_for_factory_and_cancel_skips_chat(world, op) -> None:
    seen: dict[str, object] = {}

    def on_event(event: threading.Event) -> None:
        seen["registered"] = event in world.controller._cancel_registry._events
        seen["fresh"] = not event.is_set()
        world.controller.cancel()  # operator cancel while admission is in flight
        seen["reached"] = event.is_set()

    factory = _Factory(world, on_event=on_event)
    controller = world.controller_with(run_client_factory=factory)
    world.run(op)

    event, client = factory.events[0], factory.clients[0]
    assert seen == {"registered": True, "fresh": True, "reached": True}
    assert event is not controller._cancel_event and client._cancel_event is event
    assert world.requests == [] and client.close_count == 1
    assert _fell_back(op, world.summary(op))
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
def test_cancel_during_chat_reaches_owned_client_event_but_in_flight_summary_succeeds(world, op) -> None:
    # LLMClient checks cancellation only before sending and the OpenAI path has no
    # post-response check, so a cancel raised mid-request cannot abort that request.
    seen: dict[str, bool] = {}
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)

    def on_request() -> None:
        event, client = factory.events[0], factory.clients[0]
        seen["registered"] = event in controller._cancel_registry._events
        seen["client_bound"] = client._cancel_event is event
        controller.cancel()
        seen["delivered"] = event.is_set()

    world.on_request = on_request
    world.run(op)

    assert seen == {"registered": True, "client_bound": True, "delivered": True}
    assert len(world.requests) == 1 and len(factory.events) == 1
    assert not _fell_back(op, world.summary(op))
    assert f"summary-{HOST_A}" in world.summary(op)
    assert factory.clients[0].close_count == 1
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
def test_stale_owned_cancellation_is_ignored_and_not_cleared(world, op) -> None:
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)
    controller._cancel_event.set()

    world.run(op)
    assert factory.events[0] is not controller._cancel_event and not factory.events[0].is_set()
    assert controller._cancel_event.is_set()
    assert world.hosts() == [HOST_A] and f"summary-{HOST_A}" in world.summary(op)
    world.assert_no_events_registered()


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("preset", [False, True])
def test_external_event_identity_and_state_are_preserved(world, op, preset) -> None:
    external = threading.Event()
    if preset:
        external.set()
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory, cancel_event=external)

    world.run(op)
    assert factory.events == [external] and factory.clients[0]._cancel_event is external
    assert external.is_set() is preset and controller._cancel_registry is None
    assert (world.requests == []) is preset
    assert _fell_back(op, world.summary(op)) is preset
    assert factory.clients[0].close_count == 1


# ---------------------------------------------------------------------------
# Gates: no admission when no summary is needed
# ---------------------------------------------------------------------------

def _gate_cases(world):
    def reset_nosave():
        world.seed()
        return world.controller.reset_task(save=False)

    def reset_empty():
        world.seed()
        world.working.clear()
        return world.controller.reset_task(save=True)

    def compress_empty():
        world.short_term.clear()
        return world.controller.compress_context()

    def compress_single():
        world.short_term.clear()
        world.short_term.add("user", "only one")
        return world.controller.compress_context()

    def compress_no_stm():
        world.controller.short_term = None
        return world.controller.compress_context()

    return [reset_nosave, reset_empty, compress_empty, compress_single, compress_no_stm]


@pytest.mark.parametrize("index", range(5))
def test_no_admission_when_no_summary_is_needed(world, index) -> None:
    factory = _Factory(world)
    world.controller_with(llm=world.borrowed(), run_client_factory=factory)
    _gate_cases(world)[index]()
    assert factory.events == [] and world.requests == [] and world.log == []


# ---------------------------------------------------------------------------
# Ordering, preserved behaviour
# ---------------------------------------------------------------------------

def test_reset_closes_owned_client_before_persistence_and_keeps_order(world) -> None:
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)
    controller._confirmation.grant_ledger.add("file_write", "/tmp", GrantLifetime.SESSION)
    controller.notify_history_restored(None)
    world.seed()
    assert controller.selection_guard_armed
    before = world.state()

    msg = controller.reset_task(save=True)

    assert world.log == ["factory", "request", "close", "result"]
    at_close = world.close_snapshots[0]
    assert at_close == before
    assert (at_close["results"], at_close["stm"], at_close["working"]) == (0, 2, True)
    assert at_close["conv_id"] == "conv-old" and at_close["saved"] == []
    assert world.results.added == [{"goal": GOAL, "summary": f"summary-{HOST_A}"}]
    assert msg == "✅ Task saved to results memory. Starting fresh context."
    state = world.state()
    assert state["saved"] == ["conv-old.json"] and state["conv_id"] != "conv-old"
    assert state["stm"] == 0 and state["working"] is False
    assert controller._confirmation.grant_ledger.check("file_write", "/tmp") is False
    assert controller.selection_guard_armed is False


def test_reset_fallback_summary_is_first_300_chars_and_works_without_results_store(world) -> None:
    world.chat_error = RuntimeError("chat boom")
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)
    world.working.start_task("g" * 400)
    controller.reset_task(save=True)
    assert world.results.added[-1]["summary"] == ("Goal: " + "g" * 400)[:300]

    world.chat_error = None
    world.log.clear()
    controller.results = None
    world.seed()
    requests_before = len(world.requests)
    msg = controller.reset_task(save=True)
    assert len(world.requests) == requests_before + 1
    assert world.hosts()[-1] == HOST_A
    assert world.log == ["factory", "request", "close"]
    assert msg == "✅ Task saved to results memory. Starting fresh context."


def test_compress_truncates_input_closes_before_mutation_and_keeps_guard(world) -> None:
    factory = _Factory(world)
    controller = world.controller_with(run_client_factory=factory)
    world.seed()
    world.short_term.clear()
    world.short_term.add("user", "x" * 1000)
    world.short_term.add("assistant", "ok")
    controller.notify_history_restored(None)
    assert controller.selection_guard_armed
    before = world.state()

    msg = controller.compress_context()

    body = world.requests[0].content.decode()
    assert "x" * 600 in body and "x" * 601 not in body
    assert world.log == ["factory", "request", "close"]
    assert world.close_snapshots[0] == before and before["stm"] == 2
    assert world.summary("compress") == f"[Compressed context summary]\nsummary-{HOST_A}"
    assert msg.startswith("✅") and controller.selection_guard_armed is True


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("mode", ["summary", "unselected"])
def test_summary_paths_never_touch_checkpoints_or_leases(world, op, mode) -> None:
    registry = world.registry if mode == "summary" else world.make_registry("empty", select=None)
    world.controller_with(run_client_factory=_Factory(world, registry=registry))
    before = world.save_checkpoint()

    world.run(op)

    assert world.checkpoint_bytes() == before
    owner = world.store.acquire_owner("r-0a1b2c3d")
    assert owner is not None, "checkpoint owner lease leaked"
    owner.release()
