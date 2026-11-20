"""Tests for the LongTermMemory → Graph backfill service.

Covers:
- LongTermMemory.entries() snapshot API
- backfill_longterm_to_graph() dry-run
- successful import into upsert_entity, add_relation, add_episode
- state-file skip (already-imported entries with matching checksum)
- --force flag reprocesses entries
- LLM extraction failure records "failed" and continues
- no_extraction path (no entities/facts returned) still writes episode
- short content is NOT filtered by min_message_length (backfill bypasses it)
"""

from __future__ import annotations

import builtins
import copy
import dataclasses
import json
import os
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, call

import httpx
import pytest

import backfill_graph_memory
import graph_memory
import model_discovery
import token_usage
from backfill_graph_memory import (
    _entry_checksum,
    _load_backfill_state,
    _save_backfill_state,
    backfill_longterm_to_graph,
)
from config_schema import (
    AgentConfig,
    AppConfig,
    EmbeddingsConfig,
    GraphMemoryConfig,
    ResolvedProviderConnection,
    parse_config,
)
from embedding_client import EmbeddingClient
from graph_memory import GraphPrerequisiteError, GraphPrerequisites
from llm_client import LLMClient
from memory_store import LongTermMemory
from model_registry import ModelMetadata, ModelRecord, ModelRegistry, RunModel
from providers._errors import LLMContextOverflowError, LLMError, LLMPermanentError
from xdg import xdg_paths


# ---------------------------------------------------------------------------
# build_extraction_llm_call — shared extraction LLM builder
# ---------------------------------------------------------------------------

_GRAPH_UPSTREAM = "vendor/graph-model"
_GRAPH_URL = "https://graph.example.test/v1/chat/completions"
_GRAPH_KEY = "fake-graph-key"
_EXTRACTION_REPLY = '{"entities":[],"relationships":[]}'
_GRAPH_WIRE_KEYS = {"model", "messages", "max_completion_tokens", "temperature"}


def _graph_pin() -> RunModel:
    """Return a settled graph pin with a slash-bearing upstream ID."""
    model = ModelRecord(
        "graph/vendor/graph-model",
        "graph",
        _GRAPH_UPSTREAM,
        "Graph Model",
        ModelMetadata(context_tokens=4096, output_tokens=2048),
    )
    connection = ResolvedProviderConnection(
        "graph", "openai", "https://graph.example.test/v1", 15, _GRAPH_KEY
    )
    return RunModel(model, connection, 1, 1)


def _graph_agent_config() -> AgentConfig:
    """Return a valid agent config whose local context differs from the graph cap."""
    return AgentConfig(ctx_max_tokens=3000, max_retries=0)


def _extraction_reply(request: httpx.Request) -> httpx.Response:
    """Return a successful OpenAI-compatible extraction response."""
    del request
    return httpx.Response(
        200, json={"choices": [{"message": {"content": _EXTRACTION_REPLY}}]}
    )


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


def _assert_graph_wire(request: httpx.Request, *, prompt: str = "extract") -> None:
    """Assert one sent request targets the pin, prompt, and fixed policy."""
    assert str(request.url) == _GRAPH_URL
    assert request.headers["authorization"] == f"Bearer {_GRAPH_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-goog-api-key" not in request.headers
    payload = json.loads(request.content)
    assert set(payload) == _GRAPH_WIRE_KEYS
    assert payload["model"] == _GRAPH_UPSTREAM
    assert payload["messages"] == [{"role": "user", "content": prompt}]
    assert payload["max_completion_tokens"] == 1024
    assert payload["temperature"] == 0.1


class TestBuildExtractionLLMCall:
    """Regression tests for the settled graph_memory.build_extraction_llm_call.

    After the 12.25 cutover the helper accepts a resolved ``RunModel`` and typed
    ``AgentConfig``, builds a distinct ``LLMClient.for_graph_extraction`` per
    call, calls ``chat(messages)`` only, returns the string, and closes the
    client in ``finally`` on both success and failure.  Both
    ``backfill_graph_memory._build_llm_call`` and ``main.py`` delegate to this
    helper.
    """

    def test_chat_called_without_unsupported_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """chat() gets messages only; result passes through; closure always closes."""
        from graph_memory import build_extraction_llm_call

        pin = _graph_pin()
        agent_config = _graph_agent_config()
        transports: list[_TrackingTransport] = []
        clients: list[LLMClient] = []
        close_calls: list[LLMClient] = []
        chat_calls: list[tuple[LLMClient, tuple[object, ...], dict[str, object]]] = []

        real_for_graph = LLMClient.for_graph_extraction
        real_chat = LLMClient.chat

        def spy_for_graph_extraction(*args: object, **kwargs: object) -> LLMClient:
            """Observe exact factory inputs and delegate with an external transport."""
            assert len(args) == 1
            assert args[0] is pin
            assert kwargs["agent_config"] is agent_config
            transport = _TrackingTransport(_extraction_reply)
            transports.append(transport)
            kwargs["http_transport"] = transport
            client = real_for_graph(*args, **kwargs)
            clients.append(client)
            real_close = client.close

            def close() -> None:
                """Record the production close before delegating to the real client."""
                close_calls.append(client)
                real_close()

            monkeypatch.setattr(client, "close", close)
            return client

        def spy_chat(
            client: LLMClient,
            *args: object,
            **kwargs: object,
        ) -> str:
            """Record the exact call shape without adding default arguments."""
            chat_calls.append((client, args, dict(kwargs)))
            return real_chat(client, *args, **kwargs)

        monkeypatch.setattr(LLMClient, "for_graph_extraction", spy_for_graph_extraction)
        monkeypatch.setattr(LLMClient, "chat", spy_chat)

        fn = build_extraction_llm_call(pin, agent_config)
        result = fn("some prompt")

        assert result == _EXTRACTION_REPLY
        assert len(transports) == 1
        assert transports[0].close_count == 1
        assert len(clients) == 1
        assert close_calls == clients
        assert clients[0]._closed
        assert len(chat_calls) == 1
        client, args, kwargs = chat_calls[0]
        assert client is clients[0]
        assert args == ([{"role": "user", "content": "some prompt"}],)
        assert kwargs == {}
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()

    def test_temperature_and_max_tokens_injected_into_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real fixed 0.1/1024 policy appears on the wire; pin/config stay immutable."""
        from graph_memory import build_extraction_llm_call

        pin = _graph_pin()
        agent_config = _graph_agent_config()
        transports: list[_TrackingTransport] = []
        clients: list[LLMClient] = []
        close_calls: list[LLMClient] = []

        real_for_graph = LLMClient.for_graph_extraction

        def spy_for_graph_extraction(*args: object, **kwargs: object) -> LLMClient:
            """Record each real client created from the exact extraction inputs."""
            assert len(args) == 1
            assert args[0] is pin
            assert kwargs["agent_config"] is agent_config
            transport = _TrackingTransport(_extraction_reply)
            transports.append(transport)
            kwargs["http_transport"] = transport
            client = real_for_graph(*args, **kwargs)
            clients.append(client)
            real_close = client.close

            def close() -> None:
                """Record the production close and close the real HTTP client."""
                close_calls.append(client)
                real_close()

            monkeypatch.setattr(client, "close", close)
            return client

        monkeypatch.setattr(LLMClient, "for_graph_extraction", spy_for_graph_extraction)

        fn = build_extraction_llm_call(pin, agent_config)
        fn("first")
        assert len(clients) == 1
        assert close_calls == clients
        assert clients[0]._closed
        fn("second")

        assert len(clients) == 2
        assert clients[0] is not clients[1]
        assert close_calls == clients
        assert all(client._closed for client in clients)
        assert len(transports) == 2
        assert transports[0] is not transports[1]
        assert transports[0].close_count == 1
        assert transports[1].close_count == 1
        _assert_graph_wire(transports[0].requests[0], prompt="first")
        _assert_graph_wire(transports[1].requests[0], prompt="second")
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()

    def test_failed_chat_closes_owned_client_and_transports_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A chat failure raises its typed error and the helper still closes the client."""
        from graph_memory import build_extraction_llm_call

        pin = _graph_pin()
        agent_config = _graph_agent_config()
        transports: list[_TrackingTransport] = []
        clients: list[LLMClient] = []
        close_calls: list[LLMClient] = []
        real_for_graph = LLMClient.for_graph_extraction

        def spy_for_graph_extraction(*args: object, **kwargs: object) -> LLMClient:
            """Delegate the real factory; only the external HTTP response fails."""
            assert len(args) == 1
            assert args[0] is pin
            assert kwargs["agent_config"] is agent_config
            transport = _TrackingTransport(lambda request: httpx.Response(401, text="denied"))
            transports.append(transport)
            kwargs["http_transport"] = transport
            client = real_for_graph(*args, **kwargs)
            clients.append(client)
            real_close = client.close

            def close() -> None:
                """Observe cleanup on the real typed-error path."""
                close_calls.append(client)
                real_close()

            monkeypatch.setattr(client, "close", close)
            return client

        monkeypatch.setattr(LLMClient, "for_graph_extraction", spy_for_graph_extraction)

        fn = build_extraction_llm_call(pin, agent_config)
        with pytest.raises(LLMPermanentError):
            fn("failing prompt")

        assert len(clients) == 1
        assert close_calls == clients
        assert clients[0]._closed
        assert len(transports) == 1
        assert transports[0].close_count == 1
        assert len(transports[0].requests) == 1
        _assert_graph_wire(transports[0].requests[0], prompt="failing prompt")
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()

    def test_backfill_delegate_calls_shared_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """backfill_graph_memory._build_llm_call delegates with caller_tag='backfill'."""
        from backfill_graph_memory import _build_llm_call

        pin = _graph_pin()
        agent_config = _graph_agent_config()
        captured: dict[str, Any] = {}
        real_helper = graph_memory.build_extraction_llm_call
        real_for_graph = LLMClient.for_graph_extraction
        transports: list[_TrackingTransport] = []
        clients: list[LLMClient] = []
        close_calls: list[LLMClient] = []

        def spy_helper(*args: object, **kwargs: object) -> Callable[[str], str]:
            """Observe actual backfill delegation without substituting its closure."""
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            return real_helper(*args, **kwargs)

        def spy_for_graph_extraction(*args: object, **kwargs: object) -> LLMClient:
            """Preserve the real factory with only its transport boundary replaced."""
            assert len(args) == 1
            assert args[0] is pin
            assert kwargs["agent_config"] is agent_config
            assert kwargs.get("caller_tag") == "backfill"
            transport = _TrackingTransport(_extraction_reply)
            transports.append(transport)
            kwargs["http_transport"] = transport
            client = real_for_graph(*args, **kwargs)
            clients.append(client)
            real_close = client.close

            def close() -> None:
                """Record the helper-owned real client close."""
                close_calls.append(client)
                real_close()

            monkeypatch.setattr(client, "close", close)
            return client

        monkeypatch.setattr("graph_memory.build_extraction_llm_call", spy_helper)
        monkeypatch.setattr(LLMClient, "for_graph_extraction", spy_for_graph_extraction)

        fn = _build_llm_call(pin, agent_config)
        assert len(captured["args"]) == 2
        assert captured["args"][0] is pin
        assert captured["args"][1] is agent_config
        assert captured["kwargs"] == {"caller_tag": "backfill"}
        assert fn("x") == _EXTRACTION_REPLY
        assert len(clients) == 1
        assert close_calls == clients
        assert clients[0]._closed
        assert len(transports) == 1
        assert transports[0].close_count == 1
        assert len(transports[0].requests) == 1
        _assert_graph_wire(transports[0].requests[0], prompt="x")
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()


