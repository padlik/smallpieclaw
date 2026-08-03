"""
Shared pytest fixtures for the smallpieclaw test suite.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_config() -> dict:
    """Minimal valid configuration dict (raw TOML-like structure)."""
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
        "models": [
            {
                "name": "test-model",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-test-key",
                "base_url": "https://api.openai.com/v1",
            }
        ],
        "paths": {
            "workspace_dir": "~/Documents",
        },
    }


# ---------------------------------------------------------------------------
# LLM mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_response():
    """Factory fixture returning a mock LLM that responds with given JSON."""
    def _factory(response_json: dict | str):
        if isinstance(response_json, dict):
            response_json = json.dumps(response_json)
        mock = MagicMock()
        mock.chat.return_value = response_json
        mock.chat_with_fallback.return_value = response_json
        mock.embed.return_value = [0.1] * 128
        return mock
    return _factory


@pytest.fixture
def finish_response():
    """A standard finish action JSON string."""
    return json.dumps({"action": "finish", "result": "Task completed successfully."})


@pytest.fixture
def shell_response():
    """A standard shell tool call JSON string."""
    return json.dumps({
        "action": "tool",
        "tool": "shell",
        "args": {"command": "echo hello"},
        "thought": "I need to run a command.",
    })


# ---------------------------------------------------------------------------
# Subprocess/tool mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run to return a controlled result."""
    with patch("subprocess.run") as mock_run:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "mock output"
        result.stderr = ""
        mock_run.return_value = result
        yield mock_run


# ---------------------------------------------------------------------------
# Temp directory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_xdg(tmp_path, monkeypatch):
    """Override all XDG env vars to tmp_path subdirs. Tests never touch real home.

    XDG_RUNTIME_DIR itself is pre-created, mirroring systemd-logind's real-world
    guarantee — only the agent-scoped subdirectory under it is created lazily.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    return tmp_path
