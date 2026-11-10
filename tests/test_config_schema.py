"""Tests for config_schema.py — typed config parsing and validation."""

from __future__ import annotations

import os

import pytest

from config_schema import (
    AppConfig,
    ConfigError,
    VaultConfig,
    _has_sec_reference,
    _load_vault,
    expand_env,
    parse_config,
    parse_vault_content,
    resolve_model_id,
    resolve_provider_connection,
)


@pytest.fixture
def minimal_config():
    """Minimal valid connection-oriented configuration."""
    return {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [12345], "security_mode": "allowlist"},
        "agent": {"max_iterations": 8, "scheduled_max_iterations": 100, "tool_timeout": 10, "max_output_size": 4000, "top_tools": 3, "ctx_max_tokens": 90000},
        "providers": {"openai-main": {"type": "openai", "api_key": "test-provider-key", "base_url": "https://api.openai.com/v1"}},
        "embeddings": {
            "provider": "openai",
            "base_url": "https://embedding.example.test/v1",
            "api_key": "fake-embedding-key",
            "model": "test-embedding",
        },
        "paths": {},
    }


class TestParseConfig:
    """Happy-path parsing tests."""

    def test_minimal_valid_config(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert isinstance(cfg, AppConfig)
        assert cfg.telegram.bot_token == "123456:ABC-DEF"
        assert cfg.agent.max_iterations == 8
        assert cfg.agent.agent_name == "piclaw"
        assert cfg.models == []
        assert cfg.providers["openai-main"].type == "openai"
        assert cfg.vault.type == "file"

    def test_defaults_applied(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.tool_timeout == 10
        assert cfg.agent.max_output_size == 4000
        assert cfg.agent.ctx_max_tokens == 90_000
        assert cfg.scheduler.enabled is True

    def test_legacy_models_key_is_rejected_even_when_empty(self, minimal_config):
        minimal_config["models"] = []
        with pytest.raises(ConfigError, match=r"models.*providers.*\/models"):
            parse_config(minimal_config)

    def test_model_entry_error_does_not_report_legacy_fields(self, minimal_config):
        minimal_config["models"] = [{"temperature": 0.4, "name": "PRIVATE_MODEL_SENTINEL"}]
        with pytest.raises(ConfigError, match=r"models.*providers") as error:
            parse_config(minimal_config)
        assert "PRIVATE_MODEL_SENTINEL" not in str(error.value)

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
        # _raw holds the resolved (env-expanded) copy, plus expanded defaults for
        # path fields and agent_home. Compare non-path sections that exist.
        assert cfg._raw["telegram"] == minimal_config["telegram"]
        assert cfg._raw["providers"] == minimal_config["providers"]
        if "embeddings" in minimal_config:
            assert cfg._raw["embeddings"] == minimal_config["embeddings"]
        if "scheduler" in minimal_config:
            assert cfg._raw["scheduler"] == minimal_config["scheduler"]

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

    def test_mcp_invalid_transport(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "bad",
            "transport": "websocket",
        }]
        with pytest.raises(ConfigError, match="'stdio', 'http', or 'sse'"):
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

    def test_mcp_oauth_all_fields(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
            },
        }]
        cfg = parse_config(minimal_config)
        oauth = cfg.mcp_servers[0].oauth
        assert oauth is not None
        assert oauth.client_id == "id"
        assert oauth.client_secret == "secret"
        assert oauth.redirect_uri == "https://ddns.example.com/callback"
        assert oauth.scope == "https://mail.google.com/"
        assert oauth.cert_path == "/etc/letsencrypt/live/ddns/fullchain.pem"
        assert oauth.key_path == "/etc/letsencrypt/live/ddns/privkey.pem"
        assert oauth.callback_port == 8000
        assert oauth.callback_bind == "0.0.0.0"

    def test_mcp_oauth_optional(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
        }]
        cfg = parse_config(minimal_config)
        assert cfg.mcp_servers[0].oauth is None

    def test_mcp_oauth_missing_client_id(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
            },
        }]
        with pytest.raises(ConfigError, match="client_id"):
            parse_config(minimal_config)

    def test_mcp_oauth_missing_client_secret(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
            },
        }]
        with pytest.raises(ConfigError, match="client_secret"):
            parse_config(minimal_config)

    def test_mcp_oauth_defaults(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
            },
        }]
        cfg = parse_config(minimal_config)
        oauth = cfg.mcp_servers[0].oauth
        assert oauth is not None
        assert oauth.callback_port == 8000
        assert oauth.callback_bind == "0.0.0.0"
        assert oauth.extra_auth_params == {}

    def test_mcp_oauth_extra_auth_params_table(self, minimal_config):
        """A TOML table round-trips and coerces keys/values to str."""
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
                "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
            },
        }]
        cfg = parse_config(minimal_config)
        oauth = cfg.mcp_servers[0].oauth
        assert oauth is not None
        assert oauth.extra_auth_params == {"access_type": "offline", "prompt": "consent"}

    def test_mcp_oauth_extra_auth_params_coerces_non_string(self, minimal_config):
        """Non-string values in the table are coerced to str."""
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
                "extra_auth_params": {"flag": True},
            },
        }]
        cfg = parse_config(minimal_config)
        oauth = cfg.mcp_servers[0].oauth
        assert oauth is not None
        assert oauth.extra_auth_params == {"flag": "True"}

    def test_mcp_oauth_extra_auth_params_non_table_raises(self, minimal_config):
        """A non-table value for extra_auth_params raises ConfigError."""
        minimal_config["mcp_servers"] = [{
            "name": "gmail",
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "client_id": "id",
                "client_secret": "secret",
                "redirect_uri": "https://ddns.example.com/callback",
                "scope": "https://mail.google.com/",
                "cert_path": "/etc/letsencrypt/live/ddns/fullchain.pem",
                "key_path": "/etc/letsencrypt/live/ddns/privkey.pem",
                "extra_auth_params": "not-a-table",
            },
        }]
        with pytest.raises(ConfigError, match="extra_auth_params must be a table"):
            parse_config(minimal_config)

    def test_mcp_missing_name(self, minimal_config):
        minimal_config["mcp_servers"] = [{
            "transport": "http",
            "url": "https://example.com",
        }]
        with pytest.raises(ConfigError, match="name"):
            parse_config(minimal_config)


