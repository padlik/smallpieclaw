"""Focused contract tests for the independent embedding client."""

from __future__ import annotations

import httpx
import pytest

import embedding_client
from config_schema import EmbeddingsConfig
from embedding_client import EmbeddingClient, EmbeddingUnavailableError


def _client(
    embedding: EmbeddingsConfig,
    *,
    http_client: httpx.Client | None = None,
) -> EmbeddingClient:
    return EmbeddingClient(embedding, http_client=http_client)


def test_openai_embeddings_are_explicit_and_do_not_inherit_chat_state() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    transport = httpx.MockTransport(handler)
    injected = httpx.Client(
        transport=transport,
        auth=("injected-user", "injected-password"),
        headers={"Authorization": "Bearer injected"},
        cookies={"session": "injected"},
        params={"injected": "yes"},
    )
    client = _client(
        EmbeddingsConfig(provider="openai", model="text-embedding-3-small", api_key="embedding-key", base_url="https://api.openai.com/v1"),
        http_client=injected,
    )
    try:
        assert client.embed("hello") == [0.1, 0.2]
    finally:
        client.close()
        injected.close()

    request = requests[0]
    assert str(request.url) == "https://api.openai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer embedding-key"
    assert "cookie" not in request.headers
    assert "injected" not in request.url.params
    assert request.read() == b'{"model":"text-embedding-3-small","input":"hello"}'


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (EmbeddingsConfig(), "explicitly"),
        (EmbeddingsConfig(provider="openai", model="embed", base_url="https://api.openai.com/v1"), "api_key"),
        (EmbeddingsConfig(provider="anthropic", model="embed", api_key="key", base_url="https://a.example.test"), "unsupported"),
        (EmbeddingsConfig(provider="unknown", model="embed", api_key="key", base_url="https://a.example.test"), "supported"),
        (EmbeddingsConfig(provider="openai", model="embed", api_key="key", base_url="file:///secret"), "URL"),
    ],
)
def test_unavailable_configuration_is_safe_and_fails_actionably(
    config: EmbeddingsConfig, reason: str
) -> None:
    client = _client(config)
    assert not client.available
    assert reason.lower() in client.unavailable_reason.lower()
    assert "secret" not in repr(client).lower()
    with pytest.raises(EmbeddingUnavailableError, match="embedding"):
        client.embed("not sent")
    client.close()


def test_client_takes_no_chat_connections_so_a_missing_key_cannot_be_borrowed() -> None:
    import inspect

    assert "providers" not in inspect.signature(EmbeddingClient).parameters
    client = _client(
        EmbeddingsConfig(provider="openai", model="embed-model", base_url="https://api.openai.com/v1")
    )
    assert not client.available
    assert "embeddings.api_key" in client.unavailable_reason
    client.close()


@pytest.mark.parametrize("source", ["env:EMBED_TEST_KEY", "sec:embed_test_key"])
def test_credential_references_resolve_only_inside_embedding_client(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBED_TEST_KEY", "fake-environment-secret")
    monkeypatch.setattr(
        embedding_client, "_load_vault", lambda _path: {"embed_test_key": "fake-vault-secret"}
    )
    requests: list[httpx.Request] = []
    injected = httpx.Client(transport=httpx.MockTransport(lambda request: (
        requests.append(request)
        or httpx.Response(200, json={"data": [{"embedding": [6.0]}]})
    )))
    client = EmbeddingClient(
        EmbeddingsConfig(provider="openai", model="embed", api_key=source, base_url="https://api.openai.com/v1"),
        vault_file="mock-vault-path",
        http_client=injected,
    )
    try:
        assert client.available
        assert client.embed("fake") == [6.0]
        assert "fake-environment-secret" not in repr(client)
        assert "fake-vault-secret" not in repr(client)
    finally:
        client.close()
        injected.close()
    assert requests[0].headers["authorization"] == "Bearer " + (
        "fake-environment-secret" if source.startswith("env:") else "fake-vault-secret"
    )


def test_cancelled_request_is_rejected_before_transport() -> None:
    import threading

    event = threading.Event()
    event.set()
    client = EmbeddingClient(
        EmbeddingsConfig(provider="openai", model="embed", api_key="key", base_url="https://api.openai.com/v1"),
        cancel_event=event,
    )
    with pytest.raises(EmbeddingUnavailableError, match="cancelled"):
        client.embed("not sent")
    client.close()


def test_google_batch_order_and_openai_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, json={"embeddings": [
                {"values": [1.0]}, {"values": [2.0]},
            ]})
        data = request.read().decode()
        if '"input":["a","b"]' in data:
            return httpx.Response(200, json={"data": [
                {"embedding": [3.0]}, {"embedding": [4.0]},
            ]})
        return httpx.Response(200, json={"data": [{"embedding": [5.0]}]})

    injected = httpx.Client(transport=httpx.MockTransport(handler))
    google = _client(
        EmbeddingsConfig(provider="google", model="gemini-embedding-001", api_key="google-key", base_url="https://generativelanguage.googleapis.com/v1beta"),
        http_client=injected,
    )
    openai = _client(
        EmbeddingsConfig(provider="openai", model="embed", api_key="openai-key", base_url="https://api.openai.com/v1"),
        http_client=injected,
    )
    try:
        assert google.embed_batch(["first", "second"]) == [[1.0], [2.0]]
        assert openai.embed_batch(["a", "b"]) == [[3.0], [4.0]]
        assert openai.embed("a") == [3.0]
        assert len(requests) == 2
    finally:
        google.close()
        openai.close()
        injected.close()
    assert str(requests[0].url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-embedding-001:batchEmbedContents"
    )
    assert requests[0].headers["x-goog-api-key"] == "google-key"


