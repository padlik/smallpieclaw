"""Tests for config_schema.py — typed config parsing and validation."""

from __future__ import annotations

import pytest

from config_schema import (
    AppConfig,
    ConfigError,
    expand_env,
    parse_config,
    resolve_model_id,
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
        # _raw holds the resolved (env-expanded) copy — equal to input when no
        # placeholders are present, but not necessarily the same object.
        assert cfg._raw == minimal_config

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


# ---------------------------------------------------------------------------
# resolve_model_id
# ---------------------------------------------------------------------------

class TestResolveModelId:
    MODELS = [
        {"name": "kimi-k2.5", "model": "kimi-k2.5:cloud", "aliases": ["kimi"]},
        {"name": "deepseek-v4-pro", "model": "deepseek-v4-pro:cloud", "aliases": []},
        {"name": "grok", "model": "grok-4-1-fast", "aliases": ["grok-fast"]},
    ]

    def test_exact_model_id_match(self):
        assert resolve_model_id("kimi-k2.5:cloud", self.MODELS) == "kimi-k2.5:cloud"

    def test_name_match(self):
        assert resolve_model_id("kimi-k2.5", self.MODELS) == "kimi-k2.5:cloud"

    def test_case_insensitive_name(self):
        assert resolve_model_id("KIMI-K2.5", self.MODELS) == "kimi-k2.5:cloud"

    def test_alias_match(self):
        assert resolve_model_id("kimi", self.MODELS) == "kimi-k2.5:cloud"

    def test_alias_case_insensitive(self):
        assert resolve_model_id("Grok-Fast", self.MODELS) == "grok-4-1-fast"

    def test_no_match_returns_empty(self):
        assert resolve_model_id("nonexistent-model", self.MODELS) == ""

    def test_empty_string_returns_empty(self):
        assert resolve_model_id("", self.MODELS) == ""

    def test_empty_models_list(self):
        assert resolve_model_id("kimi-k2.5", []) == ""


# ---------------------------------------------------------------------------
# expand_env — environment variable placeholder expansion
# ---------------------------------------------------------------------------

class TestExpandEnv:
    """Unit tests for the config environment-variable expansion helper."""

    def test_env_colon_var_substituted(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        result = expand_env({"key": "env:MY_TOKEN"})
        assert result == {"key": "secret123"}

    def test_missing_var_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_REQUIRED", raising=False)
        with pytest.raises(ConfigError, match="MISSING_REQUIRED"):
            expand_env({"key": "env:MISSING_REQUIRED"})

    def test_literal_string_unchanged(self):
        result = expand_env({"key": "just-a-plain-value"})
        assert result == {"key": "just-a-plain-value"}

    def test_partial_env_prefix_not_substituted(self):
        # Strings that start with "env:" but embed more content are opaque.
        result = expand_env({"key": "Bearer env:TOKEN"})
        assert result == {"key": "Bearer env:TOKEN"}

    def test_env_colon_empty_name_raises(self):
        with pytest.raises(ConfigError, match="empty or invalid"):
            expand_env({"key": "env:"})

    def test_env_colon_invalid_name_raises(self):
        with pytest.raises(ConfigError, match="empty or invalid"):
            expand_env({"key": "env:1INVALID"})

    def test_recursive_dict(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "abc:xyz")
        result = expand_env({"telegram": {"bot_token": "env:BOT_TOKEN"}})
        assert result == {"telegram": {"bot_token": "abc:xyz"}}

    def test_recursive_list(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-test")
        result = expand_env({"models": [{"api_key": "env:API_KEY"}]})
        assert result == {"models": [{"api_key": "sk-test"}]}

    def test_non_string_scalars_unchanged(self):
        result = expand_env({"count": 5, "flag": True, "rate": 0.2})
        assert result == {"count": 5, "flag": True, "rate": 0.2}

    def test_error_message_includes_path(self, monkeypatch):
        monkeypatch.delenv("MISSING", raising=False)
        with pytest.raises(ConfigError, match="api_key"):
            expand_env({"models": [{"api_key": "env:MISSING"}]})

    def test_mcp_env_section_expanded(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_KEY", "key-value")
        result = expand_env({
            "mcp_servers": [{"env": {"API_KEY": "env:UPSTREAM_KEY"}}]
        })
        assert result["mcp_servers"][0]["env"]["API_KEY"] == "key-value"


class TestParseConfigWithEnvVars:
    """Integration tests: parse_config expands env vars before validation."""

    def test_bot_token_from_env(self, minimal_config, monkeypatch):
        monkeypatch.setenv("TG_TOKEN", "99999:TEST-TOKEN")
        minimal_config["telegram"]["bot_token"] = "env:TG_TOKEN"
        cfg = parse_config(minimal_config)
        assert cfg.telegram.bot_token == "99999:TEST-TOKEN"

    def test_api_key_from_env(self, minimal_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        minimal_config["models"][0]["api_key"] = "env:OPENAI_API_KEY"
        cfg = parse_config(minimal_config)
        assert cfg.models[0].api_key == "sk-from-env"

    def test_missing_required_var_fails_startup(self, minimal_config, monkeypatch):
        monkeypatch.delenv("UNSET_TOKEN", raising=False)
        minimal_config["telegram"]["bot_token"] = "env:UNSET_TOKEN"
        with pytest.raises(ConfigError, match="UNSET_TOKEN"):
            parse_config(minimal_config)

    def test_raw_dict_contains_resolved_values(self, minimal_config, monkeypatch):
        monkeypatch.setenv("RAW_KEY", "resolved-raw")
        minimal_config["models"][0]["api_key"] = "env:RAW_KEY"
        cfg = parse_config(minimal_config)
        assert cfg._raw["models"][0]["api_key"] == "resolved-raw"

    def test_string_value_for_bool_field_raises(self, minimal_config):
        # A string value (which is what you'd get if env:VAR resolved a flag)
        # must be rejected rather than silently coerced.
        minimal_config["agent"] = {"diagnose_empty_responses": "false"}
        with pytest.raises(ConfigError, match="agent.diagnose_empty_responses"):
            parse_config(minimal_config)

    def test_string_value_for_int_field_raises(self, minimal_config):
        # A numeric string like "4096" must be rejected — env:VAR in numeric fields
        # is not supported; the plan restricts env references to string fields only.
        minimal_config["models"][0]["max_tokens"] = "4096"
        with pytest.raises(ConfigError, match="models\\..*\\.max_tokens"):
            parse_config(minimal_config)

    def test_string_value_for_float_field_raises(self, minimal_config):
        minimal_config["models"][0]["temperature"] = "0.5"
        with pytest.raises(ConfigError, match="models\\..*\\.temperature"):
            parse_config(minimal_config)

    def test_env_var_in_numeric_field_raises(self, minimal_config, monkeypatch):
        # env:VAR on a numeric field resolves to a string and must be rejected.
        monkeypatch.setenv("MAX_TOKENS", "4096")
        minimal_config["models"][0]["max_tokens"] = "env:MAX_TOKENS"
        with pytest.raises(ConfigError, match="max_tokens"):
            parse_config(minimal_config)

    def test_pairing_timeout_string_raises(self, minimal_config):
        minimal_config["telegram"]["pairing_timeout"] = "300"
        with pytest.raises(ConfigError, match="telegram.pairing_timeout"):
            parse_config(minimal_config)

    def test_allowed_user_ids_accepts_int_list(self, minimal_config):
        minimal_config["telegram"]["allowed_user_ids"] = [12345, 67890]
        cfg = parse_config(minimal_config)
        assert cfg.telegram.allowed_user_ids == [12345, 67890]

    def test_allowed_user_ids_string_element_raises(self, minimal_config):
        minimal_config["telegram"]["allowed_user_ids"] = ["12345"]
        with pytest.raises(ConfigError, match=r"telegram\.allowed_user_ids\[0\]"):
            parse_config(minimal_config)

    def test_allowed_user_ids_env_var_element_raises(self, minimal_config, monkeypatch):
        monkeypatch.setenv("TELEGRAM_USER_ID", "12345")
        minimal_config["telegram"]["allowed_user_ids"] = ["env:TELEGRAM_USER_ID"]
        with pytest.raises(ConfigError, match=r"telegram\.allowed_user_ids\[0\]"):
            parse_config(minimal_config)


class TestProviderCredentialInheritance:
    """Provider defaults and credential inheritance behavior."""

    def test_model_inherits_provider_credentials_and_transport_defaults(self, minimal_config):
        minimal_config["providers"] = {
            "openai": {
                "api_key": "provider-key",
                "base_url": "https://provider.example/v1",
                "request_timeout": 180,
                "max_retries": 7,
                "retry_delay": 3,
            }
        }
        model = minimal_config["models"][0]
        del model["api_key"]
        del model["base_url"]
        model.pop("request_timeout", None)
        model.pop("max_retries", None)
        model.pop("retry_delay", None)

        cfg = parse_config(minimal_config)

        assert cfg.models[0].api_key == "provider-key"
        assert cfg.models[0].base_url == "https://provider.example/v1"
        assert cfg.models[0].request_timeout == 180
        assert cfg.models[0].max_retries == 7
        assert cfg.models[0].retry_delay == 3
        assert cfg._raw["models"][0]["api_key"] == "provider-key"
        assert cfg._raw["models"][0]["base_url"] == "https://provider.example/v1"
        assert cfg._raw["models"][0]["request_timeout"] == 180

    def test_model_level_secret_source_overrides_provider_source(self, minimal_config, tmp_path):
        key_file = tmp_path / "model-api-key"
        key_file.write_text("model-file-key\n")
        minimal_config["providers"] = {"openai": {"api_key": "provider-key"}}
        minimal_config["models"][0]["api_key_file"] = str(key_file)
        del minimal_config["models"][0]["api_key"]

        cfg = parse_config(minimal_config)

        assert cfg.models[0].api_key == "model-file-key"
        assert cfg._raw["models"][0]["api_key"] == "model-file-key"

    def test_legacy_config_raw_dict_remains_unchanged(self, minimal_config):
        cfg = parse_config(minimal_config)

        assert cfg._raw == minimal_config

    def test_embeddings_inherit_provider_credentials_when_section_present(self, minimal_config):
        minimal_config["providers"] = {
            "openai": {
                "api_key": "provider-key",
                "base_url": "https://provider.example/v1",
            }
        }
        minimal_config["embeddings"] = {
            "provider": "openai",
            "model": "text-embedding-3-small",
        }

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.api_key == "provider-key"
        assert cfg.embeddings.base_url == "https://provider.example/v1"
        assert cfg._raw["embeddings"]["api_key"] == "provider-key"
        assert cfg._raw["embeddings"]["base_url"] == "https://provider.example/v1"

    def test_omitted_embeddings_section_preserves_active_model_fallback(self, minimal_config):
        minimal_config.pop("embeddings", None)
        minimal_config["providers"] = {
            "openai": {
                "api_key": "provider-key",
                "base_url": "https://provider.example/v1",
            }
        }

        cfg = parse_config(minimal_config)

        assert "embeddings" not in cfg._raw
        assert cfg.embeddings.api_key == ""
        assert cfg.embeddings.base_url == ""


class TestFileBackedSecretResolution:
    """File-backed secret parsing and validation."""

    def test_provider_api_key_file_resolves_secret(self, minimal_config, tmp_path):
        key_file = tmp_path / "openai-key"
        key_file.write_text("file-backed-key\n")
        minimal_config["providers"] = {"openai": {"api_key_file": str(key_file)}}
        del minimal_config["models"][0]["api_key"]

        cfg = parse_config(minimal_config)

        assert cfg.models[0].api_key == "file-backed-key"
        assert cfg._raw["providers"]["openai"]["api_key"] == "file-backed-key"
        assert cfg._raw["models"][0]["api_key"] == "file-backed-key"

    def test_secret_file_path_can_come_from_environment(self, minimal_config, tmp_path, monkeypatch):
        key_file = tmp_path / "openai-key"
        key_file.write_text("env-file-key\n")
        monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_file))
        minimal_config["providers"] = {"openai": {"api_key_file": "env:OPENAI_API_KEY_FILE"}}
        del minimal_config["models"][0]["api_key"]

        cfg = parse_config(minimal_config)

        assert cfg.models[0].api_key == "env-file-key"

    def test_missing_secret_file_raises_field_specific_error(self, minimal_config, tmp_path):
        missing_file = tmp_path / "missing-key"
        minimal_config["providers"] = {"openai": {"api_key_file": str(missing_file)}}

        with pytest.raises(ConfigError, match=r"providers\.openai\.api_key_file"):
            parse_config(minimal_config)

    def test_empty_secret_file_raises_field_specific_error(self, minimal_config, tmp_path):
        key_file = tmp_path / "empty-key"
        key_file.write_text("\n")
        minimal_config["providers"] = {"openai": {"api_key_file": str(key_file)}}

        with pytest.raises(ConfigError, match=r"providers\.openai\.api_key_file"):
            parse_config(minimal_config)

    def test_secret_file_strips_only_one_trailing_newline_sequence(self, minimal_config, tmp_path):
        key_file = tmp_path / "openai-key"
        key_file.write_text("  key-with-space  \n\n")
        minimal_config["providers"] = {"openai": {"api_key_file": str(key_file)}}
        del minimal_config["models"][0]["api_key"]

        cfg = parse_config(minimal_config)

        assert cfg.models[0].api_key == "  key-with-space  \n"

    def test_ambiguous_same_level_secret_sources_raise(self, minimal_config, tmp_path):
        key_file = tmp_path / "openai-key"
        key_file.write_text("file-key")
        minimal_config["providers"] = {
            "openai": {"api_key": "provider-key", "api_key_file": str(key_file)}
        }

        with pytest.raises(ConfigError, match="api_key.*api_key_file"):
            parse_config(minimal_config)

    def test_telegram_bot_token_file_resolves_secret(self, minimal_config, tmp_path):
        token_file = tmp_path / "telegram-token"
        token_file.write_text("123456:FILE-TOKEN\n")
        del minimal_config["telegram"]["bot_token"]
        minimal_config["telegram"]["bot_token_file"] = str(token_file)

        cfg = parse_config(minimal_config)

        assert cfg.telegram.bot_token == "123456:FILE-TOKEN"
        assert cfg._raw["telegram"]["bot_token"] == "123456:FILE-TOKEN"

    def test_non_utf8_secret_file_raises_field_specific_error(self, minimal_config, tmp_path):
        key_file = tmp_path / "binary-key"
        key_file.write_bytes(b"\xff\xfe")
        minimal_config["providers"] = {"openai": {"api_key_file": str(key_file)}}

        with pytest.raises(ConfigError, match=r"providers\.openai\.api_key_file"):
            parse_config(minimal_config)

    def test_config_repr_hides_secret_values(self, minimal_config, tmp_path):
        key_file = tmp_path / "openai-key"
        token_file = tmp_path / "telegram-token"
        key_file.write_text("provider-secret")
        token_file.write_text("telegram-secret")
        del minimal_config["telegram"]["bot_token"]
        del minimal_config["models"][0]["api_key"]
        minimal_config["telegram"]["bot_token_file"] = str(token_file)
        minimal_config["providers"] = {"openai": {"api_key_file": str(key_file)}}

        cfg = parse_config(minimal_config)

        rendered = repr(cfg)
        assert "provider-secret" not in rendered
        assert "telegram-secret" not in rendered
