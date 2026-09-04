"""Tests for the [security] config section parsing."""

from __future__ import annotations

import pytest

from config_schema import SecurityConfig, parse_config
from exceptions import ConfigError


class TestSecurityConfigParsing:
    def test_missing_security_section_defaults_to_empty_lists(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert isinstance(cfg.security, SecurityConfig)
        assert cfg.security.prohibited_dirs == []
        assert cfg.security.allowed_dirs == []

    def test_valid_lists_round_trip(self, minimal_config):
        minimal_config["security"] = {
            "prohibited_dirs": ["~/vaults"],
            "allowed_dirs": ["~/projects", "/data"],
        }
        cfg = parse_config(minimal_config)
        assert cfg.security.prohibited_dirs == ["~/vaults"]
        assert cfg.security.allowed_dirs == ["~/projects", "/data"]

    def test_prohibited_dirs_not_a_list_raises(self, minimal_config):
        minimal_config["security"] = {"prohibited_dirs": "x"}
        with pytest.raises(ConfigError, match="security.prohibited_dirs"):
            parse_config(minimal_config)

    def test_allowed_dirs_not_a_list_raises(self, minimal_config):
        minimal_config["security"] = {"allowed_dirs": 5}
        with pytest.raises(ConfigError, match="security.allowed_dirs"):
            parse_config(minimal_config)

    def test_list_with_non_string_item_raises(self, minimal_config):
        minimal_config["security"] = {"allowed_dirs": ["/data", 42]}
        with pytest.raises(ConfigError, match="security.allowed_dirs"):
            parse_config(minimal_config)

    def test_empty_lists_explicitly_allowed(self, minimal_config):
        minimal_config["security"] = {"prohibited_dirs": [], "allowed_dirs": []}
        cfg = parse_config(minimal_config)
        assert cfg.security.prohibited_dirs == []
        assert cfg.security.allowed_dirs == []
