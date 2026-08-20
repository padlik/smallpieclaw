"""Tests for fallback_models deprecation warnings.

Covers:
- agent.fallback_models in config.toml logs a deprecation warning and is ignored.
- Per-job fallback_models in scheduler.toml logs a deprecation warning and is
  dropped from job meta on load.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_schema import parse_config
from scheduler import Scheduler
from xdg import XDGPaths


def _paths_for(state_dir) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
    state_dir = Path(state_dir)
    logs_dir = state_dir / "logs"
    return XDGPaths(
        config_home=state_dir, data_home=state_dir, state_home=state_dir,
        cache_home=state_dir, runtime_dir=state_dir,
        config_file=state_dir / "config.toml",
        scheduler_config=state_dir / "scheduler.toml",
        memory_file=state_dir / "memory.json",
        graph_memory_db=state_dir / "graph_memory",
        tool_index_file=state_dir / "tool_index.json",
        pid_file=state_dir / "agent.pid",
        secrets_file=state_dir / "secrets.toml",
        mcp_tokens_dir=state_dir / "mcp_tokens",
        logs_dir=logs_dir,
        log_file=logs_dir / "agent.log",
        log_jsonl=logs_dir / "agent.jsonl",
        skills_dir=state_dir / "skills",
        scheduler_state=state_dir / "scheduler_state",
        scheduler_commands=state_dir / "scheduler_commands",
        scheduler_jobs=state_dir / "scheduler_jobs",
        job_execution_log=state_dir / "job_execution_log",
    )


class TestAgentFallbackDeprecation:
    """agent.fallback_models logs a deprecation warning and is ignored."""

    def test_deprecation_warning_logged(self, caplog):
        """agent.fallback_models emits a deprecation warning and keeps the value."""
        raw = {
            "models": [
                {"name": "m1", "provider": "openai", "model": "gpt-4o-mini",
                 "api_key": "sk", "base_url": "https://api.openai.com/v1"},
            ],
            "agent": {
                "default_model": "gpt-4o-mini",
                "fallback_models": ["gpt-4o", "claude-3-haiku"],
            },
            "telegram": {"bot_token": "test"},
        }
        with caplog.at_level(logging.WARNING, logger="config_schema"):
            app_cfg = parse_config(raw)
        assert any("deprecated" in r.getMessage() and "fallback_models" in r.getMessage()
                   for r in caplog.records)
        # The value is parsed but unused — LLMClient is single-model.
        assert app_cfg.agent.fallback_models == ["gpt-4o", "claude-3-haiku"]

    def test_no_warning_when_empty(self, caplog):
        """Empty agent.fallback_models does not emit a deprecation warning."""
        raw = {
            "models": [
                {"name": "m1", "provider": "openai", "model": "gpt-4o-mini",
                 "api_key": "sk", "base_url": "https://api.openai.com/v1"},
            ],
            "agent": {"default_model": "gpt-4o-mini"},
            "telegram": {"bot_token": "test"},
        }
        with caplog.at_level(logging.WARNING, logger="config_schema"):
            parse_config(raw)
        assert not any("fallback_models" in r.getMessage() for r in caplog.records)


class TestSchedulerFallbackDeprecation:
    """Per-job fallback_models in scheduler.toml warns and drops the key on load."""

    _TOML = """
[jobs.legacy_job]
enabled = true
schedule = "cron"
cron = "0 2 * * *"
task = "legacy task"
fallback_models = ["model-b"]
"""

    def test_deprecation_warning_and_key_dropped(self, tmp_path, caplog):
        """Scheduler job fallback_models warns and is dropped from loaded meta."""
        config_path = tmp_path / "scheduler.toml"
        config_path.write_text(self._TOML)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with caplog.at_level(logging.WARNING, logger="scheduler"):
            sched = Scheduler(
                config={"scheduler": {"enabled": False},
                        "agent": {"scheduled_max_iterations": 10}},
                notify_fn=MagicMock(),
                agent_fn=MagicMock(),
                scheduler_config_path=str(config_path),
                paths=_paths_for(data_dir),
            )
        # Deprecation warning logged
        assert any("deprecated" in r.getMessage() and "fallback_models" in r.getMessage()
                   for r in caplog.records)
        # Key dropped from meta
        assert "fallback_models" not in sched._jobs_meta["legacy_job"]
