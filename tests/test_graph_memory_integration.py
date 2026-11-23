"""Integration tests for graph memory wiring.

Verifies that:
- When graph memory is disabled (default), ReactContext.graph_memory is None
- When graph memory is enabled and ladybug available, store/writer are wired
- BuiltinExecutor tools return graceful errors when graph memory is absent
- BuiltinExecutor tools return results when graph memory is wired
"""

from __future__ import annotations

import dataclasses
import json
import threading
import traceback
from pathlib import Path
from typing import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import graph_memory
import main as main_mod
import token_usage
from config_schema import (
    AgentConfig,
    AppConfig,
    EmbeddingsConfig,
    GraphMemoryConfig,
    ProviderConfig,
    ResolvedProviderConnection,
    parse_config,
)
from embedding_client import EmbeddingClient
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
from tests.model_stores import ModelStores
from providers._errors import LLMCancelledError, LLMPermanentError
from react_loop import ReactContext


# ---------------------------------------------------------------------------
# ReactContext — graph memory defaults
# ---------------------------------------------------------------------------

class TestReactContextGraphMemoryDefaults:
    def test_graph_memory_defaults_to_none(self, minimal_config):
        ctx = ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
            builtin_executor=MagicMock(),
            mcp_manager=None,
            skill_registry=None,
        )
        assert ctx.graph_memory is None
        assert ctx.graph_memory_writer is None
        assert ctx.graph_memory_max_entries == 10

    def test_graph_memory_can_be_set(self, minimal_config):
        mock_store = MagicMock()
        mock_writer = MagicMock()
        ctx = ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
            builtin_executor=MagicMock(),
            mcp_manager=None,
            skill_registry=None,
            graph_memory=mock_store,
            graph_memory_writer=mock_writer,
            graph_memory_max_entries=5,
        )
        assert ctx.graph_memory is mock_store
        assert ctx.graph_memory_writer is mock_writer
        assert ctx.graph_memory_max_entries == 5


# ---------------------------------------------------------------------------
# BuiltinExecutor — memory_graph_search / memory_graph_store without graph memory
# ---------------------------------------------------------------------------

class TestBuiltinExecutorGraphMemoryDisabled:
    @pytest.fixture
    def builtin(self, make_builtin_executor, tmp_path):
        return make_builtin_executor(data_dir=str(tmp_path))

    def test_memory_graph_search_no_store(self, builtin):
        result = builtin.execute("memory_graph_search", {"query": "test"})
        assert result["success"] is False
        assert "not enabled" in result["error"].lower() or "not available" in result["error"].lower()

    def test_memory_graph_store_no_store(self, builtin):
        result = builtin.execute("memory_graph_store", {"content": "test fact"})
        assert result["success"] is False
        assert "not enabled" in result["error"].lower() or "not available" in result["error"].lower()

    def test_memory_graph_search_missing_query(self, builtin):
        builtin._graph_memory = MagicMock()
        result = builtin.execute("memory_graph_search", {})
        assert result["success"] is False
        assert "query" in result["error"].lower()

    def test_memory_graph_store_missing_content(self, builtin):
        builtin._graph_memory = MagicMock()
        builtin._graph_memory_writer = None
        result = builtin.execute("memory_graph_store", {})
        assert result["success"] is False
        assert "content" in result["error"].lower()


# ---------------------------------------------------------------------------
# BuiltinExecutor — memory_graph_search / memory_graph_store with mock store
# ---------------------------------------------------------------------------

