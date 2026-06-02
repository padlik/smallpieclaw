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
            "tools_dir": "tools",
            "generated_tools_dir": "tools_generated",
            "data_dir": "data",
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
def tmp_agent_dir(tmp_path):
    """Create a temporary agent working directory with standard subdirs."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools_generated").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "downloads").mkdir()
    return tmp_path
