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
from unittest.mock import MagicMock

import httpx
import pytest

import graph_memory
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
from model_settings import ModelSettings, SettingsLoadResult
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
# resolve_graph_prerequisites — graph pin, fixed-cap budget, embedding check
#
# Uses the real ModelRegistry, ModelSettings, budget kernel and EmbeddingClient.
# Only the credential resolver, environment, discovery and HTTP transport are
# faked. The helper is imported inside each test so this module still collects
# before the API exists.
# ---------------------------------------------------------------------------

_PREREQ_GRAPH_ID = "g/org/graph-model"
_PREREQ_MAIN_ID = "p/main"
_PREREQ_GRAPH_KEY = "fake-prereq-graph-key"
_PREREQ_EMBED_KEY = "fake-prereq-embedding-key"
_PREREQ_CONN_KEY = "fake-prereq-connection-key"
_PREREQ_LEAK = "sk-prereqleak12345"
_PREREQ_SECRETS = (_PREREQ_GRAPH_KEY, _PREREQ_EMBED_KEY, _PREREQ_CONN_KEY, _PREREQ_LEAK)
_PREREQ_LOCAL_CONTEXT = 3000
_PREREQ_SEEDED_AT = "2026-09-01T00:00:00Z"
_REMEMBERED_MAIN = {"unselected": None, "remembered-missing": "p/gone", "active-main": _PREREQ_MAIN_ID}
_MAIN_STATUS = {
    "unselected": "no_selection",
    "remembered-missing": "saved_selection_unavailable",
    "active-main": "active",
}
_REAL_HTTPX_CLIENT = httpx.Client


def _meta(context: int | None = None, output: int | None = None) -> ModelMetadata:
    """Return provider-reported metadata for the populated token limits."""
    return ModelMetadata(context_tokens=context, output_tokens=output)


def _prereq_providers() -> dict[str, ProviderConfig]:
    """Return graph, Main, embedding and chat-only connections with distinct credentials."""
    def connection(name: str, adapter: str, key: str) -> ProviderConfig:
        return ProviderConfig(name, adapter, api_key=key, base_url=f"https://{name}.prereq.test/v1", request_timeout=20)

    return {
        "g": connection("g", "openai", "env:PREREQ_GRAPH_KEY"),
        "p": connection("p", "openai", "env:PREREQ_MAIN_KEY"),
        "e": connection("e", "openai", "env:PREREQ_CONN_KEY"),
        "a": connection("a", "anthropic", "env:PREREQ_CONN_KEY"),
    }


_PREREQ_EMBED_URL = "https://embed.prereq.test/v1"
_DEFAULT_EMBEDDINGS = EmbeddingsConfig(
    provider="openai", base_url=_PREREQ_EMBED_URL, model="text-embed", api_key="env:PREREQ_EMBED_KEY"
)


class _GraphCredentials:
    """Fake external credential resolver that records each resolved provider identity."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, config: ProviderConfig) -> ResolvedProviderConnection:
        self.calls.append(config.name)
        if self.fail:
            raise RuntimeError(f"vault {_PREREQ_LEAK} token: {_PREREQ_GRAPH_KEY}")
        return ResolvedProviderConnection(
            config.name, config.type, config.base_url, config.request_timeout, _PREREQ_GRAPH_KEY
        )


class _NoDiscovery:
    """External discovery boundary that records and refuses any fetch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, connection: ResolvedProviderConnection) -> object:
        self.calls.append(connection.provider_id)
        raise AssertionError("graph prerequisites must not run discovery")


class _ObservedSettings(ModelSettings):
    """Real managed settings that records writes; may serve an unvalidated document."""

    def __init__(self, path: Path, preloaded: CatalogDocument | None = None) -> None:
        super().__init__(path)
        self.writes: list[CatalogDocument] = []
        self._preloaded = preloaded

    def load(self) -> SettingsLoadResult:
        """Return the preloaded document, if any, else the real persisted state."""
        if self._preloaded is not None:
            return SettingsLoadResult("valid", self._preloaded)
        return super().load()

    def write(self, document: CatalogDocument) -> None:
        """Record, then delegate the real validated write."""
        self.writes.append(document)
        super().write(document)


@dataclasses.dataclass
class _PrereqWorld:
    """Real registry/config plus the observed fake boundaries."""

    app_cfg: AppConfig
    registry: ModelRegistry
    settings: _ObservedSettings
    discovery: _NoDiscovery
    credentials: _GraphCredentials
    transports: list[_TrackingTransport]
    tripwires: list[str]

    def observe(self) -> tuple[object, ...]:
        """Snapshot registry state, settings bytes and side-effect counters."""
        return (
            self.registry.view(),
            self.settings.path.read_bytes(),
            len(self.settings.writes),
            len(self.discovery.calls),
        )


