"""Task 7.2: manual ``/compress`` owns an independently admitted summary client.

Real ``main._make_run_client_factory`` (the root factory), a real ``ModelRegistry``
and real ``LLMClient`` objects on a counting ``httpx.MockTransport``; the real
``AgentController`` runs an ordinary run (``react_loop`` stubbed) before ``/compress``.
Provider-failure and cancel-before-call publication semantics belong to task 7.3 and
are not asserted here beyond closing the owned client.
"""

from __future__ import annotations

import copy
import io
import logging
import sqlite3
import threading
import traceback
from pathlib import Path

import httpx
import pytest
import structlog

import agent_controller
import agent_logging
import main
import sqlite_log
from agent_controller import AgentController
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from llm_client import LLMClient, LLMError
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ProviderState,
)
from tests.model_stores import ModelStores

AGENT = AgentConfig(ctx_max_tokens=4096, max_retries=1, retry_delay=0)
HOST_A, HOST_B = "a.example.test", "b.example.test"
URLS = {"pa": f"https://{HOST_A}/v1", "pb": f"https://{HOST_B}/v1"}
SECRET = "synthetic-secret-key-7-2"
CANARY_KEY = "sk-canary-7-2-ABCDEFGH"
CANARY_URL = "https://private.internal.example/v1?key=zz"


def _model(provider: str, upstream: str) -> ModelRecord:
    meta = ModelMetadata(50_000, None, 2048, False, True, None)
    return ModelRecord(f"{provider}/{upstream}", provider, upstream, upstream, meta)


MODEL_A, MODEL_B = _model("pa", "ma"), _model("pb", "mb")


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=SECRET,
    )


