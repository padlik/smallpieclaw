"""
Shared pytest fixtures for the smallpieclaw test suite.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config_schema import AgentConfig, ExecutorPaths

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
        "paths": {},
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
# Agent controller factory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_agent_controller():
    """Factory fixture returning an AgentController with default typed config.

    Builds the required ``AgentConfig`` and ``ExecutorPaths`` bundles, then
    constructs ``AgentController`` with MagicMock collaborators by default.
    Tests can override the config bundles or any explicit collaborator kwarg::

        ctrl = make_agent_controller(llm=my_llm, depth=1)
        ctrl = make_agent_controller(
            agent_cfg=AgentConfig(max_iterations=3),
            paths=ExecutorPaths(tmp_dir=str(tmp_path)),
        )
    """
    from agent_controller import AgentController

    def _factory(**kwargs):
        agent_cfg = kwargs.pop("agent_cfg", AgentConfig())
        paths = kwargs.pop("paths", ExecutorPaths())
        kwargs.setdefault("llm", MagicMock())
        kwargs.setdefault("tool_index", MagicMock())
        kwargs.setdefault("memory", MagicMock())
        return AgentController(agent_cfg=agent_cfg, paths=paths, **kwargs)

    return _factory


# ---------------------------------------------------------------------------
# Builtin executor factory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_builtin_executor(tmp_path):
    """Factory fixture returning a BuiltinExecutor with default typed config.

    Builds the required ``AgentConfig`` and ``ExecutorPaths`` bundles, then
    constructs ``BuiltinExecutor`` with MagicMock collaborators by default.
    Tests can override the config bundles or any explicit collaborator kwarg::

        ex = make_builtin_executor()
        ex = make_builtin_executor(
            agent_cfg=AgentConfig(tool_timeout=30, max_output_size=50),
            paths=ExecutorPaths(data_dir=str(tmp_path)),
            sub_agent_factory=my_factory,
        )

    For convenience, old flat ``BuiltinExecutor`` kwargs are translated into
    the appropriate config/path bundle fields so existing tests need only
    change the constructor name. Recognised legacy kwargs:

    * ``default_timeout`` -> ``agent_cfg.tool_timeout``
    * ``max_output`` -> ``agent_cfg.max_output_size``
    * ``shell_backend``, ``shell_pty_cols``, ``shell_pty_rows``,
      ``shell_streaming``, ``shell_nsjail_confirm_mode``,
      ``shell_nsjail_memory_mb``, ``shell_nsjail_pids_max``,
      ``shell_nsjail_cpu_percent``, ``shell_nsjail_dump_config_on_error``,
      ``allow_net``, ``nsjail_dns_nameserver`` (``dns_nameserver``),
      ``max_subagents``, ``subagent_result_timeout``, ``agent_name`` ->
      matching ``AgentConfig`` fields.
    * ``data_dir``, ``state_home``, ``workspace_dir``, ``tmp_dir``,
      ``skills_dir``, ``vault_path``, ``log_jsonl_path``,
      ``nsjail_session_tmpdir``, ``nsjail_trusted_dirs_path``,
      ``nsjail_agent_dir``, ``vault_secrets`` -> matching ``ExecutorPaths``
      fields.
    """
    from builtin_executor import BuiltinExecutor

    # AgentConfig fields the fixture can translate from legacy kwargs.
    _AGENT_FIELDS: set[str] = {
        "agent_name", "max_iterations", "scheduled_max_iterations",
        "tool_timeout", "max_output_size", "top_tools", "ctx_max_tokens",
        "max_subagents", "subagent_result_timeout", "long_run_warn_minutes",
        "diagnose_empty_responses", "default_model", "background_model",
        "fallback_models", "shell_backend", "shell_pty_cols",
        "shell_pty_rows", "shell_streaming", "shell_nsjail_confirm_mode",
        "shell_nsjail_memory_mb", "shell_nsjail_pids_max",
        "shell_nsjail_cpu_percent", "shell_nsjail_dump_config_on_error",
        "allow_net", "dns_nameserver", "session_logs_retention_days",
        "creativity_mode", "plan_max_iterations", "inactivity_warn_minutes",
    }
    # Legacy kwarg aliases for AgentConfig fields.
    _AGENT_ALIASES: dict[str, str] = {
        "default_timeout": "tool_timeout",
        "max_output": "max_output_size",
        "nsjail_dns_nameserver": "dns_nameserver",
    }
    # ExecutorPaths fields the fixture can translate from legacy kwargs.
    _PATHS_FIELDS: set[str] = {
        "tmp_dir", "downloads_dir", "workspace_dir", "log_file",
        "log_backup_count", "data_dir", "state_home", "skills_dir",
        "vault_path", "log_jsonl_path", "nsjail_session_tmpdir",
        "nsjail_trusted_dirs_path", "nsjail_agent_dir", "vault_secrets",
    }
    # Collaborators passed straight through to BuiltinExecutor.
    _COLLABORATORS: set[str] = {
        "scheduler", "sub_agent_factory", "memory", "working", "results",
        "notify_html_fn", "context_monitor",
    }

    def _factory(**kwargs):
        agent_cfg = kwargs.pop("agent_cfg", None)
        paths = kwargs.pop("paths", None)

        agent_overrides: dict[str, Any] = {}
        paths_overrides: dict[str, Any] = {}
        collaborators: dict[str, Any] = {}

        for key, value in list(kwargs.items()):
            if key in _COLLABORATORS:
                collaborators[key] = kwargs.pop(key)
            elif key in _AGENT_ALIASES:
                agent_overrides[_AGENT_ALIASES[key]] = kwargs.pop(key)
            elif key in _AGENT_FIELDS:
                agent_overrides[key] = kwargs.pop(key)
            elif key in _PATHS_FIELDS:
                paths_overrides[key] = kwargs.pop(key)
            # else: leave in kwargs so BuiltinExecutor can raise if truly unknown

        if agent_cfg is None:
            agent_cfg = AgentConfig(**agent_overrides)
        elif agent_overrides:
            raise TypeError(
                "Cannot pass both agent_cfg= and legacy agent config kwargs "
                f"({set(agent_overrides)!r}) to make_builtin_executor."
            )

        if paths is None:
            defaults = ExecutorPaths(
                data_dir=str(tmp_path),
                state_home=str(tmp_path / "state"),
                workspace_dir=str(tmp_path / "workspace"),
                tmp_dir=str(tmp_path / "tmp"),
            )
            merged = {}
            for field in dataclasses.fields(ExecutorPaths):
                name = field.name
                merged[name] = paths_overrides.get(name, getattr(defaults, name))
            paths = ExecutorPaths(**merged)
        elif paths_overrides:
            raise TypeError(
                "Cannot pass both paths= and legacy path kwargs "
                f"({set(paths_overrides)!r}) to make_builtin_executor."
            )

        if kwargs:
            raise TypeError(f"Unrecognised make_builtin_executor kwargs: {set(kwargs)!r}")

        collaborators.setdefault("scheduler", None)
        collaborators.setdefault("sub_agent_factory", None)
        collaborators.setdefault("memory", None)
        collaborators.setdefault("working", None)
        collaborators.setdefault("results", None)
        collaborators.setdefault("notify_html_fn", None)
        collaborators.setdefault("context_monitor", None)
        return BuiltinExecutor(agent_cfg=agent_cfg, paths=paths, **collaborators)

    return _factory


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
