"""Shared legacy ``scheduler_jobs.json`` fixtures for scheduler owner suites.

Task 2.4 relocated the keep/rewrite nodes of
``tests/test_scheduler_legacy_readonly.py`` into their owner suites (see the
change's test-preservation ledger). Those nodes still construct a real
Scheduler beside a legacy JSON file, so the data and the scheduler factory
they share with the remaining legacy-only nodes live here, unchanged.

Deferred: tasks 8.4/10.3 rewrite the relocated nodes to existence-only legacy
detection and remove the read-only JSON runtime; this module goes with them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import MagicMock

import pytest
import schedule

import scheduler as scheduler_module
from scheduler import Scheduler
from xdg import xdg_paths

SECRET = "fake-secret-sentinel-legacy-4c1e"

# Sentinel: create the legacy path as a directory (unreadable as a file).
LEGACY_DIR = object()

# Far-future crons (1 January) so nothing fires during a test.
TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "toml alpha"
notify = false

[jobs.dormant]
enabled = false
schedule = "cron"
cron = "0 3 1 1 *"
task = "toml dormant"
notify = false
"""

LEGACY = {
    "legacyjob": {
        "schedule_type": "cron",
        "cron": "0 4 1 1 *",
        "task": f"legacy task {SECRET}",
        "enabled": True,
        "notify": False,
    },
    "legacypaused": {
        "schedule_type": "cron",
        "cron": "0 5 1 1 *",
        "task": "legacy paused",
        "enabled": False,
        "notify": False,
    },
}

UNRELATED_LEGACY = {"legacyjob": LEGACY["legacyjob"]}

SHADOW_ENTRY = {
    "schedule_type": "cron",
    "cron": "0 7 1 1 *",
    "task": f"legacy shadow {SECRET}",
    "enabled": True,
    "notify": False,
}

SHADOW_LEGACY = {"alpha": SHADOW_ENTRY, "dormant": SHADOW_ENTRY}

MALFORMED_LEGACY = b'{"legacyjob": {"task": "' + SECRET.encode() + b'"'


def legacy_bytes(jobs: dict) -> bytes:
    """Return operator-formatted legacy JSON bytes (two-space indent)."""
    return (json.dumps(jobs, indent=2) + "\n").encode("utf-8")


def write_legacy(path: Path, legacy: object) -> None:
    """Write *legacy* (None: absent, dict: JSON, bytes: raw, LEGACY_DIR: directory)."""
    if legacy is None:
        return
    if legacy is LEGACY_DIR:
        path.mkdir()
    elif isinstance(legacy, bytes):
        path.write_bytes(legacy)
    else:
        path.write_bytes(legacy_bytes(legacy))


def file_state(path: Path) -> tuple:
    """Return the kind of *path* and, for a regular file, its exact bytes."""
    if path.is_dir():
        return ("dir",)
    if not path.exists():
        return ("missing",)
    return ("file", path.read_bytes())


def parse_toml(path: Path) -> dict:
    """Parse the scheduler file with the scheduler's own parser."""
    return scheduler_module.tomli.loads(path.read_text(encoding="utf-8"))


def tags(sched: Scheduler) -> list[str]:
    """Return the public list of job tags."""
    return [job["tag"] for job in sched.list_jobs()]


class LegacyEnv:
    """A real scheduler with its scheduler.toml path, legacy path, and notify mock."""

    def __init__(
        self, sched: Scheduler, toml_path: Path, legacy_path: Path, notify: MagicMock,
    ) -> None:
        self.sched = sched
        self.toml_path = toml_path
        self.legacy_path = legacy_path
        self.notify = notify

    def snapshot(self) -> dict:
        """Capture map, document, hash, registrations, generation, and both files."""
        sched = self.sched
        with sched._running_lock:
            state = {
                "meta": copy.deepcopy(sched._jobs_meta),
                "status": sched._load_result.status,
                "document": copy.deepcopy(sched._load_result.document),
                "hash": sched._load_result.content_hash,
                "registrations": list(schedule.default_scheduler.jobs),
                "generation": sched._config_generation,
            }
        state["toml"] = file_state(self.toml_path)
        state["legacy"] = file_state(self.legacy_path)
        state["backups"] = sorted(
            p.name for p in self.toml_path.parent.glob(self.toml_path.name + ".bak.*")
        )
        return state


MakeLegacyScheduler = Callable[..., LegacyEnv]


def legacy_scheduler_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MakeLegacyScheduler]:
    """Construct real Schedulers over temporary XDG paths (loop off unless *enabled*).

    Replaces the external library's global scheduler (and its ``jobs`` alias)
    first. ``toml=None`` leaves scheduler.toml missing; *legacy* is written as
    described by :func:`write_legacy`. Started loops are stopped and joined.
    Owner suites wrap this generator in their own function-scoped fixture.
    """
    isolated_schedule = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated_schedule)
    monkeypatch.setattr(schedule, "jobs", isolated_schedule.jobs)
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("legacy-readonly-test")
    assert str(paths.model_selection_file).startswith(str(tmp_path))
    assert str(paths.scheduler_jobs).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(
        toml: Optional[str] = TOML, *, legacy: object = None, enabled: bool = False,
    ) -> LegacyEnv:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.state_home.mkdir(parents=True, exist_ok=True)
        if toml is not None:
            paths.scheduler_config.write_text(toml, encoding="utf-8")
        write_legacy(paths.scheduler_jobs, legacy)
        notify = MagicMock()
        sched = Scheduler(
            config={"scheduler": {"enabled": enabled}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=notify,
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(paths.scheduler_config),
        )
        created.append(sched)
        return LegacyEnv(sched, paths.scheduler_config, paths.scheduler_jobs, notify)

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()
            assert not sched._thread.is_alive(), "the scheduler loop did not stop"
