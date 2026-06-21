"""
Tests for scheduler fallback_models support.

Covers the full lifecycle: add_job stores fallback_models, list_jobs exposes them,
TOML round-trip preserves them, and execution passes them to spawn_agent.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import Scheduler


@pytest.fixture
def sched(tmp_path):
    """Create a minimal Scheduler instance with no config file."""
    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "scheduler": {"enabled": False},
        "agent": {"scheduled_max_iterations": 10},
    }
    s = Scheduler(
        config=config,
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        data_dir=str(data_dir),
    )
    return s


class TestAddJobFallbackModels:
    """add_job correctly stores fallback_models in meta."""

    def test_add_job_with_fallback_models(self, sched):
        fb = ["gemini-3-flash-preview:cloud", "gpt-4o-mini"]
        result = sched.add_job(
            tag="test_job",
            schedule_type="cron",
            task="do something",
            cron="0 */6 * * *",
            model="deepseek-v4-flash",
            fallback_models=fb,
        )
        assert result["success"]
        meta = sched._jobs_meta["test_job"]
        assert meta["model"] == "deepseek-v4-flash"
        assert meta["fallback_models"] == fb

    def test_add_job_without_fallback_models(self, sched):
        result = sched.add_job(
            tag="no_fb",
            schedule_type="cron",
            task="do something",
            cron="0 2 * * *",
            model="gpt-4o",
        )
        assert result["success"]
        meta = sched._jobs_meta["no_fb"]
        assert "fallback_models" not in meta

    def test_add_job_empty_fallback_disables_inheritance(self, sched):
        """Explicit empty list means 'disable fallback' — distinct from None/absent."""
        result = sched.add_job(
            tag="no_inherit",
            schedule_type="cron",
            task="do something",
            cron="0 2 * * *",
            fallback_models=[],
        )
        assert result["success"]
        meta = sched._jobs_meta["no_inherit"]
        assert meta["fallback_models"] == []


class TestListJobsFallbackModels:
    """list_jobs exposes fallback_models in the returned entries."""

    def test_list_jobs_includes_fallback(self, sched):
        fb = ["model-a", "model-b"]
        sched.add_job(tag="j1", schedule_type="cron", task="t", cron="0 0 * * *",
                      model="primary", fallback_models=fb)
        jobs = sched.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["fallback_models"] == fb

    def test_list_jobs_fallback_none_when_absent(self, sched):
        sched.add_job(tag="j2", schedule_type="cron", task="t", cron="0 0 * * *")
        jobs = sched.list_jobs()
        assert jobs[0]["fallback_models"] is None


class TestTomlRoundTrip:
    """fallback_models survives save → load cycle via scheduler.toml."""

    def test_roundtrip_with_fallback(self, sched, tmp_path):
        fb = ["gemini-3-flash-preview:cloud", "gpt-4o-mini"]
        sched.add_job(tag="roundtrip", schedule_type="cron", task="hello",
                      cron="*/10 * * * *", model="deepseek", fallback_models=fb)
        sched._save_scheduler_toml()

        # Read back the TOML to verify it contains fallback_models
        toml_path = tmp_path / "scheduler.toml"
        content = toml_path.read_text()
        assert "fallback_models" in content
        assert "gemini-3-flash-preview:cloud" in content
        assert "gpt-4o-mini" in content

        # Create a fresh scheduler from the same TOML
        data_dir2 = tmp_path / "data2"
        data_dir2.mkdir()
        config = {"scheduler": {"enabled": False}, "agent": {}}
        s2 = Scheduler(
            config=config,
            notify_fn=MagicMock(),
            scheduler_config_path=str(toml_path),
            data_dir=str(data_dir2),
        )
        assert s2._jobs_meta["roundtrip"]["fallback_models"] == fb

    def test_roundtrip_empty_list(self, sched, tmp_path):
        """Empty list [] is preserved (means 'no fallback, don't inherit')."""
        sched.add_job(tag="no_fb_job", schedule_type="cron", task="x",
                      cron="0 0 * * *", fallback_models=[])
        sched._save_scheduler_toml()

        toml_path = tmp_path / "scheduler.toml"
        content = toml_path.read_text()
        assert "fallback_models = []" in content

        data_dir2 = tmp_path / "data2"
        data_dir2.mkdir()
        s2 = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {}},
            notify_fn=MagicMock(),
            scheduler_config_path=str(toml_path),
            data_dir=str(data_dir2),
        )
        assert s2._jobs_meta["no_fb_job"]["fallback_models"] == []

    def test_roundtrip_absent_stays_none(self, sched, tmp_path):
        """When fallback_models is not specified, it stays None after reload."""
        sched.add_job(tag="plain", schedule_type="cron", task="x", cron="0 0 * * *")
        sched._save_scheduler_toml()

        toml_path = tmp_path / "scheduler.toml"
        data_dir2 = tmp_path / "data2"
        data_dir2.mkdir()
        s2 = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {}},
            notify_fn=MagicMock(),
            scheduler_config_path=str(toml_path),
            data_dir=str(data_dir2),
        )
        assert s2._jobs_meta["plain"]["fallback_models"] is None


