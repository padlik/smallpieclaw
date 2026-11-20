"""
Scheduler state projection (task 12.8).

``scheduler_state`` persists only a narrow projection of each job: scalar
schedule/enable fields, ``last_run``, ``last_error`` and
``schedule_description``. The raw model selector (and every other unvalidated
raw value) is never copied into the state file, while the authoritative
scheduler.toml document and the runtime job view keep it as parsed.

These tests construct a REAL Scheduler from a real TOML file; construction
saves state, and the test reads the actual state file strictly. Only external
boundaries (file system location, notify, agent) are substituted.
"""

from __future__ import annotations

import builtins
import datetime as dt
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import MagicMock

import pytest
import schedule

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import Scheduler
from tests import scheduler_legacy_fixtures as legacy_fx
from xdg import XDGPaths

# A secret-like sentinel that must never surface in the saved state.
_SECRET = "sk-fake-SENTINEL-4c1e8b"

# Far-future cron job: a started loop would never fire it during a test.
_JOB_TEMPLATE = """
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "projection job"
model = {model}
"""

# The complete, independently known projection of the valid cron job above.
_EXPECTED_JOB = {
    "enabled": True,
    "schedule_type": "cron",
    "cron": "0 2 1 1 *",
    "run_at": None,
    "last_run": None,
    "last_error": None,
    "schedule_description": "cron: 0 2 1 1 *",
}


def _paths_for(state_dir: Path) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
    logs_dir = state_dir / "logs"
    return XDGPaths(
        config_home=state_dir, data_home=state_dir, state_home=state_dir,
        cache_home=state_dir, runtime_dir=state_dir,
        config_file=state_dir / "config.toml",
        model_selection_file=state_dir / "model-selection.toml",
        models_cache_file=state_dir / "models-cache.json",
        scheduler_config=state_dir / "scheduler.toml",
        memory_file=state_dir / "memory.json",
        graph_memory_db=state_dir / "graph_memory",
        tool_index_file=state_dir / "tool_index.json",
        pid_file=state_dir / "agent.pid",
        secrets_file=state_dir / "secrets.toml",
        mcp_tokens_dir=state_dir / "mcp_tokens",
        logs_dir=logs_dir,
        log_file=logs_dir / "agent.log",
        log_store=logs_dir / "agent_logs.sqlite",
        skills_dir=state_dir / "skills",
        scheduler_state=state_dir / "scheduler_state",
        scheduler_commands=state_dir / "scheduler_commands",
        scheduler_jobs=state_dir / "scheduler_jobs",
        job_execution_log=state_dir / "job_execution_log",
    )


@pytest.fixture
def isolated_schedule() -> Iterator[None]:
    """Keep the global ``schedule`` registry clean around each test."""
    schedule.clear()
    yield
    schedule.clear()


def _reject_constant(name: str) -> object:
    """Strict JSON: reject the non-standard NaN/Infinity constants."""
    raise ValueError(f"non-standard JSON constant {name!r} in scheduler state")


def _read_state_strict(state_path: Path) -> dict:
    """Read the saved state file as strict, standard JSON."""
    assert state_path.is_file(), "constructing the scheduler must save its state file"
    return json.loads(state_path.read_text(), parse_constant=_reject_constant)


