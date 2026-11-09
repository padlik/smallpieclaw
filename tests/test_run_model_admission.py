"""Run-model admission and pinning contracts.

Wave 1 section: runtime/runner consume only an admitted RunModel pin.
Admission is performed by a real ``ModelRegistry`` OUTSIDE the runtime. Chat and
embedding traffic goes through real ``LLMClient`` / ``EmbeddingClient`` objects on
``httpx.MockTransport``. Root ``main.py`` integration is out of scope for that
section. New-API construction happens inside test bodies so failures stay per-test.

Task 7.2 root section (end of file): the same contracts through the actual
production root, driven by ``tests/root_startup_harness.py`` — the real root
factory's Main gate and the immutability of root-admitted pins across a later
Main selection and a real catalog refresh.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import agent_controller
import agent_runtime
from call_attempt_state import CallAttemptState
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient, EmbeddingUnavailableError
from llm_client import LLMClient
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
)
from model_settings import ModelCache, ModelSettings
from providers._errors import LLMCancelledError
from react_loop import ReactContext
from sub_agent_registry import get_registry
from tests.root_startup_harness import (
    ACME_HOST,
    DISCOVERED,
    GEM_HOST,
    INVALID_SAVED,
    UNCONFIGURED_ID,
    remembered_for,
    MAIN_ID,
    OTHER_ID,
    SECRETS,
    RootHarness,
)
from token_usage import TokenUsageRegistry

PROFILES = [
    "ON_DEMAND_SUBAGENT",
    "SCHEDULED_AGENT",
    "PLAN_STEP_AGENT",
    "DIAGNOSTIC_AGENT",
]


# ---------------------------------------------------------------------------
# Real registry + real client harness
# ---------------------------------------------------------------------------

def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    return ModelRecord(
        f"{provider}/{upstream}",
        provider,
        upstream,
        f"{upstream} display",
        ModelMetadata(
            context, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a", 5000)
MODEL_B = _record("pb", "up/b", 7000)
URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}


def _provider(name: str, url: str | None = None) -> ProviderConfig:
    return ProviderConfig(name, "openai", f"env:KEY_{name}", url or URLS[name], 9)


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name,
        adapter=config.type,
        base_url=config.base_url,
        request_timeout=config.request_timeout,
        api_key=f"secret-{config.name}",
    )


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during admission")


def _registry(tmp_path: Path, *, select: ModelRecord | None = MODEL_A) -> ModelRegistry:
    providers = {name: _provider(name) for name in ("pa", "pb")}
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(
        CatalogDocument(
            1, 2, None,
            tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (MODEL_A, MODEL_B),
        )
    )
    registry = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
    if select is not None:
        registry.select(select.id)
    return registry


class _TrackedLLM(LLMClient):
    """Real LLMClient that counts close() calls (constructor wrapper only)."""

    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _TrackedEmbedding(EmbeddingClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.chat_requests: list[httpx.Request] = []
        self.embed_requests: list[httpx.Request] = []
        self.chats: list[_TrackedLLM] = []
        self.embeddings: list[_TrackedEmbedding] = []
        self.factory_events: list[threading.Event] = []
        self.usage = TokenUsageRegistry()
        self.chat_transports: list[httpx.MockTransport] = []
        self.external_embed_http: list[httpx.Client] = []
        self.http_clients: list[tuple[str, httpx.Client]] = []
        monkeypatch.setattr(agent_controller, "LLMClient", self._make_llm)

    def track_http_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route clients built without an explicit transport (owned embedding
        clients) to the embedding MockTransport and record every client."""
        real_client = httpx.Client

        def build(*args: object, **kwargs: object) -> httpx.Client:
            kind = "chat"
            if "transport" not in kwargs:
                kind = "owned-embedding"
                kwargs["transport"] = httpx.MockTransport(self._embed)
            client = real_client(*args, **kwargs)
            self.http_clients.append((kind, client))
            return client

        monkeypatch.setattr(httpx, "Client", build)

    def clients(self, kind: str) -> list[httpx.Client]:
        return [c for k, c in self.http_clients if k == kind]

    def cleanup(self) -> None:
        for client in self.external_embed_http:
            client.close()

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.chat_requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    def _embed(self, request: httpx.Request) -> httpx.Response:
        self.embed_requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.5]}]})

    def _make_llm(self, run_model: RunModel, **kwargs: object) -> _TrackedLLM:
        transport = httpx.MockTransport(self._chat)
        self.chat_transports.append(transport)
        client = _TrackedLLM(run_model, http_transport=transport, **kwargs)
        self.chats.append(client)
        return client

    def embedding_factory(self, event: threading.Event) -> EmbeddingClient:
        self.factory_events.append(event)
        http_client = httpx.Client(transport=httpx.MockTransport(self._embed))
        self.external_embed_http.append(http_client)
        client = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key"),
            {},
            http_client=http_client,
            cancel_event=event,
        )
        self.embeddings.append(client)
        return client

    def owned_embedding_factory(self, event: threading.Event) -> EmbeddingClient:
        """Real EmbeddingClient that creates and therefore owns its HTTP client."""
        self.factory_events.append(event)
        client = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key"),
            {},
            cancel_event=event,
        )
        self.embeddings.append(client)
        return client

    def allocated(self) -> int:
        return len(self.chats) + len(self.embeddings) + len(self.factory_events)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    instance = Harness(monkeypatch)
    yield instance
    instance.cleanup()