class TestExecutionPassesFallback:
    """_run_job passes fallback_models to spawn_agent via builtin_executor."""

    def test_spawn_args_include_fallback(self, sched):
        fb = ["model-b", "model-c"]
        sched.add_job(tag="exec_test", schedule_type="cron", task="do work",
                      cron="0 0 * * *", model="model-a", fallback_models=fb)

        mock_executor = MagicMock()
        mock_executor._exec_spawn_agent = MagicMock(return_value={
            "success": True, "output": json.dumps({"agent_id": "sa-test123"}),
            "error": "", "exit_code": 0,
        })
        sched.builtin_executor = mock_executor

        # Run the job synchronously
        sched._run_job(tag="exec_test")

        # Verify spawn_agent was called with fallback_models in args
        mock_executor._exec_spawn_agent.assert_called_once()
        spawn_args = mock_executor._exec_spawn_agent.call_args[0][0]
        assert spawn_args["fallback_models"] == fb
        assert spawn_args["model"] == "model-a"
        assert spawn_args["task"] == "do work"

    def test_spawn_args_omit_fallback_when_absent(self, sched):
        """When fallback_models not set, spawn_args shouldn't include it."""
        sched.add_job(tag="no_fb_exec", schedule_type="cron", task="work",
                      cron="0 0 * * *", model="model-x")

        mock_executor = MagicMock()
        mock_executor._exec_spawn_agent = MagicMock(return_value={
            "success": True, "output": json.dumps({"agent_id": "sa-xyz"}),
            "error": "", "exit_code": 0,
        })
        sched.builtin_executor = mock_executor

        sched._run_job(tag="no_fb_exec")

        spawn_args = mock_executor._exec_spawn_agent.call_args[0][0]
        assert "fallback_models" not in spawn_args

    def test_spawn_args_pass_empty_list(self, sched):
        """Empty list is explicitly passed (disables inheritance)."""
        sched.add_job(tag="empty_fb", schedule_type="cron", task="work",
                      cron="0 0 * * *", fallback_models=[])

        mock_executor = MagicMock()
        mock_executor._exec_spawn_agent = MagicMock(return_value={
            "success": True, "output": json.dumps({"agent_id": "sa-abc"}),
            "error": "", "exit_code": 0,
        })
        sched.builtin_executor = mock_executor

        sched._run_job(tag="empty_fb")

        spawn_args = mock_executor._exec_spawn_agent.call_args[0][0]
        assert spawn_args["fallback_models"] == []

    def test_preserve_context_normalizes_space_containing_tag(self, sched):
        """Quoted TOML/static tags may contain spaces; context_key must stay safe."""
        sched._jobs_meta["Nightly Health Check"] = {
            "enabled": True,
            "task": "check health",
            "preserve_context": True,
            "notify": False,
        }

        mock_executor = MagicMock()
        mock_executor._exec_spawn_agent = MagicMock(return_value={
            "success": True, "output": json.dumps({"agent_id": "sa-space"}),
            "error": "", "exit_code": 0,
        })
        sched.builtin_executor = mock_executor

        sched._run_job(tag="Nightly Health Check")

        spawn_args = mock_executor._exec_spawn_agent.call_args[0][0]
        assert spawn_args["context_key"] == "nightly_health_check"

    def test_preserve_context_long_tag_passes_validator(self, sched):
        """A very long tag must normalize to a validator-accepted context_key
        (≤128 chars) so the scheduled job still runs instead of failing."""
        from builtin_executor import _validate_context_key
        from scheduler import _normalize_context_key

        long_tag = "word " * 60  # >128 chars when normalized
        key = _normalize_context_key(long_tag)
        assert len(key) <= 128
        # Must not raise — confirms spawn_agent will accept it.
        assert _validate_context_key(key) == key


class TestBuiltinExecutorScheduleTool:
    """The schedule tool (_exec_schedule) passes fallback_models through."""

    def test_exec_schedule_passes_fallback_to_add_job(self):
        """builtin_executor._exec_schedule passes fallback_models to scheduler.add_job."""
        from builtin_executor import BuiltinExecutor

        mock_scheduler = MagicMock()
        mock_scheduler.add_job.return_value = {"success": True}

        executor = BuiltinExecutor.__new__(BuiltinExecutor)
        executor.scheduler = mock_scheduler
        executor._config = {}

        fb = ["fallback-a", "fallback-b"]
        args = {
            "action": "add",
            "tag": "my_job",
            "task": "test task",
            "cron": "0 0 * * *",
            "model": "primary-model",
            "fallback_models": fb,
        }
        result = executor._exec_schedule(args)
        assert result["success"]

        mock_scheduler.add_job.assert_called_once()
        call_kwargs = mock_scheduler.add_job.call_args[1]
        assert call_kwargs["fallback_models"] == fb
        assert call_kwargs["model"] == "primary-model"