def _same_selector(actual: object, expected: object) -> bool:
    """Equal by exact type and value; NaN equals NaN."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, float) and math.isnan(expected):
        return math.isnan(actual)
    return actual == expected


@pytest.mark.parametrize(
    ("model_toml", "expected_model"),
    [
        pytest.param(f'"{_SECRET}"', _SECRET, id="string_secret"),
        pytest.param(
            f'{{ provider = "openai", name = "{_SECRET}" }}',
            {"provider": "openai", "name": _SECRET},
            id="inline_table",
        ),
        pytest.param(f'["openai", "{_SECRET}"]', ["openai", _SECRET], id="list"),
        pytest.param(
            "1979-05-27T07:32:00Z",
            dt.datetime(1979, 5, 27, 7, 32, tzinfo=dt.timezone.utc),
            id="offset_datetime",
        ),
        pytest.param(
            "1979-05-27T07:32:00", dt.datetime(1979, 5, 27, 7, 32), id="local_datetime",
        ),
        pytest.param("1979-05-27", dt.date(1979, 5, 27), id="local_date"),
        pytest.param("07:32:00", dt.time(7, 32), id="local_time"),
        pytest.param("nan", math.nan, id="nan"),
        pytest.param("inf", math.inf, id="inf"),
        pytest.param("-inf", -math.inf, id="neg_inf"),
    ],
)
def test_saved_state_excludes_raw_model_selector(
    tmp_path: Path, isolated_schedule: None, model_toml: str, expected_model: object,
) -> None:
    """The saved job is the narrow projection; the raw selector stays authoritative."""
    toml_path = tmp_path / "scheduler.toml"
    toml_path.write_text(_JOB_TEMPLATE.format(model=model_toml))
    toml_bytes = toml_path.read_bytes()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    paths = _paths_for(data_dir)

    sched = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=paths,
        scheduler_config_path=str(toml_path),
    )
    try:
        state = _read_state_strict(paths.scheduler_state)
        state_text = paths.scheduler_state.read_text()

        assert set(state) == {"jobs", "history"}
        assert state["history"] == {}
        assert state["jobs"] == {"alpha": _EXPECTED_JOB}
        assert _SECRET not in state_text
        assert "1979" not in state_text and "07:32" not in state_text

        # The authoritative document and the runtime job view keep the selector as parsed.
        assert toml_path.read_bytes() == toml_bytes
        raw_model = tomllib.loads(toml_bytes.decode("utf-8"))["jobs"]["alpha"]["model"]
        assert _same_selector(raw_model, expected_model)
        (job,) = sched.list_jobs()
        assert _same_selector(job["model"], expected_model)
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Shared construction helpers for the remaining contracts
# ---------------------------------------------------------------------------

_ALPHA_TOML = _JOB_TEMPLATE.format(model=f'"{_SECRET}"')
_LAST_RUN = "2026-01-01T02:00:00+00:00"
_REMOVED_LAST_RUN = "2025-12-31T02:00:00+00:00"


def _construct(tmp_path: Path, toml_text: str, *, state: Optional[dict] = None) -> tuple:
    """Write scheduler.toml (and optional pre-seeded state); build a real Scheduler.

    Returns ``(sched, paths, toml_path)``; the caller stops the scheduler.
    """
    toml_path = tmp_path / "scheduler.toml"
    if not toml_path.exists():
        toml_path.write_text(toml_text)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    paths = _paths_for(data_dir)
    if state is not None:
        paths.scheduler_state.write_text(json.dumps(state, indent=2))
    sched = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=paths,
        scheduler_config_path=str(toml_path),
    )
    return sched, paths, toml_path


# ---------------------------------------------------------------------------
# Invalid ``enabled`` never reaches the state file
# ---------------------------------------------------------------------------

_ENABLED_TEMPLATE = """
[jobs.alpha]
enabled = {enabled}
schedule = "cron"
cron = "0 2 1 1 *"
task = "projection job"
model = "{secret}"
"""


@pytest.mark.parametrize(
    ("enabled_toml", "raw_enabled"),
    [
        pytest.param(f'"{_SECRET}"', _SECRET, id="string_secret"),
        pytest.param("1", 1, id="integer_one"),
        pytest.param("nan", math.nan, id="nan"),
        pytest.param(
            "1979-05-27T07:32:00Z",
            dt.datetime(1979, 5, 27, 7, 32, tzinfo=dt.timezone.utc),
            id="offset_datetime",
        ),
        pytest.param(f'{{ on = true, note = "{_SECRET}" }}', {"on": True, "note": _SECRET},
                     id="inline_table"),
    ],
)
def test_saved_state_persists_invalid_enabled_as_false(
    tmp_path: Path, isolated_schedule: None, enabled_toml: str, raw_enabled: object,
) -> None:
    """A non-boolean ``enabled`` is saved as False; the runtime keeps the raw value."""
    toml_text = _ENABLED_TEMPLATE.format(enabled=enabled_toml, secret=_SECRET)
    sched, paths, toml_path = _construct(tmp_path, toml_text)
    toml_bytes = toml_text.encode("utf-8")
    try:
        state = _read_state_strict(paths.scheduler_state)
        state_text = paths.scheduler_state.read_text()

        assert set(state) == {"jobs", "history"}
        assert state["jobs"] == {"alpha": dict(_EXPECTED_JOB, enabled=False)}
        assert state["jobs"]["alpha"]["enabled"] is False
        assert _SECRET not in state_text
        assert "1979" not in state_text

        assert toml_path.read_bytes() == toml_bytes
        (job,) = sched.list_jobs()
        assert _same_selector(job["enabled"], raw_enabled)
        assert job["config_error"] == "enabled must be true or false"
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Characterizations: history, legacy JSON, once jobs, write-fault containment
# ---------------------------------------------------------------------------

def test_saved_state_round_trips_history(tmp_path: Path, isolated_schedule: None) -> None:
    """History for active and removed tags survives load, overlay, and save."""
    history = {
        "alpha": {"last_run": _LAST_RUN, "last_error": "alpha failed"},
        "removed": {"last_run": _REMOVED_LAST_RUN, "last_error": None},
    }
    expected_alpha = dict(_EXPECTED_JOB, last_run=_LAST_RUN, last_error="alpha failed")
    seed = {"jobs": {"alpha": expected_alpha}, "history": history}

    sched, paths, _ = _construct(tmp_path, _ALPHA_TOML, state=seed)
    try:
        job = sched.show_job("alpha")
        assert (job["last_run"], job["last_error"]) == (_LAST_RUN, "alpha failed")
        state = _read_state_strict(paths.scheduler_state)
        assert state == {"jobs": {"alpha": expected_alpha}, "history": history}
        assert _SECRET not in paths.scheduler_state.read_text()
    finally:
        sched.stop()

    # Reconstructing from the same files reproduces the same state.
    first_bytes = paths.scheduler_state.read_bytes()
    again, _, _ = _construct(tmp_path, _ALPHA_TOML)
    try:
        assert paths.scheduler_state.read_bytes() == first_bytes
    finally:
        again.stop()


def test_saved_state_migrates_jobs_only_history(tmp_path: Path, isolated_schedule: None) -> None:
    """An old state with last fields only under ``jobs`` becomes a ``history`` section."""
    seed = {"jobs": {"alpha": {
        "task": "old task", "model": _SECRET, "enabled": True,
        "last_run": _LAST_RUN, "last_error": "old failure",
    }}}

    sched, paths, _ = _construct(tmp_path, _ALPHA_TOML, state=seed)
    try:
        job = sched.show_job("alpha")
        assert (job["last_run"], job["last_error"]) == (_LAST_RUN, "old failure")
        state = _read_state_strict(paths.scheduler_state)
        assert state == {
            "jobs": {"alpha": dict(_EXPECTED_JOB, last_run=_LAST_RUN, last_error="old failure")},
            "history": {"alpha": {"last_run": _LAST_RUN, "last_error": "old failure"}},
        }
        assert _SECRET not in paths.scheduler_state.read_text()
    finally:
        sched.stop()


def test_saved_state_never_projects_a_legacy_file_job(tmp_path: Path, isolated_schedule: None) -> None:
    """Task 8.4: a legacy JSON job is never loaded, so the saved state projects only TOML jobs."""
    legacy = {"legacyjob": {
        "schedule_type": "cron",
        "cron": "0 4 1 1 *",
        "task": f"legacy task {_SECRET}",
        "enabled": True,
        "notify": False,
        "model": _SECRET,
        "source": "operator",
        "_legacy_read_only": False,
        "_operator_note": _SECRET,
    }}
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_path = _paths_for(data_dir).scheduler_jobs
    legacy_path.write_text(json.dumps(legacy))
    legacy_bytes = legacy_path.read_bytes()

    sched, paths, _ = _construct(tmp_path, _ALPHA_TOML)
    try:
        state = _read_state_strict(paths.scheduler_state)
        assert "legacyjob" not in state["jobs"] and "alpha" in state["jobs"]
        assert _SECRET not in paths.scheduler_state.read_text()
        assert "legacy, read-only" not in paths.scheduler_state.read_text()

        assert legacy_path.read_bytes() == legacy_bytes
        assert "legacyjob" not in {job["tag"] for job in sched.list_jobs()}
    finally:
        sched.stop()


def test_saved_state_projects_once_job(tmp_path: Path, isolated_schedule: None) -> None:
    """A paused once-job keeps its derived schedule scalars in state."""
    toml_text = """