# ---------------------------------------------------------------------------
# LongTermMemory.entries() snapshot
# ---------------------------------------------------------------------------

class TestLongTermMemoryEntries:
    @pytest.fixture
    def ltm(self, tmp_path):
        path = str(tmp_path / "ltm.json")
        return LongTermMemory(path=path)

    def test_empty_returns_empty_list(self, ltm):
        assert ltm.entries() == []

    def test_entries_returns_all_ids(self, ltm):
        id1 = ltm.add("fact one", source="manual")
        id2 = ltm.add("fact two", source="scheduled")
        pairs = ltm.entries()
        ids = [eid for eid, _ in pairs]
        assert id1 in ids
        assert id2 in ids
        assert len(pairs) == 2

    def test_entries_sorted_oldest_first(self, ltm):
        # Add with different timestamps by patching datetime inside memory_store
        ltm.add("first fact")
        ltm.add("second fact")
        pairs = ltm.entries()
        # Timestamps are ISO strings; first added should have an earlier timestamp
        # (both added in same second in practice — just assert count and types)
        assert len(pairs) == 2
        for eid, entry in pairs:
            assert isinstance(eid, str)
            assert "content" in entry
            assert "timestamp" in entry

    def test_entries_returns_shallow_copies(self, ltm):
        ltm.add("some content")
        pairs = ltm.entries()
        _, entry = pairs[0]
        # Mutating the returned dict must not affect internal state
        entry["content"] = "MUTATED"
        re_pairs = ltm.entries()
        _, re_entry = re_pairs[0]
        assert re_entry["content"] == "some content"

    def test_entries_preserves_source_and_timestamp(self, ltm):
        ltm.add("my fact", source="test_source")
        pairs = ltm.entries()
        _, entry = pairs[0]
        assert entry["source"] == "test_source"
        assert entry["timestamp"]  # not empty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestBackfillHelpers:
    def test_entry_checksum_is_stable(self):
        entry = {"content": "Hello", "source": "manual", "timestamp": "2024-01-01"}
        c1 = _entry_checksum(entry)
        c2 = _entry_checksum(entry)
        assert c1 == c2
        assert c1.startswith("sha256:")

    def test_entry_checksum_changes_on_content_change(self):
        e1 = {"content": "Hello", "source": "manual", "timestamp": "2024-01-01"}
        e2 = {"content": "World", "source": "manual", "timestamp": "2024-01-01"}
        assert _entry_checksum(e1) != _entry_checksum(e2)

    def test_load_backfill_state_missing_file(self, tmp_path):
        state = _load_backfill_state(str(tmp_path / "missing.json"))
        assert state == {"version": 1, "imported": {}}

    def test_load_backfill_state_corrupt_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            f.write("NOT JSON{{{")
        state = _load_backfill_state(path)
        assert state == {"version": 1, "imported": {}}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        data = {"version": 1, "imported": {"abc": {"checksum": "sha256:xyz", "episode_id": "ep:1"}}}
        _save_backfill_state(path, data)
        loaded = _load_backfill_state(path)
        assert loaded == data


# ---------------------------------------------------------------------------
# Fixtures shared by backfill tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_store():
    store = MagicMock()
    store.upsert_entity.side_effect = lambda name, etype, ts: f"ent:{name.lower()}:{etype}"
    store.add_relation.return_value = None
    store.add_episode.return_value = "ep:mock:001"
    return store


@pytest.fixture
def good_llm():
    """LLM callable that returns one entity and one fact."""
    def _llm(prompt: str) -> str:
        return json.dumps({
            "entities": [{"name": "Alice", "entity_type": "person"}],
            "facts": [{"source": "Alice", "target": "Python", "relation_type": "USES",
                       "fact": "Alice uses Python."}],
        })
    return _llm


@pytest.fixture
def empty_llm():
    """LLM callable that returns empty extraction."""
    return lambda _: '{"entities":[],"facts":[]}'


@pytest.fixture
def error_llm():
    """LLM callable that always raises."""
    def _llm(prompt: str) -> str:
        raise RuntimeError("LLM unavailable")
    return _llm


def _make_entries(content: str = "Alice uses Python in her projects.",
                  source: str = "manual",
                  timestamp: str = "2024-01-01T00:00:00") -> list[tuple[str, dict]]:
    return [("entry-001", {"content": content, "source": source, "timestamp": timestamp})]


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

class TestBackfillDryRun:
    def test_dry_run_returns_count_but_no_writes(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
            dry_run=True,
        )
        # Count is right
        assert result.total == 1
        assert result.imported == 1
        # No graph writes
        mock_store.upsert_entity.assert_not_called()
        mock_store.add_relation.assert_not_called()
        mock_store.add_episode.assert_not_called()
        # State file must NOT be written in dry-run
        assert not os.path.exists(state_path)

    def test_dry_run_with_limit(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            dry_run=True,
            limit=1,
        )
        assert result.imported == 1
        assert result.total == 2


# ---------------------------------------------------------------------------
# Successful import
# ---------------------------------------------------------------------------

