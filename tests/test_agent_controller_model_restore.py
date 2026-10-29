"""Regression tests for AgentController.run() model pinning across run exits.

A run executes on one immutable admitted model. Completion — success, exception
or cancellation — must neither retarget that run nor reset Main: an operator
selection made while the run is in flight survives the run, and the next
admission uses it. This replaces the retired ``_active_idx`` restore contract.
Only ``agent_controller.react_loop`` is patched; registry, settings, admission
and chat clients are real, with all HTTP served by ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

import agent_controller
from config_schema import (
    AgentConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from llm_client import LLMCancelledError, LLMClient
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings
from token_usage import TokenUsageRegistry

STOPPED = "⚠️ Task stopped by operator."
AGENT = AgentConfig(max_retries=0, retry_delay=0)


def _record(provider: str, upstream: str, context: int) -> ModelRecord:
    """Build a catalog record with provider-sourced metadata."""
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
HOSTS = {MODEL_A.id: "a.example.test", MODEL_B.id: "b.example.test"}


class _NoDiscovery:
    """Discovery stand-in: admission and selection must never refresh."""

    def fetch(self, connection: object) -> object:
        """Fail loudly if discovery is reached."""
        raise AssertionError("discovery must not run during admission")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    """External credential resolver returning a synthetic per-provider key."""
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


class _TrackedLLM(LLMClient):
    """Real LLMClient that counts close() calls."""

    close_count = 0

    def close(self) -> None:
        """Count, then close the real transport."""
        self.close_count += 1
        super().close()


class _World:
    """One controller over a real settings file, registry and pinned clients."""

    def __init__(self, tmp_path: Path, stack: ExitStack, make_agent_controller: Callable) -> None:
        """Persist catalog A+B, select A and build the controller."""
        self.stack = stack
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        self.settings_path = tmp_path / "models-config.toml"
        settings = ModelSettings(self.settings_path)
        settings.write(CatalogDocument(
            1, 2, None,
            tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (MODEL_A, MODEL_B), (),
        ))
        self.registry = ModelRegistry(providers, settings, _NoDiscovery(), _resolve)
        self.registry.select(MODEL_A.id)
        self.usage = TokenUsageRegistry()
        self.requests: list[httpx.Request] = []
        self.events: list[threading.Event] = []
        self.owned: list[_TrackedLLM] = []
        # Caller-owned standing client: must stay self.llm and never be used or closed.
        self.standing = self.client(self.registry.admit())
        self.controller = make_agent_controller(
            agent_cfg=AGENT,
            paths=ExecutorPaths(
                tmp_dir=str(tmp_path / "tmp"), downloads_dir=str(tmp_path / "dl"),
                workspace_dir=str(tmp_path / "ws"),
            ),
            llm=self.standing,
            run_client_factory=self.factory,
        )

    def _chat(self, request: httpx.Request) -> httpx.Response:
        """Record the wire request and answer with a valid completion."""
        self.requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    def client(self, run_model: RunModel, event: Optional[threading.Event] = None) -> _TrackedLLM:
        """Build a real pinned client; teardown bypasses the close counter."""
        client = _TrackedLLM(
            run_model, agent_config=AGENT, http_transport=httpx.MockTransport(self._chat),
            cancel_event=event, usage_registry=self.usage,
        )
        self.stack.callback(LLMClient.close, client)
        return client

    def factory(self, event: threading.Event) -> LLMClient:
        """run_client_factory: admit current Main through the real registry."""
        self.events.append(event)
        client = self.client(self.registry.admit(), event)
        self.owned.append(client)
        return client

    def main_selection(self) -> tuple[Optional[str], Optional[str]]:
        """Return (active Main, persisted default) as re-read from disk."""
        loaded = ModelSettings(self.settings_path).load()
        assert loaded.status == "valid" and loaded.document is not None
        return self.registry.view().active_model_id, loaded.document.default_model


class _Loop:
    """Patched react_loop: real pinned request, then a scripted exit."""

    def __init__(self, world: _World, exit_kind: str, reselect: Optional[str], owned: bool) -> None:
        """Script one exit path and an optional mid-run operator selection."""
        self.world = world
        self.exit_kind = exit_kind
        self.reselect = reselect
        self.owned = owned
        self.seen: list[dict] = []

    def __call__(self, ctx, user_goal, progress_callback=None, images=None, **_kwargs) -> str:
        """Run one request on ctx.llm and exit as scripted."""
        self.seen.append({
            "llm": ctx.llm, "event": ctx.cancel_event, "progress": progress_callback,
            "main_llm": self.world.controller.llm,
        })
        if self.reselect is not None:
            self.world.registry.select(self.reselect)
        answer = ctx.llm.chat([{"role": "user", "content": user_goal}])
        if progress_callback is not None:
            progress_callback(f"answer:{answer}")
        if self.exit_kind == "exception":
            raise RuntimeError("simulated crash")
        if self.exit_kind == "cancellation":
            self.world.controller.cancel()
            assert ctx.cancel_event.is_set()
            if self.owned:
                with pytest.raises(LLMCancelledError):
                    ctx.llm.chat([{"role": "user", "content": "after cancel"}])
            return STOPPED
        return "done"


@pytest.fixture
def world(tmp_path: Path, make_agent_controller: Callable) -> Iterator[_World]:
    """Yield an isolated world; every client is closed at teardown."""
    with ExitStack() as stack:
        yield _World(tmp_path, stack, make_agent_controller)


def _exercise(
    world: _World, monkeypatch: pytest.MonkeyPatch, exit_kind: str, operator: str, ownership: str
) -> None:
    """Drive one A-pinned run through ``exit_kind`` and verify pin/Main/ownership."""
    owned = ownership == "owned"
    expected = MODEL_B.id if operator == "select-b" else MODEL_A.id
    loop = _Loop(world, exit_kind, MODEL_B.id if operator == "select-b" else None, owned)
    monkeypatch.setattr(agent_controller, "react_loop", loop)
    supplied = None if owned else world.client(world.registry.admit())
    kwargs = {} if owned else {"admitted_llm": supplied}
    progress_seen: list[str] = []

    def progress(message: str) -> None:
        progress_seen.append(message)

    if exit_kind == "exception":
        with pytest.raises(RuntimeError, match="simulated crash"):
            world.controller.run("goal", progress, **kwargs)
    else:
        result = world.controller.run("goal", progress, **kwargs)
        assert result == ("done" if exit_kind == "success" else STOPPED)

    # The run stayed on its admitted A pin: one real request, model and endpoint A.
    seen = loop.seen[0]
    run_client = seen["llm"]
    assert run_client.llm_cfg["id"] == MODEL_A.id
    assert len(world.requests) == 1
    request = world.requests[0]
    assert (request.url.host, request.url.path) == ("a.example.test", "/v1/chat/completions")
    assert json.loads(request.content)["model"] == "up/a"
    assert request.headers["authorization"] == "Bearer secret-pa"
    assert world.usage.get_today() == {MODEL_A.id: {"prompt": 3, "completion": 2, "total": 5}}
    assert run_client.get_today_usage()["total_tokens"] == 5
    assert seen["progress"] is progress and progress_seen == ["answer:ok"]

    # Ownership: factory client closed exactly once; supplied client stays caller-owned.
    if owned:
        assert world.owned == [run_client] and run_client.close_count == 1
        assert world.events == [seen["event"]] and run_client._cancel_event is seen["event"]
    else:
        assert run_client is supplied and world.events == [] and world.owned == []
        assert supplied.close_count == 0

    # self.llm is never swapped, used or closed by the run.
    assert seen["main_llm"] is world.standing and world.controller.llm is world.standing
    assert world.standing.close_count == 0

    # Main/settings keep the operator's choice (or A); completion resets nothing.
    assert world.main_selection() == (expected, expected)

    # The next admission follows current Main, on a fresh uncancelled run.
    loop.exit_kind, loop.reselect = "success", None
    assert world.controller.run("next") == "done"
    follow_up = loop.seen[1]["llm"]
    assert follow_up is world.owned[-1] and follow_up.llm_cfg["id"] == expected
    assert follow_up.close_count == 1 and len(world.requests) == 2
    assert world.requests[1].url.host == HOSTS[expected]
    assert world.main_selection() == (expected, expected)
    assert run_client.llm_cfg["id"] == MODEL_A.id

    if supplied is not None:
        assert supplied.close_count == 0
        supplied.close()
        assert supplied.close_count == 1


_MATRIX = pytest.mark.parametrize(
    ("operator", "ownership"),
    [(op, own) for op in ("no-change", "select-b") for own in ("owned", "supplied")],
)


class TestAgentControllerModelRestore:
    """AgentController.run() keeps the run pin and the operator's Main selection."""

    @_MATRIX
    def test_success_preserves_run_pin_and_main_selection(
        self, world: _World, monkeypatch: pytest.MonkeyPatch, operator: str, ownership: str
    ) -> None:
        """A successful run returns its result without resetting Main."""
        _exercise(world, monkeypatch, "success", operator, ownership)

    @_MATRIX
    def test_exception_preserves_run_pin_and_main_selection(
        self, world: _World, monkeypatch: pytest.MonkeyPatch, operator: str, ownership: str
    ) -> None:
        """A react_loop RuntimeError propagates without resetting Main."""
        _exercise(world, monkeypatch, "exception", operator, ownership)

    @_MATRIX
    def test_cancellation_preserves_run_pin_and_main_selection(
        self, world: _World, monkeypatch: pytest.MonkeyPatch, operator: str, ownership: str
    ) -> None:
        """A cancelled run returns the stop result without resetting Main."""
        _exercise(world, monkeypatch, "cancellation", operator, ownership)

    def test_does_not_change_primary_when_no_fallback_occurs(self, make_agent_controller):
        """_active_idx stays at 0 throughout when no fallback is triggered."""
        llm = MagicMock()
        llm._active_idx = 0
        ctrl = make_agent_controller(llm=llm)

        def normal_react_loop(ctx, goal, _progress, images):
            # No fallback — _active_idx never changes
            return "answer"

        with patch("agent_controller.react_loop", side_effect=normal_react_loop):
            ctrl.run("simple question")

        assert llm._active_idx == 0