class TestProviderConnections:
    """Connection identity and obsolete model configuration are validated."""

    @pytest.mark.parametrize("field", ["max_tokens", "temperature", "top_p", "model", "provider", "api_key_override"])
    def test_legacy_model_entry_rejected(self, minimal_config, field):
        minimal_config["models"] = [{field: "obsolete"}]
        with pytest.raises(ConfigError, match="models"):
            parse_config(minimal_config)

    def test_legacy_model_error_does_not_echo_name_or_secret_source(self, minimal_config):
        minimal_config["models"] = [{
            "name": "PRIVATE_MODEL_SENTINEL",
            "api_key_file": "env:SECRET_REFERENCE_SENTINEL",
        }]
        with pytest.raises(ConfigError) as error:
            parse_config(minimal_config)
        assert "PRIVATE_MODEL_SENTINEL" not in str(error.value)
        assert "SECRET_REFERENCE_SENTINEL" not in str(error.value)

    def test_unsupported_provider_field_rejected_before_expansion(self, minimal_config):
        minimal_config["providers"]["openai-main"]["temperature"] = "env:SECRET_REFERENCE_SENTINEL"
        with pytest.raises(ConfigError) as error:
            parse_config(minimal_config)
        assert "providers.openai-main.temperature" in str(error.value)
        assert "SECRET_REFERENCE_SENTINEL" not in str(error.value)

    @pytest.mark.parametrize("providers", [["bad"], "bad", 7])
    def test_provider_container_type_is_validated(self, minimal_config, providers):
        minimal_config["providers"] = providers
        with pytest.raises(ConfigError, match="providers must be a table"):
            parse_config(minimal_config)

    def test_provider_adapter_type_must_be_string(self, minimal_config):
        minimal_config["providers"]["openai-main"]["type"] = ["openai"]
        with pytest.raises(ConfigError, match=r"providers\.openai-main\.type"):
            parse_config(minimal_config)

    @pytest.mark.parametrize(
        "connection_id",
        ["", "team/account", "team account", "_team", "équipe", "a" * 129, 7],
    )
    def test_connection_ids_must_be_nonempty_string_without_slash(self, minimal_config, connection_id):
        minimal_config["providers"] = {connection_id: {"type": "openai"}}
        with pytest.raises(ConfigError, match="connection IDs"):
            parse_config(minimal_config)

    @pytest.mark.parametrize("connection_id", ["a", "a" * 128])
    def test_connection_id_codec_boundaries_preserved(self, minimal_config, connection_id):
        minimal_config["providers"] = {connection_id: {"type": "openai"}}
        cfg = parse_config(minimal_config)
        assert list(cfg.providers) == [connection_id]
        assert cfg.providers[connection_id].name == connection_id

    @pytest.mark.parametrize("field", ["max_retries", "retry_delay"])
    def test_provider_retry_keys_rejected_actionably(self, minimal_config, field):
        minimal_config["providers"]["openai-main"][field] = 2
        with pytest.raises(ConfigError, match=rf"providers\.openai-main\.{field}"):
            parse_config(minimal_config)

    @pytest.mark.parametrize("value", [0, -1, True, "10", float("nan"), float("inf"), -float("inf")])
    def test_timeout_must_be_positive_number(self, minimal_config, value):
        minimal_config["providers"]["openai-main"]["request_timeout"] = value
        with pytest.raises(ConfigError, match="request_timeout"):
            parse_config(minimal_config)

    @pytest.mark.parametrize(
        "url",
        [
            "not-an-endpoint",
            "ftp://api.example.test/v1",
            "https:///v1",
            "https://api.example.test:PRIVATE_VALUE/v1",
            "https://[broken/v1",
            "https://api.example.test:99999/v1",
        ],
    )
    def test_invalid_provider_urls_raise_sanitized_config_error(self, minimal_config, url):
        minimal_config["providers"]["openai-main"]["base_url"] = url
        with pytest.raises(ConfigError) as error:
            parse_config(minimal_config)
        assert "providers.openai-main.base_url" in str(error.value)
        assert "PRIVATE_VALUE" not in str(error.value)

    def test_non_ollama_connection_path_and_missing_endpoint_are_allowed(self, minimal_config):
        minimal_config["providers"]["openai-main"].pop("base_url")
        assert parse_config(minimal_config).providers["openai-main"].base_url == ""
        minimal_config["providers"]["openai-main"]["base_url"] = "https://api.example.test/account/path/"
        assert parse_config(minimal_config).providers["openai-main"].base_url == "https://api.example.test/account/path"

    def test_ollama_is_official_cloud_only(self, minimal_config):
        minimal_config["providers"]["ollama"] = {
            "type": "ollama", "api_key": "env:OLLAMA_KEY", "base_url": "http://localhost:11434"
        }
        with pytest.raises(ConfigError, match="official cloud Ollama"):
            parse_config(minimal_config)

    def test_ollama_normalizes_api_suffix_and_requires_key(self, minimal_config):
        minimal_config["providers"]["ollama"] = {
            "type": "ollama", "api_key": "env:OLLAMA_KEY", "base_url": "https://ollama.com/api/"
        }
        assert parse_config(minimal_config).providers["ollama"].base_url == "https://ollama.com"

    def test_ollama_requires_credential_source(self, minimal_config):
        minimal_config["providers"]["ollama"] = {"type": "ollama"}
        with pytest.raises(ConfigError, match="api_key.*required"):
            parse_config(minimal_config)

    def test_obsolete_agent_selectors_rejected(self, minimal_config):
        minimal_config["agent"]["default_model"] = "gpt-x"
        with pytest.raises(ConfigError, match="agent.default_model"):
            parse_config(minimal_config)

    @pytest.mark.parametrize(
        "endpoint",
        ["https://ollama.com:PRIVATE_VALUE", "https://[broken", "https://ollama.com:99999"],
    )
    def test_ollama_malformed_urls_are_sanitized(self, minimal_config, endpoint):
        minimal_config["providers"]["ollama"] = {
            "type": "ollama", "api_key": "fake-key", "base_url": endpoint
        }
        with pytest.raises(ConfigError) as error:
            parse_config(minimal_config)
        assert "providers.ollama.base_url" in str(error.value) or "official cloud Ollama" in str(error.value)
        assert "PRIVATE_VALUE" not in str(error.value)


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
# expand_env — environment variable and vault-secret expansion
# ---------------------------------------------------------------------------