class TestBackfillImport:
    def test_entities_and_facts_written(self, mock_store, good_llm, tmp_path):
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.imported == 1
        assert result.failed == 0
        # upsert_entity called for Alice (and auto-created Python)
        assert mock_store.upsert_entity.called
        assert mock_store.add_relation.called
        assert mock_store.add_episode.called

    def test_episode_source_is_backfill(self, mock_store, good_llm, tmp_path):
        backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        _, kwargs = mock_store.add_episode.call_args
        # source kwarg (or positional arg index 2)
        call_args = mock_store.add_episode.call_args
        # positional: (content, user_id, source) or via kwargs
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        assert "longterm_memory_backfill" in all_args

    def test_state_file_updated_after_import(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        state = _load_backfill_state(state_path)
        assert "entry-001" in state["imported"]
        imported = state["imported"]["entry-001"]
        assert imported["checksum"].startswith("sha256:")
        assert imported["episode_id"]
        assert imported["imported_at"]

    def test_total_counts_accumulated(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "content one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "content two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.total == 2
        assert result.imported == 2
        assert result.total_entities >= 2   # Alice from each batch
        assert result.total_facts >= 2      # one fact per batch


# ---------------------------------------------------------------------------
# Idempotency — skip already-imported entries
# ---------------------------------------------------------------------------

class TestBackfillIdempotency:
    def test_already_imported_entry_is_skipped(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        entries = _make_entries()
        # First run: import
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        # Second run: must skip
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        assert result.skipped == 1
        assert result.imported == 0
        mock_store.upsert_entity.assert_not_called()

    def test_force_reprocesses_already_imported(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        entries = _make_entries()
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
            force=True,
        )
        assert result.imported == 1
        assert result.skipped == 0
        assert mock_store.upsert_entity.called

    def test_changed_content_is_reprocessed(self, mock_store, good_llm, tmp_path):
        """An entry whose content changed since last import must be reprocessed."""
        state_path = str(tmp_path / "state.json")
        entries_v1 = [("e1", {"content": "original", "source": "manual", "timestamp": "2024-01-01"})]
        backfill_longterm_to_graph(
            long_term_entries=entries_v1,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        entries_v2 = [("e1", {"content": "changed content", "source": "manual", "timestamp": "2024-01-01"})]
        result = backfill_longterm_to_graph(
            long_term_entries=entries_v2,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        # Checksum differs → must reprocess
        assert result.imported == 1
        assert result.skipped == 0


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestBackfillFailures:
    def test_llm_failure_records_failed_and_continues(self, mock_store, error_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        # Second LLM call succeeds
        call_count = {"n": 0}
        def mixed_llm(prompt: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first call fails")
            return json.dumps({
                "entities": [{"name": "Bob", "entity_type": "person"}],
                "facts": [],
            })

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=mixed_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.failed == 1
        assert result.imported == 1  # second entry succeeds
        failed_entries = [er for er in result.entries if er.status == "failed"]
        assert len(failed_entries) == 1
        assert failed_entries[0].error != ""

    def test_no_extraction_still_writes_episode(self, mock_store, empty_llm, tmp_path):
        """When LLM returns empty JSON, we still store an episode."""
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=empty_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.no_extraction == 1
        assert result.imported == 0
        mock_store.add_episode.assert_called_once()
        # Source must still be backfill
        call_args = mock_store.add_episode.call_args
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        assert "longterm_memory_backfill" in all_args

    def test_short_content_not_filtered(self, mock_store, good_llm, tmp_path):
        """Backfill must not apply min_message_length — all content is imported."""
        short_entries = [("e1", {"content": "Hi", "source": "manual", "timestamp": "2024-01-01"})]
        result = backfill_longterm_to_graph(
            long_term_entries=short_entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        # Not failed/skipped — reached LLM extraction
        assert result.failed == 0
        assert result.skipped == 0

    def test_add_episode_failure_is_hard_failure_in_main_path(self, mock_store, good_llm, tmp_path):
        """If add_episode raises after entity/fact writes, the entry must be marked failed,
        not imported, so the next run re-attempts it."""
        mock_store.add_episode.side_effect = RuntimeError("DB write error")
        state_path = str(tmp_path / "state.json")

        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )

        assert result.failed == 1
        assert result.imported == 0
        # State file must NOT record this entry
        state = _load_backfill_state(state_path)
        assert "entry-001" not in state.get("imported", {})

    def test_add_episode_failure_no_extraction_path(self, mock_store, empty_llm, tmp_path):
        """In the no-extraction path, add_episode failure must also be a hard failure."""
        mock_store.add_episode.side_effect = RuntimeError("episode write error")
        state_path = str(tmp_path / "state.json")

        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=empty_llm,
            state_path=state_path,
        )

        assert result.failed == 1
        assert result.no_extraction == 0
        state = _load_backfill_state(state_path)
        assert "entry-001" not in state.get("imported", {})

    def test_state_save_failure_marks_entry_failed(self, mock_store, good_llm, tmp_path):
        """If the state file cannot be written after graph writes succeed,
        the entry must be marked failed (not imported) so the next run re-imports it."""
        from unittest.mock import patch
        state_path = str(tmp_path / "state.json")

        with patch("backfill_graph_memory._save_backfill_state", side_effect=OSError("disk full")):
            result = backfill_longterm_to_graph(
                long_term_entries=_make_entries(),
                store=mock_store,
                llm_call_fn=good_llm,
                state_path=state_path,
            )

        assert result.failed == 1
        assert result.imported == 0
        # Graph writes did happen (entities were upserted before the state-save attempt)
        assert mock_store.upsert_entity.called

    def test_state_save_failure_restores_prior_state(self, mock_store, good_llm, tmp_path):
        """Regression: when save fails for entry-1 and later succeeds for entry-2,
        entry-1's prior import record must still be present in the persisted state.

        The original bug: rollback did imported_map.pop(entry_id) which erased the
        prior record. Entry-2's subsequent successful save would then persist the map
        without entry-1, causing duplicate imports on the next run.
        """
        from unittest.mock import patch

        state_path = str(tmp_path / "state.json")
        entry1_id = "entry-001"
        entry1 = {"content": "Alice uses Python in her projects.", "source": "manual", "timestamp": "2024-01-01"}
        entry2_id = "entry-002"
        entry2 = {"content": "Bob uses Go for services.", "source": "manual", "timestamp": "2024-01-02"}

        prior_record = {
            "checksum": _entry_checksum(entry1),
            "episode_id": "ep-prior",
            "imported_at": "2024-01-01T00:00:00+00:00",
        }
        # Pre-seed a prior successful import for entry-1
        _save_backfill_state(state_path, {"version": 1, "imported": {entry1_id: prior_record}})

        # Save fails for entry-1, succeeds for entry-2 (None = no side effect = normal write)
        real_save = _save_backfill_state
        save_calls = []
        def patched_save(path, state):
            save_calls.append(len(state.get("imported", {})))
            if len(save_calls) == 1:
                raise OSError("disk full")
            real_save(path, state)

        with patch("backfill_graph_memory._save_backfill_state", side_effect=patched_save):
            result = backfill_longterm_to_graph(
                long_term_entries=[(entry1_id, entry1), (entry2_id, entry2)],
                store=mock_store,
                llm_call_fn=good_llm,
                state_path=state_path,
                force=True,
            )

        assert result.failed == 1   # entry-1 state save failed
        assert result.imported == 1  # entry-2 succeeded
        assert len(save_calls) == 2  # both entries attempted a save

        # After entry-2's successful save, state file must contain entry-1's PRIOR record
        loaded = _load_backfill_state(state_path)
        assert loaded["imported"].get(entry1_id) == prior_record, (
            "entry-1 prior record was erased by the failed-save rollback"
        )
        assert entry2_id in loaded["imported"], "entry-2 must be in state after successful save"

    def test_state_save_failure_restores_prior_state_no_extraction_path(
        self, mock_store, empty_llm, tmp_path
    ):
        """Same regression as above but via the no_extraction path (empty LLM response)."""
        from unittest.mock import patch

        state_path = str(tmp_path / "state.json")
        entry1_id = "entry-001"
        entry1 = {"content": "Alice uses Python in her projects.", "source": "manual", "timestamp": "2024-01-01"}
        entry2_id = "entry-002"
        entry2 = {"content": "Bob uses Go for services.", "source": "manual", "timestamp": "2024-01-02"}

        prior_record = {
            "checksum": _entry_checksum(entry1),
            "episode_id": "ep-prior-ne",
            "imported_at": "2024-01-01T00:00:00+00:00",
        }
        _save_backfill_state(state_path, {"version": 1, "imported": {entry1_id: prior_record}})

        real_save = _save_backfill_state
        save_calls = []
        def patched_save(path, state):
            save_calls.append(True)
            if len(save_calls) == 1:
                raise OSError("disk full")
            real_save(path, state)

        with patch("backfill_graph_memory._save_backfill_state", side_effect=patched_save):
            result = backfill_longterm_to_graph(
                long_term_entries=[(entry1_id, entry1), (entry2_id, entry2)],
                store=mock_store,
                llm_call_fn=empty_llm,
                state_path=state_path,
                force=True,
            )

        assert result.failed == 1
        assert result.no_extraction == 1
        loaded = _load_backfill_state(state_path)
        assert loaded["imported"].get(entry1_id) == prior_record, (
            "no_extraction path erased entry-1 prior record on failed save"
        )
        assert entry2_id in loaded["imported"]

    def test_limit_stops_early(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "two", "source": "manual", "timestamp": "2024-01-02"}),
            ("e3", {"content": "three", "source": "manual", "timestamp": "2024-01-03"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            limit=2,
        )
        assert result.imported + result.failed + result.no_extraction == 2


# ---------------------------------------------------------------------------
# Real extraction wire: per-entry failure isolation
# ---------------------------------------------------------------------------

_IMPORTED_REPLY = {
    "choices": [{"message": {"content": json.dumps({
        "entities": [{"name": "Bob", "entity_type": "person"}],
        "facts": [],
    })}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5},
}


def _imported_reply(request: httpx.Request) -> httpx.Response:
    """Return a successful extraction with one entity and real usage numbers."""
    del request
    return httpx.Response(200, json=_IMPORTED_REPLY)


def _context_overflow_reply(request: httpx.Request) -> httpx.Response:
    """Return a provider-side context-length rejection."""
    del request
    return httpx.Response(
        400, json={"error": {"message": "maximum context length exceeded: context_length_exceeded"}}
    )


def _rejected_reply(request: httpx.Request) -> httpx.Response:
    """Return a synthetic, permanent provider rejection (not a context overflow)."""
    del request
    return httpx.Response(
        400,
        json={"error": {"type": "invalid_request_error", "message": "synthetic rejection"}},
    )


def _unknown_output_graph_pin() -> RunModel:
    """Return the graph pin with its output ceiling left unknown (no invented value)."""
    pin = _graph_pin()
    metadata = dataclasses.replace(pin.model.metadata, output_tokens=None)
    return dataclasses.replace(pin, model=dataclasses.replace(pin.model, metadata=metadata))


@dataclasses.dataclass
class _WireObservations:
    """Real clients, transports, chat calls and typed errors seen by delegating spies."""

    transports: list[_TrackingTransport] = dataclasses.field(default_factory=list)
    clients: list[LLMClient] = dataclasses.field(default_factory=list)
    close_calls: list[LLMClient] = dataclasses.field(default_factory=list)
    chat_calls: list[tuple[LLMClient, tuple[object, ...], dict[str, object]]] = dataclasses.field(
        default_factory=list
    )
    chat_errors: list[tuple[LLMClient, LLMError]] = dataclasses.field(default_factory=list)
    usage_registry: token_usage.TokenUsageRegistry = dataclasses.field(
        default_factory=token_usage.TokenUsageRegistry
    )

    def prompt(self, index: int) -> str:
        """Return the prompt text handed to the indexed real chat call."""
        _, args, _ = self.chat_calls[index]
        return args[0][0]["content"]


def _observe_backfill_extraction(
    monkeypatch: pytest.MonkeyPatch,
    pin: RunModel,
    agent_config: AgentConfig,
    handlers: list[Callable[[httpx.Request], httpx.Response]],
) -> _WireObservations:
    """Delegate the real factory and chat; inject only the next mock HTTP transport."""
    seen = _WireObservations()
    # Isolate the real shared usage registry; monkeypatch restores the global.
    monkeypatch.setattr(token_usage, "_registry", seen.usage_registry)
    real_for_graph = LLMClient.for_graph_extraction
    real_chat = LLMClient.chat

    def spy_for_graph_extraction(*args: object, **kwargs: object) -> LLMClient:
        """Check the exact pinned inputs, then build the real client on a mock transport."""
        assert len(args) == 1
        assert args[0] is pin
        assert kwargs["agent_config"] is agent_config
        assert kwargs.get("caller_tag") == "backfill"
        assert "http_transport" not in kwargs
        transport = _TrackingTransport(handlers[len(seen.transports)])
        seen.transports.append(transport)
        kwargs["http_transport"] = transport
        client = real_for_graph(*args, **kwargs)
        seen.clients.append(client)
        real_close = client.close

        def close() -> None:
            """Record the helper-owned close before delegating to the real client."""
            seen.close_calls.append(client)
            real_close()

        monkeypatch.setattr(client, "close", close)
        return client

    def spy_chat(client: LLMClient, *args: object, **kwargs: object) -> str:
        """Record the exact call and any typed error, without altering either."""
        seen.chat_calls.append((client, args, dict(kwargs)))
        try:
            return real_chat(client, *args, **kwargs)
        except LLMError as exc:
            seen.chat_errors.append((client, exc))
            raise

    monkeypatch.setattr(LLMClient, "for_graph_extraction", spy_for_graph_extraction)
    monkeypatch.setattr(LLMClient, "chat", spy_chat)
    return seen


def _request_counts(seen: _WireObservations) -> list[int]:
    """Return the number of requests each owned transport actually received."""
    return [len(transport.requests) for transport in seen.transports]


def _effects_snapshot(
    seen: _WireObservations, store: MagicMock, state_path: str
) -> dict[str, object]:
    """Capture wire, usage, store and state effects at one point in the loop."""
    return {
        "clients": len(seen.clients),
        "requests": _request_counts(seen),
        "closed": [client._closed for client in seen.clients],
        "usage": [client.get_today_usage()["total_tokens"] for client in seen.clients],
        "store_calls": list(store.method_calls),
        "state_exists": os.path.exists(state_path),
        "global_usage": seen.usage_registry.get_today(),
    }


class TestBackfillExtractionWireIsolation:
    """Backfill over the real shared helper, real client and budget kernel.

    Only the provider HTTP transport and the graph DB store are substituted.
    Each case proves that one failed extraction stays contained to its entry
    while the next entry imports on the same immutable pin and fixed policy.
    """

    def test_oversized_source_failure_is_contained_per_entry(
        self, monkeypatch: pytest.MonkeyPatch, mock_store: MagicMock, tmp_path: Path
    ) -> None:
        """A huge source is sent unchecked; the provider's rejection fails only that entry."""
        pin = _graph_pin()
        agent_config = _graph_agent_config()
        state_path = str(tmp_path / "state.json")
        huge_body = " ".join(f"ledger{index} migrated" for index in range(6000))
        fit_content = "Bob reviews the graph migration plan."
        entries = [
            ("entry-huge", {"content": f"\n  {huge_body}  \n", "source": "manual",
                            "timestamp": "2024-01-01"}),
            ("entry-fit", {"content": fit_content, "source": "manual",
                           "timestamp": "2024-01-02"}),
        ]
        entries_before = copy.deepcopy(entries)
        seen = _observe_backfill_extraction(
            monkeypatch, pin, agent_config, [_context_overflow_reply, _imported_reply]
        )
        snapshots: list[dict[str, object]] = []

        def notify(current: int, total: int, result: object, entry_result: Any) -> None:
            """Snapshot all effects as each entry finishes."""
            del current, total, result
            snapshots.append(
                {"entry": entry_result.entry_id, "status": entry_result.status,
                 **_effects_snapshot(seen, mock_store, state_path)}
            )

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=backfill_graph_memory._build_llm_call(pin, agent_config),
            state_path=state_path,
            notify_fn=notify,
        )

        # The failed entry reached the provider once and produced no usage, store or state effect.
        assert snapshots[0] == {
            "entry": "entry-huge", "status": "failed", "clients": 1, "requests": [1],
            "closed": [True], "usage": [0], "store_calls": [], "state_exists": False,
            "global_usage": {},
        }
        # The whole original prompt reached the real chat, untruncated.
        assert len(seen.chat_calls) == 2
        for _, args, kwargs in seen.chat_calls:
            assert len(args) == 1
            assert kwargs == {}
        assert seen.prompt(0).startswith(graph_memory.EXTRACTION_PROMPT)
        assert "Long-term memory entry entry-huge" in seen.prompt(0)
        assert seen.prompt(0).endswith(huge_body)
        # Provider-reported overflow (no local fit check), fixed cap unlowered.
        assert len(seen.chat_errors) == 1
        failed_client, error = seen.chat_errors[0]
        assert failed_client is seen.clients[0]
        assert isinstance(error, LLMContextOverflowError)
        assert error.status_code == 400
        _assert_graph_wire(seen.transports[0].requests[0], prompt=seen.prompt(0))
        assert [(er.entry_id, er.status) for er in result.entries] == [
            ("entry-huge", "failed"), ("entry-fit", "imported"),
        ]
        assert result.entries[0].error == str(error)
        assert (result.total, result.imported, result.failed) == (2, 1, 1)
        # The next entry imports on the same pin with the fixed policy, once.
        assert _request_counts(seen) == [1, 1]
        _assert_graph_wire(seen.transports[1].requests[0], prompt=seen.prompt(1))
        assert seen.prompt(1).endswith(fit_content)
        assert [client.get_today_usage()["total_tokens"] for client in seen.clients] == [0, 16]
        assert seen.usage_registry.get_today() == {
            pin.model.id: {"prompt": 11, "completion": 5, "total": 16}
        }
        assert mock_store.add_episode.call_args_list == [
            call(fit_content, user_id="backfill", source="longterm_memory_backfill")
        ]
        assert set(_load_backfill_state(state_path)["imported"]) == {"entry-fit"}
        # One distinct real client per attempted extraction, all closed by the helper.
        assert len(seen.clients) == 2
        assert seen.clients[0] is not seen.clients[1]
        assert seen.close_calls == seen.clients
        assert all(client._closed for client in seen.clients)
        assert [transport.close_count for transport in seen.transports] == [1, 1]
        assert entries == entries_before
        assert pin == _graph_pin()
        assert agent_config == _graph_agent_config()

    def test_unknown_output_provider_rejection_is_contained_per_entry(
        self, monkeypatch: pytest.MonkeyPatch, mock_store: MagicMock, tmp_path: Path
    ) -> None:
        """An unknown-output pin sends once; a provider 400 fails only that entry."""
        pin = _unknown_output_graph_pin()
        agent_config = _graph_agent_config()
        state_path = str(tmp_path / "state.json")
        rejected_content = "Carol plans the catalog cutover."
        fit_content = "Bob reviews the graph migration plan."
        entries = [
            ("entry-rejected", {"content": rejected_content, "source": "manual",
                                "timestamp": "2024-01-01"}),
            ("entry-fit", {"content": fit_content, "source": "manual",
                           "timestamp": "2024-01-02"}),
        ]
        entries_before = copy.deepcopy(entries)
        seen = _observe_backfill_extraction(
            monkeypatch, pin, agent_config, [_rejected_reply, _imported_reply]
        )
        snapshots: list[dict[str, object]] = []

        def notify(current: int, total: int, result: object, entry_result: Any) -> None:
            """Snapshot all effects as each entry finishes."""
            del current, total, result
            snapshots.append(
                {"entry": entry_result.entry_id, "status": entry_result.status,
                 **_effects_snapshot(seen, mock_store, state_path)}
            )

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=backfill_graph_memory._build_llm_call(pin, agent_config),
            state_path=state_path,
            notify_fn=notify,
        )

        # Exactly one send for the rejected entry; no store, usage or state effect.
        assert snapshots[0] == {
            "entry": "entry-rejected", "status": "failed", "clients": 1, "requests": [1],
            "closed": [True], "usage": [0], "store_calls": [], "state_exists": False,
            "global_usage": {},
        }
        assert pin.model.metadata.output_tokens is None
        assert all(client.metadata.output_tokens is None for client in seen.clients)
        _assert_graph_wire(seen.transports[0].requests[0], prompt=seen.prompt(0))
        assert seen.prompt(0).startswith(graph_memory.EXTRACTION_PROMPT)
        assert seen.prompt(0).endswith(rejected_content)
        # The real typed provider error is recorded on that entry only.
        assert len(seen.chat_errors) == 1
        failed_client, error = seen.chat_errors[0]
        assert failed_client is seen.clients[0]
        assert isinstance(error, LLMPermanentError)
        assert not isinstance(error, LLMContextOverflowError)
        assert "invalid_request_error" in str(error)
        assert [(er.entry_id, er.status) for er in result.entries] == [
            ("entry-rejected", "failed"), ("entry-fit", "imported"),
        ]
        assert result.entries[0].error == str(error)
        assert (result.total, result.imported, result.failed) == (2, 1, 1)
        # No retry or alternate attempt; the next entry imports on the same pin.
        assert len(seen.chat_calls) == 2
        assert all(kwargs == {} for _, _, kwargs in seen.chat_calls)
        assert _request_counts(seen) == [1, 1]
        _assert_graph_wire(seen.transports[1].requests[0], prompt=seen.prompt(1))
        assert seen.prompt(1).endswith(fit_content)
        assert [client.get_today_usage()["total_tokens"] for client in seen.clients] == [0, 16]
        assert seen.usage_registry.get_today() == {
            pin.model.id: {"prompt": 11, "completion": 5, "total": 16}
        }
        assert mock_store.add_episode.call_args_list == [
            call(fit_content, user_id="backfill", source="longterm_memory_backfill")
        ]
        assert set(_load_backfill_state(state_path)["imported"]) == {"entry-fit"}
        assert len(seen.clients) == 2
        assert seen.clients[0] is not seen.clients[1]
        assert seen.close_calls == seen.clients
        assert all(client._closed for client in seen.clients)
        assert [transport.close_count for transport in seen.transports] == [1, 1]
        assert entries == entries_before
        assert pin == _unknown_output_graph_pin()
        assert agent_config == _graph_agent_config()


# ---------------------------------------------------------------------------
# notify_fn / progress callback
# ---------------------------------------------------------------------------


class TestBackfillProgress:
    """Verify the notify_fn callback contract for backfill_longterm_to_graph."""

    def test_notify_called_once_per_entry_all_imported(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
            ("e3", {"content": "fact three", "source": "manual", "timestamp": "2024-01-03"}),
        ]
        calls: list[tuple] = []
        def _notify(current, total, result, entry_result):
            calls.append((current, total, entry_result.entry_id, entry_result.status))

        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            notify_fn=_notify,
        )

        assert len(calls) == 3
        # current is 1-based and matches len(result.entries) at call time
        assert calls[0][0] == 1
        assert calls[1][0] == 2
        assert calls[2][0] == 3
        # total is always the full count of input entries
        assert all(total == 3 for _, total, _, _ in calls)
        # all entries imported successfully
        assert all(status == "imported" for _, _, _, status in calls)
        assert [eid for _, _, eid, _ in calls] == ["e1", "e2", "e3"]

    def test_notify_called_for_skipped_entries(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
        ]
        # First run to populate state
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )

        calls: list[str] = []
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
            notify_fn=lambda current, total, result, er: calls.append(er.status),
        )

        assert calls == ["skipped"]

    def test_notify_called_for_failed_entry(self, mock_store, error_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
        ]
        calls: list[str] = []
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=error_llm,
            state_path=str(tmp_path / "state.json"),
            notify_fn=lambda current, total, result, er: calls.append(er.status),
        )

        assert calls == ["failed"]

    def test_notify_called_for_dry_run(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        calls: list[str] = []
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            dry_run=True,
            notify_fn=lambda current, total, result, er: calls.append(er.status),
        )

        assert calls == ["imported (dry-run)", "imported (dry-run)"]

    def test_notify_receives_live_result_counters(self, mock_store, good_llm, tmp_path):
        """Each notify_fn call receives the result snapshot at that point in time."""
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        imported_counts: list[int] = []
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            notify_fn=lambda current, total, result, er: imported_counts.append(result.imported),
        )

        # After first import imported=1, after second imported=2
        assert imported_counts == [1, 2]

    def test_notify_none_does_not_raise(self, mock_store, good_llm, tmp_path):
        """Passing notify_fn=None (default) works without errors."""
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            notify_fn=None,
        )
        assert result.imported == 1


