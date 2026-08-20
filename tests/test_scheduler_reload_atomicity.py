"""
Tests that Scheduler.reload() publishes its new job map atomically.

Regression: reload() used to clear _jobs_meta under _running_lock and then
repopulate it outside the lock, so a concurrent reader holding the lock (e.g.
_save_scheduler_toml's snapshot) could observe an empty or half-filled map and
persist a scheduler.toml with no jobs in it.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

_TOML = """
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 * * *"
task = "job alpha"

[jobs.beta]
enabled = true
schedule = "cron"
cron = "0 3 * * *"
task = "job beta"
"""


@pytest.fixture
def sched(tmp_path):
    """Scheduler backed by a two-job scheduler.toml, loop not running."""
    config_path = tmp_path / "scheduler.toml"
    config_path.write_text(_TOML)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"scheduled_max_iterations": 10}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=_paths_for(data_dir),
    )


class TestReloadAtomicPublish:
    """reload() must never expose an empty or partial _jobs_meta."""

    def test_reload_keeps_jobs(self, sched):
        assert set(sched._jobs_meta) == {"alpha", "beta"}
        result = sched.reload()
        assert result["reloaded"] == 2
        assert set(sched._jobs_meta) == {"alpha", "beta"}

    def test_concurrent_reader_never_sees_empty_map(self, sched):
        """A reader taking _running_lock mid-reload still sees a full job map."""
        observed: list[set] = []
        parsing = threading.Event()
        release = threading.Event()
        real_read = sched._read_config_jobs

        def slow_read(path):
            parsing.set()          # reload() is now mid-parse
            release.wait(5.0)      # hold it there until the reader has looked
            return real_read(path)

        sched._read_config_jobs = slow_read

        def reader():
            parsing.wait(5.0)
            with sched._running_lock:
                observed.append(set(sched._jobs_meta))
            release.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        sched.reload()
        t.join(5.0)

        assert observed == [{"alpha", "beta"}]

    def test_save_during_reload_does_not_lose_jobs(self, sched, tmp_path):
        """_save_scheduler_toml fired mid-reload still writes both jobs."""
        config_path = tmp_path / "scheduler.toml"
        parsing = threading.Event()
        release = threading.Event()
        real_read = sched._read_config_jobs

        def slow_read(path):
            parsing.set()
            release.wait(5.0)
            return real_read(path)

        sched._read_config_jobs = slow_read

        def saver():
            parsing.wait(5.0)
            sched._save_scheduler_toml()
            release.set()

        t = threading.Thread(target=saver, daemon=True)
        t.start()
        sched.reload()
        t.join(5.0)

        written = config_path.read_text()
        assert "[jobs.alpha]" in written
        assert "[jobs.beta]" in written