AGENT_CFG = AgentConfig(
    top_tools=4, ctx_max_tokens=12345, max_retries=1, retry_delay=0.25,
    tool_timeout=17, max_iterations=8, max_subagents=3,
)


def _runtime(harness: Harness, **overrides: object):
    from agent_runtime import AgentRuntime

    kwargs: dict = dict(
        agent_cfg=AGENT_CFG,
        paths=ExecutorPaths(tmp_dir="/tmp/agent", downloads_dir="downloads",
                            workspace_dir="~/Documents", log_file="agent.log",
                            log_backup_count=30),
        tool_index=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
        skill_registry=MagicMock(),
        mcp_manager=None,
        results=MagicMock(),
        usage_registry=harness.usage,
        embedding_factory=harness.embedding_factory,
        scheduled_max_iterations=42,
    )
    kwargs.update(overrides)
    return AgentRuntime(**kwargs)


def _create(runtime, profile: str, **option_kwargs: object):
    from agent_runtime import RuntimeOptions, RuntimeProfile

    return runtime.create(RuntimeProfile[profile], RuntimeOptions(**option_kwargs))


def _send(llm: LLMClient) -> str:
    with llm.bind_attempt(CallAttemptState(max_retries=1, retry_delay=0)):
        return llm.chat([{"role": "user", "content": "hello"}])


# ---------------------------------------------------------------------------
# Admission contract: reject before any allocation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", PROFILES)
def test_unbound_options_rejected_before_any_allocation(
    profile: str, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtin_executor

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduled dispatch allocated before admission check")

    monkeypatch.setattr(builtin_executor, "ScheduledToolDispatch", forbidden)
    runtime = _runtime(harness)
    with pytest.raises(ValueError):
        _create(runtime, profile)
    assert harness.allocated() == 0


@pytest.mark.parametrize(
    "bad_pin",
    ["pa/up/a", {"model": "up/a", "api_key": "k"}, object(), 7],
    ids=["selector-string", "raw-dict", "plain-object", "int"],
)
def test_malformed_run_model_rejected_before_any_allocation(
    bad_pin: object, harness: Harness
) -> None:
    runtime = _runtime(harness)
    with pytest.raises(ValueError):
        _create(runtime, "ON_DEMAND_SUBAGENT", run_model=bad_pin)
    assert harness.allocated() == 0


def test_unsupported_profile_and_bad_dependencies_rejected_before_allocation(
    harness: Harness, tmp_path: Path
) -> None:
    from agent_runtime import RuntimeOptions, RuntimeProfile

    pin = _registry(tmp_path).admit()
    options = RuntimeOptions(run_model=pin)
    with pytest.raises(ValueError):
        _runtime(harness).create("main", options)
    for bad in ({"agent": {}}, "not-config"):
        with pytest.raises(ValueError):
            _runtime(harness, agent_cfg=bad).create(RuntimeProfile.ON_DEMAND_SUBAGENT, options)
    for bad in (None, "not-callable"):
        with pytest.raises(ValueError):
            _runtime(harness, embedding_factory=bad).create(RuntimeProfile.ON_DEMAND_SUBAGENT, options)
    assert harness.allocated() == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"model": "pa/up/a"}, {"max_tokens": 1}, {"temperature": 0.1}, {"top_p": 0.5}],
    ids=["selector", "max_tokens", "temperature", "top_p"],
)
def test_obsolete_generation_and_selector_options_are_rejected_not_ignored(
    kwargs: dict, tmp_path: Path
) -> None:
    from agent_runtime import RuntimeOptions

    with pytest.raises((TypeError, ValueError)):
        RuntimeOptions(run_model=_registry(tmp_path).admit(), **kwargs)


def test_unbound_options_remain_constructible() -> None:
    from agent_runtime import RuntimeOptions

    assert RuntimeOptions().run_model is None


