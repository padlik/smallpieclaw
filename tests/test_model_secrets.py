"""Credential resolution and secret-representation boundaries."""

from __future__ import annotations

import pytest

from config_schema import ConfigError, ProviderConfig, parse_config, resolve_provider_connection


def _config(api_key: str) -> dict:
    """Return a minimal test config with one named fake connection."""
    return {
        "telegram": {"bot_token": "fake-telegram-token"},
        "providers": {
            "account-one": {
                "type": "openai",
                "api_key": api_key,
                "base_url": "https://api.example.invalid/v1",
                "request_timeout": 30,
            }
        },
    }


def test_connection_and_raw_config_keep_secret_reference_out_of_repr() -> None:
    """Parsing preserves a reference and does not reveal it in typed repr."""
    app_config = parse_config(_config("sec:fake_key"))
    connection = app_config.providers["account-one"]

    assert connection.api_key == "sec:fake_key"
    assert "fake_key" not in repr(connection)
    assert "fake_key" not in repr(app_config)
    assert app_config._raw["providers"]["account-one"]["api_key"] == "sec:fake_key"


def test_runtime_resolver_reads_fake_vault_without_leaking_failures(tmp_path) -> None:
    """Only runtime resolution reads the secret and errors stay sanitized."""
    vault = tmp_path / "vault.toml"
    vault.write_text('fake_key = "fake-secret-value"\n', encoding="utf-8")
    connection = parse_config(_config("sec:fake_key")).providers["account-one"]

    resolved = resolve_provider_connection(connection, vault_file=str(vault))
    assert resolved.api_key == "fake-secret-value"
    assert "fake-secret-value" not in repr(resolved)

    with pytest.raises(ConfigError) as error:
        resolve_provider_connection(connection, vault_file=str(tmp_path / "missing.toml"))
    assert "fake_key" not in str(error.value)
    assert "fake-secret-value" not in str(error.value)


def test_resolved_snapshot_hides_api_key_and_provider_endpoint() -> None:
    """Resolved provider secrets and transport URLs are excluded from repr."""
    connection = ProviderConfig(
        name="account-one",
        type="openai",
        api_key="fake-secret-value",
        base_url="https://private.example.invalid/v1",
        request_timeout=10,
    )
    resolved = resolve_provider_connection(connection, vault_file=None)
    assert "fake-secret-value" not in repr(resolved)
    assert "private.example.invalid" not in repr(resolved)


@pytest.mark.parametrize(
    ("connection_id", "valid"),
    [
        ("a", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("team/account", False),
        ("team account", False),
        ("_team", False),
        ("équipe", False),
        ("", False),
    ],
)
def test_quoted_toml_connection_ids_use_settings_codec(
    tmp_path, connection_id, valid, caplog
) -> None:
    """Quoted TOML keys obey exact managed-settings IDs without aliasing."""
    from main import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "[telegram]\n"
        'bot_token = "fake-token"\n'
        f'[providers."{connection_id}"]\n'
        'type = "openai"\n'
        'api_key = "env:PRIVATE_REFERENCE_SENTINEL"\n',
        encoding="utf-8",
    )

    if valid:
        _, app_config = load_config(path, vault_file=None)
        assert list(app_config.providers) == [connection_id]
        assert app_config.providers[connection_id].name == connection_id
    else:
        with pytest.raises(SystemExit):
            load_config(path, vault_file=None)
        if connection_id:
            assert connection_id not in caplog.text
    assert "PRIVATE_REFERENCE_SENTINEL" not in caplog.text