class _CountingTransport(httpx.MockTransport):
    """Real httpx transport whose ``close`` is counted (the resource under test)."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.requests: list[httpx.Request] = []
        self.keys: list[str] = []
        self.transports: list[_CountingTransport] = []
        self.clients: list[LLMClient] = []
        self.status = 200
        self.on_request = None
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        self.stores = ModelStores.at(tmp_path)
        self.stores.cache.write(CatalogDocument(
            1, 2, tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (MODEL_A, MODEL_B),
        ))
        self.registry = self.stores.registry(providers, _NoDiscovery(), _resolve)
        self.registry.select(MODEL_A.id)
        world = self

        class _Client(LLMClient):  # boundary seam: counted MockTransport, nothing else
            def __init__(self, *args: object, **kwargs: object) -> None:
                transport = _CountingTransport(world._chat)
                world.transports.append(transport)
                super().__init__(*args, http_transport=transport, **kwargs)
                world.clients.append(self)

        monkeypatch.setattr(main, "LLMClient", _Client)
        embed_http = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"embedding": [0.5]}]})))
        self.embed_http = embed_http
        self.embedding = EmbeddingClient(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key",
                             base_url="https://embed.example.test/v1"),
            http_client=embed_http,
        )
        self.factory = main._make_run_client_factory(self.registry, AGENT, self.embedding)
        self.short_term = ShortTermMemory(max_turns=80)
        self.working = WorkingMemory()
        self.controller = AgentController(
            AGENT, ExecutorPaths(tmp_dir=str(tmp_path / "t")),
            tool_index=object(), memory=object(), llm=None,
            short_term=self.short_term, working=self.working,
            run_client_factory=self.factory,
        )
        monkeypatch.setattr(agent_controller, "react_loop", lambda *args, **kwargs: "ok")

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.keys.append(request.headers.get("authorization", ""))
        if self.on_request is not None:
            self.on_request()
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": f"summary-{request.url.host}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    def seed(self) -> None:
        self.short_term.clear()
        self.short_term.add("user", "hello")
        self.short_term.add("assistant", "hi")
        self.working.start_task("goal-7-2")

    def state(self) -> dict:
        return {
            "history": copy.deepcopy(self.short_term.get_messages()),
            "working": copy.deepcopy(self.working.to_dict()),
            "text": self.working.to_summary_text(),
        }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = _World(tmp_path, monkeypatch)
    yield w
    for client in w.clients:
        LLMClient.close(client)
    w.embed_http.close()


def test_idle_compress_after_run_uses_new_client_not_the_closed_run_pin(world) -> None:
    world.seed()
    assert world.controller.run("goal") == "ok"
    (run_client,) = world.clients
    assert run_client._closed and world.transports[0].close_count == 1
    world.registry.select(MODEL_B.id)  # Main changes between run and manual call

    msg = world.controller.compress_context()

    assert msg.startswith("✅ Context compressed.")
    assert len(world.clients) == 2 and world.clients[1] is not run_client
    assert [r.url.host for r in world.requests] == [HOST_B]  # current selection, not the old pin
    assert world.clients[1].llm_cfg["id"] == MODEL_B.id and run_client.llm_cfg["id"] == MODEL_A.id
    assert [t.close_count for t in world.transports] == [1, 1]
    assert world.registry.admit().model.id == MODEL_B.id  # selection untouched by compress


def test_distinct_manual_calls_admit_distinct_clients_each_closed_once(world) -> None:
    world.seed()
    world.controller.compress_context()
    world.seed()
    world.registry.select(MODEL_B.id)
    world.controller.compress_context()

    assert len(world.clients) == 2 and world.clients[0] is not world.clients[1]
    assert [r.url.host for r in world.requests] == [HOST_A, HOST_B]
    assert [t.close_count for t in world.transports] == [1, 1]
    assert world.controller.llm is None


def test_owned_transport_closed_on_success_provider_failure_and_cancel(world) -> None:
    world.seed()
    world.controller.compress_context()  # success
    world.seed()
    world.status = 503
    world.controller.compress_context()  # provider failure (publication policy is 7.3)
    world.status = 200
    world.seed()
    world.on_request = None
    original = world.factory

    def cancelling(event: threading.Event):
        binding = original(event)
        event.set()  # operator cancel after admission, before the model call
        return binding

    world.controller._run_client_factory = cancelling
    world.controller.compress_context()  # cancellation

    assert len(world.transports) == 3
    assert [t.close_count for t in world.transports] == [1, 1, 1]


def test_base_exception_in_summary_still_closes_owned_transport(world) -> None:
    class _Stop(BaseException):
        pass

    def stop() -> None:
        raise _Stop

    world.seed()
    world.on_request = stop
    before = world.state()
    with pytest.raises(_Stop):
        world.controller.compress_context()
    assert [t.close_count for t in world.transports] == [1]
    assert world.state() == before


def test_unselected_admission_failure_is_actionable_and_leaves_everything_unchanged(
    world, caplog,
) -> None:
    world.seed()
    world.registry = ModelRegistry(
        {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS},
        ModelStores.at(world.tmp_path, "empty").cache, _NoDiscovery(), _resolve,
        preference=ModelStores.at(world.tmp_path, "empty").preference,
    )
    world.controller._run_client_factory = main._make_run_client_factory(
        world.registry, AGENT, world.embedding,
    )
    before = world.state()

    with caplog.at_level(logging.WARNING):
        msg = world.controller.compress_context()

    assert msg.startswith("❌")
    assert "no active selection" in msg  # known-safe typed registry reason preserved
    assert "/models" in msg and "unchanged" in msg.lower()
    assert "deterministic" not in msg.lower() and SECRET not in msg and "https://" not in msg
    assert world.clients == [] and world.requests == []
    assert world.state() == before
    assert SECRET not in caplog.text


class _CanaryError(LLMError):
    """Untrusted factory fault whose str() and repr() both carry canaries."""

    def __repr__(self) -> str:
        return f"_CanaryError({CANARY_KEY!r}, {CANARY_URL!r})"


@pytest.fixture
def prod_logging(tmp_path: Path):
    """Real agent_logging pipeline with NO registered secrets (no global redactor relied on).

    Original root handlers are detached (not closed) before bootstrap; only the
    handlers bootstrap created are closed on teardown, then the originals return.
    """
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    for handler in saved:
        root.removeHandler(handler)
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log_file = str(tmp_path / "agent.log")
    db_path = agent_logging.setup_logging(log_file, secret_values=[], stream=io.StringIO())
    capture = _Capture(level=logging.DEBUG)
    root.addHandler(capture)
    try:
        yield log_file, db_path, captured
    finally:
        agent_logging.shutdown_log_store()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            if handler not in saved:
                handler.close()
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)
        structlog.reset_defaults()


def _everything_logged(log_file: str, db_path: str, captured: list[logging.LogRecord]) -> str:
    writer = sqlite_log._active_writer
    if writer is not None:
        writer.flush()
    for handler in logging.getLogger().handlers:
        handler.flush()
    parts = [open(log_file, encoding="utf-8").read()]
    conn = sqlite3.connect(db_path)
    try:
        parts += [repr(tuple(row)) for row in conn.execute("SELECT * FROM events")]
    finally:
        conn.close()
    for record in captured:  # every structured field, not just the rendered message
        parts.append(record.getMessage())
        parts.append(repr(record.__dict__))
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(parts)


@pytest.mark.parametrize("error_type", [LLMError, _CanaryError])
def test_untrusted_admission_error_never_reaches_response_or_production_log_pipeline(
    world, prod_logging, error_type,
) -> None:
    def failing(event: threading.Event):
        raise error_type(f"credential {CANARY_KEY} rejected by {CANARY_URL}")

    world.controller._run_client_factory = failing
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    logged = _everything_logged(*prod_logging)
    assert "summary admission failed" in logged  # the warning really went through the pipeline
    for canary in (CANARY_KEY, CANARY_URL, "private.internal", "_CanaryError"):
        assert canary not in msg and canary not in logged, canary
    assert msg.startswith("❌") and "model admission failed" in msg
    assert "/models" in msg and "unchanged" in msg.lower() and "deterministic" not in msg.lower()
    assert world.state() == before and world.requests == []
    assert world.clients == [] and world.transports == []


def test_no_factory_refuses_instead_of_borrowing_a_possibly_closed_client(world) -> None:
    from unittest.mock import MagicMock

    borrowed = MagicMock()
    world.controller._run_client_factory = None
    world.controller.llm = borrowed
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    assert msg.startswith("❌") and "unchanged" in msg.lower()
    borrowed.chat.assert_not_called()
    borrowed.close.assert_not_called()
    assert world.state() == before
