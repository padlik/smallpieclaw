"""Task 5.1: ``[embeddings]`` requires all four explicit fields and never inherits.

Layers (all real code, only HTTP / UI / graph database are boundaries):

* ``parse_config``: each field x {missing, blank, whitespace, wrong type}
  degrades embeddings alone, names the field and leaks no value; chat
  structural faults still raise ``ConfigError``.
* ``EmbeddingClient`` over ``httpx.MockTransport``: exact host/key/model on the
  wire, no default endpoint, no chat connection inheritance.
* ``main._run`` through the root harness: chat stays usable with one field
  missing and no embedding or graph request is made; with all four fields the
  indexing and graph vector paths reach the same root client and only the
  embedding endpoint.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from config_schema import ConfigError, parse_config
from embedding_client import EmbeddingClient
from tests import root_startup_harness as rsh
from tests.root_startup_harness import (
    ACME_HOST,
    EMBED_HOST,
    GRAPH_HOST,
    SECRETS,
    RootHarness,
)

FIELDS = ("provider", "base_url", "api_key", "model")
_CHAT_KEY = "chat-connection-key-NEVER-FOR-EMBEDDINGS"
_CHAT_URL = "https://chat-connection.example.test/v1"
_FULL = {
    "provider": "openai",
    "base_url": "https://embed.example.test/v1",
    "api_key": "sec:embedding_key",
    "model": "embed-explicit",
}


def _raw(embeddings: Any = None, *, providers: dict | None = None) -> dict:
    """Return a minimal provider-based raw config with a same-type chat connection."""
    raw: dict[str, Any] = {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1]},
        "agent": {},
        "paths": {},
        "providers": providers
        if providers is not None
        else {"openai-main": {"type": "openai", "api_key": _CHAT_KEY, "base_url": _CHAT_URL}},
    }
    if embeddings is not None:
        raw["embeddings"] = embeddings
    return raw


# ---------------------------------------------------------------------------
# parse_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("defect", ["missing", "empty", "whitespace", "wrong-type"])
def test_each_embedding_field_is_required_and_named(field: str, defect: str) -> None:
    section = dict(_FULL)
    if defect == "missing":
        del section[field]
    else:
        section[field] = {"empty": "", "whitespace": "   ", "wrong-type": 7}[defect]

    cfg = parse_config(_raw(section), vault_file=None)

    assert cfg.embeddings.unavailable_reason
    assert f"embeddings.{field}" in cfg.embeddings.unavailable_reason
    for secret in ("embedding_key", "embed.example.test", "embed-explicit"):
        assert secret not in cfg.embeddings.unavailable_reason
        assert secret not in repr(cfg.embeddings)
    assert cfg.providers["openai-main"].api_key == _CHAT_KEY  # chat untouched


def test_all_missing_fields_are_listed_together() -> None:
    cfg = parse_config(_raw({"provider": "openai", "model": "embed-explicit"}))

    reason = cfg.embeddings.unavailable_reason
    assert "embeddings.base_url" in reason and "embeddings.api_key" in reason


@pytest.mark.parametrize("omitted", [("api_key",), ("base_url",), ("api_key", "base_url")])
def test_same_type_chat_connection_never_supplies_missing_fields(omitted: tuple[str, ...]) -> None:
    section = {k: v for k, v in _FULL.items() if k not in omitted}
    section["provider"] = "openai-main"  # a connection id, not an adapter type

    cfg = parse_config(_raw(section))

    assert cfg.embeddings.unavailable_reason
    assert cfg.embeddings.api_key == ""
    assert cfg.embeddings.base_url == ""
    assert _CHAT_KEY not in repr(cfg.embeddings) + cfg.embeddings.unavailable_reason
    assert _CHAT_URL not in repr(cfg.embeddings) + cfg.embeddings.unavailable_reason


def test_adapter_provider_with_matching_connection_does_not_inherit() -> None:
    section = {"provider": "openai", "model": "embed-explicit"}

    cfg = parse_config(_raw(section))

    assert "embeddings.api_key" in cfg.embeddings.unavailable_reason
    assert "embeddings.base_url" in cfg.embeddings.unavailable_reason
    assert cfg.embeddings.api_key == "" and cfg.embeddings.base_url == ""


def test_complete_explicit_fields_are_used_exactly_despite_conflicting_connection() -> None:
    cfg = parse_config(_raw(dict(_FULL)), vault_file=None)

    assert cfg.embeddings.unavailable_reason == ""
    assert cfg.embeddings.provider == "openai"
    assert cfg.embeddings.base_url == _FULL["base_url"]
    assert cfg.embeddings.api_key == _FULL["api_key"]  # unresolved reference
    assert cfg.embeddings.model == "embed-explicit"


def test_embedding_defects_do_not_hide_structural_chat_faults() -> None:
    bad_chat = _raw({"provider": "openai"}, providers={"x": {"type": "no-such-adapter"}})
    with pytest.raises(ConfigError):
        parse_config(bad_chat)


def test_missing_global_env_reference_still_fails_startup_with_degraded_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ABSENT_GLOBAL_5_1", raising=False)
    raw = _raw({"provider": "openai"})
    raw["telegram"]["bot_token"] = "env:ABSENT_GLOBAL_5_1"
    with pytest.raises(ConfigError):
        parse_config(raw)


# ---------------------------------------------------------------------------
# EmbeddingClient wire
# ---------------------------------------------------------------------------


def _transport(requests: list[httpx.Request], vector: list[float]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host.endswith("googleapis.test"):
            return httpx.Response(200, json={"embedding": {"values": vector}})
        return httpx.Response(200, json={"data": [{"embedding": vector}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_wire_uses_exactly_the_four_explicit_fields() -> None:
    from config_schema import EmbeddingsConfig

    requests: list[httpx.Request] = []
    http = _transport(requests, [0.5])
    client = EmbeddingClient(
        EmbeddingsConfig(
            provider="openai", base_url="https://own-embed.example.test/v9", api_key="own-key", model="own-model"
        ),
        http_client=http,
    )
    try:
        assert client.embed("x") == [0.5]
    finally:
        client.close()
        http.close()
    request = requests[0]
    assert str(request.url) == "https://own-embed.example.test/v9/embeddings"
    assert request.headers["authorization"] == "Bearer own-key"
    assert json.loads(request.content)["model"] == "own-model"


def test_google_wire_uses_explicit_local_style_endpoint_and_key() -> None:
    from config_schema import EmbeddingsConfig

    requests: list[httpx.Request] = []
    http = _transport(requests, [0.25])
    client = EmbeddingClient(
        EmbeddingsConfig(
            provider="google", base_url="https://embed.googleapis.test/v1beta", api_key="dummy-local", model="g-embed"
        ),
        http_client=http,
    )
    try:
        assert client.embed("x") == [0.25]
    finally:
        client.close()
        http.close()
    assert str(requests[0].url) == "https://embed.googleapis.test/v1beta/models/g-embed:embedContent"
    assert requests[0].headers["x-goog-api-key"] == "dummy-local"


@pytest.mark.parametrize("provider", ["openai", "openrouter", "google"])
def test_missing_base_url_is_unavailable_with_no_default_endpoint_and_no_request(provider: str) -> None:
    from config_schema import EmbeddingsConfig

    requests: list[httpx.Request] = []
    http = _transport(requests, [1.0])
    client = EmbeddingClient(
        EmbeddingsConfig(provider=provider, api_key="own-key", model="m"), http_client=http
    )
    try:
        assert not client.available
        assert "base_url" in client.unavailable_reason
        with pytest.raises(Exception, match="embedding"):
            client.embed("never sent")
    finally:
        client.close()
        http.close()
    assert requests == []


def test_client_without_key_or_url_is_unavailable_and_has_no_connection_input() -> None:
    import inspect

    from config_schema import EmbeddingsConfig

    assert "providers" not in inspect.signature(EmbeddingClient).parameters
    requests: list[httpx.Request] = []
    http = _transport(requests, [1.0])
    client = EmbeddingClient(EmbeddingsConfig(provider="openai", model="m"), http_client=http)
    try:
        assert not client.available
        assert "embeddings.base_url" in client.unavailable_reason
        assert "embeddings.api_key" in client.unavailable_reason
    finally:
        client.close()
        http.close()
    assert requests == []


# ---------------------------------------------------------------------------
# Actual root (main._run)
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Yield a root harness; leftovers are released only after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch)
    yield harness
    harness.release_leftovers()


def _chat_finish(harness: RootHarness) -> None:
    """Serve ordinary Main chat with a finish action and record the exchange."""
    harness.serve_provider_chat()
    served = harness._route

    def route(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.host == ACME_HOST and request.url.path == "/v1/messages":
            request.read()
            harness.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": json.dumps({"action": "finish", "result": "chat-ok"})}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            )
        return served(request)

    harness.monkeypatch.setattr(harness, "_route", route)


@pytest.fixture
def patch_config(monkeypatch: pytest.MonkeyPatch):
    """Return a helper that rewrites the harness config text for one test."""
    original = rsh._config_text

    def apply(mutate: Any) -> None:
        monkeypatch.setattr(
            rsh, "_config_text", lambda w, *, graph_enabled=False: mutate(original(w, graph_enabled=graph_enabled))
        )

    return apply


_LINES = {
    "provider": 'provider = "openai"\n',
    "model": 'model = "embed-small"\n',
    "api_key": 'api_key = "env:EMBED_KEY"\n',
    "base_url": f'base_url = "https://{EMBED_HOST}/v1"\n',
}


@pytest.mark.parametrize("field", FIELDS)
def test_root_chat_works_and_nothing_embeds_when_one_field_is_missing(
    root: RootHarness, patch_config: Any, caplog: pytest.LogCaptureFixture, field: str
) -> None:
    def drop(text: str) -> str:
        head, sep, tail = text.partition("[embeddings]\n")
        assert sep and _LINES[field] in tail
        return head + sep + tail.replace(_LINES[field], "", 1)

    patch_config(drop)
    _chat_finish(root)
    outcome: dict[str, Any] = {}

    def hold(harness: RootHarness) -> None:
        outcome["reply"] = harness.agent_handler(1, "hello", None, trace_id="r-00051001")

    with caplog.at_level(logging.WARNING):
        root.start(saved_default=None, write_cache=False, graph_enabled=True, hold=hold)

    assert outcome["reply"] == "chat-ok"
    assert [r for r in root.requests_to(ACME_HOST) if r.method == "POST"], "chat reached its own endpoint"
    assert root.requests_to(EMBED_HOST) == []
    assert [r for r in root.requests if r.url.path.endswith("/embeddings") or "embedContent" in r.url.path] == []
    assert not root.app_cfg.embeddings.unavailable_reason == ""
    assert f"embeddings.{field}" in root.app_cfg.embeddings.unavailable_reason
    assert root.graph_store_instances == [] and root.graph_writer_instances == []
    assert not root.root_embedding.available
    messages = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert f"embeddings.{field}" in messages
    for secret in SECRETS.values():
        assert secret not in messages
    assert root.unexpected == []


def test_root_connection_id_provider_never_borrows_a_same_type_chat_connection(
    root: RootHarness, patch_config: Any
) -> None:
    """``graph`` is an openai-type connection holding GRAPH_KEY at GRAPH_HOST."""

    def borrow(text: str) -> str:
        head, sep, tail = text.partition("[embeddings]\n")
        tail = tail.replace('provider = "openai"', 'provider = "graph"', 1)
        tail = tail.replace(_LINES["api_key"], "", 1).replace(_LINES["base_url"], "", 1)
        return head + sep + tail

    patch_config(borrow)
    root.start(saved_default=None, write_cache=False, graph_enabled=True)

    assert not root.root_embedding.available
    assert [r for r in root.requests_to(GRAPH_HOST) if r.url.path.endswith("/embeddings")] == []
    assert root.requests_to(EMBED_HOST) == []
    assert root.graph_store_instances == []


def test_root_valid_embeddings_serve_indexing_and_graph_vectors_from_one_client(root: RootHarness) -> None:
    seen: dict[str, Any] = {}

    def hold(harness: RootHarness) -> None:
        client = harness.root_embedding
        index = harness.tool_indexes[0]
        seen["same_index_client"] = index.llm is client
        seen["graph_embedder_owner"] = harness.graph_store.kwargs["embedder_fn"].__self__
        before = len(harness.requests_to(EMBED_HOST))
        index.search("a fresh indexing query")  # indexing code path
        harness.graph_store.kwargs["embedder_fn"]("a fresh graph vector text")  # graph vector path
        seen["new_requests"] = harness.requests_to(EMBED_HOST)[before:]
        seen["client"] = client

    root.start(saved_default=None, write_cache=False, graph_enabled=True, hold=hold)

    assert seen["same_index_client"] and seen["graph_embedder_owner"] is seen["client"]
    assert len(seen["new_requests"]) == 2
    embed_requests = root.requests_to(EMBED_HOST)
    assert embed_requests
    for request in embed_requests:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == f"Bearer {SECRETS['EMBED_KEY']}"
        assert json.loads(request.content)["model"] == "embed-small"
    others = [
        r for r in root.requests
        if r.url.host != EMBED_HOST
        and (r.url.path.endswith("/embeddings") or SECRETS["EMBED_KEY"] in str(r.headers))
    ]
    assert others == []
    for secret_name in ("KEY_ACME", "KEY_GEM", "GRAPH_KEY"):
        assert all(SECRETS[secret_name] not in str(r.headers) for r in embed_requests)
    assert root.unexpected == []