@pytest.fixture
def prereq_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _PrereqWorld]:
    """Return a builder for a real registry, AppConfig and tracked embedding transport."""

    def build(
        *,
        main: str = "unselected",
        freshness: str = "fresh",
        metadata: ModelMetadata | None = None,
        unvalidated: ModelMetadata | None = None,
        embeddings: EmbeddingsConfig = _DEFAULT_EMBEDDINGS,
        selector: object = _PREREQ_GRAPH_ID,
        enabled: bool = True,
    ) -> _PrereqWorld:
        monkeypatch.setenv("PREREQ_EMBED_KEY", _PREREQ_EMBED_KEY)
        monkeypatch.delenv("PREREQ_CONN_KEY", raising=False)
        graph = ModelRecord(_PREREQ_GRAPH_ID, "g", "org/graph-model", "Graph Model", metadata or _meta(4096, 2048))
        main_record = ModelRecord(_PREREQ_MAIN_ID, "p", "main", "Main", _meta(8192, 1024))
        document = CatalogDocument(
            1,
            3,
            _REMEMBERED_MAIN[main],
            (
                ProviderState("g", "openai", _PREREQ_SEEDED_AT, freshness),
                ProviderState("p", "openai", _PREREQ_SEEDED_AT, "fresh"),
            ),
            (graph, main_record),
        )
        settings = _ObservedSettings(tmp_path / "models-config.toml")
        settings.write(document)
        if unvalidated is not None:
            # Simulates a loaded record that bypassed metadata validation; the
            # budget kernel must still refuse it.
            settings._preloaded = dataclasses.replace(
                document, models=(dataclasses.replace(graph, metadata=unvalidated), main_record)
            )
        settings.writes.clear()
        providers = _prereq_providers()
        credentials = _GraphCredentials()
        discovery = _NoDiscovery()
        registry = ModelRegistry(
            {key: providers[key] for key in ("g", "p")}, settings, discovery, credentials
        )
        assert registry.restore_selection().selection_status == _MAIN_STATUS[main]
        base = parse_config({
            "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [12345]},
            "agent": {"compaction_threshold_tokens": _PREREQ_LOCAL_CONTEXT},
            "paths": {},
        })
        app_cfg = dataclasses.replace(
            base,
            # Retained internal attribute (no longer configurable; removed in 6.2).
            agent=dataclasses.replace(base.agent, ctx_max_tokens=_PREREQ_LOCAL_CONTEXT),
            providers=providers,
            embeddings=embeddings,
            graph_memory=GraphMemoryConfig(enabled=enabled, extraction_model=selector),  # type: ignore[arg-type]
        )

        transports: list[_TrackingTransport] = []
        tripwires: list[str] = []

        def client_factory(*args: object, **kwargs: object) -> httpx.Client:
            transport = _TrackingTransport(_refuse_connection)
            transports.append(transport)
            return _REAL_HTTPX_CLIENT(transport=transport)

        def tripwire(name: str) -> Callable[..., None]:
            def tripped(*args: object, **kwargs: object) -> None:
                tripwires.append(name)
                raise AssertionError(f"{name} must not run during prerequisite checks")
            return tripped

        monkeypatch.setattr(httpx, "Client", client_factory)
        monkeypatch.setattr(LLMClient, "__init__", tripwire("LLMClient"))
        monkeypatch.setattr(LLMClient, "for_graph_extraction", tripwire("for_graph_extraction"))
        monkeypatch.setattr(graph_memory, "create_graph_memory", tripwire("create_graph_memory"))
        monkeypatch.setattr(graph_memory.GraphMemoryStore, "__init__", tripwire("GraphMemoryStore"))
        monkeypatch.setattr(graph_memory.GraphMemoryWriter, "__init__", tripwire("GraphMemoryWriter"))
        credentials.calls.clear()
        return _PrereqWorld(app_cfg, registry, settings, discovery, credentials, transports, tripwires)

    return build


def _assert_no_side_effects(world: _PrereqWorld, before: tuple[object, ...]) -> None:
    """Assert no state change, no forbidden construction, and closed request-free transports."""
    assert world.observe() == before
    assert world.settings.writes == []
    assert world.discovery.calls == []
    assert world.tripwires == []
    assert len(world.transports) <= 1
    for transport in world.transports:
        assert transport.requests == []
        assert transport.close_count == 1


