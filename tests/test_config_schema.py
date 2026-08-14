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
)


class TestParseConfig:
    """Happy-path parsing tests."""

    def test_minimal_valid_config(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert isinstance(cfg, AppConfig)
        assert cfg.telegram.bot_token == "123456:ABC-DEF"
        assert cfg.agent.max_iterations == 8
        assert cfg.agent.agent_name == "piclaw"
        assert len(cfg.models) == 1
        assert cfg.models[0].provider == "openai"
        assert cfg.vault.type == "file"

    def test_defaults_applied(self, minimal_config):
        cfg = parse_config(minimal_config)
        assert cfg.agent.tool_timeout == 10
        assert cfg.agent.max_output_size == 4000
        assert cfg.agent.ctx_max_tokens == 90_000
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
        # _raw holds the resolved (env-expanded) copy, plus expanded defaults for
        # path fields and agent_home. Compare non-path sections that exist.
        assert cfg._raw["telegram"] == minimal_config["telegram"]
        assert cfg._raw["models"] == minimal_config["models"]
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
        minimal_config["models"][0]["api_key"] = "sec:openai_key"
        cfg = parse_config(minimal_config, vault_file=str(vault_file))
        assert cfg.models[0].api_key == "sk-vault"
        assert cfg._raw["models"][0]["api_key"] == "sk-vault"

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

    def test_provider_sec_value_inherited_by_model(self, minimal_config, tmp_path):
        vault_file = tmp_path / "secrets.toml"
        vault_file.write_text('openai_key = "provider-vault-key"\n')
        minimal_config["providers"] = {"openai": {"api_key": "sec:openai_key"}}
        del minimal_config["models"][0]["api_key"]

        cfg = parse_config(minimal_config, vault_file=str(vault_file))

        assert cfg.providers["openai"].api_key == "provider-vault-key"
        assert cfg.models[0].api_key == "provider-vault-key"
        assert cfg._raw["providers"]["openai"]["api_key"] == "provider-vault-key"
        assert cfg._raw["models"][0]["api_key"] == "provider-vault-key"

    def test_legacy_config_raw_dict_remains_unchanged(self, minimal_config):
        cfg = parse_config(minimal_config)

        # Only non-path sections remain identical because path fields are
        # normalized at parse time.
        assert cfg._raw["telegram"] == minimal_config["telegram"]
        assert cfg._raw["models"] == minimal_config["models"]
        if "embeddings" in minimal_config:
            assert cfg._raw["embeddings"] == minimal_config["embeddings"]
        if "scheduler" in minimal_config:
            assert cfg._raw["scheduler"] == minimal_config["scheduler"]

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
        minimal_config["models"][0]["api_key_file"] = "/run/secrets/openai_key"
        with pytest.raises(ConfigError, match="api_key_file"):
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
        with pytest.raises(ConfigError, match=r"sec:"):
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
