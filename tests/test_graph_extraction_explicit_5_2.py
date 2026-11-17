"""Task 5.2: graph extraction requires its own explicit endpoint, credential and model.

Covers ``provider-credential-inheritance`` / "Graph extraction credentials are
independent" (ADR-0042): with graph memory enabled, ``[graph_memory]``
``extraction_provider``, ``extraction_base_url``, ``extraction_api_key`` and
``extraction_model`` are all required and nothing is inherited from chat
connections, the chat catalog, Main, or ``[embeddings]``.  Invalid extraction
settings disable graph memory only; chat and indexing keep working.

Canary credentials only; the HTTP boundary is a router patched onto the default
httpx transport, so real clients (``LLMClient``, ``EmbeddingClient``) and the
real ``GraphMemoryWriter`` send real requests to an allow-listed set of hosts.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import main as main_mod
from config_schema import (
    AppConfig,
    parse_config,
    resolve_provider_connection,
)
from embedding_client import EmbeddingClient
from exceptions import ConfigError
import graph_memory
from graph_memory import GraphPrerequisiteError
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, ModelRegistry, RunModel

CHAT_HOST = "chat.five2.test"
EMBED_HOST = "embed.five2.test"
GRAPH_HOST = "graph.five2.test"
CHAT_KEY = "chat-canary-key-aaa111"
EMBED_KEY = "embed-canary-key-bbb222"
GRAPH_KEY = "graph-canary-key-ccc333"
GRAPH_MODEL = "vendor/extract-1:latest"
ALL_CANARIES = (CHAT_KEY, EMBED_KEY, GRAPH_KEY)
FIELDS = ("extraction_provider", "extraction_base_url", "extraction_api_key", "extraction_model")

_EXTRACTION_REPLY = json.dumps({
    "entities": [{"name": "Alice", "entity_type": "person"}, {"name": "Python", "entity_type": "tool"}],
    "facts": [{"source": "Alice", "target": "Python", "relation_type": "USES", "fact": "Alice uses Python."}],
})


def _graph_section(**overrides: Any) -> dict[str, Any]:
    """Return a complete enabled ``[graph_memory]`` table; ``None`` removes a key."""
    section: dict[str, Any] = {
        "enabled": True,
        "extraction_provider": "openai",
        "extraction_base_url": f"https://{GRAPH_HOST}/v1",
        "extraction_api_key": GRAPH_KEY,
        "extraction_model": GRAPH_MODEL,
        "extract_every_n_turns": 1,
        "min_message_length": 5,
    }
    section.update(overrides)
    return {key: value for key, value in section.items() if value is not None}


def _raw_config(graph: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Valid chat + embeddings config whose chat/embedding values are all plausible fallbacks."""
    raw: dict[str, Any] = {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1], "security_mode": "allowlist"},
        "agent": {"compaction_threshold_tokens": 60000, "required_output_cap_tokens": 5000},
        "paths": {},
        "providers": {
            "chat": {"type": "openai", "api_key": CHAT_KEY, "base_url": f"https://{CHAT_HOST}/v1"},
        },
        "embeddings": {
            "provider": "openai",
            "model": "embed-small",
            "api_key": EMBED_KEY,
            "base_url": f"https://{EMBED_HOST}/v1",
        },
    }
    if graph is not None:
        raw["graph_memory"] = graph
    raw.update(extra)
    return copy.deepcopy(raw)


