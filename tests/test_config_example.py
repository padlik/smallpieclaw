"""Validation of the shipped ``config.toml.example`` (task 10.3).

The example must load through the production parser exactly as an operator
copies it. ``parse_config`` resolves ``sec:``/``env:`` references everywhere
except the provider and embedding sections (deferred to the matching client),
so the only active resolvable reference is the Telegram bot token — supplied
here through a temporary vault file. The example must also stay free of the
removed ``[[models]]``/selector configuration, and re-adding it must be
rejected actionably.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

try:
    import tomli as _toml  # type: ignore[import]
except ImportError:
    import tomllib as _toml  # type: ignore[no-redef]  # Python 3.11+

from config_schema import parse_config
from exceptions import ConfigError

_EXAMPLE = Path(__file__).resolve().parent.parent / "config.toml.example"
# Removed legacy configuration: [[models]] tables, model selectors, and
# per-model tuning. None of these keys may appear anywhere in the example.
_FORBIDDEN_KEYS = frozenset({
    "models", "default_model", "background_model", "fallback_model", "fallback_models",
    "temperature", "max_tokens", "vision", "aliases", "ctx_max_tokens",
})


def _example_text() -> str:
    """The shipped example configuration, verbatim."""
    return _EXAMPLE.read_text(encoding="utf-8")


def _parsed_example() -> dict[str, Any]:
    """The example parsed as plain TOML, before any config validation."""
    return _toml.loads(_example_text())


def _temp_vault(tmp_path: Path) -> str:
    """A minimal vault holding the one active ``sec:`` reference in the example."""
    vault = tmp_path / "secrets.toml"
    vault.write_text('TELEGRAM_BOT_TOKEN = "example-test-token"\n', encoding="utf-8")
    return str(vault)


def test_example_config_parses_with_production_parser(tmp_path: Path) -> None:
    """The example, copied as-is, loads through ``parse_config`` with a temp vault."""
    app_config = parse_config(
        _parsed_example(), vault_file=_temp_vault(tmp_path), agent_name="piclaw",
    )

    assert app_config.providers["openai-main"].type == "openai"
    assert app_config.telegram.bot_token == "example-test-token"


def test_example_config_has_no_legacy_model_keys() -> None:
    """No table anywhere in the example carries a removed models/selector key."""

    def present_keys(value: Any) -> set[str]:
        """Every mapping key reachable from *value*, through tables and arrays."""
        found: set[str] = set()
        if isinstance(value, dict):
            found.update(value)
            for child in value.values():
                found |= present_keys(child)
        elif isinstance(value, list):
            for child in value:
                found |= present_keys(child)
        return found

    assert present_keys(_parsed_example()).isdisjoint(_FORBIDDEN_KEYS)


@pytest.mark.parametrize(
    ("append_legacy", "message"),
    [
        pytest.param(
            lambda example: example.update(models=[{
                "name": "legacy-model", "provider": "openai-main", "model": "gpt-x",
            }]),
            "models",
            id="models-entry",
        ),
        pytest.param(
            lambda example: example["agent"].update(default_model="gpt-x"),
            r"agent\.default_model",
            id="agent-default-model",
        ),
        pytest.param(
            lambda example: example["agent"].update(ctx_max_tokens=90000),
            r"agent\.ctx_max_tokens.*agent\.compaction_threshold_tokens",
            id="agent-ctx-max-tokens",
        ),
    ],
)
def test_example_config_rejects_appended_legacy_keys(
    tmp_path: Path, append_legacy: Callable[[dict[str, Any]], None], message: str,
) -> None:
    """Re-adding a legacy ``[[models]]`` entry or selector is rejected actionably."""
    example = _parsed_example()
    append_legacy(example)

    with pytest.raises(ConfigError, match=message):
        parse_config(example, vault_file=_temp_vault(tmp_path), agent_name="piclaw")


def test_example_sets_the_default_compaction_threshold(tmp_path: Path) -> None:
    """The example carries the documented default threshold; the cap keeps its default."""
    app_config = parse_config(
        _parsed_example(), vault_file=_temp_vault(tmp_path), agent_name="piclaw",
    )

    assert app_config.agent.compaction_threshold_tokens == 60000
    assert app_config.agent.required_output_cap_tokens == 1024


def test_example_provider_credentials_stay_unresolved(tmp_path: Path) -> None:
    """Provider credential references survive parsing as unresolved strings."""
    app_config = parse_config(
        _parsed_example(), vault_file=_temp_vault(tmp_path), agent_name="piclaw",
    )

    assert app_config.providers["openai-main"].api_key == "sec:OPENAI_API_KEY"

def test_example_documents_durable_recovery_without_the_retired_timeout(tmp_path: Path) -> None:
    """The shipped example keeps checkpointing on and carries no active retry-timeout key."""
    parsed = _parsed_example()
    assert parsed["llm_error_handling"] == {"checkpoint_enabled": True}
    app_config = parse_config(parsed, vault_file=_temp_vault(tmp_path), agent_name="piclaw")
    assert app_config.llm_error_handling.checkpoint_enabled is True
    text = _example_text()
    assert "/resume" in text and "retry_timeout_seconds" in text  # only in the removal note


def test_example_rejects_the_retired_retry_timeout_when_re_added(tmp_path: Path) -> None:
    parsed = _parsed_example()
    parsed["llm_error_handling"]["retry_timeout_seconds"] = 120
    with pytest.raises(ConfigError, match="llm_error_handling.retry_timeout_seconds"):
        parse_config(parsed, vault_file=_temp_vault(tmp_path), agent_name="piclaw")