[jobs.later]
enabled = false
schedule = "once"
run_at = "23:59"
task = "once job"
"""
    sched, paths, _ = _construct(tmp_path, toml_text)
    try:
        state = _read_state_strict(paths.scheduler_state)
        assert state["jobs"] == {"later": {
            "enabled": False,
            "schedule_type": "once",
            "cron": None,
            "run_at": "23:59",
            "last_run": None,
            "last_error": None,
            "schedule_description": "once at 23:59",
        }}
    finally:
        sched.stop()


class _FaultyStateStream:
    """Delegating text stream: the first write lands, every later write raises."""

    def __init__(self, real: object, error: Exception) -> None:
        self.real = real
        self.error = error
        self.writes = 0
        self.raised = 0

    def write(self, data: str) -> int:
        """Write the first chunk through; raise the injected error afterwards."""
        if self.writes:
            self.raised += 1
            raise self.error
        self.writes += 1
        return self.real.write(data)

    def __enter__(self) -> _FaultyStateStream:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.real.close()
        return False

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)


@pytest.mark.parametrize("error_type", [TypeError, ValueError], ids=["type_error", "value_error"])
def test_state_write_fault_is_contained(
    tmp_path: Path, isolated_schedule: None, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, error_type: type,
) -> None:
    """A serialization fault leaves the old state and recovers on the next save."""
    history = {"alpha": {"last_run": _LAST_RUN, "last_error": None}}
    seed = {"jobs": {"alpha": dict(_EXPECTED_JOB, last_run=_LAST_RUN)}, "history": history}
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = _paths_for(data_dir).scheduler_state
    tmp_state = str(state_path) + ".tmp"
    streams: list[_FaultyStateStream] = []
    armed = [True]
    real_open = builtins.open

    def faulty_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = real_open(file, mode, *args, **kwargs)
        if armed[0] and isinstance(file, (str, os.PathLike)) and (
            os.fspath(file) == tmp_state and mode == "w"
        ):
            stream = _FaultyStateStream(handle, error_type(f"cannot encode {_SECRET}"))
            streams.append(stream)
            return stream
        return handle

    monkeypatch.setattr(builtins, "open", faulty_open)
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        sched, paths, toml_path = _construct(tmp_path, _ALPHA_TOML, state=seed)
    try:
        seed_bytes = json.dumps(seed, indent=2).encode("utf-8")
        assert streams and streams[0].writes == 1 and streams[0].raised >= 1
        assert all(stream.real.closed for stream in streams)
        assert not os.path.exists(tmp_state)
        assert paths.scheduler_state.read_bytes() == seed_bytes
        assert toml_path.read_bytes() == _ALPHA_TOML.encode("utf-8")
        assert any(
            f"Could not serialize scheduler state ({error_type.__name__})" in r.getMessage()
            for r in caplog.records
        )
        assert _SECRET not in caplog.text
        assert [job["tag"] for job in sched.list_jobs()] == ["alpha"]

        armed[0] = False
        assert sched.pause_job("alpha") is True
        state = _read_state_strict(paths.scheduler_state)
        assert state == {
            "jobs": {"alpha": dict(_EXPECTED_JOB, enabled=False, last_run=_LAST_RUN)},
            "history": history,
        }
        assert sched.resume_job("alpha") is True
        state = _read_state_strict(paths.scheduler_state)
        assert state["jobs"] == {"alpha": dict(_EXPECTED_JOB, last_run=_LAST_RUN)}
        assert not os.path.exists(tmp_state)
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Rejected scheduling input never reaches the state file
# ---------------------------------------------------------------------------

_SCHEDULE_TEMPLATE = """
[jobs.alpha]
enabled = true
schedule = {schedule}
cron = {cron}
{extra}task = "invalid schedule job"
"""

_VALID_CRON = "0 2 1 1 *"

# The state-only placeholder for a job whose scheduling input was rejected.
_UNSCHEDULED_JOB = {
    "enabled": False,
    "schedule_type": "cron",
    "cron": None,
    "run_at": None,
    "last_run": None,
    "last_error": None,
    "schedule_description": "cron (no expression)",
}


@pytest.mark.parametrize(
    ("schedule_toml", "cron_toml", "extra", "raw_schedule", "raw_cron", "raw_run_at"),
    [
        pytest.param(f'"{_SECRET}"', f'"{_VALID_CRON}"', "", _SECRET, _VALID_CRON, None,
                     id="unknown_string"),
        pytest.param(f'{{ kind = "{_SECRET}" }}', f'"{_VALID_CRON}"', "",
                     {"kind": _SECRET}, _VALID_CRON, None, id="inline_table"),
        pytest.param(f'["cron", "{_SECRET}"]', f'"{_VALID_CRON}"', "",
                     ["cron", _SECRET], _VALID_CRON, None, id="list"),
        pytest.param("1979-05-27T07:32:00Z", f'"{_VALID_CRON}"', "",
                     dt.datetime(1979, 5, 27, 7, 32, tzinfo=dt.timezone.utc),
                     _VALID_CRON, None, id="offset_datetime"),
        pytest.param("nan", f'"{_VALID_CRON}"', "", math.nan, _VALID_CRON, None, id="nan"),
        pytest.param('"cron"', f'"{_SECRET}"', "", "cron", _SECRET, None, id="invalid_cron"),
        pytest.param('"once"', f'"{_VALID_CRON}"', f'run_at = "{_SECRET}"\n',
                     "once", _VALID_CRON, _SECRET, id="invalid_run_at"),
    ],
)
def test_saved_state_excludes_invalid_schedule_values(
    tmp_path: Path, isolated_schedule: None, schedule_toml: str, cron_toml: str,
    extra: str, raw_schedule: object, raw_cron: str, raw_run_at: Optional[str],
) -> None:
    """A rejected schedule is saved as the unscheduled placeholder; runtime keeps raw values."""
    toml_text = _SCHEDULE_TEMPLATE.format(schedule=schedule_toml, cron=cron_toml, extra=extra)
    sched, paths, toml_path = _construct(tmp_path, toml_text)
    try:
        state = _read_state_strict(paths.scheduler_state)
        state_text = paths.scheduler_state.read_text()
        assert _SECRET not in state_text
        assert "1979" not in state_text
        assert set(state) == {"jobs", "history"}
        assert state["jobs"] == {"alpha": _UNSCHEDULED_JOB}

        # The authoritative document keeps every raw scheduling value as parsed.
        toml_bytes = toml_text.encode("utf-8")
        assert toml_path.read_bytes() == toml_bytes
        raw_job = tomllib.loads(toml_bytes.decode("utf-8"))["jobs"]["alpha"]
        assert _same_selector(raw_job["schedule"], raw_schedule)
        assert raw_job["cron"] == raw_cron
        assert raw_job.get("run_at") == raw_run_at

        # The runtime view keeps the classified raw values and reports the error.
        (job,) = sched.list_jobs()
        assert _same_selector(job["schedule_type"], raw_schedule)
        assert job["cron"] == raw_cron
        assert job["enabled"] is True
        assert job["config_error"]
        shown = sched.show_job("alpha")
        assert shown["run_at"] is None
        assert shown["config_error"] == job["config_error"]
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Healthy TOML schedule prose
#
# Relocated by task 2.4 from tests/test_scheduler_legacy_readonly.py (ledger
# provenance in openspec/changes/simplify-model-runtime/test-preservation-ledger).
# Keep (ledger 448).
# ---------------------------------------------------------------------------


@pytest.fixture
def make_legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[legacy_fx.MakeLegacyScheduler]:
    """Real legacy-beside-TOML schedulers (shared factory, see the support module)."""
    yield from legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)


def test_healthy_toml_schedule_prose_is_unmarked(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler,
) -> None:
    """A scheduler.toml job keeps its compact native prose beside legacy jobs."""
    env = make_legacy_scheduler(legacy_fx.TOML, legacy=legacy_fx.LEGACY)

    listed = next(job for job in env.sched.list_jobs() if job["tag"] == "alpha")

    assert env.sched.show_job("alpha")["schedule"] == "cron: 0 2 1 1 *"
    assert listed["schedule"] == "cron: 0 2 1 1 *"