def _parse(graph: dict[str, Any] | None, **kwargs: Any) -> AppConfig:
    return parse_config(_raw_config(graph), **kwargs)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestExplicitExtractionConfig:
    def test_complete_settings_are_kept_verbatim(self) -> None:
        cfg = _parse(_graph_section(extraction_model="  org/Model:v1  ")).graph_memory
        assert cfg.extraction_unavailable_reason == ""
        assert cfg.extraction_provider == "openai"
        assert cfg.extraction_base_url == f"https://{GRAPH_HOST}/v1"
        assert cfg.extraction_api_key == GRAPH_KEY
        assert cfg.extraction_model == "  org/Model:v1  "
        assert GRAPH_KEY not in repr(cfg)
        assert GRAPH_HOST not in repr(cfg)

    @pytest.mark.parametrize("missing", FIELDS)
    def test_each_missing_field_disables_graph_and_names_only_the_field(self, missing: str) -> None:
        app_cfg = _parse(_graph_section(**{missing: None}))
        reason = app_cfg.graph_memory.extraction_unavailable_reason
        assert f"graph_memory.{missing}" in reason
        for other in FIELDS:
            if other != missing:
                assert f"graph_memory.{other}" not in reason
        # Chat connection and embeddings were fully usable fallbacks and stay untouched.
        assert app_cfg.providers["chat"].api_key == CHAT_KEY
        assert app_cfg.embeddings.unavailable_reason == ""
        assert app_cfg.graph_memory.extraction_api_key != CHAT_KEY
        assert app_cfg.graph_memory.extraction_api_key != EMBED_KEY

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("value", [123, ["x"], True, "", "   "], ids=["int", "list", "bool", "empty", "blank"])
    def test_wrong_type_or_blank_is_contained(self, field: str, value: object) -> None:
        reason = _parse(_graph_section(**{field: value})).graph_memory.extraction_unavailable_reason
        assert f"graph_memory.{field}" in reason

    @pytest.mark.parametrize(
        "overrides",
        [
            {"extraction_provider": "mystery-adapter"},
            {"extraction_provider": " openai"},
            {"extraction_base_url": "not a url"},
            {"extraction_base_url": "ftp://graph.five2.test"},
            {"extraction_api_key": "env:1-bad-name"},
            {"extraction_api_key": "sec:"},
            {"extraction_model": "env:FIVE2_UNSET_MODEL_VAR"},
            {"extraction_base_url": "env:FIVE2_UNSET_URL_VAR"},
        ],
    )
    def test_invalid_values_are_contained_and_never_echoed(
        self, overrides: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FIVE2_UNSET_MODEL_VAR", raising=False)
        monkeypatch.delenv("FIVE2_UNSET_URL_VAR", raising=False)
        reason = _parse(_graph_section(**overrides)).graph_memory.extraction_unavailable_reason
        (field, value), = overrides.items()
        assert f"graph_memory.{field}" in reason
        assert str(value) not in reason

    def test_secret_reference_is_not_resolved_at_parse_time(self) -> None:
        """A ``sec:`` credential needs no vault to parse; it is resolved only for the graph client."""
        cfg = _parse(_graph_section(extraction_api_key="sec:graph_key"), vault_file=None).graph_memory
        assert cfg.extraction_unavailable_reason == ""
        assert cfg.extraction_api_key == "sec:graph_key"

    def test_disabled_graph_ignores_unused_invalid_fields(self) -> None:
        graph = {
            "enabled": False,
            "extraction_provider": 5,
            "extraction_base_url": ["x"],
            "extraction_api_key": "sec:missing_everywhere",
            "extraction_model": "env:FIVE2_UNSET_MODEL_VAR",
        }
        cfg = _parse(graph).graph_memory
        assert cfg.enabled is False
        assert cfg.extraction_unavailable_reason == ""

    def test_structural_chat_error_still_fails_with_graph_fault(self) -> None:
        raw = _raw_config(_graph_section(extraction_api_key=None))
        raw["providers"]["chat"]["type"] = "no-such-adapter"
        with pytest.raises(ConfigError):
            parse_config(raw)

    def test_non_boolean_enabled_is_still_a_structural_error(self) -> None:
        with pytest.raises(ConfigError):
            _parse(_graph_section(enabled="yes"))


# ---------------------------------------------------------------------------
# Startup resolution
# ---------------------------------------------------------------------------


class TestResolveGraphStartup:
    def test_invalid_settings_fail_before_any_client_is_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("no client/registry may be touched for invalid extraction settings")

        monkeypatch.setattr(EmbeddingClient, "__init__", forbidden)
        monkeypatch.setattr(ModelRegistry, "resolve_graph_model", forbidden)
        app_cfg = _parse(_graph_section(extraction_api_key=None))
        with pytest.raises(GraphPrerequisiteError) as info:
            graph_memory.resolve_graph_startup(app_cfg, vault_file=None)
        assert "graph_memory.extraction_api_key" in str(info.value)

    def test_unresolvable_credential_reference_is_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIVE2_GRAPH_SECRET_VAR", raising=False)
        app_cfg = _parse(_graph_section(extraction_api_key="env:FIVE2_GRAPH_SECRET_VAR"))
        assert app_cfg.graph_memory.extraction_unavailable_reason == ""
        with pytest.raises(GraphPrerequisiteError) as info:
            graph_memory.resolve_graph_startup(app_cfg, vault_file=None)
        assert "graph_memory.extraction_api_key" in str(info.value)
        assert "FIVE2_GRAPH_SECRET_VAR" not in str(info.value)

    def test_valid_settings_build_a_private_pin_without_a_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIVE2_GRAPH_SECRET_VAR", GRAPH_KEY)
        monkeypatch.setattr(
            ModelRegistry, "resolve_graph_model",
            lambda *_a, **_k: pytest.fail("graph extraction must not consult the catalog"),
        )
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_a, **_k: httpx.Response(200, json={}))
        app_cfg = _parse(_graph_section(extraction_api_key="env:FIVE2_GRAPH_SECRET_VAR"))
        prepared = graph_memory.resolve_graph_startup(app_cfg, vault_file=None)
        pin = prepared.pin
        assert pin.model.upstream_id == GRAPH_MODEL
        assert pin.connection.adapter == "openai"
        assert pin.connection.base_url == f"https://{GRAPH_HOST}/v1"
        assert pin.connection.api_key == GRAPH_KEY
        assert pin.connection.provider_id != "chat"