@dataclasses.dataclass
class _CLIWorld:
    """Files and entry metadata produced by ``TestBackfillMainLiveAndDryRun._write_world``."""

    config_path: Path
    entries: list[tuple[str, dict]]
    state_path: Path
    before_tree: dict[str, bytes | None]


class _FakeGraphStore:
    """Thread/file-free fake for the actual CLI ``GraphMemoryStore`` binding.

    Records the keyword arguments passed at construction and every store mutation
    so the live CLI path can be exercised without spinning up LadybugDB.
    """

    def __init__(
        self,
        *,
        db_path: str,
        embedder_fn: Callable[[str], list[float]],
        embedding_dim: int,
        buffer_pool_mb: int = 256,
    ) -> None:
        self.db_path = db_path
        self.embedder_fn = embedder_fn
        self.embedding_dim = embedding_dim
        self.buffer_pool_mb = buffer_pool_mb
        self.closed = False
        self.close_count = 0
        self.upserts: list[tuple[str, str, str | None]] = []
        self.relations: list[tuple[str, str, str, str, str | None]] = []
        self.episodes: list[tuple[str, str, str]] = []

    def upsert_entity(self, name: str, entity_type: str, ts: str | None = None) -> str:
        """Return a deterministic fake entity id."""
        self.upserts.append((name, entity_type, ts))
        return f"ent:{name.lower()}:{entity_type}"

    def add_relation(
        self,
        src_id: str,
        tgt_id: str,
        relation_type: str,
        fact: str,
        ts: str | None = None,
    ) -> None:
        """Record a relation write."""
        self.relations.append((src_id, tgt_id, relation_type, fact, ts))

    def add_episode(self, content: str, *, user_id: str, source: str) -> str:
        """Record an episode write and return a fake episode id."""
        self.episodes.append((content, user_id, source))
        return f"ep:fake:{len(self.episodes)}"

    def close(self) -> None:
        """Mark the store closed."""
        self.closed = True
        self.close_count += 1


