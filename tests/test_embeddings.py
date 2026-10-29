"""Tests for independent embedding delegation from the pinned chat client."""

from __future__ import annotations

import copy
from contextlib import ExitStack
from unittest.mock import MagicMock

import httpx
import pytest

from config_schema import AgentConfig, AppConfig, ResolvedProviderConnection, parse_config
from embedding_client import EmbeddingClient, EmbeddingUnavailableError
from llm_client import LLMClient
from model_registry import ModelRecord, RunModel
from tool_index import ToolIndex
from tool_registry import Tool, ToolRegistry


def _make_client(embedding_client: MagicMock | None = None) -> LLMClient:
    """Construct a pinned chat client with independently injected embeddings."""
    model = ModelRecord("test/model", "test", "model", "Test model")
    connection = ResolvedProviderConnection(
        provider_id="test",
        adapter="openai",
        base_url="https://api.example.invalid/v1",
        request_timeout=30,
        api_key="chat-key",
    )
    run_model = RunModel(model, connection, catalog_revision=1, selection_revision=1)
    return LLMClient(run_model, agent_config=AgentConfig(), embedding_client=embedding_client)


def test_embed_delegates_without_changing_input_or_result() -> None:
    embedding = MagicMock()
    embedding.embed.return_value = [0.1, 0.2]
    client = _make_client(embedding)

    assert client.embed("embedding input") == [0.1, 0.2]
    embedding.embed.assert_called_once_with("embedding input")


def test_embed_batch_delegates_without_changing_input_or_result() -> None:
    embedding = MagicMock()
    embedding.embed_batch.return_value = [[0.1], [0.2]]
    client = _make_client(embedding)
    texts = ["first", "second"]

    assert client.embed_batch(texts) == [[0.1], [0.2]]
    embedding.embed_batch.assert_called_once_with(texts)


@pytest.mark.parametrize("method,args", [("embed", ("text",)), ("embed_batch", (["text"],))])
def test_missing_embedding_injection_fails_actionably(method: str, args: tuple) -> None:
    client = _make_client()

    with pytest.raises(EmbeddingUnavailableError, match="not configured"):
        getattr(client, method)(*args)


def test_closing_chat_client_does_not_close_borrowed_embedding_client() -> None:
    embedding = MagicMock()
    client = _make_client(embedding)

    client.close()

    embedding.close.assert_not_called()


# ---------------------------------------------------------------------------
# Parsed missing/invalid [embeddings] degrade the real client and tool index
# ---------------------------------------------------------------------------

_CHAT_KEY = "fake-valid-chat-key"
_EMBEDDING_CASES = {
    "missing": (None, "embeddings.provider and embeddings.model must be configured explicitly"),
    "invalid-provider": (
        {"provider": "not-a-provider", "model": "text-embed"},
        "embeddings.provider must name a supported adapter or configured provider connection",
    ),
}


def _parsed_config(minimal_config: dict, case: str) -> AppConfig:
    """Parse a provider-based config with a valid chat connection and the given embeddings."""
    raw = copy.deepcopy(minimal_config)
    raw.pop("models", None)
    raw["agent"].pop("default_model", None)
    raw["providers"] = {
        "chat": {"type": "openai", "api_key": _CHAT_KEY, "base_url": "https://chat.example.invalid/v1"}
    }
    section, _ = _EMBEDDING_CASES[case]
    if section is not None:
        raw["embeddings"] = section
    return parse_config(raw)


def _counting_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    """Return a real MockTransport that records and fails any request."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError(f"unexpected network I/O: {request.url}")

    return httpx.MockTransport(handler)


def _inventory_registry() -> MagicMock:
    """Return a ToolRegistry inventory standing in for upstream tool definitions."""
    registry = MagicMock(spec=ToolRegistry)
    registry.all.return_value = [
        Tool(name=name, language="python", description=f"{name} description")
        for name in ("alpha", "beta", "gamma")
    ]
    return registry


@pytest.mark.parametrize("case", sorted(_EMBEDDING_CASES))
def test_parsed_unavailable_embeddings_degrade_client_and_tool_index(
    minimal_config: dict, tmp_path, case: str
) -> None:
    """Missing or invalid explicit embeddings never borrow the valid chat connection."""
    app_cfg = _parsed_config(minimal_config, case)
    assert app_cfg.embeddings.unavailable_reason == _EMBEDDING_CASES[case][1]
    assert app_cfg.providers["chat"].api_key == _CHAT_KEY

    embedding_requests: list[httpx.Request] = []
    chat_requests: list[httpx.Request] = []
    with ExitStack() as stack:
        http = stack.enter_context(httpx.Client(transport=_counting_transport(embedding_requests)))
        embedding = EmbeddingClient(app_cfg.embeddings, app_cfg.providers, http_client=http)
        stack.callback(embedding.close)
        assert embedding.available is False
        assert embedding.unavailable_reason == "embedding configuration unavailable"
        assert _CHAT_KEY not in embedding.unavailable_reason
        assert embedding.model_name == ""

        model = ModelRecord("chat/model", "chat", "model", "Chat model")
        connection = ResolvedProviderConnection(
            "chat", "openai", "https://chat.example.invalid/v1", 30, _CHAT_KEY
        )
        client = LLMClient(
            RunModel(model, connection, 1, 1),
            agent_config=AgentConfig(max_retries=0, retry_delay=0),
            embedding_client=embedding,
            http_transport=_counting_transport(chat_requests),
        )
        stack.callback(client.close)
        with pytest.raises(EmbeddingUnavailableError, match="configure an explicit supported provider"):
            client.embed("text")
        with pytest.raises(EmbeddingUnavailableError, match="configure an explicit supported provider") as caught:
            client.embed_batch(["first", "second"])
        assert _CHAT_KEY not in str(caught.value)

        index_path = tmp_path / "tool_index.json"
        index = ToolIndex(_inventory_registry(), embedding, str(index_path))
        index.build()
        assert not index_path.exists()
        first = [tool.name for tool in index.search("query", top_k=2)]
        second = [tool.name for tool in index.search("other query", top_k=2)]
        assert first == second == ["alpha", "beta"]

    assert embedding_requests == []
    assert chat_requests == []
    assert http.is_closed