# ---------------------------------------------------------------------------
# Wire isolation through the real startup path and real clients
# ---------------------------------------------------------------------------


class _Wire:
    """In-process router installed on the default httpx transport."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        wire = self

        def handle(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            request.read()
            wire.requests.append(request)
            host, path = request.url.host, request.url.path
            if host == EMBED_HOST and path == "/v1/embeddings":
                body = json.loads(request.content)
                inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
                return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in inputs]})
            if host == GRAPH_HOST and path == "/v1/chat/completions":
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": _EXTRACTION_REPLY}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                })
            if host == CHAT_HOST and path == "/v1/chat/completions":
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "chat ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                })
            wire.unexpected.append(f"{request.method} {request.url}")
            return httpx.Response(404, json={})

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    return _Wire(monkeypatch)


def _build(app_cfg: AppConfig, tmp_path: Path) -> tuple[Any, Any, SimpleNamespace, SimpleNamespace]:
    """Run the real ``_build_graph_memory`` with a real embedding client and tmp DB."""
    embed_client = EmbeddingClient(app_cfg.embeddings)
    agent = SimpleNamespace()
    builtin = SimpleNamespace()
    paths = SimpleNamespace(graph_memory_db=tmp_path / "graph_db")
    store, writer = main_mod._build_graph_memory(
        app_cfg=app_cfg,
        paths=paths,
        agent=agent,
        builtin=builtin,
        vault_file=str(tmp_path / "no-vault"),
        embedder_fn=embed_client.embed,
    )
    return store, writer, agent, builtin


def _chat_ok(app_cfg: AppConfig) -> str:
    """Ordinary chat through the chat connection's own endpoint/key."""
    connection = resolve_provider_connection(app_cfg.providers["chat"], vault_file=None)
    record = ModelRecord("chat/m", "chat", "m", "m", ModelMetadata())
    client = LLMClient(RunModel(record, connection, 1, 0), agent_config=app_cfg.agent)
    try:
        return client.chat([{"role": "user", "content": "hi"}])
    finally:
        client.close()


