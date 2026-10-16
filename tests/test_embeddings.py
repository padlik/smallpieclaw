"""Tests for independent embedding delegation from the pinned chat client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from embedding_client import EmbeddingUnavailableError
from llm_client import LLMClient
from model_registry import ModelRecord, RunModel


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
