"""Tests for config_schema.py — typed config parsing and validation."""

from __future__ import annotations

import pytest

from config_schema import (
    AppConfig,
    ConfigError,
    parse_config,
)


class TestParseConfig:
    """Happy-path parsing tests."""

    def test_minimal_valid_config(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert isinstance(cfg, AppConfig)
        assert cfg.telegram.bot_token == "123456:ABC-DEF"
        assert cfg.agent.max_iterations == 8
        assert len(cfg.models) == 1
        assert cfg.models[0].provider == "openai"

    def test_defaults_applied(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.tool_timeout == 10
        assert cfg.agent.max_output_size == 4000
        assert cfg.agent.ctx_max_tokens == 90_000
        assert cfg.paths.tools_dir == "tools"
        assert cfg.scheduler.enabled is True

    def test_multiple_models(self, minimal_config):
        minimal_config["models"].append({
            "name": "claude",
            "provider": "anthropic",
            "model": "claude-3-haiku",
            "api_key": "sk-ant-test",
        })
        cfg = parse_config(minimal_config)
        assert len(cfg.models) == 2
        assert cfg.models[1].name == "claude"

    def test_mcp_servers_stdio(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "fs",
            "transport": "stdio",
            "command": ["npx", "server-fs", "/tmp"],
            "env": {"FOO": "bar"},
        }]
        cfg = parse_config(minimal_config)
        assert len(cfg.mcp_servers) == 1
        assert cfg.mcp_servers[0].transport == "stdio"
        assert cfg.mcp_servers[0].env == {"FOO": "bar"}

    def test_mcp_servers_http(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "api",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer x"},
        }]
        cfg = parse_config(minimal_config)
        assert cfg.mcp_servers[0].url == "https://example.com/mcp"
        assert cfg.mcp_servers[0].headers["Authorization"] == "Bearer x"

    def test_raw_dict_preserved(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg._raw is minimal_config

    def test_frozen_immutability(self, minimal_config):
        cfg = parse_config(minimal_config)
        with pytest.raises(Exception):  # FrozenInstanceError
            cfg.agent = None  # type: ignore


class TestValidation:
    """Config validation and error cases."""

    def test_missing_bot_token(self, minimal_config):
        del minimal_config["telegram"]["bot_token"]
        with pytest.raises(ConfigError, match="bot_token"):
            parse_config(minimal_config)

    def test_no_models(self, minimal_config):
        minimal_config["models"] = []
        with pytest.raises(ConfigError, match="At least one"):
            parse_config(minimal_config)

    def test_model_missing_provider(self, minimal_config):
        minimal_config["models"][0]["provider"] = ""
        with pytest.raises(ConfigError, match="provider"):
            parse_config(minimal_config)

    def test_model_missing_model_id(self, minimal_config):
        del minimal_config["models"][0]["model"]
        with pytest.raises(ConfigError, match="model"):
            parse_config(minimal_config)

    def test_mcp_invalid_transport(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "bad",
            "transport": "websocket",
        }]
        with pytest.raises(ConfigError, match="'stdio' or 'http'"):
            parse_config(minimal_config)

    def test_mcp_stdio_missing_command(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "bad",
            "transport": "stdio",
        }]
        with pytest.raises(ConfigError, match="command"):
            parse_config(minimal_config)

    def test_mcp_http_missing_url(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "bad",
            "transport": "http",
        }]
        with pytest.raises(ConfigError, match="url"):
            parse_config(minimal_config)

    def test_mcp_missing_name(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "transport": "http",
            "url": "https://example.com",
        }]
        with pytest.raises(ConfigError, match="name"):
            parse_config(minimal_config)


class TestModelConfig:
    """Model-specific parsing."""

    def test_vision_flag(self, minimal_config):
        minimal_config["models"][0]["vision"] = True
        cfg = parse_config(minimal_config)
        assert cfg.models[0].vision is True

    def test_reasoning_flag(self, minimal_config):
        minimal_config["models"][0]["reasoning"] = True
        cfg = parse_config(minimal_config)
        assert cfg.models[0].reasoning is True

    def test_top_p_optional(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.models[0].top_p is None

    def test_top_p_set(self, minimal_config):
        minimal_config["models"][0]["top_p"] = 0.9
        cfg = parse_config(minimal_config)
        assert cfg.models[0].top_p == 0.9
