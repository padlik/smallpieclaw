"""Tests for LLMClient.embed_batch and provider batch embedding helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm_client import LLMClient
from providers._errors import LLMError
from providers import openai_provider, google_provider


def _make_client(provider: str = "openai") -> LLMClient:
    """Return an LLMClient with a single model and embeddings config."""
    cfg = {
        "models": [
            {
                "name": "test",
                "provider": provider,
                "model": "test-model",
                "api_key": "secret",
                "base_url": "https://api.example.com",
                "max_tokens": 1024,
                "temperature": 0.2,
                "request_timeout": 30,
                "max_retries": 1,
                "retry_delay": 0,
            }
        ],
        "agent": {"default_model": "test-model"},
        "embeddings": {
            "provider": provider,
            "model": "embed-model",
            "api_key": "secret",
            "base_url": "https://api.example.com",
        },
    }
    return LLMClient(cfg)


class TestLLMClientEmbedBatch:
    """Exercise LLMClient.embed_batch caching and provider routing."""

    def test_empty_list_returns_empty(self):
        client = _make_client()
        assert client.embed_batch([]) == []

    def test_openai_batch_returns_vectors_and_caches(self):
        client = _make_client("openai")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "data": [
                {"embedding": [0.1, 0.2]},
                {"embedding": [0.3, 0.4]},
            ]
        }
        fake_resp.raise_for_status.return_value = None

        with patch.object(client._http, "post", return_value=fake_resp) as mock_post:
            vectors = client.embed_batch(["hello", "world"])

        assert len(vectors) == 2
        assert vectors[0] == [0.1, 0.2]
        assert vectors[1] == [0.3, 0.4]
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["hello", "world"]
        # Second call should be served from cache
        with patch.object(client._http, "post", return_value=fake_resp) as mock_post2:
            vectors2 = client.embed_batch(["hello", "world"])
        mock_post2.assert_not_called()
        assert vectors2 == vectors

    def test_google_batch_returns_vectors(self):
        client = _make_client("google")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "embeddings": [
                {"values": [0.5, 0.6]},
                {"values": [0.7, 0.8]},
            ]
        }
        fake_resp.raise_for_status.return_value = None

        with patch.object(client._http, "post", return_value=fake_resp) as mock_post:
            vectors = client.embed_batch(["foo", "bar"])

        assert len(vectors) == 2
        assert vectors[0] == [0.5, 0.6]
        assert vectors[1] == [0.7, 0.8]
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["requests"] == [
            {"model": "embed-model", "content": {"parts": [{"text": "foo"}]}},
            {"model": "embed-model", "content": {"parts": [{"text": "bar"}]}},
        ]

    def test_unsupported_provider_falls_back_to_serial(self):
        client = _make_client("ollama")
        # ollama is not a native embeddings provider; LLMClient falls back to
        # openai_provider.embed for individual embed() calls, so we patch that.
        side_effects = [[0.1, 0.2], [0.3, 0.4]]
        with patch.object(
            client, "embed", side_effect=side_effects
        ) as mock_embed:
            vectors = client.embed_batch(["a", "b"])
        assert mock_embed.call_count == 2
        assert vectors == side_effects

    def test_partial_cache_hit_makes_network_only_for_missing(self):
        client = _make_client("openai")
        client._embed_cache["cached"] = [1.0, 2.0]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": [{"embedding": [3.0, 4.0]}]}
        fake_resp.raise_for_status.return_value = None

        with patch.object(client._http, "post", return_value=fake_resp) as mock_post:
            vectors = client.embed_batch(["cached", "new"])

        assert vectors == [[1.0, 2.0], [3.0, 4.0]]
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["new"]

    def test_mismatch_count_raises_llm_error(self):
        client = _make_client("openai")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
        fake_resp.raise_for_status.return_value = None

        with patch.object(client._http, "post", return_value=fake_resp):
            with pytest.raises(LLMError, match="1 vectors for 3 inputs"):
                client.embed_batch(["a", "b", "c"])


class TestOpenAIProviderEmbedBatch:
    """Direct tests for providers.openai_provider.embed_batch."""

    def test_returns_vectors_in_order(self):
        ctx = MagicMock()
        ctx.emb_cfg = {"api_key": "k", "model": "m", "base_url": "https://api.example.com"}
        ctx.max_retries = 1
        ctx.retry_delay = 0
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "data": [
                {"embedding": [0.1]},
                {"embedding": [0.2]},
                {"embedding": [0.3]},
            ]
        }
        fake_resp.raise_for_status.return_value = None
        ctx.http.post.return_value = fake_resp

        vectors = openai_provider.embed_batch(ctx, ["a", "b", "c"])
        assert vectors == [[0.1], [0.2], [0.3]]


class TestGoogleProviderEmbedBatch:
    """Direct tests for providers.google_provider.embed_batch."""

    def test_returns_vectors_in_order(self):
        ctx = MagicMock()
        ctx.emb_cfg = {"api_key": "k", "model": "models/text-embedding-004"}
        ctx.max_retries = 1
        ctx.retry_delay = 0
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "embeddings": [
                {"values": [0.1]},
                {"values": [0.2]},
            ]
        }
        fake_resp.raise_for_status.return_value = None
        ctx.http.post.return_value = fake_resp

        vectors = google_provider.embed_batch(ctx, ["x", "y"])
        assert vectors == [[0.1], [0.2]]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
