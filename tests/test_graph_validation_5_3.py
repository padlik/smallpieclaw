"""Task 5.3: graph startup and live backfill validate their own configuration first.

Covers ``provider-model-catalog`` / "Graph startup and live backfill validate
their own configuration before side effects" (ADR-0039/0042).

Side-effect events recorded by the spies below: construction of the graph
store, of any ``EmbeddingClient``/``LLMClient``/``httpx.Client`` after the
test's own setup, and any catalog/preference object (``ModelSettings``,
``ModelRegistry``, ``ModelDiscovery.fetch``).  Invalid settings must produce
no event at all; valid settings must produce the real store first.  Canary
credentials only.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

import backfill_graph_memory
import config_schema
import graph_memory
import main as main_mod
import model_discovery
from config_schema import parse_config
from embedding_client import EmbeddingClient
from graph_memory import GraphPrerequisiteError
from llm_client import LLMClient
from memory_store import LongTermMemory
from model_registry import ModelRegistry
from model_settings import ModelSettings
from xdg import xdg_paths

GRAPH_HOST = "graph.v53.test"
EMBED_HOST = "embed.v53.test"
GRAPH_KEY = "graph-canary-key-v53-aaa"
EMBED_KEY = "embed-canary-key-v53-bbb"
GRAPH_MODEL = "vendor/extract-v53"
AGENT = "v53-agent"
CANARY_TEXTS = (
    GRAPH_KEY, EMBED_KEY, GRAPH_HOST, EMBED_HOST, "V53_UNSET_VAR", "V53_VAULT_REF", "V53_BLANK_ENV", "V53_BLANK_SEC",
)
BLANK_VAULT = 'V53_BLANK_SEC = "   "\n'  # a vault entry that resolves to whitespace only


def _embeddings(**overrides: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "provider": "openai",
        "model": "embed-v53",
        "api_key": EMBED_KEY,
        "base_url": f"https://{EMBED_HOST}/v1",
    }
    section.update(overrides)
    return {k: v for k, v in section.items() if v is not None}


def _graph(**overrides: Any) -> dict[str, Any]:
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
    return {k: v for k, v in section.items() if v is not None}


def _raw(embeddings: dict[str, Any] | None = None, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    """Valid config with NO chat provider connection at all."""
    return {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1], "security_mode": "allowlist"},
        "agent": {"compaction_threshold_tokens": 60000},
        "paths": {},
        "embeddings": embeddings if embeddings is not None else _embeddings(),
        "graph_memory": graph if graph is not None else _graph(),
    }


# Invalid-setting matrix shared by the root and the live CLI.  Each case is
# (id, embeddings overrides, graph overrides, field name that must be reported).
_INVALID_CASES: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
    ("emb-provider-unsupported", {"provider": "anthropic"}, {}, "embeddings"),
    ("emb-provider-missing", {"provider": None}, {}, "embeddings"),
    ("emb-model-blank", {"model": "   "}, {}, "embeddings"),
    ("emb-base-url-bad", {"base_url": "ftp://embed.v53.test"}, {}, "embeddings"),
    ("emb-key-unset-env", {"api_key": "env:V53_UNSET_VAR"}, {}, "embeddings"),
    ("emb-key-missing", {"api_key": None}, {}, "embeddings"),
    ("emb-key-env-whitespace", {"api_key": "env:V53_BLANK_ENV"}, {}, "embeddings"),
    ("emb-key-vault-whitespace", {"api_key": "sec:V53_BLANK_SEC"}, {}, "embeddings"),
    ("gm-provider-missing", {}, {"extraction_provider": None}, "graph_memory.extraction_provider"),
    ("gm-provider-unsupported", {}, {"extraction_provider": "mystery"}, "graph_memory.extraction_provider"),
    ("gm-base-url-bad", {}, {"extraction_base_url": "not a url"}, "graph_memory.extraction_base_url"),
    ("gm-model-blank", {}, {"extraction_model": "  "}, "graph_memory.extraction_model"),
    ("gm-key-missing", {}, {"extraction_api_key": None}, "graph_memory.extraction_api_key"),
    ("gm-key-unset-env", {}, {"extraction_api_key": "env:V53_UNSET_VAR"}, "graph_memory.extraction_api_key"),
    ("gm-key-env-whitespace", {}, {"extraction_api_key": "env:V53_BLANK_ENV"}, "graph_memory.extraction_api_key"),
    ("gm-key-vault-whitespace", {}, {"extraction_api_key": "sec:V53_BLANK_SEC"}, "graph_memory.extraction_api_key"),
    ("gm-key-vault-missing", {}, {"extraction_api_key": "sec:V53_VAULT_REF"}, "graph_memory.extraction_api_key"),
    (
        "gm-ollama-local-endpoint",
        {},
        {"extraction_provider": "ollama", "extraction_base_url": "http://localhost:11434"},
        "graph_memory.extraction_base_url",
    ),
]
_IDS = [case[0] for case in _INVALID_CASES]


class _Events:
    """Record graph-relevant construction events from the actual classes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, store: Callable[..., Any] | None = None) -> None:
        self.log: list[str] = []
        events = self
        real_embed_init = EmbeddingClient.__init__
        real_llm_init = LLMClient.__init__
        real_http_init = httpx.Client.__init__
        real_store = graph_memory.GraphMemoryStore

        def embed_init(inst: EmbeddingClient, *a: object, **k: object) -> None:
            events.log.append("EmbeddingClient")
            real_embed_init(inst, *a, **k)

        def llm_init(inst: LLMClient, *a: object, **k: object) -> None:
            events.log.append("LLMClient")
            real_llm_init(inst, *a, **k)

        def http_init(inst: httpx.Client, *a: object, **k: object) -> None:
            events.log.append("httpx.Client")
            real_http_init(inst, *a, **k)

        def store_factory(**kwargs: Any) -> Any:
            events.log.append("GraphMemoryStore")
            return (store or real_store)(**kwargs)

        def tripwire(label: str) -> Callable[..., Any]:
            def tripped(*_a: object, **_k: object) -> Any:
                events.log.append(label)
                raise AssertionError(f"{label} must not run for graph validation")

            return tripped

        monkeypatch.setattr(EmbeddingClient, "__init__", embed_init)
        monkeypatch.setattr(LLMClient, "__init__", llm_init)
        monkeypatch.setattr(httpx.Client, "__init__", http_init)
        monkeypatch.setattr(graph_memory, "GraphMemoryStore", store_factory)
        if "GraphMemoryStore" in vars(backfill_graph_memory):
            monkeypatch.setattr(backfill_graph_memory, "GraphMemoryStore", store_factory)
        monkeypatch.setattr(ModelSettings, "__init__", tripwire("ModelSettings"))
        monkeypatch.setattr(ModelRegistry, "__init__", tripwire("ModelRegistry"))
        monkeypatch.setattr(model_discovery.ModelDiscovery, "fetch", tripwire("ModelDiscovery.fetch"))
        # No chat connection credential is ever resolved for graph validation or live backfill.
        monkeypatch.setattr(config_schema, "resolve_provider_connection", tripwire("resolve_provider_connection"))
        if "resolve_provider_connection" in vars(backfill_graph_memory):
            monkeypatch.setattr(
                backfill_graph_memory, "resolve_provider_connection", tripwire("resolve_provider_connection")
            )


