"""Tests for graph memory config parsing (GraphMemoryConfig)."""

from __future__ import annotations

import pytest

from config_schema import GraphMemoryConfig, parse_config


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
