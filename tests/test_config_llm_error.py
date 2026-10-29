"""Tests for the [llm_error_handling] config section."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from config_schema import AppConfig, LLMErrorHandlingConfig, parse_config


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
            "ctx_max_tokens": 90000,
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
    """Custom [llm_error_handling] values are parsed correctly."""
    raw = _make_config(
        {"llm_error_handling": {"retry_timeout_seconds": 60, "checkpoint_enabled": False}}
    )
    app_cfg: AppConfig = parse_config(raw)

    assert app_cfg.llm_error_handling.retry_timeout_seconds == 60
    assert app_cfg.llm_error_handling.checkpoint_enabled is False
    assert isinstance(app_cfg.llm_error_handling, LLMErrorHandlingConfig)


def test_llm_error_handling_defaults(minimal_config: dict[str, Any]) -> None:
    """When [llm_error_handling] is absent, defaults are applied."""
    app_cfg: AppConfig = parse_config(minimal_config)

    assert app_cfg.llm_error_handling.retry_timeout_seconds == 120
    assert app_cfg.llm_error_handling.checkpoint_enabled is True
    assert isinstance(app_cfg.llm_error_handling, LLMErrorHandlingConfig)