def _resolve_ok(world: _PrereqWorld) -> object:
    """Resolve prerequisites, asserting the success-path side-effect contract."""
    from graph_memory import resolve_graph_prerequisites

    before = world.observe()
    embeddings = world.app_cfg.embeddings
    result = resolve_graph_prerequisites(world.app_cfg, world.registry)
    _assert_no_side_effects(world, before)
    assert len(world.transports) == 1
    assert world.credentials.calls == ["g"]
    assert world.app_cfg.embeddings is embeddings
    return result


def _resolve_fails(world: _PrereqWorld, code: str) -> Exception:
    """Resolve prerequisites expecting a safe GraphPrerequisiteError with ``code``."""
    from graph_memory import GraphPrerequisiteError, resolve_graph_prerequisites

    before = world.observe()
    with pytest.raises(GraphPrerequisiteError) as caught:
        resolve_graph_prerequisites(world.app_cfg, world.registry)
    error = caught.value
    assert isinstance(error, ValueError)
    assert error.code == code
    rendered = "".join(traceback.format_exception(error))
    for secret in _PREREQ_SECRETS:
        assert secret not in str(error)
        assert secret not in rendered
    _assert_no_side_effects(world, before)
    return error


@pytest.mark.parametrize(
    ("main_state", "freshness"),
    [
        ("unselected", "fresh"),
        ("remembered-missing", "stale"),
        ("active-main", "fresh"),
        ("active-main", "stale"),
    ],
)
def test_prerequisites_pin_exact_graph_record_independent_of_main(
    prereq_world: Callable[..., _PrereqWorld], main_state: str, freshness: str
) -> None:
    """Graph selector pins its exact record and budget whatever Main's state or freshness."""
    from graph_memory import GraphPrerequisites

    world = prereq_world(main=main_state, freshness=freshness)
    view = world.registry.view()
    assert {state.provider_id: state.status for state in view.providers}["g"] == freshness

    result = _resolve_ok(world)

    assert isinstance(result, GraphPrerequisites)
    assert {item.name for item in dataclasses.fields(result)} == {
        "pin", "embedding_provider", "embedding_model"
    }
    assert result.pin.model == next(item for item in view.models if item.id == _PREREQ_GRAPH_ID)
    assert result.pin.connection.provider_id == "g"
    assert result.pin.connection.api_key == _PREREQ_GRAPH_KEY
    assert (result.pin.catalog_revision, result.pin.selection_revision) == (view.revision, view.selection_revision)
    assert (result.embedding_provider, result.embedding_model) == ("openai", "text-embed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.embedding_model = "other"  # type: ignore[misc]


def test_prerequisites_do_not_open_ordinary_admission(prereq_world: Callable[..., _PrereqWorld]) -> None:
    """A graph pin never activates Main: implicit ordinary admission stays closed."""
    world = prereq_world()
    _resolve_ok(world)
    with pytest.raises(ModelRegistryError) as error:
        world.registry.admit()
    assert error.value.code == "no_active_selection"
    assert world.registry.view().active_model_id is None


@pytest.mark.parametrize(
    ("selector", "code"),
    [
        pytest.param("", "model_not_found", id="missing"),
        pytest.param("   ", "model_not_found", id="blank"),
        pytest.param(" g/org/graph-model", "model_not_found", id="whitespace"),
        pytest.param("G/org/graph-model", "model_not_found", id="case-alias"),
        pytest.param("org/graph-model", "model_not_found", id="noncanonical"),
        pytest.param("g/org/unknown", "model_not_found", id="unknown-model"),
        pytest.param(f"g/{_PREREQ_LEAK}", "model_not_found", id="secretish-unknown"),
        pytest.param(None, "invalid_request", id="none"),
        pytest.param(42, "invalid_request", id="int"),
    ],
)
def test_unusable_graph_selector_fails_with_registry_code_and_guidance(
    prereq_world: Callable[..., _PrereqWorld], selector: object, code: str
) -> None:
    """Selector errors keep the registry code, name the fix, and resolve no credential."""
    world = prereq_world(selector=selector)
    error = _resolve_fails(world, code)
    assert "graph_memory.extraction_model" in str(error) or "/models" in str(error)
    assert world.credentials.calls == []


def test_graph_credential_failure_is_sanitized(prereq_world: Callable[..., _PrereqWorld]) -> None:
    """A raising graph credential resolver yields its safe registry code once."""
    world = prereq_world()
    world.credentials.fail = True
    _resolve_fails(world, "credential_resolution_failed")
    assert world.credentials.calls == ["g"]


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid-selector"])
def test_disabled_config_is_still_validated_when_invoked(
    prereq_world: Callable[..., _PrereqWorld], valid: bool
) -> None:
    """The enabled gate belongs to the caller; invoking the helper always validates."""
    if valid:
        _resolve_ok(prereq_world(enabled=False))
    else:
        _resolve_fails(prereq_world(enabled=False, selector="g/org/unknown"), "model_not_found")


@pytest.mark.parametrize(
    ("metadata", "unvalidated"),
    [
        pytest.param(_meta(4096, 1023), None, id="output-below-fixed-cap"),
        pytest.param(_meta(1024), None, id="no-headroom"),
        pytest.param(_meta(), None, id="no-limits"),
        pytest.param(
            None, ModelMetadata(context_tokens=4096, output_tokens="2048"), id="raw-string-output",  # type: ignore[arg-type]
        ),
        pytest.param(None, ModelMetadata(context_tokens=True), id="bool-context"),
    ],
)
def test_graph_prerequisites_have_no_budget_or_limit_gate(
    prereq_world: Callable[..., _PrereqWorld],
    metadata: ModelMetadata | None,
    unvalidated: ModelMetadata | None,
) -> None:
    """Discovered limits (even malformed) never fail graph prerequisites; no budget is computed."""
    result = _resolve_ok(prereq_world(metadata=metadata, unvalidated=unvalidated))
    assert not hasattr(result, "budget")


@pytest.mark.parametrize(
    "embeddings",
    [
        pytest.param(
            EmbeddingsConfig(base_url=_PREREQ_EMBED_URL, model="text-embed", api_key="env:PREREQ_EMBED_KEY"),
            id="provider-missing",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", base_url=_PREREQ_EMBED_URL, api_key="env:PREREQ_EMBED_KEY"),
            id="model-missing",
        ),
        pytest.param(
            EmbeddingsConfig(
                provider="anthropic", base_url=_PREREQ_EMBED_URL, model="text-embed", api_key="env:PREREQ_EMBED_KEY"
            ),
            id="unsupported-adapter",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", base_url=_PREREQ_EMBED_URL, model="text-embed"),
            id="api-key-missing",
        ),
        pytest.param(
            EmbeddingsConfig(provider="openai", model="text-embed", api_key="env:PREREQ_EMBED_KEY"),
            id="base-url-missing",
        ),
        pytest.param(
            EmbeddingsConfig(unavailable_reason="embeddings.base_url must be configured explicitly"),
            id="parse-unavailable",
        ),
        pytest.param(
            EmbeddingsConfig(
                provider="openai", model="text-embed", api_key="env:PREREQ_EMBED_KEY",
                base_url=f"https://user:{_PREREQ_LEAK}@embed.prereq.test/v1",
            ),
            id="invalid-endpoint",
        ),
    ],
)
def test_unavailable_embeddings_fail_actionably_and_close_client(
    prereq_world: Callable[..., _PrereqWorld], embeddings: EmbeddingsConfig
) -> None:
    """Embedding problems raise embedding_unavailable, name [embeddings], and close the client."""
    world = prereq_world(embeddings=embeddings)
    error = _resolve_fails(world, "embedding_unavailable")
    assert "embeddings" in str(error).lower()
    assert len(world.transports) == 1
    assert world.credentials.calls in ([], ["g"])


@pytest.mark.parametrize(
    ("embeddings", "connection_key"),
    [
        pytest.param(_DEFAULT_EMBEDDINGS, None, id="explicit-key-overrides-unavailable-connection-key"),
        pytest.param(_DEFAULT_EMBEDDINGS, _PREREQ_CONN_KEY, id="explicit-key-wins-over-set-connection-key"),
    ],
)
def test_embedding_credential_comes_only_from_embedding_config(
    prereq_world: Callable[..., _PrereqWorld],
    monkeypatch: pytest.MonkeyPatch,
    embeddings: EmbeddingsConfig,
    connection_key: str | None,
) -> None:
    """The explicit [embeddings] key is used; a chat connection key is never consulted."""
    world = prereq_world(embeddings=embeddings)
    if connection_key is not None:
        monkeypatch.setenv("PREREQ_CONN_KEY", connection_key)
    result = _resolve_ok(world)
    assert (result.embedding_provider, result.embedding_model) == ("openai", "text-embed")