# ---------------------------------------------------------------------------
# Pinning: all profiles, explicit B pin, immutability, exact wire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", PROFILES)
def test_actual_admitted_pin_reaches_exact_provider_on_every_profile(
    profile: str, harness: Harness, tmp_path: Path
) -> None:
    pin = _registry(tmp_path).admit()
    runner = _create(_runtime(harness), profile, run_model=pin)
    try:
        assert runner.model_id == MODEL_A.id
        assert runner._llm._run_model is pin
        assert runner._llm.metadata == pin.model.metadata
        assert _send(runner._llm) == "ok"
    finally:
        runner.close()

    request = harness.chat_requests[0]
    assert str(request.url) == "https://a.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-pa"
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "up/a"


def test_explicit_b_pin_while_main_a_is_active(harness: Harness, tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    pin_b = registry.admit(MODEL_B.id)
    runner = _create(_runtime(harness), "PLAN_STEP_AGENT", run_model=pin_b)
    try:
        assert runner.model_id == MODEL_B.id
        assert runner._llm._run_model is pin_b
        assert runner._llm.metadata == pin_b.model.metadata
        _send(runner._llm)
    finally:
        runner.close()

    request = harness.chat_requests[0]
    assert str(request.url) == "https://b.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-pb"
    assert json.loads(request.content)["model"] == "up/b"
    assert registry.view().active_model_id == MODEL_A.id


def test_selection_change_never_retargets_existing_runners(
    harness: Harness, tmp_path: Path
) -> None:
    registry = _registry(tmp_path)
    runtime = _runtime(harness)
    runner_a = _create(runtime, "ON_DEMAND_SUBAGENT", run_model=registry.admit())
    runner_b = _create(runtime, "DIAGNOSTIC_AGENT", run_model=registry.admit(MODEL_B.id))

    registry.select(MODEL_B.id)
    try:
        _send(runner_a._llm)
        _send(runner_b._llm)
    finally:
        runner_a.close()
        runner_b.close()

    urls = [str(r.url) for r in harness.chat_requests]
    assert urls == [
        "https://a.example.test/v1/chat/completions",
        "https://b.example.test/v1/chat/completions",
    ]
    assert [r.headers["authorization"] for r in harness.chat_requests] == [
        "Bearer secret-pa", "Bearer secret-pb",
    ]
    assert runner_a.model_id == MODEL_A.id
    assert runner_b.model_id == MODEL_B.id


# ---------------------------------------------------------------------------
# Event identity, usage / caller / trace, options policy
# ---------------------------------------------------------------------------

def test_supplied_event_identity_and_preset_state_are_preserved(
    harness: Harness, tmp_path: Path
) -> None:
    event = threading.Event()
    event.set()
    runner = _create(
        _runtime(harness), "SCHEDULED_AGENT",
        run_model=_registry(tmp_path).admit(), cancel_event=event,
    )
    try:
        assert runner._cancel_event is event
        assert runner._agent._cancel_event is event
        assert harness.factory_events == [event]
        assert event.is_set()
        with pytest.raises(LLMCancelledError):
            _send(runner._llm)
        assert harness.chat_requests == []
    finally:
        runner.close()


def test_runner_created_event_is_the_one_given_to_embedding_factory(
    harness: Harness, tmp_path: Path
) -> None:
    runner = _create(_runtime(harness), "ON_DEMAND_SUBAGENT", run_model=_registry(tmp_path).admit())
    try:
        assert isinstance(runner._cancel_event, threading.Event)
        assert harness.factory_events == [runner._cancel_event]
        assert runner._agent._cancel_event is runner._cancel_event
    finally:
        runner.close()


def test_usage_caller_and_trace_attribution_forwarded(harness: Harness, tmp_path: Path) -> None:
    runner = _create(
        _runtime(harness), "PLAN_STEP_AGENT",
        run_model=_registry(tmp_path).admit(), trace_id="r-abc12345",
    )
    try:
        _send(runner._llm)
        assert runner.trace_id == "r-abc12345"
        assert runner._llm._base_caller_tag == runner.agent_id
        today = harness.usage.get_today()
        assert today[MODEL_A.id] == {"prompt": 3, "completion": 2, "total": 5}
        assert "up/a" not in today
    finally:
        runner.close()


def test_runner_run_reaches_controller_boundary_with_forwarded_trace_and_prompt(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _create(
        _runtime(harness), "PLAN_STEP_AGENT",
        run_model=_registry(tmp_path).admit(), trace_id="r-abc12345",
    )
    controller = runner._agent
    seen: list[tuple[str, object, object]] = []

    def record(task: str, prompt_id: object = None, **kwargs: object) -> str:
        seen.append((task, prompt_id, controller._trace_id))
        return "deterministic-result"

    monkeypatch.setattr(controller, "run", record)
    try:
        assert runner.run("do it", prompt_id="p-1") == "deterministic-result"
    finally:
        runner.close()
    assert seen == [("do it", "p-1", "r-abc12345")]
    assert harness.chat_requests == []


@pytest.mark.parametrize(
    ("profile", "option", "expected"),
    [
        ("SCHEDULED_AGENT", None, 42),
        ("SCHEDULED_AGENT", 7, 7),
        ("PLAN_STEP_AGENT", None, 42),
        ("ON_DEMAND_SUBAGENT", 9, 9),
    ],
)
def test_max_iterations_policy_changes_only_that_agent_config_field(
    profile: str, option: int | None, expected: int, harness: Harness, tmp_path: Path
) -> None:
    runner = _create(
        _runtime(harness), profile,
        run_model=_registry(tmp_path).admit(), max_iterations=option,
    )
    try:
        assert runner._agent.max_iterations == expected
        assert runner._agent._agent_cfg == replace(AGENT_CFG, max_iterations=expected)
        assert AGENT_CFG.max_iterations == 8
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Preserved context / prompt / callback / profile-memory permissions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", PROFILES)
def test_context_prompt_callbacks_and_profile_memory_permissions_preserved(
    profile: str, harness: Harness, tmp_path: Path
) -> None:
    from agent_runtime import _ScheduledRunMemory

    base_memory, results, executor = MagicMock(), MagicMock(), MagicMock()
    notify, trace_cb = MagicMock(), MagicMock()
    payload = {"parent_working_summary": "state"}
    runtime = _runtime(harness, base_memory=base_memory, results=results, builtin_executor=executor)
    from agent_runtime import RuntimeOptions, RuntimeProfile

    runner = runtime.create(
        RuntimeProfile[profile],
        RuntimeOptions(
            run_model=_registry(tmp_path).admit(), label="lbl",
            prompt_variant="sub-agent", context_payload=payload,
        ),
        notify_fn=notify, on_tool_trace=trace_cb,
    )
    try:
        assert runner.label == "lbl"
        assert runner._agent._depth == 1
        assert runner.prompt_variant == "sub-agent"
        assert runner._agent._prompt_variant == "sub-agent"
        assert runner._agent._context_payload == payload
        assert runner.notify_fn is notify
        assert runner._agent._on_tool_trace is trace_cb
        if profile == "SCHEDULED_AGENT":
            import builtin_executor

            dispatch = runner._agent.builtin_executor
            assert isinstance(dispatch, builtin_executor.ScheduledToolDispatch)
            assert dispatch._executor is executor
            assert isinstance(runner._agent.memory, _ScheduledRunMemory)
            assert runner._agent.results is None
        else:
            assert runner._agent.memory is base_memory
            assert runner._agent.results is results
            assert runner._agent.builtin_executor is executor
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Embedding ownership and lifecycle
# ---------------------------------------------------------------------------

def test_each_runner_owns_an_independent_embedding_client_used_for_embeddings(
    harness: Harness, tmp_path: Path
) -> None:
    pin = _registry(tmp_path).admit()
    runtime = _runtime(harness)
    first = _create(runtime, "ON_DEMAND_SUBAGENT", run_model=pin)
    second = _create(runtime, "ON_DEMAND_SUBAGENT", run_model=pin)
    try:
        assert len(harness.embeddings) == 2
        assert harness.embeddings[0] is not harness.embeddings[1]
        assert first._llm.embed("hi") == [0.5]
        assert harness.embed_requests[0].headers["authorization"] == "Bearer embed-key"
        assert harness.chat_requests == []
    finally:
        first.close()
        second.close()


def test_missing_embedding_configuration_keeps_unavailable_behavior(
    harness: Harness, tmp_path: Path
) -> None:
    def unavailable(event: threading.Event) -> EmbeddingClient:
        return EmbeddingClient(EmbeddingsConfig(), {}, cancel_event=event)

    runner = _create(
        _runtime(harness, embedding_factory=unavailable), "ON_DEMAND_SUBAGENT",
        run_model=_registry(tmp_path).admit(),
    )
    try:
        with pytest.raises(EmbeddingUnavailableError):
            runner._llm.embed("hi")
        assert harness.chat_requests == []
    finally:
        runner.close()


def test_closing_one_runner_closes_only_its_own_clients(harness: Harness, tmp_path: Path) -> None:
    pin = _registry(tmp_path).admit()
    runtime = _runtime(harness)
    first = _create(runtime, "ON_DEMAND_SUBAGENT", run_model=pin)
    second = _create(runtime, "SCHEDULED_AGENT", run_model=pin)
    try:
        first.close()
        assert (harness.chats[0].close_count, harness.embeddings[0].close_count) == (1, 1)
        assert (harness.chats[1].close_count, harness.embeddings[1].close_count) == (0, 0)
        assert _send(second._llm) == "ok"
        assert second._llm.embed("still-open") == [0.5]
        assert len(harness.chat_requests) == 1
        assert len(harness.embed_requests) == 1
        # Close counters are not transport ownership: injected embedding HTTP
        # clients are caller-owned and stay open until fixture cleanup.
        assert not any(c.is_closed for c in harness.external_embed_http)
    finally:
        second.close()
    assert (harness.chats[1].close_count, harness.embeddings[1].close_count) == (1, 1)


def test_owned_embedding_and_chat_transports_close_per_runner_and_repeat_close_is_safe(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.track_http_clients(monkeypatch)
    pin = _registry(tmp_path).admit()
    runtime = _runtime(harness, embedding_factory=harness.owned_embedding_factory)
    created: list = []
    try:
        created.append(_create(runtime, "ON_DEMAND_SUBAGENT", run_model=pin))
        created.append(_create(runtime, "PLAN_STEP_AGENT", run_model=pin))
        first, second = created
        owned = harness.clients("owned-embedding")
        chat_http = harness.clients("chat")
        assert len(owned) == 2 and len(chat_http) == 2
        assert harness.chat_transports[0] is not harness.chat_transports[1]
        first.close()
        first.close()
        assert owned[0].is_closed and chat_http[0].is_closed
        assert not owned[1].is_closed and not chat_http[1].is_closed
        assert _send(second._llm) == "ok"
        assert second._llm.embed("alive") == [0.5]
        assert len(harness.chat_requests) == 1
        assert len(harness.embed_requests) == 1
    finally:
        for runner in created:
            runner.close()
    assert owned[1].is_closed and chat_http[1].is_closed


def test_constructor_failure_closes_owned_chat_and_embedding(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(RuntimeError):
        pass

    def failing_controller(*args: object, **kwargs: object) -> None:
        raise Boom("controller construction failed")

    monkeypatch.setattr(agent_controller, "AgentController", failing_controller)
    with pytest.raises(Boom):
        _create(_runtime(harness), "ON_DEMAND_SUBAGENT", run_model=_registry(tmp_path).admit())
    assert [c.close_count for c in harness.chats] == [1]
    assert [e.close_count for e in harness.embeddings] == [1]


def test_chat_client_construction_failure_closes_already_built_embedding(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_llm(*args: object, **kwargs: object) -> None:
        raise RuntimeError("chat client construction failed")

    monkeypatch.setattr(agent_controller, "LLMClient", failing_llm)
    with pytest.raises(RuntimeError):
        _create(_runtime(harness), "ON_DEMAND_SUBAGENT", run_model=_registry(tmp_path).admit())
    assert [e.close_count for e in harness.embeddings] == [1]


# ---------------------------------------------------------------------------
# Typed runner constructor
# ---------------------------------------------------------------------------

def _runner_kwargs(harness: Harness, pin: object) -> dict:
    return dict(
        run_model=pin,
        agent_cfg=AGENT_CFG,
        embedding_factory=harness.embedding_factory,
        paths=ExecutorPaths(tmp_dir="/tmp/agent", downloads_dir="downloads",
                            workspace_dir="~/Documents", log_file="agent.log",
                            log_backup_count=30),
        tool_index=MagicMock(),
        base_memory=MagicMock(),
        builtin_executor=MagicMock(),
    )


def test_runner_requires_typed_pin_and_rejects_legacy_model_config(
    harness: Harness, tmp_path: Path
) -> None:
    pin = _registry(tmp_path).admit()
    runner = agent_controller.SubAgentRunner(**_runner_kwargs(harness, pin))
    runner.close()

    with pytest.raises((TypeError, ValueError)):
        agent_controller.SubAgentRunner(**_runner_kwargs(harness, None))
    legacy = _runner_kwargs(harness, pin)
    legacy.update(model_cfg={"model": "up/a"}, config={"models": []})
    with pytest.raises(TypeError):
        agent_controller.SubAgentRunner(**legacy)
    assert len(harness.chats) == 1


# ---------------------------------------------------------------------------
# Task 7.2 root section: actual production-root factory gate and pin immutability
#
# Drives the real ``main._run`` via ``tests/root_startup_harness.py`` and calls
# the main-owned factory installed on the root builtin. Real: registry,
# settings, discovery refresh, credential resolution, AgentRuntime,
# SubAgentRunner, AgentController and LLMClient. Replaced: the harness's
# external effects (HTTP router extended below with provider chat replies and
# a changed acme inventory) and ``agent_controller.react_loop``. Admission,
# credential resolution, runtime construction and run registration are
# observed by delegating spies installed after startup.
# ---------------------------------------------------------------------------

ROOT_ORDINARY_PROFILES = ("ON_DEMAND_SUBAGENT", "PLAN_STEP_AGENT", "DIAGNOSTIC_AGENT")
ROOT_REMOVED_ID = "acme/claude-b"
ROOT_ADDED_ID = "acme/claude-c"
ROOT_REFRESHED_ACME_ROWS = [
    {"id": "claude-a", "display_name": "Claude claude-a refreshed",
     "max_input_tokens": 50_000, "max_tokens": 1024},
    {"id": "claude-c", "display_name": "Claude claude-c",
     "max_input_tokens": 100_000, "max_tokens": 4096},
]


@pytest.fixture
def production_root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Keep production ownership assertions ahead of fallback fixture hygiene."""
    root = RootHarness(tmp_xdg, monkeypatch)
    try:
        yield root
    finally:
        root.release_leftovers()


class _RootRunBoundary:
    """Replaces only ``agent_controller.react_loop``; each run sends one real pinned request."""

    def __init__(self, on_call: Callable[[int, SimpleNamespace], None] | None = None) -> None:
        """Record runs; ``on_call(index, call)`` runs inside the run before its request."""
        self.on_call = on_call
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self, ctx: ReactContext, goal: str, progress_callback: Any = None,
        images: Any = None, **kwargs: Any,
    ) -> str:
        """Capture the run's pin, run the hook, then send one request through the client."""
        call = SimpleNamespace(goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(len(self.calls), call)
        call.reply = ctx.llm.chat([{"role": "user", "content": "ping"}])
        return f"pinned-answer-{len(self.calls)}"


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


def _serve_root_providers(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, refreshed: dict[str, bool],
) -> None:
    """Extend the harness HTTP boundary: provider chat replies and, once
    ``refreshed["acme"]`` is set, a changed acme inventory (claude-a metadata
    changed, claude-b removed, claude-c added)."""
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Serve chat and the changed acme listing; delegate everything else."""
        host, path, method = request.url.host, request.url.path, request.method
        if method == "GET" and host == ACME_HOST and path == "/v1/models" and refreshed["acme"]:
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={"data": ROOT_REFRESHED_ACME_ROWS, "has_more": False})
        if method == "POST" and host == ACME_HOST and path == "/v1/messages":
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            })
        if method == "POST" and host == GEM_HOST and path.endswith(":generateContent"):
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


@pytest.mark.parametrize("profile", ROOT_ORDINARY_PROFILES)
@pytest.mark.parametrize(
    "saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["no-saved-selection", "saved-model-missing"],
)
def test_root_factory_admits_explicit_selector_without_active_main(
    production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    saved_default: str | None, profile: str,
) -> None:
    """Task 4.1: the actual root factory admits a valid explicit selector with no active Main
    (cached or not), resolving one credential and sending no request; Main, preference and
    cache stay unchanged. Full explicit-run wiring ownership is task 4.2."""
    outcome: dict[str, Any] = {}

    def hold(root: RootHarness) -> None:
        """Call the installed root factory with a known explicit selector."""
        outcome["binding"] = _root_binding(root)
        spies = _RootAttemptSpies(root, monkeypatch)
        outcome["spies"] = spies
        runner = root.builtin._sub_agent_factory(
            model=OTHER_ID, label="gate-probe",
            runtime_profile=agent_runtime.RuntimeProfile[profile],
        )
        try:
            outcome["runner_model"] = runner.model_id
        finally:
            runner.close()  # the run never starts; release its owned clients
        outcome["view_after"] = root.registry.view()
        outcome["settings_after"] = root.paths.models_cache_file.read_bytes()

    production_root.start(saved_default=saved_default, hold=hold)
    root = production_root
    spies: _RootAttemptSpies = outcome["spies"]

    assert outcome["binding"] == (True, True, True)
    before = spies.view_before
    assert before.active_model_id is None and before.remembered_model_id == remembered_for(saved_default)
    assert OTHER_ID in {m.id for m in before.models}
    cached_other = next(m for m in before.models if m.id == OTHER_ID)
    assert outcome["runner_model"] == OTHER_ID
    assert spies.admits == {"admit": [OTHER_ID], "admit_scheduled": []}
    assert spies.codes == [] and [pin.model for pin in spies.pins] == [cached_other]
    assert spies.resolved == [OTHER_ID.split("/", 1)[0]] and len(spies.creates) == 1
    assert len(spies.new_llm_clients()) == 1 and spies.provider_requests() == []
    assert outcome["view_after"] == before
    assert outcome["settings_after"] == spies.settings_before
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_root_pins_survive_later_main_selection_and_real_refresh(
    production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-admitted omitted (Main A) and explicit (B) pins keep their model,
    connection, revisions, metadata and wire after Main changes to OTHER_ID and
    a real refresh changes the catalog mid-run; later admissions see the change."""
    refreshed = {"acme": False}
    _serve_root_providers(production_root, monkeypatch, refreshed)
    outcome: dict[str, Any] = {}

    def change_during_first_run(index: int, call: SimpleNamespace) -> None:
        """While the first pinned run is in flight: select a different Main, then refresh."""
        if index != 1:
            return
        registry = production_root.registry
        outcome["mid_pin"] = call.pin
        registry.select(OTHER_ID)
        refreshed["acme"] = True
        outcome["report"] = registry.refresh()
        outcome["mid_view"] = registry.view()

    boundary = _RootRunBoundary(on_call=change_during_first_run)
    monkeypatch.setattr(agent_controller, "react_loop", boundary)

    def hold(root: RootHarness) -> None:
        """Admit two pins, run them across the change, then admit again."""
        root.registry.refresh()  # the deliberate /models refresh: startup no longer discovers a cached catalog
        outcome["binding"] = _root_binding(root)
        spies = _RootAttemptSpies(root, monkeypatch)
        outcome["spies"] = spies
        factory = root.builtin._sub_agent_factory
        profiles = agent_runtime.RuntimeProfile
        runners: list[agent_controller.SubAgentRunner] = []
        try:
            omitted = factory(model=None, label="pin-omitted", runtime_profile=profiles.ON_DEMAND_SUBAGENT)
            runners.append(omitted)
            explicit = factory(
                model=ROOT_REMOVED_ID, label="pin-explicit", runtime_profile=profiles.PLAN_STEP_AGENT,
            )
            runners.append(explicit)
            outcome["pins_at_admission"] = list(spies.pins)
            outcome["answers"] = [omitted.run("first pinned run"), explicit.run("second pinned run")]
            outcome["pins_after_runs"] = [omitted._llm._run_model, explicit._llm._run_model]
            outcome["view_after"] = root.registry.view()
            next_main = factory(model=None, label="next-omitted", runtime_profile=profiles.ON_DEMAND_SUBAGENT)
            runners.append(next_main)
            next_explicit = factory(
                model=MAIN_ID, label="next-explicit", runtime_profile=profiles.DIAGNOSTIC_AGENT,
            )
            runners.append(next_explicit)
            outcome["next_answers"] = [next_main.run("next main run"), next_explicit.run("next explicit run")]
            # Task 4.1: a candidate the refresh removed is still a valid explicit target.
            removed = factory(model=ROOT_REMOVED_ID, label="removed", runtime_profile=profiles.ON_DEMAND_SUBAGENT)
            runners.append(removed)
        finally:
            for runner in runners:
                runner.close()
        outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
        outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
        outcome["saved_after"] = ModelCache(root.paths.models_cache_file).load()
        outcome["preference_after"] = root.paths.model_selection_file.read_bytes()

    production_root.start(saved_default=MAIN_ID, hold=hold)
    root = production_root
    spies: _RootAttemptSpies = outcome["spies"]
    assert outcome["binding"] == (True, True, True)

    # Before: Main A active over the discovered catalog with the original metadata.
    before = spies.view_before
    before_models = {m.id: m for m in before.models}
    assert before.active_model_id == MAIN_ID and tuple(before_models) == DISCOVERED
    assert before_models[MAIN_ID].metadata.output_tokens == 4096

    # Two pins were admitted before any change: omitted -> Main A, explicit -> B.
    # Neither is the later Main, so the selection change is a real change for both.
    pin_a, pin_b = outcome["pins_at_admission"]
    assert pin_a.model == before_models[MAIN_ID] and pin_b.model == before_models[ROOT_REMOVED_ID]
    assert OTHER_ID not in (pin_a.model.id, pin_b.model.id)
    for pin in (pin_a, pin_b):
        assert pin.catalog_revision == before.revision
        assert pin.selection_revision == before.selection_revision
        assert pin.connection.provider_id == "acme"
        assert pin.connection.base_url == f"https://{ACME_HOST}"

    # The change happened while the first pinned run was in flight (nonvacuous).
    assert outcome["mid_pin"] is pin_a
    report = outcome["report"]
    assert sorted(report.refreshed_provider_ids) == ["acme", "gem"] and report.persisted is True
    mid, after = outcome["mid_view"], outcome["view_after"]
    assert after == mid
    after_models = {m.id: m for m in after.models}
    assert after.active_model_id == OTHER_ID and after.selection_status == "active"
    assert tuple(after_models) == (MAIN_ID, ROOT_ADDED_ID, OTHER_ID, "gem/gem-2")
    assert ROOT_REMOVED_ID not in after_models
    assert after.revision > before.revision
    assert after.selection_revision > before.selection_revision
    assert after_models[MAIN_ID] != before_models[MAIN_ID]
    assert after_models[MAIN_ID].metadata.output_tokens == 1024
    assert after_models[MAIN_ID].metadata.input_tokens == 50_000
    saved = outcome["saved_after"]
    assert saved.status == "valid" and saved.document.default_model is None
    assert outcome["preference_after"] == f'model = "{OTHER_ID}"\n'.encode()
    # The disposable cache stores no revision; it holds the refreshed Main limits.
    assert {m.id: m.metadata.output_tokens for m in saved.document.models}[MAIN_ID] == 1024

    # Existing pins are the same immutable snapshots after the change.
    assert outcome["pins_after_runs"][0] is pin_a and outcome["pins_after_runs"][1] is pin_b
    assert pin_a.model == before_models[MAIN_ID] and pin_a.model.metadata.output_tokens == 4096
    assert pin_b.model == before_models[ROOT_REMOVED_ID]
    assert outcome["answers"] == ["pinned-answer-1", "pinned-answer-2"]

    # Wire: both old pins still reach acme with their original model and limits;
    # the later admissions reach the new Main and the refreshed metadata.
    posts = [r for r in spies.provider_requests() if r.method == "POST"]
    assert [(r.url.host, r.url.path) for r in posts] == [
        (ACME_HOST, "/v1/messages"),
        (ACME_HOST, "/v1/messages"),
        (GEM_HOST, "/v1beta/models/gem-1:generateContent"),
        (ACME_HOST, "/v1/messages"),
    ]
    first, second, third, fourth = posts
    for request in (first, second, fourth):
        assert request.headers["x-api-key"] == SECRETS["KEY_ACME"]
    assert third.headers["x-goog-api-key"] == SECRETS["KEY_GEM"]
    first_body, second_body, fourth_body = (json.loads(r.content) for r in (first, second, fourth))
    # The wire cap is the configured required cap (default 1024), not the discovered output limit.
    assert (first_body["model"], first_body["max_tokens"]) == ("claude-a", 1024)
    assert (second_body["model"], second_body["max_tokens"]) == ("claude-b", 1024)
    assert (fourth_body["model"], fourth_body["max_tokens"]) == ("claude-a", 1024)

    # Subsequent admissions observe the current selection, inventory and metadata.
    assert len(spies.pins) == 5
    next_main, next_a, uncached = spies.pins[2:]
    assert next_main.model == after_models[OTHER_ID]
    assert next_a.model == after_models[MAIN_ID]
    for pin in (next_main, next_a):
        assert pin.catalog_revision == after.revision
        assert pin.selection_revision == after.selection_revision
    assert outcome["next_answers"] == ["pinned-answer-3", "pinned-answer-4"]
    assert (uncached.model.id, uncached.model.upstream_id) == (ROOT_REMOVED_ID, "claude-b")
    assert uncached.model.metadata == ModelMetadata()  # removed from the cache; nothing is invented
    assert uncached.connection.provider_id == "acme"

    # One ordinary admission per run; each pin reached the runtime exactly once.
    assert spies.admits == {
        "admit": [None, ROOT_REMOVED_ID, None, MAIN_ID, ROOT_REMOVED_ID],
        "admit_scheduled": [],
    }
    assert spies.codes == []
    assert len(spies.creates) == 5
    assert all(options.run_model is pin for (_p, options), pin in zip(spies.creates, spies.pins))
    assert len(boundary.calls) == 4
    assert all(call.pin is pin for call, pin in zip(boundary.calls, spies.pins))
    # Credentials: two admissions, the refresh (both providers), two admissions.
    assert spies.resolved[:2] == ["acme", "acme"]
    assert sorted(spies.resolved[2:4]) == ["acme", "gem"]
    assert spies.resolved[4:] == ["gem", "acme", "acme"]
    assert spies.registered == []
    # Run-owned clients were closed by their runners before fixture hygiene.
    assert outcome["llm_closed"] == [True] * 5 and outcome["http_open"] == []
    assert root.unexpected == [] and root.open_clients_at_exit == []
