"""Tests for the [llm_error_handling] config section."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from config_schema import AppConfig, LLMErrorHandlingConfig, parse_config
from exceptions import ConfigError


def _make_config(extra: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal valid config with *extra* merged on top."""
    return {
        "telegram": {
            "bot_token": "123456:ABC-DEF",
            "allowed_user_ids": [12345],
            "security_mode": "allowlist",
        },
        "agent": {
            "max_iterations": 8,
            "scheduled_max_iterations": 100,
            "tool_timeout": 10,
            "max_output_size": 4000,
            "top_tools": 3,
            "compaction_threshold_tokens": 60000,
        },
        "providers": {
            "openai-main": {
                "type": "openai",
                "api_key": "test-provider-key",
                "base_url": "https://api.openai.com/v1",
            }
        },
        "paths": {},
        **extra,
    }


@pytest.fixture
def minimal_config(minimal_config: dict[str, Any]) -> dict[str, Any]:
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


def test_llm_error_handling_section_present() -> None:
    """A custom checkpoint setting is parsed; no retry timeout exists any more."""
    raw = _make_config({"llm_error_handling": {"checkpoint_enabled": False}})
    app_cfg: AppConfig = parse_config(raw)

    assert app_cfg.llm_error_handling.checkpoint_enabled is False
    assert isinstance(app_cfg.llm_error_handling, LLMErrorHandlingConfig)
    assert not hasattr(app_cfg.llm_error_handling, "retry_timeout_seconds")


def test_llm_error_handling_defaults(minimal_config: dict[str, Any]) -> None:
    """When [llm_error_handling] is absent, checkpointing defaults on and no timeout is created."""
    app_cfg: AppConfig = parse_config(minimal_config)

    assert app_cfg.llm_error_handling.checkpoint_enabled is True
    assert isinstance(app_cfg.llm_error_handling, LLMErrorHandlingConfig)
    assert not hasattr(app_cfg.llm_error_handling, "retry_timeout_seconds")


def test_empty_llm_error_handling_section_keeps_checkpoint_default() -> None:
    assert parse_config(_make_config({"llm_error_handling": {}})).llm_error_handling.checkpoint_enabled is True


@pytest.mark.parametrize("enabled", [True, False])
def test_checkpoint_enabled_boolean_is_retained(enabled: bool) -> None:
    raw = _make_config({"llm_error_handling": {"checkpoint_enabled": enabled}})
    assert parse_config(raw).llm_error_handling.checkpoint_enabled is enabled


@pytest.mark.parametrize("value", [120, 0, 1, -5, 60.5, "120", "env:SECONDS", True])
def test_obsolete_retry_timeout_is_rejected_with_any_value(value: Any) -> None:
    raw = _make_config({"llm_error_handling": {"retry_timeout_seconds": value}})
    with pytest.raises(ConfigError) as caught:
        parse_config(raw)
    message = str(caught.value)
    assert "llm_error_handling.retry_timeout_seconds" in message
    assert "remove" in message.lower() and "Retry" in message  # actionable removal guidance


def test_obsolete_retry_timeout_is_rejected_alongside_valid_settings() -> None:
    raw = _make_config(
        {"llm_error_handling": {"retry_timeout_seconds": 60, "checkpoint_enabled": False}}
    )
    with pytest.raises(ConfigError, match="retry_timeout_seconds"):
        parse_config(raw)