class TestBuiltinExecutorGraphMemoryEnabled:
    @pytest.fixture
    def builtin_with_graph_memory(self, make_builtin_executor, tmp_path):
        b = make_builtin_executor(data_dir=str(tmp_path))
        mock_store = MagicMock()
        mock_store.format_for_prompt.return_value = "KNOWLEDGE GRAPH CONTEXT:\n  • Alice (person)"
        mock_store.add_episode.return_value = "ep:123"
        b._graph_memory = mock_store
        mock_writer = MagicMock()
        b._graph_memory_writer = mock_writer
        return b, mock_store, mock_writer

    def test_memory_graph_search_returns_context(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        result = b.execute("memory_graph_search", {"query": "Alice preferences"})
        assert result["success"] is True
        assert "Alice" in result["output"]
        store.format_for_prompt.assert_called_once_with("Alice preferences")

    def test_memory_graph_search_empty_returns_not_found_message(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        store.format_for_prompt.return_value = ""
        result = b.execute("memory_graph_search", {"query": "unknown entity"})
        assert result["success"] is True
        assert "no relevant" in result["output"].lower()

    def test_memory_graph_store_requires_confirmation_then_stores_confirmed(self, builtin_with_graph_memory):
        b, store, writer = builtin_with_graph_memory
        # At depth 0 the store must request operator confirmation first.
        staged = b.execute("memory_graph_store", {"content": "Alice prefers dark mode."})
        assert staged.get("requires_confirmation") is True
        token = staged["token"]
        store.add_episode.assert_not_called()
        writer.enqueue.assert_not_called()
        # After operator approval the note is stored as a confirmed episode.
        result = b.confirm(token)
        assert result["success"] is True
        assert "ep:123" in result["output"]
        assert "confirmed" in result["output"].lower()
        store.add_episode.assert_called_once()
        _, kwargs = store.add_episode.call_args
        assert kwargs.get("admission_status") == "confirmed"
        writer.enqueue.assert_called_once()
        writer.flush.assert_called_once()

    def test_memory_graph_store_denied_writes_nothing(self, builtin_with_graph_memory):
        b, store, writer = builtin_with_graph_memory
        staged = b.execute("memory_graph_store", {"content": "Alice prefers dark mode."})
        token = staged["token"]
        b.cancel(token)
        # A cancelled token must not execute the store.
        result = b.confirm(token)
        assert result["success"] is False
        store.add_episode.assert_not_called()

    def test_memory_graph_search_store_exception_handled(self, builtin_with_graph_memory):
        b, store, _ = builtin_with_graph_memory
        store.format_for_prompt.side_effect = RuntimeError("DB error")
        result = b.execute("memory_graph_search", {"query": "anything"})
        assert result["success"] is False
        assert "failed" in result["error"].lower()


# ---------------------------------------------------------------------------
# is_builtin recognises graph memory tools
# ---------------------------------------------------------------------------

class TestBuiltinRecognition:
    def test_memory_graph_search_is_builtin(self, make_builtin_executor, tmp_path):
        b = make_builtin_executor(data_dir=str(tmp_path))
        assert b.is_builtin("memory_graph_search")

    def test_memory_graph_store_is_builtin(self, make_builtin_executor, tmp_path):
        b = make_builtin_executor(data_dir=str(tmp_path))
        assert b.is_builtin("memory_graph_store")

    def test_all_tools_includes_graph_memory(self, make_builtin_executor, tmp_path):
        b = make_builtin_executor(data_dir=str(tmp_path))
        names = {t.name for t in b.all_tools()}
        assert "memory_graph_search" in names
        assert "memory_graph_store" in names


# ---------------------------------------------------------------------------
# LLMClient.for_graph_extraction — pinned, owned extraction clients
#
# These tests prove only the existing client boundary: each test closes the
# clients it builds in its own ``finally``. They do not claim that a
# production helper or the composition root closes graph extraction clients.
# ---------------------------------------------------------------------------

_GRAPH_UPSTREAM = "vendor/graph-model"
_GRAPH_URL = "https://graph.example.test/v1/chat/completions"
_GRAPH_KEY = "fake-graph-key"
_EXTRACTION_REPLY = '{"entities":[],"relationships":[]}'
_GRAPH_WIRE_KEYS = {"model", "messages", "max_completion_tokens", "temperature"}


def _graph_pin() -> RunModel:
    """Return a settled pin whose upstream ID contains a slash."""
    model = ModelRecord(
        "graph/vendor/graph-model", "graph", _GRAPH_UPSTREAM, "Graph Model",
        ModelMetadata(context_tokens=4096, output_tokens=2048),
    )
    connection = ResolvedProviderConnection(
        "graph", "openai", "https://graph.example.test/v1", 15, _GRAPH_KEY
    )
    return RunModel(model, connection, 1, 1)


def _graph_agent_config() -> AgentConfig:
    """Return a valid agent config whose local context differs from the graph cap."""
    return AgentConfig(ctx_max_tokens=3000, max_retries=0)


def _extraction_messages() -> list[dict]:
    """Return a fresh message-only extraction request."""
    return [{"role": "user", "content": "extract"}]


def _extraction_reply(request: httpx.Request) -> httpx.Response:
    """Return a successful OpenAI-compatible extraction response."""
    del request
    return httpx.Response(
        200, json={"choices": [{"message": {"content": _EXTRACTION_REPLY}}]}
    )


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    """Fail the request as a refused connection without touching the network."""
    raise httpx.ConnectError("connection refused", request=request)


class _TrackingTransport(httpx.MockTransport):
    """Real MockTransport that records sent requests and close calls."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.close_count = 0

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(record)

    def close(self) -> None:
        """Count the close, then close the underlying transport."""
        self.close_count += 1
        super().close()


def _assert_graph_wire(request: httpx.Request) -> None:
    """Assert one sent request targets the pin and carries only the fixed policy."""
    assert str(request.url) == _GRAPH_URL
    assert request.headers["authorization"] == f"Bearer {_GRAPH_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-goog-api-key" not in request.headers
    payload = json.loads(request.content)
    assert set(payload) == _GRAPH_WIRE_KEYS
    assert payload["model"] == _GRAPH_UPSTREAM
    assert payload["messages"] == _extraction_messages()
    assert payload["max_completion_tokens"] == 1024
    assert payload["temperature"] == 0.1


class TestGraphExtractionClientBoundary:
    """Client-boundary ownership of graph extraction clients; not production wiring."""

    def test_same_pin_builds_independent_owned_clients(self) -> None:
        """Two clients from one pin send pinned fixed-policy requests and close independently."""
        pin = _graph_pin()
        first_transport = _TrackingTransport(_extraction_reply)
        second_transport = _TrackingTransport(_extraction_reply)
        first = LLMClient.for_graph_extraction(
            pin, agent_config=_graph_agent_config(), http_transport=first_transport
        )
        try:
            second = LLMClient.for_graph_extraction(
                pin, agent_config=_graph_agent_config(), http_transport=second_transport
            )
            try:
                assert first is not second
                assert first._run_model is pin
                assert second._run_model is pin
                assert first_transport.close_count == 0
                assert second_transport.close_count == 0

                assert first.chat(_extraction_messages()) == _EXTRACTION_REPLY
                assert second.chat(_extraction_messages()) == _EXTRACTION_REPLY

                first.close()
                assert first_transport.close_count == 1
                assert second_transport.close_count == 0
                assert second.chat(_extraction_messages()) == _EXTRACTION_REPLY
            finally:
                second.close()
        finally:
            first.close()

        assert first_transport.close_count == 1
        assert second_transport.close_count == 1
        assert len(first_transport.requests) == 1
        assert len(second_transport.requests) == 2
        for request in first_transport.requests + second_transport.requests:
            _assert_graph_wire(request)
        assert pin == _graph_pin()

    @pytest.mark.parametrize(
        ("handler", "error_type"),
        [
            (lambda request: httpx.Response(401, text="denied"), LLMPermanentError),
            (lambda request: httpx.Response(503, text="busy"), httpx.HTTPStatusError),
            (_refuse_connection, httpx.ConnectError),
        ],
        ids=["status_401", "status_503", "connect_error"],
    )
    def test_failed_chat_closes_only_its_owned_transport(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        error_type: type[Exception],
    ) -> None:
        """A failed chat raises its typed error and closing it leaves other clients open."""
        pin = _graph_pin()
        failing_transport = _TrackingTransport(handler)
        bystander_transport = _TrackingTransport(_extraction_reply)
        bystander = LLMClient.for_graph_extraction(
            pin, agent_config=_graph_agent_config(), http_transport=bystander_transport
        )
        try:
            client = LLMClient.for_graph_extraction(
                pin, agent_config=_graph_agent_config(), http_transport=failing_transport
            )
            try:
                with pytest.raises(error_type) as caught:
                    client.chat(_extraction_messages())
            finally:
                client.close()
            assert _GRAPH_KEY not in str(caught.value)
            assert len(failing_transport.requests) == 1
            _assert_graph_wire(failing_transport.requests[0])
            assert failing_transport.close_count == 1

            assert bystander_transport.close_count == 0
            assert bystander.chat(_extraction_messages()) == _EXTRACTION_REPLY
        finally:
            bystander.close()
        assert bystander_transport.close_count == 1
        assert pin == _graph_pin()

    def test_cancelled_chat_sends_nothing_and_transport_still_closes(self) -> None:
        """A pre-set cancel event blocks the send; the owned transport still closes."""
        cancel_event = threading.Event()
        cancel_event.set()
        transport = _TrackingTransport(_extraction_reply)
        client = LLMClient.for_graph_extraction(
            _graph_pin(),
            agent_config=_graph_agent_config(),
            http_transport=transport,
            cancel_event=cancel_event,
        )
        try:
            with pytest.raises(LLMCancelledError):
                client.chat(_extraction_messages())
        finally:
            client.close()
        assert transport.requests == []
        assert transport.close_count == 1

    def test_repeated_close_closes_owned_transport_once(self) -> None:
        """Repeated close and close_http calls close the owned transport only once."""
        transport = _TrackingTransport(_extraction_reply)
        client = LLMClient.for_graph_extraction(
            _graph_pin(), agent_config=_graph_agent_config(), http_transport=transport
        )
        try:
            assert client.chat(_extraction_messages()) == _EXTRACTION_REPLY
        finally:
            client.close()
        client.close()
        client.close_http()
        assert transport.close_count == 1
        _assert_graph_wire(transport.requests[0])


# ---------------------------------------------------------------------------
# GraphMemoryWriter — real worker/shared-helper bridge with deterministic scheduling
# ---------------------------------------------------------------------------


def test_graph_writer_processes_batches_through_shared_extraction_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real queued writer batches use distinct owned clients from one settled graph pin."""
    pin = _graph_pin()
    agent_config = _graph_agent_config()
    transports: list[_TrackingTransport] = []
    clients: list[LLMClient] = []
    close_calls: list[LLMClient] = []
    real_factory = LLMClient.for_graph_extraction
    response_text = json.dumps({
        "entities": [
            {"name": "Alice", "entity_type": "person"},
            {"name": "Python", "entity_type": "tool"},
        ],
        "facts": [{
            "source": "Alice", "target": "Python", "relation_type": "USES",
            "fact": "Alice uses Python.",
        }],
    })

    def extraction_response(request: httpx.Request) -> httpx.Response:
        """Serve valid extraction data at the external HTTP boundary."""
        del request
        return httpx.Response(200, json={"choices": [{"message": {"content": response_text}}]})

    def factory_spy(*args: object, **kwargs: object) -> LLMClient:
        """Observe exact pin/config identity while retaining the actual factory."""
        assert len(args) == 1
        assert args[0] is pin
        assert kwargs["agent_config"] is agent_config
        transport = _TrackingTransport(extraction_response)
        transports.append(transport)
        kwargs["http_transport"] = transport
        client = real_factory(*args, **kwargs)
        clients.append(client)
        real_close = client.close

        def close() -> None:
            """Count helper-owned closes before delegating to the real client."""
            close_calls.append(client)
            real_close()

        monkeypatch.setattr(client, "close", close)
        return client

    class DrainOnJoinThread:
        """External thread scheduler that runs the real worker only on join."""

        def __init__(self, *, target: Callable[[], None], daemon: bool, name: str) -> None:
            """Retain the real worker target without starting any OS thread."""
            assert daemon is True
            assert name == "graph-memory-writer"
            self.target = target
            self.started = False
            self.finished = False
            threads.append(self)

        def start(self) -> None:
            """Record scheduling without executing the worker yet."""
            self.started = True

        def join(self, timeout: float | None = None) -> None:
            """Drain the real worker after real stop has queued its sentinel."""
            assert timeout == 10.0
            assert self.started and not self.finished
            self.target()
            self.finished = True

        def is_alive(self) -> bool:
            """Report deterministic scheduling state to real stop/statistics code."""
            return self.started and not self.finished

    threads: list[DrainOnJoinThread] = []
    store = MagicMock(spec=graph_memory.GraphMemoryStore)

    def embed_entity(text: str) -> list[float]:
        """Supply deterministic vectors at the external store/embed boundary."""
        assert text in {"alice", "python"}
        return [0.25, 0.75]

    def entity_id(name: str, entity_type: str, ts: str) -> str:
        """Provide stable database IDs for actual writer relation records."""
        assert (name, entity_type) in {("Alice", "person"), ("Python", "tool")}
        assert ts
        return f"entity:{name.lower()}"

    store._embed = embed_entity
    store._embed_batch = None
    store.upsert_entity.side_effect = entity_id
    store.add_episode.return_value = "episode:recorded"
    monkeypatch.setattr(graph_memory.threading, "Thread", DrainOnJoinThread)
    monkeypatch.setattr(LLMClient, "for_graph_extraction", factory_spy)
    helper = graph_memory.build_extraction_llm_call(pin, agent_config)
    writer = graph_memory.GraphMemoryWriter(
        store, helper, extract_every_n_turns=1, min_message_length=1,
    )
    texts = ("Alice uses Python for research.", "Alice uses Python for reporting.")
    try:
        writer.enqueue(texts[0], user_id="writer-user", source="chat")
        writer.enqueue(texts[1], user_id="writer-user", source="manual")
        assert len(threads) == 1
        assert threads[0].started
        assert clients == []
        assert writer.get_stats()["batches_queued"] == 2
        writer.stop()

        # Ownership evidence is captured before defensive test cleanup.
        assert len(clients) == len(transports) == 2
        assert clients[0] is not clients[1]
        assert transports[0] is not transports[1]
        assert close_calls == clients
        assert all(client._closed and client._run_model is pin for client in clients)
        assert all(transport.close_count == 1 for transport in transports)
        for transport, text in zip(transports, texts, strict=True):
            assert len(transport.requests) == 1
            request = transport.requests[0]
            assert str(request.url) == _GRAPH_URL
            assert request.headers["authorization"] == f"Bearer {_GRAPH_KEY}"
            assert "x-api-key" not in request.headers
            assert "x-goog-api-key" not in request.headers
            payload = json.loads(request.content)
            assert set(payload) == _GRAPH_WIRE_KEYS
            assert payload["model"] == _GRAPH_UPSTREAM
            assert payload["max_completion_tokens"] == 1024
            assert payload["temperature"] == 0.1
            assert payload["messages"] == [
                {"role": "user", "content": graph_memory.EXTRACTION_PROMPT + text},
            ]

        assert [record.args for record in store.add_episode.call_args_list] == [
            (texts[0], "writer-user", "chat"), (texts[1], "writer-user", "manual"),
        ]
        assert store.upsert_entity.call_count == 4
        assert store.add_relation.call_count == 2
        for record in store.add_relation.call_args_list:
            assert record.args[:4] == ("entity:alice", "entity:python", "USES", "Alice uses Python.")
        assert store._preload_embeddings.call_count == 2
        for record in store._preload_embeddings.call_args_list:
            assert record.args == ({"alice": [0.25, 0.75], "python": [0.25, 0.75]},)
        assert store._clear_preloaded_embeddings.call_count == 2
        stats = writer.get_stats()
        for key, expected in {
            "enqueued": 2, "batches_queued": 2, "batches_processed": 2,
            "entities_extracted": 4, "facts_extracted": 2, "episodes_stored": 2,
            "llm_failures": 0, "parse_failures": 0, "write_failures": 0,
            "queue_depth": 0, "pending_depth": 0,
        }.items():
            assert stats[key] == expected
        assert stats["worker_alive"] is False
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()
    finally:
        for client in clients:
            if not client._closed:
                client.close()


def test_graph_writer_finite_stop_drains_buffered_batch_on_real_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite stop (task 12.26) flushes buffered work through the real helper,
    pin and owned client on the actual worker thread before it returns."""
    pin = _graph_pin()
    agent_config = _graph_agent_config()
    transports: list[_TrackingTransport] = []
    clients: list[LLMClient] = []
    real_factory = LLMClient.for_graph_extraction
    usage = token_usage.TokenUsageRegistry()
    response_text = json.dumps({
        "entities": [
            {"name": "Alice", "entity_type": "person"},
            {"name": "Python", "entity_type": "tool"},
        ],
        "facts": [{
            "source": "Alice", "target": "Python", "relation_type": "USES",
            "fact": "Alice uses Python.",
        }],
    })

    def extraction_response(request: httpx.Request) -> httpx.Response:
        """Serve valid extraction data at the external HTTP boundary."""
        del request
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_text}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
        })

    def factory_spy(*args: object, **kwargs: object) -> LLMClient:
        """Inject the tracking transport into the actual factory."""
        assert args == (pin,)
        assert kwargs["agent_config"] is agent_config
        transport = _TrackingTransport(extraction_response)
        transports.append(transport)
        kwargs["http_transport"] = transport
        client = real_factory(*args, **kwargs)
        clients.append(client)
        return client

    store = MagicMock(spec=graph_memory.GraphMemoryStore)

    def embed_entity(text: str) -> list[float]:
        """Supply deterministic vectors at the external store/embed boundary."""
        del text
        return [0.25, 0.75]

    store._embed = embed_entity
    store._embed_batch = None
    store.upsert_entity.side_effect = lambda name, entity_type, ts: f"entity:{name.lower()}"
    store.add_episode.return_value = "episode:recorded"
    monkeypatch.setattr(token_usage, "_registry", usage)
    monkeypatch.setattr(LLMClient, "for_graph_extraction", factory_spy)
    writer = graph_memory.GraphMemoryWriter(
        store, graph_memory.build_extraction_llm_call(pin, agent_config),
        extract_every_n_turns=5, min_message_length=1,
    )
    text = "Alice uses Python for buffered shutdown research."
    try:
        writer.enqueue(text, user_id="writer-user", source="chat")
        assert writer.get_stats()["pending_depth"] == 1
        assert clients == []
        writer.stop(join_timeout=10.0)

        # Observed before any test cleanup.
        assert not writer._thread.is_alive()
        assert len(clients) == len(transports) == 1
        client, transport = clients[0], transports[0]
        assert client._closed and client._run_model is pin
        assert transport.close_count == 1
        assert len(transport.requests) == 1
        payload = json.loads(transport.requests[0].content)
        assert payload["model"] == _GRAPH_UPSTREAM
        assert payload["messages"] == [
            {"role": "user", "content": graph_memory.EXTRACTION_PROMPT + text},
        ]
        assert [record.args for record in store.add_episode.call_args_list] == [
            (text, "writer-user", "chat"),
        ]
        assert store.add_relation.call_count == 1
        stats = writer.get_stats()
        for key, expected in {
            "batches_processed": 1, "entities_extracted": 2, "facts_extracted": 1,
            "episodes_stored": 1, "llm_failures": 0, "parse_failures": 0,
            "write_failures": 0, "queue_depth": 0, "pending_depth": 0,
        }.items():
            assert stats[key] == expected
        assert usage.get_today() == {pin.model.id: {"prompt": 9, "completion": 4, "total": 13}}
    finally:
        if writer._thread.is_alive():  # hygiene only; never shutdown evidence
            writer._queue.put(graph_memory.GraphMemoryWriter._SENTINEL)
            writer._thread.join(timeout=10)
        # Never close a client a surviving worker may still be using.
        assert not writer._thread.is_alive(), "graph worker survived the bounded cleanup join"
        for client in clients:
            if not client._closed:
                client.close()


# ---------------------------------------------------------------------------
# resolve_graph_startup — explicit graph pin, Main independence, embedding check
#
# The live startup path takes only the explicit ``[graph_memory]`` extraction
# fields and ``[embeddings]``: there is no catalog, registry or Main input.
# Real ModelStores/ModelRegistry are built only to prove their state is neither
# read nor changed. The environment and HTTP boundary are the only fakes.
# ---------------------------------------------------------------------------

_STARTUP_MODEL = "graph-extract-model"
_STARTUP_KEY = "fake-startup-graph-key"
_STARTUP_EMBED_KEY = "fake-startup-embedding-key"
_STARTUP_CONN_KEY = "fake-startup-connection-key"
_STARTUP_LEAK = "sk-startupleak12345"
_STARTUP_EMBED_URL = "https://embed.startup.test/v1"
_DEFAULT_EMBEDDINGS = EmbeddingsConfig(
    provider="openai", base_url=_STARTUP_EMBED_URL, model="text-embed", api_key="env:STARTUP_EMBED_KEY"
)
_MAIN_STATES = ("unselected", "saved-main", "unusable-main")


@dataclasses.dataclass
class _StartupWorld:
    """Explicit config, a real Main registry over real stores, and the tripwire log."""

    app_cfg: AppConfig
    stores: ModelStores
    registry: ModelRegistry
    tripwires: list[str]
    clients: list[httpx.Client]

    def observe(self) -> tuple[object, ...]:
        """Snapshot Main state and the saved cache/preference bytes."""
        return (self.registry.view(), self.stores.snapshot())


@pytest.fixture
def startup_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _StartupWorld]:
    """Return a builder for explicit graph settings beside a real Main registry."""

    def build(
        *,
        main: str = "unselected",
        embeddings: EmbeddingsConfig = _DEFAULT_EMBEDDINGS,
        enabled: bool = True,
        graph_key: str = "env:STARTUP_GRAPH_KEY",
    ) -> _StartupWorld:
        monkeypatch.setenv("STARTUP_GRAPH_KEY", _STARTUP_KEY)
        monkeypatch.setenv("STARTUP_EMBED_KEY", _STARTUP_EMBED_KEY)
        monkeypatch.delenv("STARTUP_CONN_KEY", raising=False)
        provider = ProviderConfig(
            "p", "openai", api_key="env:STARTUP_CONN_KEY", base_url="https://p.startup.test/v1", request_timeout=20
        )
        stores = ModelStores.at(tmp_path)
        saved = ModelRecord("p/main", "p", "main", "Main", ModelMetadata(context_tokens=8192, output_tokens=1024))
        if main != "unselected":
            stores.seed(
                CatalogDocument(
                    1, 3, (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),), (saved,),
                ),
                selected=saved.id if main == "saved-main" else None,
            )
        if main == "unusable-main":
            stores.preference.path.write_bytes(b"not a preference\n")
        registry = stores.registry(
            {"p": provider}, SimpleNamespace(fetch=lambda _c: pytest.fail("startup must not discover")),
            lambda cfg: ResolvedProviderConnection(
                cfg.name, cfg.type, cfg.base_url, cfg.request_timeout, _STARTUP_CONN_KEY
            ),
        )
        registry.restore_selection()
        base = parse_config({
            "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [12345]},
            "paths": {},
        })
        app_cfg = dataclasses.replace(
            base,
            providers={"p": provider},
            embeddings=embeddings,
            graph_memory=GraphMemoryConfig(
                enabled=enabled,
                extraction_model=_STARTUP_MODEL,
                extraction_provider="openai",
                extraction_base_url="https://graph.startup.test/v1/",
                extraction_api_key=graph_key,
            ),
        )

        tripwires: list[str] = []
        clients: list[httpx.Client] = []

        def client_factory(*args: object, **kwargs: object) -> httpx.Client:
            tripwires.append("httpx.Client")
            raise AssertionError("graph startup validation must build no HTTP client")

        def tripwire(name: str) -> Callable[..., None]:
            def tripped(*args: object, **kwargs: object) -> None:
                tripwires.append(name)
                raise AssertionError(f"{name} must not run during graph startup validation")
            return tripped

        monkeypatch.setattr(httpx, "Client", client_factory)
        monkeypatch.setattr(LLMClient, "__init__", tripwire("LLMClient"))
        monkeypatch.setattr(LLMClient, "for_graph_extraction", tripwire("for_graph_extraction"))
        monkeypatch.setattr(graph_memory, "create_graph_memory", tripwire("create_graph_memory"))
        monkeypatch.setattr(graph_memory.GraphMemoryStore, "__init__", tripwire("GraphMemoryStore"))
        monkeypatch.setattr(graph_memory.GraphMemoryWriter, "__init__", tripwire("GraphMemoryWriter"))
        monkeypatch.setattr(EmbeddingClient, "__init__", tripwire("EmbeddingClient"))
        return _StartupWorld(app_cfg, stores, registry, tripwires, clients)

    return build


def _start_ok(world: _StartupWorld) -> graph_memory.GraphPrerequisites:
    """Resolve graph startup, asserting no Main/store change and no client construction."""
    before = world.observe()
    result = graph_memory.resolve_graph_startup(world.app_cfg, vault_file=None)
    assert world.observe() == before
    assert world.tripwires == []
    return result


def _start_fails(world: _StartupWorld, code: str) -> Exception:
    """Resolve graph startup expecting a safe, secret-free GraphPrerequisiteError."""
    before = world.observe()
    with pytest.raises(graph_memory.GraphPrerequisiteError) as caught:
        graph_memory.resolve_graph_startup(world.app_cfg, vault_file=None)
    error = caught.value
    assert isinstance(error, ValueError)
    assert error.code == code
    rendered = "".join(traceback.format_exception(error))
    for secret in (_STARTUP_KEY, _STARTUP_EMBED_KEY, _STARTUP_CONN_KEY, _STARTUP_LEAK):
        assert secret not in str(error)
        assert secret not in rendered
    assert world.observe() == before
    assert world.tripwires == []
    return error


@pytest.mark.parametrize("main_state", _MAIN_STATES)
def test_startup_pins_explicit_settings_independent_of_main(
    startup_world: Callable[..., _StartupWorld], main_state: str
) -> None:
    """The pin comes from explicit settings alone, whatever Main's state or saved bytes."""
    world = startup_world(main=main_state)

    result = _start_ok(world)

    assert isinstance(result, graph_memory.GraphPrerequisites)
    assert {item.name for item in dataclasses.fields(result)} == {
        "pin", "embedding_provider", "embedding_model"
    }
    assert result.pin.model.id == result.pin.model.upstream_id == _STARTUP_MODEL
    assert result.pin.connection.provider_id == "graph-extraction"
    assert result.pin.connection.base_url == "https://graph.startup.test/v1"
    assert result.pin.connection.api_key == _STARTUP_KEY
    assert (result.pin.catalog_revision, result.pin.selection_revision) == (0, 0)
    assert (result.embedding_provider, result.embedding_model) == ("openai", "text-embed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.embedding_model = "other"  # type: ignore[misc]


def test_startup_does_not_open_ordinary_admission(startup_world: Callable[..., _StartupWorld]) -> None:
    """A graph pin never activates Main: implicit ordinary admission stays closed."""
    world = startup_world()
    _start_ok(world)
    with pytest.raises(ModelRegistryError) as error:
        world.registry.admit()
    assert error.value.code == "no_active_selection"
    assert world.registry.view().active_model_id is None


def test_startup_credential_failure_is_sanitized(
    startup_world: Callable[..., _StartupWorld], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable extraction credential reference yields a safe code naming only the field."""
    world = startup_world(graph_key="env:STARTUP_MISSING_VAR")
    monkeypatch.delenv("STARTUP_MISSING_VAR", raising=False)
    error = _start_fails(world, "extraction_credential_unavailable")
    assert "graph_memory.extraction_api_key" in str(error)
    assert "STARTUP_MISSING_VAR" not in str(error)


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid-credential"])
def test_disabled_config_is_still_validated_when_invoked(
    startup_world: Callable[..., _StartupWorld], valid: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enabled gate belongs to the caller; invoking startup resolution always validates."""
    if valid:
        _start_ok(startup_world(enabled=False))
    else:
        monkeypatch.delenv("STARTUP_MISSING_VAR", raising=False)
        _start_fails(startup_world(enabled=False, graph_key="env:STARTUP_MISSING_VAR"), "extraction_credential_unavailable")


def test_startup_has_no_budget_or_limit_gate(startup_world: Callable[..., _StartupWorld]) -> None:
    """No discovered limits exist on the explicit path, so no budget is computed or gated."""
    result = _start_ok(startup_world())
    assert not hasattr(result, "budget")
    assert result.pin.model.metadata == ModelMetadata()


@pytest.mark.parametrize(
    "embeddings",
    [
        pytest.param(
            EmbeddingsConfig(base_url=_STARTUP_EMBED_URL, model="text-embed", api_key="env:STARTUP_EMBED_KEY"),
            id="provider-missing",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", base_url=_STARTUP_EMBED_URL, api_key="env:STARTUP_EMBED_KEY"),
            id="model-missing",
        ),
        pytest.param(
            EmbeddingsConfig(
                provider="anthropic", base_url=_STARTUP_EMBED_URL, model="text-embed", api_key="env:STARTUP_EMBED_KEY"
            ),
            id="unsupported-adapter",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", base_url=_STARTUP_EMBED_URL, model="text-embed"),
            id="api-key-missing",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", model="text-embed", api_key="env:STARTUP_EMBED_KEY"),
            id="base-url-missing",
        ),
        pytest.param(
            EmbeddingsConfig(unavailable_reason="embeddings.base_url must be configured explicitly"),
            id="parse-unavailable",
        ),
        pytest.param(
            EmbeddingsConfig(
                provider="openai", model="text-embed", api_key="env:STARTUP_EMBED_KEY",
                base_url=f"https://user:{_STARTUP_LEAK}@embed.startup.test/v1",
            ),
            id="invalid-endpoint",
        ),
    ],
)
def test_unavailable_embeddings_fail_actionably_without_building_a_client(
    startup_world: Callable[..., _StartupWorld], embeddings: EmbeddingsConfig
) -> None:
    """Embedding problems raise embedding_unavailable and name [embeddings]; no client is built."""
    world = startup_world(embeddings=embeddings)
    error = _start_fails(world, "embedding_unavailable")
    assert "embeddings" in str(error).lower()


@pytest.mark.parametrize("connection_key", [None, _STARTUP_CONN_KEY], ids=["conn-key-unset", "conn-key-set"])
def test_embedding_credential_comes_only_from_embedding_config(
    startup_world: Callable[..., _StartupWorld], monkeypatch: pytest.MonkeyPatch, connection_key: str | None
) -> None:
    """The explicit [embeddings] key is used; a chat connection key is never consulted."""
    world = startup_world()
    if connection_key is not None:
        monkeypatch.setenv("STARTUP_CONN_KEY", connection_key)
    result = _start_ok(world)
    assert (result.embedding_provider, result.embedding_model) == ("openai", "text-embed")


# ---------------------------------------------------------------------------
# Real factory/main boundary: engine open failure (tasks 5.1-5.2)
# ---------------------------------------------------------------------------

_OPEN_FAILURE_SECRET = "SECRET-MARKER-engine-text"


def _open_failure_app_cfg() -> AppConfig:
    """Return a valid enabled-graph config with explicit extraction settings."""
    return parse_config({
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1]},
        "agent": {"compaction_threshold_tokens": 60000},
        "providers": {"chat": {"type": "openai", "api_key": "k", "base_url": "https://c.test/v1"}},
        "embeddings": {
            "provider": "openai", "model": "e", "api_key": "k", "base_url": "https://e.test/v1",
        },
        "graph_memory": {
            "enabled": True,
            "extraction_provider": "openai",
            "extraction_base_url": "https://g.test/v1",
            "extraction_api_key": "gk",
            "extraction_model": "org/m",
        },
    })


def _assert_fixed_guidance(text: str) -> None:
    """Assert every required fixed guidance element is present."""
    lowered = text.lower()
    for phrase in (
        "graph memory is unavailable",
        "stop graph users",
        "back up the database and all sidecars together",
        "supported procedures",
        "restart after repair",
        "explicit operator approval",
    ):
        assert phrase in lowered


def test_real_factory_open_failure_is_graph_unwired_with_fixed_guidance_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Injected nonmutating engine failure flows through real main/factory/store."""
    app_cfg = _open_failure_app_cfg()
    db_path = tmp_path / "graph_db"
    sidecars = {
        db_path: b"db-bytes",
        Path(f"{db_path}.wal"): b"wal-bytes",
        Path(f"{db_path}.wal.checkpoint"): b"ckpt-bytes",
    }
    for path, data in sidecars.items():
        path.write_bytes(data)
    open_calls: list[str] = []

    def failing_open(path: str, **_kwargs: object) -> None:
        open_calls.append(path)
        raise RuntimeError(f"Corrupted wal {_OPEN_FAILURE_SECRET}")

    writers: list[object] = []
    real_writer = graph_memory.GraphMemoryWriter

    def recording_writer(*args: object, **kwargs: object) -> object:
        writers.append(real_writer(*args, **kwargs))
        return writers[-1]

    monkeypatch.setattr(graph_memory.ladybug, "Database", failing_open)
    monkeypatch.setattr(graph_memory, "GraphMemoryWriter", recording_writer)
    monkeypatch.setattr(main_mod, "create_graph_memory", graph_memory.create_graph_memory)
    agent = SimpleNamespace()
    builtin = SimpleNamespace()
    with caplog.at_level("DEBUG"):
        store, writer = main_mod._build_graph_memory(
            app_cfg=app_cfg,
            paths=SimpleNamespace(graph_memory_db=db_path),
            agent=agent,
            builtin=builtin,
            vault_file=str(tmp_path / "no-vault"),
            embedder_fn=lambda _text: [0.1, 0.2, 0.3],
        )
    assert (store, writer) == (None, None)
    assert len(open_calls) == 1
    assert writers == []
    assert not hasattr(agent, "_graph_memory") and not hasattr(builtin, "_graph_memory")
    assert {p: p.read_bytes() for p in sidecars} == sidecars
    captured = capsys.readouterr()
    text = "\n".join([caplog.text, captured.out, captured.err])
    assert _OPEN_FAILURE_SECRET not in text
    assert "Traceback" not in text
    assert "RuntimeError" in text
    _assert_fixed_guidance(text)