def test_empty_batch_returns_empty_without_request() -> None:
    requests: list[httpx.Request] = []
    injected = httpx.Client(transport=httpx.MockTransport(lambda request: (
        requests.append(request) or httpx.Response(200, json={"data": []})
    )))
    client = _client(
        EmbeddingsConfig(provider="openai", model="embed", api_key="key", base_url="https://api.openai.com/v1"), http_client=injected,
    )
    try:
        assert client.embed_batch([]) == []
    finally:
        client.close()
        injected.close()
    assert requests == []


def test_partial_cache_hit_requests_only_missing_inputs_in_order() -> None:
    import json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        bodies.append(body)
        return httpx.Response(200, json={"data": [
            {"embedding": [float(len(text))]} for text in body["input"]
        ]} if isinstance(body["input"], list) else {"data": [{"embedding": [9.0]}]})

    injected = httpx.Client(transport=httpx.MockTransport(handler))
    client = _client(
        EmbeddingsConfig(provider="openai", model="embed", api_key="key", base_url="https://api.openai.com/v1"), http_client=injected,
    )
    try:
        assert client.embed("cached") == [9.0]
        assert client.embed_batch(["new", "cached", "newer"]) == [[3.0], [9.0], [5.0]]
    finally:
        client.close()
        injected.close()
    assert [body["input"] for body in bodies] == ["cached", ["new", "newer"]]


def test_google_cross_origin_redirect_does_not_forward_credentials_or_input() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/collect"})

    injected = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    client = _client(
        EmbeddingsConfig(provider="google", model="gemini-embedding-001", api_key="google-secret", base_url="https://generativelanguage.googleapis.com/v1beta"),
        http_client=injected,
    )
    try:
        with pytest.raises(EmbeddingUnavailableError) as error:
            client.embed("private embedding input")
    finally:
        client.close()
        injected.close()

    assert str(error.value) == (
        "Embedding provider request failed; verify the configured embedding provider and model."
    )
    assert "google-secret" not in str(error.value)
    assert "private embedding input" not in str(error.value)
    assert len(requests) == 1
    assert requests[0].url.host == "generativelanguage.googleapis.com"
    assert requests[0].headers["x-goog-api-key"] == "google-secret"
    assert b"private embedding input" in requests[0].read()


def test_provider_errors_are_sanitized_and_bad_batch_cardinality_fails() -> None:
    injected = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": []})
    ))
    client = _client(
        EmbeddingsConfig(provider="openai", model="embed", api_key="top-secret", base_url="https://api.openai.com/v1"),
        http_client=injected,
    )
    try:
        with pytest.raises(Exception) as error:
            client.embed("x")
        assert "top-secret" not in str(error.value)
        with pytest.raises(Exception, match="count"):
            client.embed_batch(["x", "y"])
    finally:
        client.close()
        injected.close()
