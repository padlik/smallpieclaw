"""Tests for graph memory config parsing (GraphMemoryConfig)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import httpx
import pytest

import graph_memory
from config_schema import GraphMemoryConfig, parse_config, resolve_provider_connection
from graph_memory import GraphPrerequisiteError, resolve_graph_prerequisites
from llm_client import LLMClient
from model_discovery import ModelDiscovery
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
)
from model_settings import ModelSettings


@pytest.fixture
def minimal_config(minimal_config: dict) -> dict:
    """Return the shared config migrated from legacy ``[[models]]`` to a provider connection."""
    config = copy.deepcopy(minimal_config)
    config.pop("models", None)
    config["providers"] = {
        "openai-main": {
            "type": "openai",
            "api_key": "test-provider-key",
            "base_url": "https://api.openai.com/v1",
        }
    }
    return config


class TestGraphMemoryConfigDefaults:
    """GraphMemoryConfig should be disabled by default with sensible defaults."""

    def test_default_disabled(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.enabled is False

    def test_default_buffer_pool_mb(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.buffer_pool_mb == 256

    def test_default_extraction_model_empty(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.extraction_model == ""

    def test_default_extract_every_n_turns(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.extract_every_n_turns == 3

    def test_default_min_message_length(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.min_message_length == 100

    def test_default_max_context_entries(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.max_context_entries == 10

    def test_no_graph_memory_section_still_parses(self, minimal_config):
        """Config without [graph_memory] section must still parse successfully."""
        assert "graph_memory" not in minimal_config
        cfg = parse_config(minimal_config)
        assert isinstance(cfg.graph_memory, GraphMemoryConfig)


class TestGraphMemoryConfigEnabled:
    """When [graph_memory] section is present, values should be parsed correctly."""

    def _with_gm(self, minimal_config, **kwargs):
        minimal_config["graph_memory"] = kwargs
        return parse_config(minimal_config)

    def test_enabled_true(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True)
        assert cfg.graph_memory.enabled is True

    def test_enabled_false_explicit(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=False)
        assert cfg.graph_memory.enabled is False

    def test_custom_buffer_pool_mb(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True, buffer_pool_mb=128)
        assert cfg.graph_memory.buffer_pool_mb == 128

    def test_extraction_model(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True, extraction_model="gpt-4o-mini")
        assert cfg.graph_memory.extraction_model == "gpt-4o-mini"

    def test_extract_every_n_turns(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True, extract_every_n_turns=5)
        assert cfg.graph_memory.extract_every_n_turns == 5

    def test_min_message_length(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True, min_message_length=50)
        assert cfg.graph_memory.min_message_length == 50

    def test_max_context_entries(self, minimal_config):
        cfg = self._with_gm(minimal_config, enabled=True, max_context_entries=20)
        assert cfg.graph_memory.max_context_entries == 20

    def test_string_ints_coerced(self, minimal_config):
        """TOML values are already ints but the parser uses int() for safety."""
        minimal_config["graph_memory"] = {
            "enabled": True,
            "buffer_pool_mb": 512,
            "extract_every_n_turns": 2,
            "min_message_length": 200,
            "max_context_entries": 15,
        }
        cfg = parse_config(minimal_config)
        assert cfg.graph_memory.buffer_pool_mb == 512
        assert cfg.graph_memory.extract_every_n_turns == 2
        assert cfg.graph_memory.min_message_length == 200
        assert cfg.graph_memory.max_context_entries == 15

    def test_config_is_frozen(self, minimal_config):
        """GraphMemoryConfig must be immutable (frozen dataclass)."""
        cfg = parse_config(minimal_config)
        with pytest.raises((AttributeError, TypeError)):
            cfg.graph_memory.enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parsed missing/invalid [embeddings] reject graph prerequisites
# ---------------------------------------------------------------------------

_GRAPH_MODEL_ID = "openai-main/gpt-graph"
_EMBEDDING_SECTIONS = {
    "missing": None,
    "invalid-provider": {"provider": "not-a-provider", "model": "text-embed"},
}


class _CountingTransport(httpx.MockTransport):
    """Real MockTransport that records (and refuses) requests and counts closes."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.close_count = 0

        def refuse(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            raise AssertionError(f"unexpected network I/O: {request.url}")

        super().__init__(refuse)

    def close(self) -> None:
        """Count the close, then close the underlying transport."""
        self.close_count += 1
        super().close()


def _tripwire(name: str) -> Callable[..., None]:
    """Return a constructor replacement that fails if graph resources are built."""

    def tripped(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must not be constructed during prerequisite checks")

    return tripped


@pytest.mark.parametrize("case", sorted(_EMBEDDING_SECTIONS))
def test_parsed_unavailable_embeddings_reject_graph_prerequisites(
    minimal_config: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A valid chat connection never substitutes for missing/invalid explicit embeddings."""
    minimal_config["graph_memory"] = {"enabled": True, "extraction_model": _GRAPH_MODEL_ID}
    if _EMBEDDING_SECTIONS[case] is not None:
        minimal_config["embeddings"] = _EMBEDDING_SECTIONS[case]
    app_cfg = parse_config(minimal_config)
    assert app_cfg.embeddings.unavailable_reason
    assert app_cfg.graph_memory.extraction_model == _GRAPH_MODEL_ID

    metadata = ModelMetadata(
        context_tokens=8192,
        output_tokens=2048,
    )
    document = CatalogDocument(
        1,
        1,
        None,
        (ProviderState("openai-main", "openai", "2026-09-01T00:00:00Z", "fresh"),),
        (ModelRecord(_GRAPH_MODEL_ID, "openai-main", "gpt-graph", "Graph", metadata),),
    )
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(document)
    discovery = MagicMock(spec=ModelDiscovery)
    discovery.fetch.side_effect = AssertionError("graph prerequisites must not run discovery")
    registry = ModelRegistry(
        app_cfg.providers,
        settings,
        discovery,
        lambda config: resolve_provider_connection(config, vault_file=None),
    )
    assert registry.restore_selection().selection_status == "no_selection"
    # The chat connection itself is valid and resolvable through the real resolver.
    assert registry.resolve_graph_model(_GRAPH_MODEL_ID).connection.api_key == "test-provider-key"

    transports: list[_CountingTransport] = []
    real_client = httpx.Client

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        transport = _CountingTransport()
        transports.append(transport)
        return real_client(transport=transport)

    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setattr(graph_memory.GraphMemoryStore, "__init__", _tripwire("GraphMemoryStore"))
    monkeypatch.setattr(LLMClient, "__init__", _tripwire("LLMClient"))
    catalog_bytes = settings.path.read_bytes()
    entries = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(GraphPrerequisiteError) as caught:
        resolve_graph_prerequisites(app_cfg, registry)

    assert caught.value.code == "embedding_unavailable"
    assert "[embeddings]" in str(caught.value)
    assert "test-provider-key" not in str(caught.value)
    assert len(transports) == 1
    assert transports[0].requests == []
    assert transports[0].close_count == 1
    assert settings.path.read_bytes() == catalog_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == entries
    discovery.fetch.assert_not_called()
    view = registry.view()
    assert (view.selection_status, view.active_model_id, view.remembered_model_id) == (
        "no_selection", None, None
    )
    with pytest.raises(ModelRegistryError) as admission:
        registry.admit()
    assert admission.value.code == "no_active_selection"
