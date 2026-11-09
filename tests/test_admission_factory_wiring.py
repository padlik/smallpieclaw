"""Root sub-agent factory profile-branch wiring (task 12.11, tests-first).

Drives the MAIN-owned helper ``main._make_sub_agent_factory(runtime, registry)``
against a REAL ``ModelRegistry`` (saved catalog with canonical models A and B),
a REAL ``AgentRuntime`` and the REAL ``SubAgentRunner``/``LLMClient`` products it
builds. Spies on ``admit``/``admit_scheduled``/``runtime.create`` and the
credential resolver only delegate and record; every admission and construction
decision is made by the real objects.

Scope: this proves the helper contract only. It is not a root-startup proof
until ``main._run`` consumes this same helper and the legacy nested factory is
deleted (F2 / 7.1, production proof 12.20). It does not reuse the injected C2
``_runtime_factory`` adapter.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent_controller import SubAgentRunner
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from memory_store import ShortTermMemory
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


_URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}
_SCHEDULED = RuntimeProfile.SCHEDULED_AGENT
_PROFILES = tuple(RuntimeProfile)


def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    """Build one canonical catalog record for *provider*/*upstream*."""
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            context, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a", 5000)
MODEL_B = _record("pb", "up/b", 7000)
_REMEMBERED = {"active": MODEL_A.id, "no-selection": None, "remembered-missing": "pa/up/gone"}


@pytest.fixture(autouse=True)
def _block_provider_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse provider requests even if construction unexpectedly sends one."""
    def refuse(_client: httpx.Client, _request: httpx.Request, **_kwargs: object) -> NoReturn:
        raise AssertionError("factory construction must not send HTTP requests")

    monkeypatch.setattr(httpx.Client, "send", refuse)


class _NoDiscovery:
    """Discovery double that fails the test if any refresh is attempted."""

    def fetch(self, connection: object) -> object:
        """Reject discovery: admission must use the saved catalog only."""
        raise AssertionError("discovery must not run during factory admission")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    """Synthetic credential resolver producing a matching provider connection."""
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    """MockTransport handler: any embedding HTTP request is a test failure."""
    raise AssertionError(f"unexpected embedding request to {request.url}")


def _recording(real: Callable[..., object], sink: list) -> Callable[..., object]:
    """Wrap *real* so each actual return value is appended to *sink*."""
    def call(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        sink.append(result)
        return result
    return call


@dataclass
class _Wiring:
    """Real registry/runtime pair plus delegating spies and owned resources."""

    registry: ModelRegistry
    runtime: AgentRuntime
    settings_path: Path
    memory: MagicMock
    results: MagicMock
    admit: MagicMock
    admit_scheduled: MagicMock
    create: MagicMock
    resolver: MagicMock
    pins: list[RunModel] = field(default_factory=list)
    runners: list[SubAgentRunner] = field(default_factory=list)
    embeddings: list[EmbeddingClient] = field(default_factory=list)

    def factory(self) -> Callable[..., SubAgentRunner]:
        """Build the root helper's factory (import here: API absent => RED)."""
        from main import _make_sub_agent_factory

        return _make_sub_agent_factory(self.runtime, self.registry)

    def admissions(self) -> int:
        """Total admissions across both registry entry points."""
        return self.admit.call_count + self.admit_scheduled.call_count


@pytest.fixture
def build(tmp_path: Path) -> Iterator[Callable[[str], _Wiring]]:
    """Yield a builder of real wiring for a Main state; close owned clients."""
    stack = ExitStack()
    built: list[_Wiring] = []

    def _build(state: str = "active") -> _Wiring:
        providers = {
            name: ProviderConfig(name, "openai", f"env:KEY_{name}", url, 9)
            for name, url in _URLS.items()
        }
        settings_path = tmp_path / state / "models-config.toml"
        settings_path.parent.mkdir()
        ModelSettings(settings_path).write(CatalogDocument(
            1, 2, _REMEMBERED[state],
            tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (MODEL_A, MODEL_B),
        ))
        registry = ModelRegistry(providers, ModelSettings(settings_path), _NoDiscovery(), _resolve)
        registry.restore_selection()
        embeddings: list[EmbeddingClient] = []

        def embedding_factory(event: threading.Event) -> EmbeddingClient:
            http = stack.enter_context(httpx.Client(transport=httpx.MockTransport(_refuse)))
            client = EmbeddingClient(EmbeddingsConfig(), {}, http_client=http, cancel_event=event)
            embeddings.append(client)
            return client

        memory, results = MagicMock(name="operator_memory"), MagicMock(name="operator_results")
        runtime = AgentRuntime(
            agent_cfg=AgentConfig(), embedding_factory=embedding_factory,
            paths=ExecutorPaths(
                tmp_dir=str(tmp_path / state / "tmp"), downloads_dir=str(tmp_path / state / "downloads"),
                workspace_dir=str(tmp_path / state / "workspace"),
            ),
            tool_index=MagicMock(), base_memory=memory, builtin_executor=MagicMock(),
            skill_registry=MagicMock(), results=results, data_dir=str(tmp_path / state),
        )
        pins: list[RunModel] = []
        runners: list[SubAgentRunner] = []
        spy = stack.enter_context
        wiring = _Wiring(
            registry, runtime, settings_path, memory, results,
            admit=spy(patch.object(registry, "admit", wraps=_recording(registry.admit, pins))),
            admit_scheduled=spy(patch.object(
                registry, "admit_scheduled", wraps=_recording(registry.admit_scheduled, pins))),
            create=spy(patch.object(runtime, "create", wraps=_recording(runtime.create, runners))),
            resolver=spy(patch.object(
                registry, "_credential_resolver", wraps=registry._credential_resolver)),
            pins=pins, runners=runners, embeddings=embeddings,
        )
        built.append(wiring)
        return wiring

    try:
        yield _build
    finally:
        for wiring in built:
            for runner in wiring.runners:
                runner.close()
            assert all(runner._llm._closed for runner in wiring.runners)
        stack.close()


def _only_arg(spy: MagicMock) -> object:
    """Return the single selector argument of *spy*'s one call."""
    args, kwargs = spy.call_args
    values = [*args, *kwargs.values()]
    assert len(values) == 1, spy.call_args
    return values[0]


def _create_inputs(wiring: _Wiring) -> tuple[RuntimeProfile, RuntimeOptions, dict]:
    """Return ``(profile, options, keyword wiring)`` of the one create call."""
    assert wiring.create.call_count == 1
    args, kwargs = wiring.create.call_args
    rest = dict(kwargs)
    profile = args[0] if args else rest.pop("profile")
    options = args[1] if len(args) > 1 else rest.pop("options")
    return profile, options, rest


def test_helper_construction_is_pure(build: Callable[[str], _Wiring]) -> None:
    """Building the factory admits, resolves and allocates nothing."""
    wiring = build("active")
    factory = wiring.factory()
    assert callable(factory)
    assert wiring.admissions() == 0
    assert wiring.create.call_count == wiring.resolver.call_count == 0
    assert wiring.embeddings == [] and wiring.runners == []


@pytest.mark.parametrize("profile", _PROFILES, ids=[p.value for p in _PROFILES])
@pytest.mark.parametrize("selector,expected", [(None, MODEL_A), (MODEL_B.id, MODEL_B)],
                         ids=["inherit-main", "explicit-B"])
def test_profile_branch_admits_once_and_pins_runner(
    build: Callable[[str], _Wiring], profile: RuntimeProfile,
    selector: str | None, expected: ModelRecord,
) -> None:
    """Each profile uses its own entry once; the exact pin reaches the runner."""
    wiring = build("active")
    before = wiring.settings_path.read_bytes()
    runner = wiring.factory()(model=selector, runtime_profile=profile)

    primary, other = ((wiring.admit_scheduled, wiring.admit) if profile is _SCHEDULED
                      else (wiring.admit, wiring.admit_scheduled))
    assert primary.call_count == 1 and _only_arg(primary) == selector
    assert other.call_count == 0
    created_profile, options, _ = _create_inputs(wiring)
    (pin,) = wiring.pins
    assert created_profile is profile
    assert options.run_model is pin and pin.model == expected
    assert wiring.runners == [runner]
    assert runner._llm._run_model is pin and runner.model_id == expected.id
    assert wiring.admissions() == 1  # runtime performs no second admission
    assert wiring.resolver.call_count == 1
    assert wiring.embeddings == [runner._embedding]
    assert wiring.registry.view().active_model_id == MODEL_A.id
    assert wiring.settings_path.read_bytes() == before
    if profile is _SCHEDULED:
        assert runner._agent.memory is not wiring.memory and runner._agent.results is None
    else:
        assert runner._agent.memory is wiring.memory and runner._agent.results is wiring.results


@pytest.mark.parametrize("explicit", [False, True], ids=["omitted", "explicit-None"])
def test_default_profile_is_on_demand(build: Callable[[str], _Wiring], explicit: bool) -> None:
    """An omitted or ``None`` profile builds ON_DEMAND through ordinary admit."""
    wiring = build("active")
    kwargs = {"runtime_profile": None} if explicit else {}
    runner = wiring.factory()(**kwargs)
    assert wiring.admit.call_count == 1 and wiring.admit_scheduled.call_count == 0
    assert _create_inputs(wiring)[0] is RuntimeProfile.ON_DEMAND_SUBAGENT
    assert runner.model_id == MODEL_A.id


def test_scheduled_raw_selector_forwarded_by_identity(build: Callable[[str], _Wiring]) -> None:
    """A malformed scheduled selector reaches ``admit_scheduled`` unconverted."""
    wiring = build("active")
    raw = [MODEL_B.id]
    runner = wiring.factory()(model=raw, runtime_profile=_SCHEDULED)
    assert _only_arg(wiring.admit_scheduled) is raw
    assert wiring.admit.call_count == 0
    # Existing registry policy decides: a non-string selector inherits Main.
    assert runner._llm._run_model is wiring.pins[0] and runner.model_id == MODEL_A.id


@pytest.mark.parametrize("profile", _PROFILES, ids=[p.value for p in _PROFILES])
def test_supported_arguments_forwarded(
    build: Callable[[str], _Wiring], profile: RuntimeProfile,
) -> None:
    """Context/label/callbacks/limits/cancel/trace inputs reach options and runner."""
    wiring = build("active")
    event, payload = threading.Event(), {"summary": "parent state"}
    notify, tool_trace = MagicMock(name="notify"), MagicMock(name="tool_trace")
    runner = wiring.factory()(
        model=MODEL_B.id, context_key="job-ctx", label="nightly", notify_fn=notify,
        max_iterations=3, on_tool_trace=tool_trace, cancel_event=event,
        trace_id="r-0123abcd", context_payload=payload, prompt_variant="sub-agent",
        runtime_profile=profile,
    )
    _, options, extra = _create_inputs(wiring)
    assert (options.context_key, options.label, options.max_iterations) == ("job-ctx", "nightly", 3)
    assert options.cancel_event is event and options.context_payload is payload
    assert (options.trace_id, options.prompt_variant) == ("r-0123abcd", "sub-agent")
    assert extra == {"notify_fn": notify, "on_tool_trace": tool_trace}
    assert (runner.context_key, runner.label, runner.prompt_variant) == ("job-ctx", "nightly", "sub-agent")
    assert runner.context_payload is payload and runner.notify_fn is notify
    assert runner._cancel_event is event and runner.trace_id == "r-0123abcd"
    assert runner._agent.max_iterations == 3 and runner._agent._on_tool_trace is tool_trace
    assert isinstance(runner.short_term, ShortTermMemory)
    assert runner.model_id == MODEL_B.id


@pytest.mark.parametrize("state", ["no-selection", "remembered-missing"])
def test_ordinary_explicit_selector_is_admitted_once_without_active_main(
    build: Callable[[str], _Wiring], state: str,
) -> None:
    """Task 4.1: an ordinary explicit selector needs neither Main nor cache membership."""
    wiring = build(state)
    before = wiring.settings_path.read_bytes()
    runner = wiring.factory()(model=MODEL_B.id, runtime_profile=RuntimeProfile.ON_DEMAND_SUBAGENT)
    assert wiring.admissions() == 1 and [pin.model.id for pin in wiring.pins] == [MODEL_B.id]
    assert wiring.resolver.call_count == 1 and runner.model_id == MODEL_B.id
    assert wiring.registry.view().active_model_id is None
    assert wiring.settings_path.read_bytes() == before


@pytest.mark.parametrize("state", ["no-selection", "remembered-missing"])
def test_inactive_main_rejects_scheduled_selector_before_allocation(
    build: Callable[[str], _Wiring], state: str,
) -> None:
    """Scheduled admission still requires Main (scheduled handling is task 8.x)."""
    wiring = build(state)
    before = wiring.settings_path.read_bytes()
    with pytest.raises(ModelRegistryError) as caught:
        wiring.factory()(model=MODEL_B.id, runtime_profile=_SCHEDULED)
    assert caught.value.code == "no_active_selection"
    assert wiring.admissions() == 1 and wiring.pins == []
    assert wiring.resolver.call_count == wiring.create.call_count == 0
    assert wiring.embeddings == [] and wiring.runners == []
    assert wiring.registry.view().active_model_id is None
    assert wiring.settings_path.read_bytes() == before


@pytest.mark.parametrize("value", [None, 1], ids=["None", "numeric"])
@pytest.mark.parametrize("name", ["max_tokens", "temperature", "top_p"])
def test_obsolete_generation_keyword_rejected_before_admission(
    build: Callable[[str], _Wiring], name: str, value: int | None,
) -> None:
    """Removed generation keywords raise ``TypeError`` with no side effects."""
    wiring = build("active")
    with pytest.raises(TypeError):
        wiring.factory()(**{name: value})
    assert wiring.admissions() == 0
    assert wiring.create.call_count == wiring.resolver.call_count == 0
    assert wiring.embeddings == [] and wiring.runners == []