class TestExtractionWireIsolation:
    def test_extraction_uses_only_its_own_endpoint_key_model_and_fixed_cap(
        self, wire: _Wire, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ModelRegistry, "resolve_graph_model",
            lambda *_a, **_k: pytest.fail("graph extraction must not consult the catalog"),
        )
        app_cfg = _parse(_graph_section())
        store, writer, agent, builtin = _build(app_cfg, tmp_path)
        assert store is not None and writer is not None
        try:
            assert agent._graph_memory is store and builtin._graph_memory_writer is writer
            writer.enqueue("Alice uses Python for scripting every day.")
            writer.stop()
        finally:
            store.close()

        graph_calls = wire.to(GRAPH_HOST)
        assert len(graph_calls) >= 1
        for request in graph_calls:
            assert request.headers["authorization"] == f"Bearer {GRAPH_KEY}"
            body = json.loads(request.content)
            assert body["model"] == GRAPH_MODEL
            # agent.required_output_cap_tokens = 5000 is NOT inherited: fixed graph wire cap.
            assert body["max_completion_tokens"] == 1024
        assert wire.to(CHAT_HOST) == []
        assert wire.unexpected == []
        # Shared embeddings: vectors used their own host/key/model, never graph's or chat's.
        embed_calls = wire.to(EMBED_HOST)
        assert len(embed_calls) >= 2  # dimension probe + entity vectors
        for request in embed_calls:
            assert request.headers["authorization"] == f"Bearer {EMBED_KEY}"
            assert json.loads(request.content)["model"] == "embed-small"
        for request in wire.requests:
            sent = f"{request.headers} {request.content!r}"
            if request.url.host != GRAPH_HOST:
                assert GRAPH_KEY not in sent
            if request.url.host != EMBED_HOST:
                assert EMBED_KEY not in sent
            assert CHAT_KEY not in sent

    @pytest.mark.parametrize("missing", FIELDS)
    def test_missing_field_disables_graph_while_chat_and_indexing_work(
        self, missing: str, wire: _Wire, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app_cfg = _parse(_graph_section(**{missing: None}))
        with caplog.at_level(logging.WARNING):
            store, writer, agent, builtin = _build(app_cfg, tmp_path)
        assert (store, writer) == (None, None)
        assert not hasattr(agent, "_graph_memory") and not hasattr(builtin, "_graph_memory")
        assert not (tmp_path / "graph_db").exists()
        warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert f"graph_memory.{missing}" in warnings
        for canary in ALL_CANARIES:
            assert canary not in warnings
        # Nothing left the process for graph memory...
        assert wire.requests == []
        # ...while ordinary chat and embedding/indexing keep working on their own routes.
        assert _chat_ok(app_cfg) == "chat ok"
        assert EmbeddingClient(app_cfg.embeddings).embed("index me") == [0.1, 0.2, 0.3, 0.4]
        assert wire.to(CHAT_HOST)[0].headers["authorization"] == f"Bearer {CHAT_KEY}"
        assert wire.to(EMBED_HOST)[0].headers["authorization"] == f"Bearer {EMBED_KEY}"
        assert wire.to(GRAPH_HOST) == [] and wire.unexpected == []

    def test_unresolvable_credential_reference_disables_graph_without_fallback(
        self, wire: _Wire, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FIVE2_GRAPH_SECRET_VAR", raising=False)
        app_cfg = _parse(_graph_section(extraction_api_key="env:FIVE2_GRAPH_SECRET_VAR"))
        with caplog.at_level(logging.WARNING):
            store, writer, _agent, _builtin = _build(app_cfg, tmp_path)
        assert (store, writer) == (None, None)
        warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "graph_memory.extraction_api_key" in warnings
        assert "FIVE2_GRAPH_SECRET_VAR" not in warnings
        assert wire.requests == []

    def test_disabled_graph_resolves_nothing_and_builds_nothing(
        self, wire: _Wire, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("disabled graph memory must not resolve or build anything")

        monkeypatch.setattr(main_mod, "resolve_graph_startup", forbidden)
        app_cfg = _parse({"enabled": False, "extraction_api_key": "sec:nope", "extraction_model": 7})
        store, writer, _agent, _builtin = _build(app_cfg, tmp_path)
        assert (store, writer) == (None, None)
        assert wire.requests == [] and not (tmp_path / "graph_db").exists()