class _RecordingRouter:
    """Routes fake provider traffic through the real ``httpx.HTTPTransport``.

    Replaces only ``httpx.HTTPTransport.handle_request`` so ``EmbeddingClient``
    and ``LLMClient`` still construct real ``httpx.Client`` instances. All
    unmatched requests are recorded and rejected with 404.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.unknown: list[httpx.Request] = []
        self.embed_status = 200
        self.embed_vector: list[float] = [0.1, 0.2, 0.3]
        self.chat_status = 200
        self.chat_content = '{"entities":[],"facts":[]}'

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request and serve provider-specific fake responses."""
        self.requests.append(request)
        url = request.url
        if url.host == "e.backfill.test" and url.path == "/v1/embeddings":
            if self.embed_status != 200:
                return httpx.Response(self.embed_status, text="embedding unavailable")
            inputs = json.loads(request.content)["input"]
            count = len(inputs) if isinstance(inputs, list) else 1
            return httpx.Response(200, json={
                "data": [{"index": index, "embedding": list(self.embed_vector)} for index in range(count)]
            })
        if url.host == "g.backfill.test" and url.path == "/v1/chat/completions":
            if self.chat_status != 200:
                return httpx.Response(self.chat_status, text="denied")
            return httpx.Response(200, json={"choices": [{"message": {"content": self.chat_content}}]})
        self.unknown.append(request)
        return httpx.Response(404, text=f"unexpected {url}")