class TestExpandEnv:
    """Unit tests for the config environment-variable expansion helper."""

    def test_env_colon_var_substituted(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        result = expand_env({"key": "env:MY_TOKEN"}, vault=None)
        assert result == {"key": "secret123"}

    def test_missing_var_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_REQUIRED", raising=False)
        with pytest.raises(ConfigError, match="MISSING_REQUIRED"):
            expand_env({"key": "env:MISSING_REQUIRED"}, vault=None)

    def test_literal_string_unchanged(self):
        result = expand_env({"key": "just-a-plain-value"}, vault=None)
        assert result == {"key": "just-a-plain-value"}

    def test_partial_env_prefix_not_substituted(self):
        # Strings that start with "env:" but embed more content are opaque.
        result = expand_env({"key": "Bearer env:TOKEN"}, vault=None)
        assert result == {"key": "Bearer env:TOKEN"}

    def test_env_colon_empty_name_raises(self):
        with pytest.raises(ConfigError, match="empty or invalid"):
            expand_env({"key": "env:"}, vault=None)

    def test_env_colon_invalid_name_raises(self):
        with pytest.raises(ConfigError, match="empty or invalid"):
            expand_env({"key": "env:1INVALID"}, vault=None)

    def test_recursive_dict(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "abc:xyz")
        result = expand_env({"telegram": {"bot_token": "env:BOT_TOKEN"}}, vault=None)
        assert result == {"telegram": {"bot_token": "abc:xyz"}}

    def test_recursive_list(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-test")
        result = expand_env({"models": [{"api_key": "env:API_KEY"}]}, vault=None)
        assert result == {"models": [{"api_key": "sk-test"}]}

    def test_non_string_scalars_unchanged(self):
        result = expand_env({"count": 5, "flag": True, "rate": 0.2}, vault=None)
        assert result == {"count": 5, "flag": True, "rate": 0.2}

    def test_error_message_includes_path(self, monkeypatch):
        monkeypatch.delenv("MISSING", raising=False)
        with pytest.raises(ConfigError, match="api_key"):
            expand_env({"models": [{"api_key": "env:MISSING"}]}, vault=None)

    def test_mcp_env_section_expanded(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_KEY", "key-value")
        result = expand_env({
            "mcp_servers": [{"env": {"API_KEY": "env:UPSTREAM_KEY"}}]
        }, vault=None)
        assert result["mcp_servers"][0]["env"]["API_KEY"] == "key-value"

    def test_sec_colon_found_key_substituted(self):
        vault = {"openai_key": "vault-secret"}
        result = expand_env({"key": "sec:openai_key"}, vault=vault)
        assert result == {"key": "vault-secret"}

    def test_sec_colon_missing_key_raises(self):
        vault = {"other_key": "value"}
        with pytest.raises(ConfigError, match="Add it to the vault"):
            expand_env({"key": "sec:openai_key"}, vault=vault)

    def test_sec_colon_no_vault_raises(self):
        with pytest.raises(ConfigError, match="no vault loaded"):
            expand_env({"key": "sec:openai_key"}, vault=None)

    def test_sec_colon_and_env_colon_coexist(self, monkeypatch):
        monkeypatch.setenv("BASE_URL", "https://api.example.com")
        vault = {"api_key": "vault-key"}
        result = expand_env(
            {"api_key": "sec:api_key", "base_url": "env:BASE_URL"},
            vault=vault,
        )
        assert result == {"api_key": "vault-key", "base_url": "https://api.example.com"}


# ---------------------------------------------------------------------------
# Vault loading helpers
# ---------------------------------------------------------------------------

class TestVaultLoading:
    """Tests for vault file loading and metadata helpers.

    Vault files use TOML format: plain ``key = "value"`` assignments at the
    top level.  All values must be strings; nested tables, arrays, integers,
    booleans, and other non-string types are rejected.
    """

    def test_valid_toml_loads(self, tmp_path):
        """A minimal TOML vault file is loaded correctly."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('openai_key = "secret"\n')
        data = _load_vault(str(vault_file))
        assert data == {"openai_key": "secret"}

    def test_valid_toml_multiple_keys(self, tmp_path):
        """Multiple top-level keys are all loaded."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('openai_key = "sk-abc"\nbot_token = "1234:TOKEN"\n')
        data = _load_vault(str(vault_file))
        assert data == {"openai_key": "sk-abc", "bot_token": "1234:TOKEN"}

    def test_missing_file_raises(self, tmp_path):
        missing = tmp_path / "missing.toml"
        with pytest.raises(ConfigError, match="Cannot read vault file"):
            _load_vault(str(missing))

    def test_invalid_toml_raises(self, tmp_path):
        """Unparseable TOML raises ConfigError referencing TOML."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("= orphan value\n")  # no key — invalid TOML
        with pytest.raises(ConfigError, match="Invalid TOML"):
            _load_vault(str(vault_file))

    def test_toml_with_comments(self, tmp_path):
        """TOML comments are silently ignored."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text(
            "# Secrets vault\n"
            'openai_key = "sk-abc"\n'
            "# end\n"
        )
        data = _load_vault(str(vault_file))
        assert data == {"openai_key": "sk-abc"}

    def test_toml_multiline_values(self, tmp_path):
        """Multi-line TOML format with several keys loads correctly."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text(
            "# Production secrets\n"
            'openai_key = "sk-abc"\n'
            'bot_token  = "1234:TOKEN"\n'
            'ollama_host = "http://localhost:11434"\n'
        )
        data = _load_vault(str(vault_file))
        assert data == {
            "openai_key": "sk-abc",
            "bot_token": "1234:TOKEN",
            "ollama_host": "http://localhost:11434",
        }

    def test_nested_table_rejected(self, tmp_path):
        """A TOML nested table produces a dict value which is rejected."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("[credentials]\napi_key = \"secret\"\n")
        with pytest.raises(ConfigError, match="credentials"):
            _load_vault(str(vault_file))

    def test_array_value_rejected(self, tmp_path):
        """A TOML array value is rejected."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('my_list = ["a", "b"]\n')
        with pytest.raises(ConfigError, match="my_list"):
            _load_vault(str(vault_file))

    def test_integer_value_rejected(self, tmp_path):
        """A TOML integer value is rejected."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("port = 8080\n")
        with pytest.raises(ConfigError, match="port"):
            _load_vault(str(vault_file))

    def test_has_sec_reference_detects_nested_values(self):
        assert _has_sec_reference({"models": [{"api_key": "sec:key"}]})
        assert _has_sec_reference({"key": "sec:key"})
        assert _has_sec_reference(["sec:key"])
        assert not _has_sec_reference({"key": "plain"})
        assert not _has_sec_reference({"count": 5})


# ---------------------------------------------------------------------------
# parse_vault_content public API
# ---------------------------------------------------------------------------

class TestParseVaultContent:
    def test_parse_vault_content_toml(self):
        """parse_vault_content accepts a TOML string."""
        data = parse_vault_content('key = "value"\n', "/fake/path")
        assert data == {"key": "value"}

    def test_parse_vault_content_multiple_keys(self):
        """parse_vault_content loads multiple TOML keys."""
        data = parse_vault_content(
            'key = "value"\nother_key = "v2"\n', "/fake/path"
        )
        assert data == {"key": "value", "other_key": "v2"}

    def test_parse_vault_content_invalid_raises(self):
        """parse_vault_content raises ConfigError on unparseable TOML."""
        with pytest.raises(ConfigError, match="Invalid TOML"):
            parse_vault_content("= orphan\n", "/fake/path")

    def test_parse_vault_content_non_string_raises(self):
        """parse_vault_content raises ConfigError for non-string values."""
        with pytest.raises(ConfigError, match="my_int"):
            parse_vault_content("my_int = 42\n", "/fake/path")

    def test_parse_vault_content_lenient_allows_non_string(self):
        """require_all_strings=False returns the table including non-string values."""
        data = parse_vault_content(
            "my_int = 42\n", "/fake/path", require_all_strings=False
        )
        assert data == {"my_int": 42}

    def test_parse_vault_content_lenient_allows_sibling_table(self):
        """require_all_strings=False keeps a string key alongside a table sibling."""
        data = parse_vault_content(
            'api_key = "sk"\n[jira]\ntoken = "t"\n',
            "/fake/path",
            require_all_strings=False,
        )
        assert data["api_key"] == "sk"
        assert data["jira"] == {"token": "t"}

    def test_parse_vault_content_strict_still_raises_naming_key(self):
        """require_all_strings=True (explicit) still raises ConfigError naming the key."""
        with pytest.raises(ConfigError, match="my_int"):
            parse_vault_content(
                "my_int = 42\n", "/fake/path", require_all_strings=True
            )

    def test_parse_vault_content_lenient_still_checks_toml_format(self):
        """Format checks (TOML parse) apply even when require_all_strings=False."""
        with pytest.raises(ConfigError, match="Invalid TOML"):
            parse_vault_content(
                "= orphan\n", "/fake/path", require_all_strings=False
            )



# ---------------------------------------------------------------------------
# parse_config with secrets
# ---------------------------------------------------------------------------

class TestParseConfigWithVault:
    """Integration tests: parse_config loads and resolves vault secrets.

    Vault files use TOML format (``key = "value"`` at top level).
    """

    def test_bot_token_from_vault(self, minimal_config, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('bot_token = "99999:VAULT-TOKEN"\n')
        minimal_config["telegram"]["bot_token"] = "sec:bot_token"
        cfg = parse_config(minimal_config, vault_file=str(vault_file))
        assert cfg.telegram.bot_token == "99999:VAULT-TOKEN"
        assert cfg._raw["telegram"]["bot_token"] == "99999:VAULT-TOKEN"

    def test_api_key_from_vault(self, minimal_config, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('openai_key = "sk-vault"\n')
        minimal_config["providers"]["openai-main"]["api_key"] = "sec:openai_key"
        cfg = parse_config(minimal_config, vault_file=str(vault_file))
        assert cfg.providers["openai-main"].api_key == "sec:openai_key"
        assert cfg._raw["providers"]["openai-main"]["api_key"] == "sec:openai_key"

    def test_missing_vault_key_fails_startup(self, minimal_config, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('other_key = "value"\n')
        minimal_config["telegram"]["bot_token"] = "sec:bot_token"
        with pytest.raises(ConfigError, match="bot_token"):
            parse_config(minimal_config, vault_file=str(vault_file))

    def test_no_sec_reference_skips_vault_load(self, minimal_config):
        # With no sec: references, a missing vault_file is fine.
        cfg = parse_config(minimal_config)
        assert cfg.telegram.bot_token == "123456:ABC-DEF"

    def test_sec_reference_without_vault_file_raises(self, minimal_config):
        minimal_config["telegram"]["bot_token"] = "sec:bot_token"
        with pytest.raises(ConfigError, match="no vault file"):
            parse_config(minimal_config)

    def test_vault_config_defaults_to_file(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.vault == VaultConfig(type="file")

    def test_vault_config_custom_type(self, minimal_config):
        minimal_config["vault"] = {"type": "keyring"}
        cfg = parse_config(minimal_config)
        assert cfg.vault.type == "keyring"


# ---------------------------------------------------------------------------
# parse_config expands env vars before validation
# ---------------------------------------------------------------------------

class TestParseConfigWithEnvVars:
    """Integration tests: parse_config expands env vars before validation."""

    def test_bot_token_from_env(self, minimal_config, monkeypatch):
        monkeypatch.setenv("TG_TOKEN", "99999:TEST-TOKEN")
        minimal_config["telegram"]["bot_token"] = "env:TG_TOKEN"
        cfg = parse_config(minimal_config)
        assert cfg.telegram.bot_token == "99999:TEST-TOKEN"

    def test_api_key_from_env(self, minimal_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        minimal_config["providers"]["openai-main"]["api_key"] = "env:OPENAI_API_KEY"
        cfg = parse_config(minimal_config)
        assert cfg.providers["openai-main"].api_key == "env:OPENAI_API_KEY"

    def test_missing_required_var_fails_startup(self, minimal_config, monkeypatch):
        monkeypatch.delenv("UNSET_TOKEN", raising=False)
        minimal_config["telegram"]["bot_token"] = "env:UNSET_TOKEN"
        with pytest.raises(ConfigError, match="UNSET_TOKEN"):
            parse_config(minimal_config)

    def test_raw_dict_preserves_provider_credential_reference(self, minimal_config, monkeypatch):
        monkeypatch.setenv("RAW_KEY", "resolved-raw")
        minimal_config["providers"]["openai-main"]["api_key"] = "env:RAW_KEY"
        cfg = parse_config(minimal_config)
        assert cfg._raw["providers"]["openai-main"]["api_key"] == "env:RAW_KEY"

    def test_string_value_for_bool_field_raises(self, minimal_config):
        # A string value (which is what you'd get if env:VAR resolved a flag)
        # must be rejected rather than silently coerced.
        minimal_config["agent"] = {"diagnose_empty_responses": "false"}
        with pytest.raises(ConfigError, match="agent.diagnose_empty_responses"):
            parse_config(minimal_config)

    def test_string_value_for_int_field_raises(self, minimal_config):
        # A numeric string like "4096" must be rejected — env:VAR in numeric fields
        # is not supported; the plan restricts env references to string fields only.
        minimal_config["models"] = [{"max_tokens": "4096"}]
        with pytest.raises(ConfigError, match="models"):
            parse_config(minimal_config)

    def test_string_value_for_float_field_raises(self, minimal_config):
        minimal_config["models"] = [{"temperature": "0.5"}]
        with pytest.raises(ConfigError, match="models"):
            parse_config(minimal_config)

    def test_env_var_in_numeric_field_raises(self, minimal_config, monkeypatch):
        # env:VAR on a numeric field resolves to a string and must be rejected.
        monkeypatch.setenv("MAX_TOKENS", "4096")
        minimal_config["models"] = [{"max_tokens": "env:MAX_TOKENS"}]
        with pytest.raises(ConfigError, match="models"):
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


# ---------------------------------------------------------------------------
# AgentConfig fields
# ---------------------------------------------------------------------------

class TestAgentConfigFields:
    """Tests for agent_name defaults and overrides."""

    def test_defaults(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.agent_name == "piclaw"

    def test_custom_values(self, minimal_config):
        minimal_config["agent"]["agent_name"] = "myagent"
        cfg = parse_config(minimal_config)
        assert cfg.agent.agent_name == "myagent"

    def test_retry_defaults_and_agent_overrides(self, minimal_config):
        defaults = parse_config(minimal_config).agent
        assert defaults.max_retries == 4
        assert defaults.retry_delay == 2.0

        minimal_config["agent"].update(max_retries=0, retry_delay=0)
        configured = parse_config(minimal_config).agent
        assert configured.max_retries == 0
        assert configured.retry_delay == 0.0

    @pytest.mark.parametrize("value", [-1, True, 1.5, "4", None])
    def test_agent_max_retries_requires_nonnegative_integer(self, minimal_config, value):
        minimal_config["agent"]["max_retries"] = value
        with pytest.raises(ConfigError, match=r"agent\.max_retries"):
            parse_config(minimal_config)

    @pytest.mark.parametrize("value", [-0.1, True, "2", float("nan"), float("inf"), -float("inf")])
    def test_agent_retry_delay_requires_finite_nonnegative_number(self, minimal_config, value):
        minimal_config["agent"]["retry_delay"] = value
        with pytest.raises(ConfigError, match=r"agent\.retry_delay"):
            parse_config(minimal_config)

    def test_allow_net_defaults_false(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.allow_net is False

    def test_allow_net_true(self, minimal_config):
        minimal_config["agent"]["allow_net"] = True
        cfg = parse_config(minimal_config)
        assert cfg.agent.allow_net is True

    def test_allow_net_rejects_string(self, minimal_config):
        minimal_config["agent"]["allow_net"] = "true"
        with pytest.raises(ConfigError, match=r"agent\.allow_net"):
            parse_config(minimal_config)

    def test_legacy_shell_nsjail_network_rejected(self, minimal_config):
        minimal_config["agent"]["shell_nsjail_network"] = "none"
        with pytest.raises(ConfigError, match=r"shell_nsjail_network.*has been removed"):
            parse_config(minimal_config)


# ---------------------------------------------------------------------------
# Provider credential inheritance
# ---------------------------------------------------------------------------

class TestProviderCredentialInheritance:
    """Provider credentials stay unresolved until matching-client setup."""

    def test_provider_secret_reference_deferred_until_resolver(self, minimal_config, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('sample_key = "fake-vault-value"\n')
        minimal_config["providers"]["openai-main"]["api_key"] = "sec:sample_key"
        connection = parse_config(minimal_config, vault_file="/not-read").providers["openai-main"]
        assert connection.api_key == "sec:sample_key"
        assert "fake-vault-value" not in repr(connection)
        resolved = resolve_provider_connection(connection, vault_file=str(vault_file))
        assert resolved.api_key == "fake-vault-value"
        assert "fake-vault-value" not in repr(resolved)

    def test_env_credential_reference_is_deferred(self, minimal_config, monkeypatch):
        monkeypatch.setenv("FAKE_PROVIDER_KEY", "fake-env-value")
        minimal_config["providers"]["openai-main"]["api_key"] = "env:FAKE_PROVIDER_KEY"
        connection = parse_config(minimal_config).providers["openai-main"]
        assert connection.api_key == "env:FAKE_PROVIDER_KEY"
        assert resolve_provider_connection(connection, vault_file=None).api_key == "fake-env-value"

    def test_resolution_errors_do_not_expose_reference_or_value(self, minimal_config, monkeypatch):
        monkeypatch.delenv("MISSING_FAKE_KEY", raising=False)
        minimal_config["providers"]["openai-main"]["api_key"] = "env:MISSING_FAKE_KEY"
        connection = parse_config(minimal_config).providers["openai-main"]
        with pytest.raises(ConfigError) as error:
            resolve_provider_connection(connection, vault_file=None)
        assert "MISSING_FAKE_KEY" not in str(error.value)
        assert "env:" not in str(error.value)

    def test_parse_raw_credentials_remain_references(self, minimal_config):
        minimal_config["providers"]["openai-main"]["api_key"] = "sec:some_key"
        cfg = parse_config(minimal_config)
        assert cfg._raw["providers"]["openai-main"]["api_key"] == "sec:some_key"

    def test_embeddings_are_explicit_and_do_not_inherit(self, minimal_config):
        minimal_config.pop("embeddings")
        cfg = parse_config(minimal_config)
        assert cfg.embeddings.provider == ""
        assert cfg.embeddings.model == ""
        assert cfg.embeddings.api_key == ""
        assert cfg.embeddings.base_url == ""

    def test_embedding_reference_remains_unresolved(self, minimal_config):
        minimal_config["embeddings"]["api_key"] = "sec:embedding_key"
        cfg = parse_config(minimal_config, vault_file="/not-read")
        assert cfg.embeddings.api_key == "sec:embedding_key"
        assert cfg._raw["embeddings"]["api_key"] == "sec:embedding_key"

    def test_omitted_embeddings_section_is_unavailable(self, minimal_config):
        minimal_config.pop("embeddings", None)
        cfg = parse_config(minimal_config)
        assert "embeddings" not in cfg._raw
        assert cfg.embeddings.api_key == ""
        assert cfg.embeddings.base_url == ""

    @pytest.mark.parametrize("field", ["provider", "model", "base_url"])
    def test_missing_embedding_environment_reference_degrades_only_embeddings(
        self, minimal_config, monkeypatch, field
    ):
        variable = f"MISSING_EMBEDDING_{field.upper()}_SENTINEL"
        monkeypatch.delenv(variable, raising=False)
        minimal_config["embeddings"][field] = f"env:{variable}"

        cfg = parse_config(minimal_config)

        assert cfg.providers["openai-main"].type == "openai"
        assert cfg.embeddings.unavailable_reason.startswith(f"embeddings.{field}")
        assert variable not in cfg.embeddings.unavailable_reason
        assert variable not in repr(cfg.embeddings)
        if field != "base_url":
            assert getattr(cfg.embeddings, field) == ""

    def test_invalid_embedding_provider_is_unavailable_without_chat_fallback(self, minimal_config):
        minimal_config["embeddings"]["provider"] = "missing-connection"
        minimal_config["embeddings"]["model"] = "explicit-embedding-model"

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.provider == ""
        assert cfg.embeddings.model == ""
        assert "supported adapter type" in cfg.embeddings.unavailable_reason

    def test_explicit_adapter_credentials_do_not_require_matching_chat_connection(self, minimal_config):
        minimal_config["providers"] = {
            "chat-account": {"type": "anthropic", "api_key": "fake-chat-key"}
        }
        minimal_config["embeddings"] = {
            "provider": "openai",
            "model": "explicit-embedding-model",
            "api_key": "sec:embedding_key",
            "base_url": "https://embedding.example.test/v1",
        }

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.provider == "openai"
        assert cfg.embeddings.model == "explicit-embedding-model"
        assert cfg.embeddings.api_key == "sec:embedding_key"
        assert cfg.embeddings.base_url == "https://embedding.example.test/v1"
        assert cfg.embeddings.unavailable_reason == ""
        assert "embedding_key" not in repr(cfg.embeddings)

    def test_explicit_adapter_uses_normal_endpoint_default_when_url_omitted(self, minimal_config):
        """Rewritten for 5.1: there is no endpoint default; a missing ``base_url`` is unavailable."""
        minimal_config["providers"] = {
            "chat-account": {"type": "anthropic", "api_key": "fake-chat-key"}
        }
        minimal_config["embeddings"] = {
            "provider": "openai", "model": "explicit-embedding-model", "api_key": "fake-key"
        }

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.unavailable_reason == "embeddings.base_url must be configured explicitly"
        assert cfg.embeddings.base_url == ""

    def test_unknown_embedding_adapter_is_unavailable_and_sanitized(self, minimal_config):
        minimal_config["embeddings"] = {
            "provider": "PRIVATE_UNKNOWN_PROVIDER_SENTINEL",
            "model": "sec:PRIVATE_MODEL_SENTINEL",
            "api_key": "sec:PRIVATE_KEY_SENTINEL",
            "base_url": "https://private.example.test",
        }

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.unavailable_reason
        for value in (
            "PRIVATE_UNKNOWN_PROVIDER_SENTINEL",
            "PRIVATE_MODEL_SENTINEL",
            "PRIVATE_KEY_SENTINEL",
            "private.example.test",
            "sec:",
        ):
            assert value not in repr(cfg.embeddings)
            assert value not in repr(cfg)

    @pytest.mark.parametrize(
        "section",
        [
            {"provider": 7, "model": "sec:PRIVATE_MODEL_SENTINEL"},
            {"provider": "openai", "model": "sec:PRIVATE_MODEL_SENTINEL", "base_url": 3},
            {"provider": ["openai"], "model": "sec:PRIVATE_MODEL_SENTINEL", "api_key": []},
        ],
    )
    def test_any_embedding_shape_failure_returns_safe_unavailable_config(self, minimal_config, section):
        minimal_config["embeddings"] = section

        cfg = parse_config(minimal_config)

        assert cfg.embeddings.unavailable_reason
        assert cfg.embeddings.provider == ""
        assert cfg.embeddings.model == ""
        assert cfg.embeddings.api_key == ""
        assert cfg.embeddings.base_url == ""
        assert "PRIVATE_MODEL_SENTINEL" not in repr(cfg.embeddings)
        assert "sec:" not in repr(cfg.embeddings)
        assert "PRIVATE_MODEL_SENTINEL" not in repr(cfg)

    def test_invalid_embedding_url_is_unavailable_not_chat_fatal(self, minimal_config):
        minimal_config["embeddings"]["base_url"] = "https://[private-value"

        cfg = parse_config(minimal_config)

        assert cfg.providers["openai-main"].type == "openai"
        assert cfg.embeddings.unavailable_reason == "embeddings.base_url must be a valid HTTP(S) URL"
        assert "private-value" not in cfg.embeddings.unavailable_reason


# ---------------------------------------------------------------------------
# Removed file-secret fields — must fail startup with ConfigError
# ---------------------------------------------------------------------------

class TestRemovedFileSecretFields:
    """Legacy removed fields must be rejected with clear migration guidance."""

    def test_telegram_bot_token_file_raises(self, minimal_config):
        minimal_config["telegram"]["bot_token_file"] = "/run/secrets/bot_token"
        with pytest.raises(ConfigError, match="bot_token_file"):
            parse_config(minimal_config)

    def test_telegram_bot_token_file_migration_guidance(self, minimal_config):
        minimal_config["telegram"]["bot_token_file"] = "/run/secrets/bot_token"
        with pytest.raises(ConfigError, match="bot_token"):
            parse_config(minimal_config)

    def test_providers_api_key_file_raises(self, minimal_config):
        minimal_config["providers"] = {
            "openai": {"api_key_file": "/run/secrets/openai_key"}
        }
        with pytest.raises(ConfigError, match="api_key_file"):
            parse_config(minimal_config)

    def test_model_api_key_file_raises(self, minimal_config):
        minimal_config["models"] = [{"api_key_file": "/run/secrets/openai_key"}]
        with pytest.raises(ConfigError, match="models"):
            parse_config(minimal_config)

    def test_embeddings_api_key_file_raises(self, minimal_config):
        minimal_config["embeddings"] = {
            "provider": "openai",
            "api_key_file": "/run/secrets/openai_key",
        }
        with pytest.raises(ConfigError, match="api_key_file"):
            parse_config(minimal_config)

    def test_error_message_includes_sec_migration_hint(self, minimal_config):
        """Error must mention sec: or vault as the migration path."""
        minimal_config["telegram"]["bot_token_file"] = "/run/secrets/bot_token"
        with pytest.raises(ConfigError, match=r"sec:"):
            parse_config(minimal_config)

    def test_providers_api_key_file_includes_migration_hint(self, minimal_config):
        minimal_config["providers"] = {
            "openai": {"api_key_file": "/run/secrets/openai_key"}
        }
        with pytest.raises(ConfigError, match=r"providers\.openai\.api_key_file"):
            parse_config(minimal_config)


# ---------------------------------------------------------------------------
# _load_vault rejects non-string values
# ---------------------------------------------------------------------------

class TestLoadVaultNonStringValues:
    """_load_vault must reject vault entries whose values are not strings.

    Vault files use TOML format.  TOML integers, booleans, floats, arrays, and
    nested tables are all non-string types and must be rejected.
    """

    def test_non_string_int_value_raises(self, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("my_key = 12345\n")
        with pytest.raises(ConfigError, match="my_key"):
            _load_vault(str(vault_file))

    def test_non_string_bool_value_raises(self, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("flag = true\n")
        with pytest.raises(ConfigError, match="flag"):
            _load_vault(str(vault_file))

    def test_non_string_float_value_raises(self, tmp_path):
        """TOML floats are not strings and must be rejected."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("my_key = 3.14\n")
        with pytest.raises(ConfigError, match="my_key"):
            _load_vault(str(vault_file))

    def test_non_string_array_value_raises(self, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('my_key = ["a", "b"]\n')
        with pytest.raises(ConfigError, match="my_key"):
            _load_vault(str(vault_file))

    def test_non_string_nested_table_raises(self, tmp_path):
        """A TOML nested table header produces a dict value — must be rejected."""
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('[my_key]\nnested = "value"\n')
        with pytest.raises(ConfigError, match="my_key"):
            _load_vault(str(vault_file))

    def test_all_string_values_loads_successfully(self, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('api_key = "sk-abc"\ntoken = "tok123"\n')
        data = _load_vault(str(vault_file))
        assert data == {"api_key": "sk-abc", "token": "tok123"}

    def test_error_message_includes_key_name(self, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text("secret_num = 99\n")
        with pytest.raises(ConfigError, match="secret_num"):
            _load_vault(str(vault_file))


# ---------------------------------------------------------------------------
# Path tilde expansion
# ---------------------------------------------------------------------------

class TestPathsExpansion:
    """Tilde references in the surviving [paths] fields are expanded at parse time."""

    def test_workspace_dir_default_expanded(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.paths.workspace_dir == os.path.expanduser("~/Documents/piclaw_workspace")

    def test_workspace_dir_tilde_expanded(self, minimal_config):
        minimal_config["paths"]["workspace_dir"] = "~/my-workspace"
        cfg = parse_config(minimal_config)
        assert cfg.paths.workspace_dir == os.path.expanduser("~/my-workspace")

    def test_prompts_dir_no_tilde_unchanged(self, minimal_config):
        minimal_config["paths"]["prompts_dir"] = "prompts"
        cfg = parse_config(minimal_config)
        assert cfg.paths.prompts_dir == "prompts"

    def test_absolute_paths_unchanged(self, minimal_config):
        minimal_config["paths"]["workspace_dir"] = "/opt/workspace"
        cfg = parse_config(minimal_config)
        assert cfg.paths.workspace_dir == "/opt/workspace"

    def test_raw_paths_dict_also_expanded(self, minimal_config):
        minimal_config["paths"]["workspace_dir"] = "~/my-workspace"
        cfg = parse_config(minimal_config)
        assert cfg._raw["paths"]["workspace_dir"] == cfg.paths.workspace_dir


def test_provider_base_url_preserves_terminal_query_slash(minimal_config):
    minimal_config["providers"]["openai-main"]["base_url"] = "https://gw.test/v1?sig=a/"
    connection = parse_config(minimal_config).providers["openai-main"]
    assert connection.base_url == "https://gw.test/v1?sig=a/"
    resolved = resolve_provider_connection(connection, vault_file=None)
    assert resolved.base_url == "https://gw.test/v1?sig=a/"


def test_provider_base_url_preserves_terminal_fragment_slash(minimal_config):
    minimal_config["providers"]["openai-main"]["base_url"] = "https://gw.test/v1#frg/"
    connection = parse_config(minimal_config).providers["openai-main"]
    assert connection.base_url == "https://gw.test/v1#frg/"
    resolved = resolve_provider_connection(connection, vault_file=None)
    assert resolved.base_url == "https://gw.test/v1#frg/"


def test_provider_connection_map_is_immutable_after_parse(minimal_config):
    """Task 3.1: ``[providers.<id>]`` is a startup-fixed, read-only connection map."""
    import dataclasses

    cfg = parse_config(minimal_config)
    original = cfg.providers["openai-main"]
    replacement = dataclasses.replace(original, base_url="https://moved.invalid/v1")

    with pytest.raises(TypeError):
        cfg.providers["openai-main"] = replacement  # type: ignore[index]
    with pytest.raises(TypeError):
        cfg.providers["other"] = replacement  # type: ignore[index]
    with pytest.raises(TypeError):
        del cfg.providers["openai-main"]  # type: ignore[attr-defined]
    for mutator in ("pop", "clear", "update", "setdefault", "popitem"):
        assert not hasattr(cfg.providers, mutator), mutator
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.base_url = "https://moved.invalid/v1"  # type: ignore[misc]

    # Editing the source document after parsing never reaches the parsed map.
    minimal_config["providers"]["openai-main"]["base_url"] = "https://moved.invalid/v1"
    minimal_config["providers"]["late"] = {"type": "openai", "api_key": "x"}
    assert list(cfg.providers) == ["openai-main"]
    assert cfg.providers["openai-main"] is original
    assert original.base_url == "https://api.openai.com/v1"


def test_directly_constructed_app_config_freezes_provider_map(minimal_config):
    """A caller-supplied dict is copied into a read-only map at construction."""
    import dataclasses

    parsed = parse_config(minimal_config)
    source = dict(parsed.providers)
    cfg = dataclasses.replace(parsed, providers=source)
    source["late"] = parsed.providers["openai-main"]

    assert list(cfg.providers) == ["openai-main"]
    with pytest.raises(TypeError):
        cfg.providers["late"] = parsed.providers["openai-main"]  # type: ignore[index]


class TestRequiredOutputCapSetting:
    """Pulled-forward 6.1 slice (user-approved under 4.1): ``agent.required_output_cap_tokens``."""

    def test_default_is_1024_and_obsolete_ctx_key_is_untouched(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.required_output_cap_tokens == 1024
        assert cfg.agent.ctx_max_tokens == 90_000  # removal belongs to task 6.1 proper

    @pytest.mark.parametrize("value", [1, 4096, 128_000])
    def test_positive_integer_is_used(self, minimal_config, value):
        minimal_config["agent"]["required_output_cap_tokens"] = value
        assert parse_config(minimal_config).agent.required_output_cap_tokens == value

    @pytest.mark.parametrize(
        "value", [True, False, 0, -1, 1.5, 1024.0, "1024", ""],
        ids=["true", "false", "zero", "negative", "float", "integral-float", "string", "empty-string"],
    )
    def test_invalid_values_fail_with_actionable_error_naming_the_key(self, minimal_config, value):
        minimal_config["agent"]["required_output_cap_tokens"] = value
        with pytest.raises(ConfigError) as caught:
            parse_config(minimal_config)
        message = str(caught.value)
        assert "agent.required_output_cap_tokens" in message and "positive integer" in message
        assert "ctx_max_tokens" not in message