class _Wire:
    """Route the default httpx transport; record per-host requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        wire = self

        def handle(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            request.read()
            wire.requests.append(request)
            if request.url.host == EMBED_HOST and request.url.path == "/v1/embeddings":
                body = json.loads(request.content)
                inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
                return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in inputs]})
            if request.url.host == GRAPH_HOST and request.url.path == "/v1/chat/completions":
                reply = json.dumps({
                    "entities": [{"name": "Alice", "entity_type": "person"}, {"name": "Python", "entity_type": "tool"}],
                    "facts": [{"source": "Alice", "target": "Python", "relation_type": "USES", "fact": "Alice uses Python."}],
                })
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": reply}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                })
            wire.unexpected.append(f"{request.method} {request.url}")
            return httpx.Response(404, json={})

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


# ---------------------------------------------------------------------------
# Root: main._build_graph_memory
# ---------------------------------------------------------------------------


def _root_build(raw: dict[str, Any], tmp_path: Path) -> tuple[Any, Any, SimpleNamespace, SimpleNamespace]:
    app_cfg = parse_config(raw)
    shared_indexing_client = EmbeddingClient(app_cfg.embeddings)  # main's own, built before graph setup
    agent, builtin = SimpleNamespace(), SimpleNamespace()
    return (*_build_with(app_cfg, shared_indexing_client, tmp_path, agent, builtin), agent, builtin)


def _build_with(app_cfg: Any, embedder: EmbeddingClient, tmp_path: Path, agent: Any, builtin: Any) -> tuple[Any, Any]:
    paths = SimpleNamespace(graph_memory_db=tmp_path / "graph_db")
    return main_mod._build_graph_memory(
        app_cfg=app_cfg,
        paths=paths,
        agent=agent,
        builtin=builtin,
        vault_file=str(tmp_path / "no-vault"),
        embedder_fn=embedder.embed,
    )


class TestRootValidatesBeforeConstruction:
    @pytest.mark.parametrize(("_id", "emb", "gm", "field"), _INVALID_CASES, ids=_IDS)
    def test_invalid_settings_construct_nothing_and_keep_indexing(
        self,
        _id: str,
        emb: dict[str, Any],
        gm: dict[str, Any],
        field: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("V53_UNSET_VAR", raising=False)
        monkeypatch.setenv("V53_BLANK_ENV", "   ")
        (tmp_path / "no-vault").write_text(BLANK_VAULT)
        wire = _Wire(monkeypatch)
        raw = _raw(_embeddings(**emb), _graph(**gm))
        app_cfg = parse_config(raw)
        indexing_client = EmbeddingClient(app_cfg.embeddings)  # main's shared indexing client (not graph-owned)
        events = _Events(monkeypatch)
        agent, builtin = SimpleNamespace(), SimpleNamespace()
        with caplog.at_level(logging.WARNING):
            store, writer = _build_with(app_cfg, indexing_client, tmp_path, agent, builtin)
        assert (store, writer) == (None, None)
        assert events.log == []  # no store, no graph-owned client/probe/transport
        assert not (tmp_path / "graph_db").exists()
        assert not hasattr(agent, "_graph_memory") and not hasattr(builtin, "_graph_memory")
        warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert field in warnings
        for canary in CANARY_TEXTS:
            assert canary not in warnings
        assert wire.requests == []

    def test_valid_settings_build_the_real_store_first_without_extra_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = _Wire(monkeypatch)
        app_cfg = parse_config(_raw())
        indexing_client = EmbeddingClient(app_cfg.embeddings)
        events = _Events(monkeypatch)
        agent, builtin = SimpleNamespace(), SimpleNamespace()
        store, writer = _build_with(app_cfg, indexing_client, tmp_path, agent, builtin)
        assert store is not None and writer is not None
        try:
            assert events.log == ["GraphMemoryStore"]  # nothing built before it; extraction client is lazy
            writer.enqueue("Alice uses Python for scripting every day.")
            writer.stop()
        finally:
            store.close()
        assert (tmp_path / "graph_db").exists()
        graph_calls = wire.to(GRAPH_HOST)
        assert graph_calls
        for request in graph_calls:
            assert request.headers["authorization"] == f"Bearer {GRAPH_KEY}"
            assert json.loads(request.content)["model"] == GRAPH_MODEL
        for request in wire.to(EMBED_HOST):
            assert request.headers["authorization"] == f"Bearer {EMBED_KEY}"
        for request in wire.requests:
            sent = f"{request.headers} {request.content!r}"
            if request.url.host != GRAPH_HOST:
                assert GRAPH_KEY not in sent
            if request.url.host != EMBED_HOST:
                assert EMBED_KEY not in sent
        assert wire.unexpected == []

    def test_ordinary_chat_structural_error_still_fails_startup_parse(self) -> None:
        raw = _raw(graph=_graph(extraction_api_key=None))
        raw["providers"] = {"chat": {"type": "no-such-adapter"}}
        from exceptions import ConfigError

        with pytest.raises(ConfigError):
            parse_config(raw)

    def test_startup_resolution_builds_no_client_for_invalid_embeddings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = _Events(monkeypatch)
        app_cfg = parse_config(_raw(_embeddings(provider="anthropic")))
        with pytest.raises(GraphPrerequisiteError) as info:
            graph_memory.resolve_graph_startup(app_cfg, vault_file=None)
        assert "embeddings" in str(info.value)
        assert events.log == []


# ---------------------------------------------------------------------------
# Live backfill CLI
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _write_toml(path: Path, raw: dict[str, Any]) -> None:
    lines: list[str] = []
    for table, body in raw.items():
        lines.append(f"[{table}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in body.items())
        lines.append("")
    path.write_text("\n".join(lines))


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


class _CliWorld(SimpleNamespace):
    config: Path
    state: Path
    before: dict[str, bytes | None]


def _cli_world(tmp_xdg: Path, tmp_path: Path, raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> _CliWorld:
    paths = xdg_paths(AGENT)
    for directory in (paths.config_home, paths.data_home, paths.state_home):
        directory.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    _write_toml(config, raw)
    ltm = LongTermMemory(path=str(paths.data_home / "longterm_memory.json"))
    ltm.add("Alice uses Python.", source="manual")
    ltm.add("Bob uses Go.", source="manual")
    state = paths.data_home / "graph_memory_backfill_state.json"
    monkeypatch.delenv("V53_UNSET_VAR", raising=False)
    monkeypatch.setenv("V53_BLANK_ENV", "   ")
    paths.secrets_file.write_text(BLANK_VAULT)
    return _CliWorld(config=config, state=state, before=_tree(tmp_path))


def _run_cli(world: _CliWorld, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    monkeypatch.setattr(
        sys, "argv", ["backfill_graph_memory.py", "--config", str(world.config), "--agent-name", AGENT, *extra]
    )
    try:
        backfill_graph_memory.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


class TestLiveBackfillValidatesItself:
    @pytest.mark.parametrize(("_id", "emb", "gm", "field"), _INVALID_CASES, ids=_IDS)
    def test_invalid_settings_exit_before_any_store_client_or_catalog(
        self,
        _id: str,
        emb: dict[str, Any],
        gm: dict[str, Any],
        field: str,
        tmp_xdg: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wire = _Wire(monkeypatch)
        world = _cli_world(tmp_xdg, tmp_path, _raw(_embeddings(**emb), _graph(**gm)), monkeypatch)
        events = _Events(monkeypatch)
        with caplog.at_level(logging.INFO):
            code = _run_cli(world, monkeypatch)
        assert code != 0
        assert events.log == []
        assert wire.requests == []
        assert not xdg_paths(AGENT).graph_memory_db.exists()
        assert _tree(tmp_path) == world.before
        out = capsys.readouterr()
        shown = " ".join([out.out, out.err] + [r.getMessage() for r in caplog.records])
        assert field in shown
        for canary in CANARY_TEXTS:
            assert canary not in shown
        failures = [r for r in caplog.records if r.getMessage().startswith("Backfill prerequisites failed:")]
        assert len(failures) == 1  # exactly one actionable operator message

    def test_valid_run_needs_no_chat_provider_or_catalog_and_isolates_keys(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = _Wire(monkeypatch)
        raw = _raw()
        assert "providers" not in raw
        world = _cli_world(tmp_xdg, tmp_path, raw, monkeypatch)
        paths = xdg_paths(AGENT)
        assert not paths.models_config_file.exists()
        events = _Events(monkeypatch)
        assert _run_cli(world, monkeypatch) == 0
        # The CLI's single embedding client (needed for the vector dimension) precedes the
        # store; there is no separate availability probe, and extraction clients come later.
        assert events.log[:3] == ["EmbeddingClient", "httpx.Client", "GraphMemoryStore"]
        assert events.log.count("EmbeddingClient") == 1
        assert events.log.count("LLMClient") == 2
        assert not paths.models_config_file.exists()
        assert not paths.models_cache_file.exists()
        embed_calls, graph_calls = wire.to(EMBED_HOST), wire.to(GRAPH_HOST)
        assert embed_calls and len(graph_calls) == 2
        for request in embed_calls:
            assert request.headers["authorization"] == f"Bearer {EMBED_KEY}"
        for request in graph_calls:
            assert request.headers["authorization"] == f"Bearer {GRAPH_KEY}"
            body = json.loads(request.content)
            assert body["model"] == GRAPH_MODEL
            assert body["max_completion_tokens"] == 1024
            assert body["temperature"] == 0.1
        for request in wire.requests:
            sent = f"{request.headers} {request.content!r}"
            if request.url.host != GRAPH_HOST:
                assert GRAPH_KEY not in sent
            if request.url.host != EMBED_HOST:
                assert EMBED_KEY not in sent
        assert wire.unexpected == []
        state = json.loads(world.state.read_text())
        assert len(state["imported"]) == 2  # real store imported both input records


class TestDryRunReadsOnlyInputAndState:
    def test_dry_run_with_unresolvable_settings_touches_nothing(
        self,
        tmp_xdg: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import builtins
        import embedding_client

        wire = _Wire(monkeypatch)
        raw = _raw(
            _embeddings(provider="anthropic", api_key="env:V53_UNSET_VAR"),
            _graph(extraction_api_key="sec:V53_VAULT_REF", extraction_provider="mystery"),
        )
        world = _cli_world(tmp_xdg, tmp_path, raw, monkeypatch)
        events = _Events(monkeypatch)
        trapped: list[str] = []

        def trap(label: str) -> Callable[..., Any]:
            def tripped(*_a: object, **_k: object) -> Any:
                trapped.append(label)
                raise AssertionError(f"{label} must not run in dry run")

            return tripped

        monkeypatch.setattr(config_schema, "parse_config", trap("parse_config"))
        monkeypatch.setattr(config_schema, "_expand_value", trap("_expand_value"))
        monkeypatch.setattr(config_schema, "_load_vault", trap("_load_vault"))
        monkeypatch.setattr(config_schema, "resolve_provider_connection", trap("resolve_provider_connection"))
        monkeypatch.setattr(embedding_client, "_resolve_credential", trap("_resolve_credential"))
        monkeypatch.setattr(graph_memory, "resolve_graph_startup", trap("resolve_graph_startup"))
        monkeypatch.setattr(graph_memory, "build_extraction_llm_call", trap("build_extraction_llm_call"))
        real_import = builtins.__import__

        def guarded(name: str, *a: object, **k: object) -> Any:
            if name == "ladybug" or name.startswith("ladybug."):
                trapped.append("ladybug import")
                raise ImportError("ladybug forbidden in dry run")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", guarded)
        assert _run_cli(world, monkeypatch, "--dry-run") == 0
        assert trapped == [] and events.log == [] and wire.requests == []
        assert _tree(tmp_path) == world.before
        out = capsys.readouterr().out
        assert "Total entries:      2" in out and "Imported:           2" in out