class _ClientRecorder:
    """Record ``EmbeddingClient``/``LLMClient`` instances and their close counts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.embed_clients: list[EmbeddingClient] = []
        self.llm_clients: list[LLMClient] = []
        self.llm_models: list[RunModel] = []

        real_embed_init = EmbeddingClient.__init__
        recorder = self

        def spy_embed_init(self: EmbeddingClient, *args: object, **kwargs: object) -> None:
            real_embed_init(self, *args, **kwargs)
            self._recorded_close_count = 0
            real_close = self.close

            def counting_close() -> None:
                self._recorded_close_count += 1
                return real_close()

            self.close = counting_close
            recorder.embed_clients.append(self)

        real_llm_init = LLMClient.__init__

        def spy_llm_init(self: LLMClient, run_model: RunModel, *args: object, **kwargs: object) -> None:
            real_llm_init(self, run_model, *args, **kwargs)
            self._recorded_close_count = 0
            recorder.llm_clients.append(self)
            recorder.llm_models.append(run_model)
            real_close = self.close

            def counting_close() -> None:
                self._recorded_close_count += 1
                return real_close()

            self.close = counting_close

        monkeypatch.setattr(EmbeddingClient, "__init__", spy_embed_init)
        monkeypatch.setattr(LLMClient, "__init__", spy_llm_init)

    def close_counts(self) -> dict[str, int]:
        """Return total close calls per client family."""
        return {
            "embed": sum(getattr(c, "_recorded_close_count", 0) for c in self.embed_clients),
            "extract": sum(getattr(c, "_recorded_close_count", 0) for c in self.llm_clients),
        }

    def close_leftovers(self) -> None:
        """Defensively close any clients that the production code forgot."""
        for client in self.embed_clients + self.llm_clients:
            if client._recorded_close_count == 0:
                client.close()


# ---------------------------------------------------------------------------
# backfill_graph_memory.main() — live CLI + dry-run caller tests (Lane1)
# ---------------------------------------------------------------------------

class TestBackfillMainLiveAndDryRun:
    """Real CLI path exercises the planned live/dry pipeline.

    These tests target the source migration where ``main()``:
    - on ``--dry-run``, reads the raw TOML plus longterm/state file metadata and
      previews counts before provider/secret/env credential resolution, model
      pinning, catalog load or refresh, ladybug import, embedding/extraction
      client allocation, or DB creation;
    - on the live path, calls ``resolve_graph_startup`` first, then
      opens the independent embedding client, graph store, shared extraction
      helper, and runs the backfill engine;
    - closes all live clients on both success and error paths.

    Until that source migration lands these tests are expected RED.
    """

    _CLI_AGENT = "cli-agent"

    @staticmethod
    def _patch_cli_store(monkeypatch: pytest.MonkeyPatch, replacement: Callable[..., object]) -> None:
        """Patch the store's owning module and an existing eager CLI alias only."""
        monkeypatch.setattr(graph_memory, "GraphMemoryStore", replacement)
        if "GraphMemoryStore" in vars(backfill_graph_memory):
            monkeypatch.setattr(backfill_graph_memory, "GraphMemoryStore", replacement)

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes | None]:
        """Return every file's bytes below *root* (directories as None)."""
        tree: dict[str, bytes | None] = {}
        for entry in sorted(root.rglob("*")):
            key = entry.relative_to(root).as_posix()
            if entry.is_file():
                with entry.open("rb") as stream:
                    tree[key] = stream.read()
            else:
                tree[key] = None
        return tree

    def _write_world(
        self,
        tmp_xdg: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        missing_telegram_secret: bool = False,
        invalid_selector: bool = False,
        longterm_entries: list[tuple[str, dict]] | None = None,
        imported: dict[str, dict] | None = None,
    ) -> _CLIWorld:
        """Write config, optional vault, saved catalog and longterm/state files."""
        from xdg import xdg_paths

        paths = xdg_paths(self._CLI_AGENT)
        for directory in (paths.config_home, paths.data_home, paths.state_home):
            directory.mkdir(parents=True, exist_ok=True)

        adapter = "invalid-adapter" if invalid_selector else "openai"
        bot_token = (
            'bot_token = "sec:missing_telegram_token"\n'
            if missing_telegram_secret
            else 'bot_token = "plain-token"\n'
        )
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[telegram]\n"
            f"{bot_token}"
            "allowed_user_ids = [1]\n"
            "\n"
            "[agent]\n"
            "compaction_threshold_tokens = 3000\n"
            "\n"
            "[embeddings]\n"
            'provider = "openai"\n'
            'model = "text-embed"\n'
            'api_key = "env:BF_EMBED_KEY"\n'
            'base_url = "https://e.backfill.test/v1"\n'
            "\n"
            "[graph_memory]\n"
            "enabled = true\n"
            f'extraction_provider = "{adapter}"\n'
            'extraction_base_url = "https://g.backfill.test/v1"\n'
            'extraction_api_key = "env:BF_GRAPH_KEY"\n'
            'extraction_model = "org/graph-model"\n'
        )

        # Live path needs the agent vault to resolve the Telegram sec ref; dry-run
        # intentionally omits it to prove missing credentials do not block preview.
        if not missing_telegram_secret:
            paths.secrets_file.write_text('bot_token = "sec-resolved-token"\n')

        # Graph backfill reads no saved catalog: none is written for the live world.
        assert not (paths.config_home / "models-config.toml").exists()

        longterm_path = paths.data_home / "longterm_memory.json"
        ltm = LongTermMemory(path=str(longterm_path))
        entries = longterm_entries or [
            ("entry-a", {"content": "Alice uses Python.", "source": "manual"}),
            ("entry-b", {"content": "Bob uses Go.", "source": "scheduled"}),
        ]
        for _, entry in entries:
            ltm.add(entry["content"], source=entry.get("source", "manual"))

        # Persisted IDs are generated by LongTermMemory; tests must use real IDs.
        persisted = list(ltm.entries())

        state_path = paths.data_home / "graph_memory_backfill_state.json"
        _save_backfill_state(str(state_path), {"version": 1, "imported": imported or {}})

        monkeypatch.setenv("BF_GRAPH_KEY", _BF_GRAPH_KEY)
        monkeypatch.setenv("BF_EMBED_KEY", _BF_EMBED_KEY)
        monkeypatch.delenv("BF_CONN_KEY", raising=False)

        before_tree = self._tree_bytes(tmp_path)
        return _CLIWorld(config_path, persisted, state_path, before_tree)

    @staticmethod
    def _forbid_side_effects(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = False) -> list[str]:
        """Refuse discovery/settings writes, plus every expensive dry-run boundary."""
        import config_schema
        import embedding_client

        captured: list[str] = []

        def tripwire(label: str) -> Callable[..., object]:
            def tripped(*args: object, **kwargs: object) -> object:
                captured.append(label)
                raise AssertionError(f"{label} must not run")
            return tripped

        targets = [
            (model_discovery.ModelDiscovery, "fetch"),
            (ModelRegistry, "refresh"),
        ]
        if dry_run:
            targets.extend([
                (config_schema, "parse_config"),
                (config_schema, "resolve_provider_connection"),
                (config_schema, "_expand_value"),
                (embedding_client, "_resolve_credential"),
                (EmbeddingClient, "__init__"),
                (LLMClient, "__init__"),
                (LLMClient, "for_graph_extraction"),
                (httpx.Client, "__init__"),
                (graph_memory, "GraphMemoryStore"),
                (graph_memory, "build_extraction_llm_call"),
            ])
            if "GraphMemoryStore" in vars(backfill_graph_memory):
                targets.append((backfill_graph_memory, "GraphMemoryStore"))
            real_import = builtins.__import__

            def blocking_import(name: str, *args: object, **kwargs: object) -> Any:
                if name == "ladybug" or name.startswith("ladybug."):
                    return tripwire("ladybug import")()
                return real_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", blocking_import)
        for owner, name in targets:
            monkeypatch.setattr(owner, name, tripwire(f"{owner.__name__}.{name}"))
        return captured

    def test_dry_run_counts_despite_missing_telegram_secret_and_invalid_selector(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run previews counts before credential/catalog/DB work."""
        world = self._write_world(
            tmp_xdg, tmp_path, monkeypatch, missing_telegram_secret=True, invalid_selector=True
        )

        captured = self._forbid_side_effects(monkeypatch, dry_run=True)

        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_graph_memory.py",
                "--config", str(world.config_path),
                "--agent-name", self._CLI_AGENT,
                "--dry-run",
            ],
        )
        try:
            backfill_graph_memory.main()
        except SystemExit as exc:
            assert exc.code == 0

        assert captured == []
        assert self._tree_bytes(tmp_path) == world.before_tree

        out = capsys.readouterr().out
        assert "Total entries:      2" in out
        assert "Imported:           2" in out
        assert "DRY-RUN" in out

    def test_dry_run_preview_limit_force_checksum_and_no_writes(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run honors limit, force and cached-checksum skip; leaves files unchanged."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch, missing_telegram_secret=True)

        first_eid, first_entry = world.entries[0]
        first_checksum = _entry_checksum(first_entry)
        _save_backfill_state(
            str(world.state_path),
            {"version": 1, "imported": {first_eid: {"checksum": first_checksum}}},
        )

        before_tree = self._tree_bytes(tmp_path)

        captured = self._forbid_side_effects(monkeypatch, dry_run=True)

        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_graph_memory.py",
                "--config", str(world.config_path),
                "--agent-name", self._CLI_AGENT,
                "--dry-run",
                "--limit", "1",
            ],
        )
        try:
            backfill_graph_memory.main()
        except SystemExit as exc:
            assert exc.code == 0
        assert captured == []

        out = capsys.readouterr().out
        assert "Total entries:      2" in out
        assert "Imported:           1" in out
        assert "Skipped (cached):   1" in out
        after_tree = self._tree_bytes(tmp_path)
        assert after_tree == before_tree

        # With --force the cached entry is counted as would-import again.
        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_graph_memory.py",
                "--config", str(world.config_path),
                "--agent-name", self._CLI_AGENT,
                "--dry-run",
                "--limit", "1",
                "--force",
            ],
        )
        try:
            backfill_graph_memory.main()
        except SystemExit as exc:
            assert exc.code == 0
        assert captured == []
        out = capsys.readouterr().out
        assert "Imported:           1" in out
        assert "Skipped (cached):   0" in out
        assert self._tree_bytes(tmp_path) == before_tree

    def test_cold_start_dry_run_never_imports_ladybug(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh CLI process previews raw files without even attempting Ladybug import."""
        world = self._write_world(
            tmp_xdg, tmp_path, monkeypatch, missing_telegram_secret=True, invalid_selector=True,
        )
        script = Path(backfill_graph_memory.__file__).resolve()
        marker = "COLD_DRY_RUN_LADYBUG_IMPORT_ATTEMPT"
        bootstrap = textwrap.dedent('''\
            import builtins
            import runpy
            import sys

            original_import = builtins.__import__

            def guarded_import(name: str, *args: object, **kwargs: object) -> object:
                """Refuse Ladybug before any application module is imported."""
                if name == "ladybug" or name.startswith("ladybug."):
                    print("COLD_DRY_RUN_LADYBUG_IMPORT_ATTEMPT", file=sys.stderr, flush=True)
                    raise ImportError("Ladybug is forbidden during cold dry-run")
                return original_import(name, *args, **kwargs)

            def forbid_network(event: str, args: tuple[object, ...]) -> None:
                """Prevent accidental external traffic in the isolated CLI process."""
                if event in ("socket.connect", "socket.getaddrinfo"):
                    raise AssertionError("cold dry-run attempted network access")

            assert "graph_memory" not in sys.modules
            assert "backfill_graph_memory" not in sys.modules
            builtins.__import__ = guarded_import
            sys.addaudithook(forbid_network)
            sys.argv = sys.argv[1:]
            runpy.run_path(sys.argv[0], run_name="__main__")
            ''')
        completed = subprocess.run(
            [
                sys.executable, "-B", "-c", bootstrap, str(script),
                "--config", str(world.config_path), "--agent-name", self._CLI_AGENT, "--dry-run",
            ],
            cwd=script.parent,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert marker not in completed.stdout + completed.stderr
        assert "Total entries:      2" in completed.stdout
        assert "Imported:           2" in completed.stdout
        assert "DRY-RUN" in completed.stdout
        assert self._tree_bytes(tmp_path) == world.before_tree

    def test_live_path_validates_then_runs_extraction_and_closes_clients(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Live CLI validates purely, calls shared helper with fixed wire, and closes all clients."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)

        real_prepare = graph_memory.resolve_graph_startup
        real_helper = graph_memory.build_extraction_llm_call
        router = _RecordingRouter()
        router.chat_content = json.dumps({
            "entities": [
                {"name": "Alice", "entity_type": "person"},
                {"name": "Python", "entity_type": "tool"},
            ],
            "facts": [
                {"source": "Alice", "target": "Python", "relation_type": "USES", "fact": "Alice uses Python."}
            ],
        })
        store_instances: list[_FakeGraphStore] = []
        calls: dict[str, Any] = {"prepare": [], "build_extraction": []}
        recorder = _ClientRecorder(monkeypatch)

        def spy_prepare(app_cfg: AppConfig, *, vault_file: str | None = None) -> GraphPrerequisites:
            """Observe real prerequisites prepared inside the CLI invocation."""
            prepared = real_prepare(app_cfg, vault_file=vault_file)
            calls["prepare"].append((app_cfg, vault_file, prepared))
            assert recorder.embed_clients == []  # pure validation: no probe client
            assert router.requests == []
            assert store_instances == []
            return prepared

        def spy_helper(*args: object, **kwargs: object) -> Callable[[str], str]:
            """Observe, but never replace, the real shared extraction helper."""
            calls["build_extraction"].append((args, kwargs))
            return real_helper(*args, **kwargs)

        def spy_store(
            *,
            db_path: str,
            embedder_fn: Callable[[str], list[float]],
            embedding_dim: int,
            buffer_pool_mb: int = 256,
        ) -> _FakeGraphStore:
            """Record store construction and return the file-free fake."""
            store = _FakeGraphStore(
                db_path=db_path,
                embedder_fn=embedder_fn,
                embedding_dim=embedding_dim,
                buffer_pool_mb=buffer_pool_mb,
            )
            store_instances.append(store)
            return store

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request))
        monkeypatch.setattr(graph_memory, "resolve_graph_startup", spy_prepare)
        self._patch_cli_store(monkeypatch, spy_store)
        monkeypatch.setattr(graph_memory, "build_extraction_llm_call", spy_helper)
        forbidden = self._forbid_side_effects(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(world.config_path), "--agent-name", self._CLI_AGENT],
        )
        try:
            try:
                backfill_graph_memory.main()
            except SystemExit as exc:
                assert exc.code == 0
            assert len(recorder.embed_clients) == 1
            assert len(recorder.llm_clients) == 2
            assert len({id(client) for client in recorder.llm_clients}) == 2
            for client in recorder.embed_clients + recorder.llm_clients:
                assert client._recorded_close_count == 1
            assert len(store_instances) == 1
            assert store_instances[0].close_count == 1
        finally:
            recorder.close_leftovers()
            for store in store_instances:
                if not store.closed:
                    store.close()

        assert len(calls["prepare"]) == 1
        assert calls["prepare"][0][1] is not None  # vault_file passed
        assert len(calls["build_extraction"]) == 1
        app_cfg, vault_file, prereqs = calls["prepare"][0]
        assert not (xdg_paths(self._CLI_AGENT).config_home / "models-config.toml").exists()
        assert vault_file == str(xdg_paths(self._CLI_AGENT).secrets_file)
        helper_args, helper_kwargs = calls["build_extraction"][0]
        assert len(helper_args) == 2
        assert helper_args[0] is prereqs.pin
        assert helper_args[1] is app_cfg.agent
        assert helper_kwargs["caller_tag"] == "backfill"
        assert len(store_instances) == 1
        assert store_instances[0].closed
        assert store_instances[0].db_path == str(xdg_paths(self._CLI_AGENT).graph_memory_db)
        assert store_instances[0].embedding_dim == len(router.embed_vector)
        assert store_instances[0].buffer_pool_mb == app_cfg.graph_memory.buffer_pool_mb
        state_key = world.state_path.relative_to(tmp_path).as_posix()
        assert {k: v for k, v in self._tree_bytes(tmp_path).items() if k != state_key} == {
            k: v for k, v in world.before_tree.items() if k != state_key
        }
        assert forbidden == []
        assert router.unknown == []

        counts = recorder.close_counts()
        # One live embedding client; validation builds no probe.
        assert counts["embed"] == 1
        # One distinct extraction client per entry, each closed once.
        assert counts["extract"] == 2

        # Authorization and model routing must stay separate.
        embed_reqs = [r for r in router.requests if r.url.host == "e.backfill.test"]
        graph_reqs = [r for r in router.requests if r.url.host == "g.backfill.test"]
        assert embed_reqs
        assert len(graph_reqs) == 2
        for req in embed_reqs:
            assert req.headers.get("authorization") == f"Bearer {_BF_EMBED_KEY}"
            payload = json.loads(req.content)
            assert payload["model"] == "text-embed"
        for req in graph_reqs:
            assert req.headers.get("authorization") == f"Bearer {_BF_GRAPH_KEY}"
            payload = json.loads(req.content)
            assert payload["model"] == "org/graph-model"
            assert payload["temperature"] == 0.1
            assert payload["max_completion_tokens"] == 1024

        # Extraction clients were constructed with the preparation pin.
        for run_model in recorder.llm_models:
            assert run_model is prereqs.pin

        out = capsys.readouterr().out
        assert "Total entries:      2" in out
        assert "Imported:           2" in out
        assert len(store_instances[0].upserts) == 4
        assert len(store_instances[0].relations) == 2
        assert len(store_instances[0].episodes) == 2

        # Live state records real entry ids and checksums.
        state = _load_backfill_state(str(world.state_path))
        assert set(state["imported"]) == {eid for eid, _entry in world.entries}
        for eid, entry in world.entries:
            assert eid in state["imported"]
            assert state["imported"][eid]["checksum"] == _entry_checksum(entry)

    def test_live_path_helper_construction_failure_closes_store(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A helper-construction exception releases the already constructed CLI store."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)
        router = _RecordingRouter()
        recorder = _ClientRecorder(monkeypatch)
        stores: list[_FakeGraphStore] = []
        engine_calls: list[dict[str, object]] = []
        helper_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def store_factory(**kwargs: Any) -> _FakeGraphStore:
            """Replace only the DB boundary with the existing recording store."""
            store = _FakeGraphStore(**kwargs)
            stores.append(store)
            return store

        def fail_helper(*args: object, **kwargs: object) -> Callable[[str], str]:
            """Inject the fault after store acquisition, before engine execution."""
            helper_calls.append((args, dict(kwargs)))
            assert len(stores) == 1
            assert stores[0].close_count == 0
            assert len(recorder.embed_clients) == 1
            assert recorder.embed_clients[0]._recorded_close_count == 0
            raise RuntimeError("injected extraction helper construction failure")

        def forbid_engine(**kwargs: object) -> None:
            """Record and reject an engine call after the injected setup failure."""
            engine_calls.append(dict(kwargs))
            raise AssertionError("backfill engine must not run after helper construction failure")

        monkeypatch.setattr(
            httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request),
        )
        self._patch_cli_store(monkeypatch, store_factory)
        monkeypatch.setattr(backfill_graph_memory, "_build_llm_call", fail_helper)
        monkeypatch.setattr(backfill_graph_memory, "backfill_longterm_to_graph", forbid_engine)
        forbidden = self._forbid_side_effects(monkeypatch)
        monkeypatch.setattr(
            sys, "argv",
            [str(Path(backfill_graph_memory.__file__)), "--config", str(world.config_path),
             "--agent-name", self._CLI_AGENT],
        )
        try:
            with pytest.raises(RuntimeError, match="injected extraction helper construction failure"):
                backfill_graph_memory.main()
            assert len(helper_calls) == 1
            assert engine_calls == []
            assert len(stores) == 1
            assert len(recorder.embed_clients) == 1
            for client in recorder.embed_clients:
                assert client._recorded_close_count == 1
            assert recorder.llm_clients == []
            assert stores[0].close_count == 1
            assert stores[0].upserts == []
            assert stores[0].relations == []
            assert stores[0].episodes == []
            assert router.requests
            assert all(request.url.host == "e.backfill.test" for request in router.requests)
            assert router.unknown == []
            assert forbidden == []
            assert self._tree_bytes(tmp_path) == world.before_tree
        finally:
            recorder.close_leftovers()
            for store in stores:
                if not store.closed:
                    store.close()

    def test_live_path_prepare_failure_exits_before_db_http_and_state_writes(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable extraction credential fails validation before DB, HTTP or state writes."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)
        monkeypatch.delenv("BF_GRAPH_KEY")
        before_tree = self._tree_bytes(tmp_path)

        router = _RecordingRouter()
        recorder = _ClientRecorder(monkeypatch)
        real_prepare = graph_memory.resolve_graph_startup
        failures: list[GraphPrerequisiteError] = []

        def spy_prepare(*args: object, **kwargs: object) -> GraphPrerequisites:
            """Record the real failure from the unresolvable credential."""
            try:
                return real_prepare(*args, **kwargs)
            except GraphPrerequisiteError as exc:
                failures.append(exc)
                raise

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request))
        monkeypatch.setattr(graph_memory, "resolve_graph_startup", spy_prepare)
        forbidden = self._forbid_side_effects(monkeypatch)
        self._patch_cli_store(
            monkeypatch,
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("store must not init after prepare failure")),
        )
        monkeypatch.setattr(
            graph_memory,
            "build_extraction_llm_call",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("helper must not run after prepare failure")),
        )

        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(world.config_path), "--agent-name", self._CLI_AGENT],
        )
        try:
            with pytest.raises(SystemExit) as caught:
                backfill_graph_memory.main()
            assert caught.value.code != 0
            assert len(failures) == 1
            assert failures[0].code == "extraction_credential_unavailable"
            assert recorder.embed_clients == []
            assert recorder.llm_clients == []
            assert router.requests == []
            assert forbidden == []
            assert self._tree_bytes(tmp_path) == before_tree
        finally:
            recorder.close_leftovers()

    @pytest.mark.parametrize("failure", ["toml-syntax", "invalid-schema"])
    def test_live_path_ignores_corrupt_catalog_and_never_reads_it(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, failure: str,
    ) -> None:
        """Task 5.3: live backfill uses no saved catalog; corrupt catalog bytes are never read.

        A corrupt ``models-config.toml`` used to fail preparation with
        ``catalog_corrupt``.  Graph validation is now independent of it: the
        run succeeds, never constructs a ``ModelRegistry`` and
        leaves the file byte-identical."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)
        paths = xdg_paths(self._CLI_AGENT)
        corrupt = {
            "toml-syntax": f"schema_version = 1\nrevision = [\n# {_BF_LEAK}\n".encode(),
            "invalid-schema": f'schema_version = 1\nrevision = 1\napi_key = "{_BF_LEAK}"\n'.encode(),
        }
        legacy_catalog = paths.config_home / "models-config.toml"
        legacy_catalog.write_bytes(corrupt[failure])
        catalog_before = legacy_catalog.read_bytes()
        before_tree = self._tree_bytes(tmp_path)

        router = _RecordingRouter()
        recorder = _ClientRecorder(monkeypatch)
        constructed: list[str] = []

        def tripwire(label: str) -> Callable[..., object]:
            def tripped(*_a: object, **_k: object) -> object:
                constructed.append(label)
                raise AssertionError(f"{label} must not run for live backfill")
            return tripped

        monkeypatch.setattr(ModelRegistry, "__init__", tripwire("ModelRegistry"))
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request))
        stores: list[_FakeGraphStore] = []

        def store_factory(**kwargs: Any) -> _FakeGraphStore:
            store = _FakeGraphStore(**kwargs)
            stores.append(store)
            return store

        self._patch_cli_store(monkeypatch, store_factory)
        forbidden = self._forbid_side_effects(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(world.config_path), "--agent-name", self._CLI_AGENT],
        )
        try:
            try:
                backfill_graph_memory.main()
            except SystemExit as exc:
                assert exc.code == 0
            assert constructed == [] and forbidden == []
            assert len(stores) == 1 and stores[0].closed
            assert legacy_catalog.read_bytes() == catalog_before
            state_key = world.state_path.relative_to(tmp_path).as_posix()
            assert {k: v for k, v in self._tree_bytes(tmp_path).items() if k != state_key} == {
                k: v for k, v in before_tree.items() if k != state_key
            }
            logged = "\n".join(r.getMessage() for r in caplog.records)
            for secret in _BF_SECRETS:
                assert secret not in logged
        finally:
            recorder.close_leftovers()

    def test_live_path_embedding_unavailable_exits_before_graph_store(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Embedding 503 closes the embed client and never opens the store."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)

        real_prepare = graph_memory.resolve_graph_startup
        router = _RecordingRouter()
        router.embed_status = 503
        store_instances: list[_FakeGraphStore] = []
        recorder = _ClientRecorder(monkeypatch)

        def spy_prepare(app_cfg: AppConfig, *, vault_file: str | None = None) -> GraphPrerequisites:
            prepared = real_prepare(app_cfg, vault_file=vault_file)
            assert recorder.embed_clients == []  # pure validation: no probe client
            assert router.requests == []
            assert store_instances == []
            return prepared

        def spy_store(
            *,
            db_path: str,
            embedder_fn: Callable[[str], list[float]],
            embedding_dim: int,
            buffer_pool_mb: int = 256,
        ) -> _FakeGraphStore:
            store = _FakeGraphStore(
                db_path=db_path,
                embedder_fn=embedder_fn,
                embedding_dim=embedding_dim,
                buffer_pool_mb=buffer_pool_mb,
            )
            store_instances.append(store)
            return store

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request))
        monkeypatch.setattr(graph_memory, "resolve_graph_startup", spy_prepare)
        self._patch_cli_store(monkeypatch, spy_store)
        monkeypatch.setattr(
            graph_memory,
            "build_extraction_llm_call",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("helper must not run after embedding failure")),
        )
        forbidden = self._forbid_side_effects(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(world.config_path), "--agent-name", self._CLI_AGENT],
        )
        try:
            with pytest.raises(SystemExit) as caught:
                backfill_graph_memory.main()
            assert caught.value.code != 0
            assert len(recorder.embed_clients) == 1
            for client in recorder.embed_clients:
                assert client._recorded_close_count == 1
            assert recorder.llm_clients == []
            assert store_instances == []
            assert router.requests
            assert all(request.url.host == "e.backfill.test" for request in router.requests)
            for request in router.requests:
                assert request.headers["authorization"] == f"Bearer {_BF_EMBED_KEY}"
                assert json.loads(request.content)["model"] == "text-embed"
            assert router.unknown == []
            assert forbidden == []
            assert self._tree_bytes(tmp_path) == world.before_tree
        finally:
            recorder.close_leftovers()
            for store in store_instances:
                if not store.closed:
                    store.close()

    def test_live_path_extraction_failure_closes_all_clients(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two extraction 401 failures still close extraction and embed clients."""
        world = self._write_world(tmp_xdg, tmp_path, monkeypatch)

        real_prepare = graph_memory.resolve_graph_startup
        real_helper = graph_memory.build_extraction_llm_call
        router = _RecordingRouter()
        router.chat_status = 401
        store_instances: list[_FakeGraphStore] = []
        recorder = _ClientRecorder(monkeypatch)

        preparations: list[tuple[AppConfig, GraphPrerequisites]] = []
        helper_calls: list[tuple[tuple, dict]] = []

        def spy_prepare(app_cfg: AppConfig, *, vault_file: str | None = None) -> GraphPrerequisites:
            prepared = real_prepare(app_cfg, vault_file=vault_file)
            preparations.append((app_cfg, prepared))
            assert recorder.embed_clients == []  # pure validation: no probe client
            assert router.requests == []
            assert store_instances == []
            return prepared

        def spy_helper(*args: object, **kwargs: object) -> Callable[[str], str]:
            helper_calls.append((args, kwargs))
            return real_helper(*args, **kwargs)

        def spy_store(
            *,
            db_path: str,
            embedder_fn: Callable[[str], list[float]],
            embedding_dim: int,
            buffer_pool_mb: int = 256,
        ) -> _FakeGraphStore:
            store = _FakeGraphStore(
                db_path=db_path,
                embedder_fn=embedder_fn,
                embedding_dim=embedding_dim,
                buffer_pool_mb=buffer_pool_mb,
            )
            store_instances.append(store)
            return store

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _transport, request: router.handle_request(request))
        monkeypatch.setattr(graph_memory, "resolve_graph_startup", spy_prepare)
        self._patch_cli_store(monkeypatch, spy_store)
        monkeypatch.setattr(graph_memory, "build_extraction_llm_call", spy_helper)
        forbidden = self._forbid_side_effects(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(world.config_path), "--agent-name", self._CLI_AGENT],
        )
        try:
            with pytest.raises(SystemExit) as caught:
                backfill_graph_memory.main()
            assert caught.value.code != 0
            assert len(recorder.embed_clients) == 1
            assert len(recorder.llm_clients) == 2
            assert len({id(client) for client in recorder.llm_clients}) == 2
            for client in recorder.embed_clients + recorder.llm_clients:
                assert client._recorded_close_count == 1
            assert len(store_instances) == 1
            assert store_instances[0].close_count == 1
        finally:
            recorder.close_leftovers()
            for store in store_instances:
                if not store.closed:
                    store.close()

        assert len(store_instances) == 1
        assert store_instances[0].closed
        assert store_instances[0].upserts == []
        assert store_instances[0].relations == []
        assert store_instances[0].episodes == []

        counts = recorder.close_counts()
        assert counts["embed"] == 1
        assert counts["extract"] == 2  # two distinct extraction clients, each closed once

        assert len(preparations) == len(helper_calls) == 1
        app_cfg, prepared = preparations[0]
        helper_args, helper_kwargs = helper_calls[0]
        assert len(helper_args) == 2
        assert helper_args[0] is prepared.pin
        assert helper_args[1] is app_cfg.agent
        assert helper_kwargs["caller_tag"] == "backfill"
        assert all(pin is prepared.pin for pin in recorder.llm_models)
        assert forbidden == []
        assert router.unknown == []
        graph_requests = [request for request in router.requests if request.url.host == "g.backfill.test"]
        assert len(graph_requests) == 2
        for request in router.requests:
            payload = json.loads(request.content)
            if request.url.host == "g.backfill.test":
                assert request.headers["authorization"] == f"Bearer {_BF_GRAPH_KEY}"
                assert payload["model"] == "org/graph-model"
            else:
                assert request.headers["authorization"] == f"Bearer {_BF_EMBED_KEY}"
                assert payload["model"] == "text-embed"
        assert self._tree_bytes(tmp_path) == world.before_tree

        out = capsys.readouterr().out
        assert "Failed:             2" in out


# ---------------------------------------------------------------------------
# backfill_graph_memory.main() — vault_file wiring regression
# ---------------------------------------------------------------------------

class TestMainResolvesVaultFile:
    """main() must pass vault_file to parse_config so sec: refs resolve.

    Regression: parse_config() requires an explicit vault_file whenever the
    config contains a sec: reference (config_schema.py). main() must resolve
    it via xdg_paths(args.agent_name).secrets_file before the config with a
    sec:-referenced field (e.g. bot_token) can be parsed at all.
    """

    def test_sec_reference_resolves_via_agent_vault(self, tmp_xdg, tmp_path, monkeypatch, caplog):
        import logging

        import backfill_graph_memory
        from xdg import xdg_paths

        agent_name = "test-agent"
        paths = xdg_paths(agent_name)
        paths.state_home.mkdir(parents=True)
        paths.secrets_file.write_text('bot_token = "sec-resolved-token"\n')

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[telegram]\nbot_token = "sec:bot_token"\nallowed_user_ids = []\n'
            '[providers.openai-main]\ntype = "openai"\napi_key = "test-provider-key"\n'
            'base_url = "https://api.openai.com/v1"\n'
            "[graph_memory]\nenabled = false\n"
        )

        monkeypatch.setattr(
            "sys.argv",
            ["backfill_graph_memory.py", "--config", str(config_path), "--agent-name", agent_name],
        )
        # graph_memory.enabled = false → main() exits(1) right after parse_config
        # succeeds. A ConfigError from a missing vault_file would also exit(1),
        # so assert on the log message to prove we got past config parsing.
        with caplog.at_level(logging.ERROR, logger="backfill_graph_memory"):
            with pytest.raises(SystemExit):
                backfill_graph_memory.main()
        assert any("not enabled" in r.message for r in caplog.records)
        assert not any("vault file" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# resolve_graph_startup as used by the live backfill — explicit settings + vault
#
# The live CLI path validates the explicit ``[graph_memory]`` extraction fields
# and ``[embeddings]`` through ``resolve_graph_startup``: there is no saved
# catalog, registry or Main input. Only the environment, the vault file and the
# HTTP/constructor boundaries are external fakes; tripwires refuse any client,
# transport, store or writer.
# ---------------------------------------------------------------------------

_BF_GRAPH_KEY = "fake-backfill-graph-key"
_BF_EMBED_KEY = "fake-backfill-embed-key"
_BF_VAULT_GRAPH_KEY = "fake-backfill-vault-graph-key"
_BF_VAULT_EMBED_KEY = "fake-backfill-vault-embed-key"
_BF_LEAK = "sk-backfillleak12345"
_BF_SECRETS = (_BF_GRAPH_KEY, _BF_EMBED_KEY, _BF_VAULT_GRAPH_KEY, _BF_VAULT_EMBED_KEY, _BF_LEAK)
_BF_EMBEDDINGS = EmbeddingsConfig(
    provider="openai", model="text-embed", api_key="env:BF_EMBED_KEY", base_url="https://e.backfill.test/v1"
)


@dataclasses.dataclass
class _StartupWorld:
    """Typed config, a supplied vault file and the forbidden-effect log."""

    app_cfg: AppConfig
    vault_file: str
    tripwires: list[str]


@pytest.fixture
def startup_world(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _StartupWorld]:
    """Build explicit graph settings and a custom vault, then install tripwires."""

    def build(
        *,
        graph_key: str = "env:BF_GRAPH_KEY",
        embeddings: EmbeddingsConfig = _BF_EMBEDDINGS,
        enabled: bool = True,
    ) -> _StartupWorld:
        vault = tmp_xdg / "world" / "vaults" / "custom-vault.toml"
        vault.parent.mkdir(parents=True)
        vault.write_text(f'graph_key = "{_BF_VAULT_GRAPH_KEY}"\nembed_key = "{_BF_VAULT_EMBED_KEY}"\n')
        monkeypatch.setenv("HOME", str(tmp_xdg / "home"))
        monkeypatch.setenv("BF_GRAPH_KEY", _BF_GRAPH_KEY)
        monkeypatch.setenv("BF_EMBED_KEY", _BF_EMBED_KEY)
        base = parse_config({
            "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [12345]},
            "paths": {},
        })
        app_cfg = dataclasses.replace(
            base,
            embeddings=embeddings,
            graph_memory=GraphMemoryConfig(
                enabled=enabled,
                extraction_model="graph-model",
                extraction_provider="openai",
                extraction_base_url="https://g.backfill.test/v1",
                extraction_api_key=graph_key,
            ),
        )
        tripwires: list[str] = []

        def tripwire(name: str) -> Callable[..., None]:
            def tripped(*_args: object, **_kwargs: object) -> None:
                tripwires.append(name)
                raise AssertionError(f"{name} must not run during startup validation")
            return tripped

        monkeypatch.setattr(httpx, "Client", tripwire("httpx.Client"))
        for owner, name in (
            (EmbeddingClient, "__init__"),
            (LLMClient, "__init__"),
            (LLMClient, "for_graph_extraction"),
            (graph_memory.GraphMemoryWriter, "__init__"),
            (graph_memory, "create_graph_memory"),
            (graph_memory, "build_extraction_llm_call"),
            (backfill_graph_memory, "_save_backfill_state"),
        ):
            monkeypatch.setattr(owner, name, tripwire(name))
        monkeypatch.setattr(graph_memory, "GraphMemoryStore", tripwire("GraphMemoryStore"))
        if "GraphMemoryStore" in vars(backfill_graph_memory):
            monkeypatch.setattr(backfill_graph_memory, "GraphMemoryStore", tripwire("GraphMemoryStore"))
        return _StartupWorld(app_cfg, str(vault), tripwires)

    return build


def _startup_fails(world: _StartupWorld, code: str, *, vault_file: str | None = None) -> GraphPrerequisiteError:
    """Resolve startup expecting a safe ``GraphPrerequisiteError`` with ``code`` and no side effect."""
    with pytest.raises(GraphPrerequisiteError) as caught:
        graph_memory.resolve_graph_startup(world.app_cfg, vault_file=vault_file)
    error = caught.value
    assert error.code == code
    rendered = "".join(traceback.format_exception(error))
    for secret in _BF_SECRETS:
        assert secret not in rendered
    assert world.tripwires == []
    return error


class TestStartupResolutionForBackfill:
    """Explicit-settings resolution as the live backfill uses it."""

    def test_disabled_graph_memory_is_still_resolved(self, startup_world: Callable[..., _StartupWorld]) -> None:
        """The enabled gate belongs to the caller; invoking resolution always validates."""
        world = startup_world(enabled=False)
        result = graph_memory.resolve_graph_startup(world.app_cfg, vault_file=None)
        assert result.pin.model.id == "graph-model"
        assert result.pin.connection.api_key == _BF_GRAPH_KEY
        assert world.tripwires == []

    def test_supplied_vault_file_resolves_graph_and_embedding_secrets(
        self, startup_world: Callable[..., _StartupWorld]
    ) -> None:
        """sec: references resolve through the supplied vault, not a default home vault."""
        world = startup_world(
            graph_key="sec:graph_key",
            embeddings=EmbeddingsConfig(
                provider="openai", model="text-embed", api_key="sec:embed_key",
                base_url="https://e.backfill.test/v1",
            ),
        )
        result = graph_memory.resolve_graph_startup(world.app_cfg, vault_file=world.vault_file)
        assert result.pin.connection.api_key == _BF_VAULT_GRAPH_KEY
        assert result.pin.connection.base_url == "https://g.backfill.test/v1"
        assert (result.embedding_provider, result.embedding_model) == ("openai", "text-embed")
        assert world.tripwires == []

    @pytest.mark.parametrize("graph_key", ["env:BF_MISSING_VAR", "sec:graph_key"], ids=["env-unset", "sec-no-vault"])
    def test_graph_credential_failure_is_sanitized(
        self, startup_world: Callable[..., _StartupWorld], monkeypatch: pytest.MonkeyPatch, graph_key: str
    ) -> None:
        """Unresolvable graph credentials fail safely; no default vault is consulted."""
        monkeypatch.delenv("BF_MISSING_VAR", raising=False)
        world = startup_world(graph_key=graph_key)
        error = _startup_fails(world, "extraction_credential_unavailable")
        assert "credential" in str(error).lower()

    @pytest.mark.parametrize(
        "embeddings",
        [
            pytest.param(
                EmbeddingsConfig(provider="openai", api_key="env:BF_EMBED_KEY", base_url="https://e.backfill.test/v1"),
                id="model-missing",
            ),
            pytest.param(
                EmbeddingsConfig(provider="openai", model="text-embed", base_url="https://e.backfill.test/v1"),
                id="no-graph-credential-inheritance",
            ),
        ],
    )
    def test_unavailable_embeddings_fail_actionably(
        self, startup_world: Callable[..., _StartupWorld], embeddings: EmbeddingsConfig
    ) -> None:
        """Bad explicit embeddings fail with guidance and build no client."""
        error = _startup_fails(startup_world(embeddings=embeddings), "embedding_unavailable")
        assert "embeddings" in str(error).lower()
