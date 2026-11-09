"""
Load and startup containment for the scheduler (task 12.2, ADR-0037).

Scheduler construction, load, and start must never raise into bot startup.
These tests construct a REAL Scheduler from the raw config dict, the same way
``main.py`` does, and observe the result through its public API, the real
``schedule`` built-in tool, and the ``notify_fn`` boundary. Only external
boundaries (the ``schedule`` library, the file system, notify) are substituted.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from builtin_tools.schedule import exec_schedule
from config_schema import parse_config
from exceptions import ConfigError
from outcome_utils import read_execution
from scheduler import Scheduler
from tests import scheduler_legacy_fixtures as legacy_fx
from xdg import XDGPaths


def _paths_for(state_dir) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
    state_dir = Path(state_dir)
    logs_dir = state_dir / "logs"
    return XDGPaths(
        config_home=state_dir, data_home=state_dir, state_home=state_dir,
        cache_home=state_dir, runtime_dir=state_dir,
        config_file=state_dir / "config.toml",
        model_selection_file=state_dir / "model-selection.toml",
        models_cache_file=state_dir / "models-cache.json",
        models_config_file=state_dir / "models.toml",
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


_TOML = """
[jobs.healthy]
enabled = true
schedule = "cron"
cron = "0 2 * * *"
task = "healthy job"
"""


@pytest.mark.parametrize(
    ("section", "key", "bad_value"),
    [
        pytest.param("scheduler", "execution_log_max_age_hours", "forty-eight", id="max_age_str"),
        pytest.param("scheduler", "execution_log_max_age_hours", None, id="max_age_none"),
        pytest.param("scheduler", "execution_log_max_per_job", [10], id="max_per_job_list"),
        pytest.param("agent", "long_run_warn_minutes", "thirty", id="warn_minutes_str"),
    ],
)
def test_malformed_local_setting_does_not_escape_construction(
    tmp_path: Path, section: str, key: str, bad_value: object
) -> None:
    """A non-castable scheduler-local setting is contained; the healthy job still loads."""
    toml_path = tmp_path / "scheduler.toml"
    toml_path.write_text(_TOML)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config: dict = {"scheduler": {"enabled": False}, "agent": {}}
    config[section][key] = bad_value

    sched = Scheduler(
        config=config,
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=_paths_for(data_dir),
        scheduler_config_path=str(toml_path),
    )
    try:
        assert [job["tag"] for job in sched.list_jobs()] == ["healthy"]
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------

# A secret-like sentinel that must never surface in notices, tool output, or logs.
_SECRET = "sk-test-SENTINEL-7f3a9c"

# Unterminated string on the cron line: tomli rejects the whole file.
_UNPARSEABLE = (
    "[jobs.alpha]\n"
    f'task = "call the api with {_SECRET}"\n'
    'cron = "0 2 * * *\n'
)

# Far-future healthy job: a started loop never fires it during a test.
_FAR_HEALTHY = """
[jobs.healthy]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "healthy job"
"""

_LOAD_STATES = ("valid", "missing", "unreadable", "unparseable")


@dataclasses.dataclass
class _Env:
    """A constructed scheduler plus the boundaries a test observes."""

    sched: Scheduler
    notify: MagicMock
    toml_path: Path
    data_dir: Path


def _write_config(toml_path: Path, file_state: str, toml_text: str) -> None:
    """Place the scheduler file at *toml_path* in the requested on-disk state."""
    if file_state == "file":
        toml_path.write_text(toml_text)
    elif file_state == "unparseable":
        toml_path.write_text(_UNPARSEABLE)
    elif file_state == "unreadable":
        # A directory exists at the path but cannot be read as a file
        # (no host permission changes needed).
        toml_path.mkdir()
    elif file_state != "missing":
        raise AssertionError(f"unknown file_state {file_state!r}")


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[..., _Env]]:
    """Factory building real Schedulers rooted in *tmp_path*; stops all on teardown."""
    created: list[Scheduler] = []

    def _make(
        toml_text: str = _TOML,
        *,
        file_state: str = "file",
        enabled: bool = False,
        state_text: Optional[str] = None,
        legacy_text: Optional[str] = None,
        state_home_is_file: bool = False,
    ) -> _Env:
        toml_path = tmp_path / "scheduler.toml"
        if not toml_path.exists():
            _write_config(toml_path, file_state, toml_text)
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        paths = _paths_for(data_dir)
        if state_text is not None:
            paths.scheduler_state.write_text(state_text)
        if legacy_text is not None:
            paths.scheduler_jobs.write_text(legacy_text)
        if state_home_is_file:
            blocker = tmp_path / "state_home_blocker"
            blocker.write_text("not a directory")
            paths = dataclasses.replace(paths, state_home=blocker)
        notify = MagicMock()
        sched = Scheduler(
            config={
                "scheduler": {"enabled": enabled},
                "agent": {"long_run_warn_minutes": 0},
            },
            notify_fn=notify,
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(toml_path),
        )
        created.append(sched)
        return _Env(sched, notify, toml_path, data_dir)

    yield _make
    for sched in created:
        sched.stop()


def _snapshot(path: Path) -> object:
    """Return the file bytes at *path*, or a marker for a directory or absence."""
    if path.is_dir():
        return "<directory>"
    if not path.exists():
        return "<missing>"
    return path.read_bytes()


def _tags(sched: Scheduler) -> list[str]:
    """Return the tags the scheduler lists, in order."""
    return [job["tag"] for job in sched.list_jobs()]


def _is_scheduled(sched: Scheduler, tag: str) -> bool:
    """True when *tag* has a computed next run or a ``schedule`` library trigger."""
    job = sched.show_job(tag)
    return bool(job and job.get("next_run")) or bool(schedule.get_jobs(tag))


def _tool_text(sched: Scheduler, args: dict) -> str:
    """Run the real ``schedule`` built-in tool and return its output plus error text."""
    result = exec_schedule(sched, args)
    return f"{result.get('output', '')}\n{result.get('error', '')}"


def _load_status(sched: Scheduler) -> str:
    """Return the status carried by the scheduler's ADR-0037 ``SchedulerLoadResult``.

    The design names the type and its four states but not the attribute that
    holds it, so the instance attributes are searched rather than assuming a
    name. Missing API fails at assertion time, never at import or setup.
    """
    result_type = getattr(scheduler_module, "SchedulerLoadResult", None)
    if result_type is None:
        pytest.fail("scheduler.SchedulerLoadResult is not implemented (ADR-0037 explicit load result)")
    results = [value for value in vars(sched).values() if isinstance(value, result_type)]
    assert len(results) == 1, "the Scheduler should hold exactly one SchedulerLoadResult"
    result = results[0]
    if dataclasses.is_dataclass(result):
        values = [getattr(result, f.name) for f in dataclasses.fields(result)]
    else:
        values = list(vars(result).values())
    statuses = {
        getattr(value, "value", value)
        for value in values
        if getattr(value, "value", value) in _LOAD_STATES
    }
    assert len(statuses) == 1, f"expected one load status among {_LOAD_STATES}, got {statuses}"
    return statuses.pop()


def _scheduler_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return WARNING-or-higher messages emitted by the scheduler module."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "scheduler" and record.levelno >= logging.WARNING
    ]


# ---------------------------------------------------------------------------
# Local settings: warning safety
# ---------------------------------------------------------------------------


def test_malformed_local_setting_warning_omits_raw_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The contained-setting warning names the setting path but never its raw value."""
    toml_path = tmp_path / "scheduler.toml"
    toml_path.write_text(_TOML)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        sched = Scheduler(
            config={"scheduler": {"enabled": False, "execution_log_max_age_hours": _SECRET}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=_paths_for(data_dir),
            scheduler_config_path=str(toml_path),
        )
    try:
        warnings = _scheduler_warnings(caplog)
        assert any("scheduler.execution_log_max_age_hours" in msg for msg in warnings)
        assert not any(_SECRET in record.getMessage() for record in caplog.records)
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# Explicit load result and cold file states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_state", "toml_text", "expected"),
    [
        pytest.param("file", _TOML, "valid", id="valid"),
        pytest.param("file", "", "valid", id="valid_empty"),
        pytest.param("missing", "", "missing", id="missing"),
        pytest.param("unreadable", "", "unreadable", id="unreadable"),
        pytest.param("unparseable", "", "unparseable", id="unparseable"),
    ],
)
def test_load_result_reports_file_state(
    make_env: Callable[..., _Env], file_state: str, toml_text: str, expected: str
) -> None:
    """The explicit load result distinguishes valid, missing, unreadable, unparseable."""
    env = make_env(toml_text, file_state=file_state)
    assert _load_status(env.sched) == expected


def test_missing_file_loads_empty_job_map_without_error(
    make_env: Callable[..., _Env], caplog: pytest.LogCaptureFixture
) -> None:
    """A missing scheduler file is a quiet empty state: no jobs, no error."""
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        env = make_env(file_state="missing")
        env.sched.start()
    assert env.sched.list_jobs() == []
    assert not any(
        record.name == "scheduler" and record.levelno >= logging.ERROR
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("file_state", "toml_text"),
    [
        pytest.param("file", _TOML, id="valid"),
        pytest.param("file", "", id="valid_empty"),
        pytest.param("missing", "", id="missing"),
    ],
)
def test_loadable_file_sends_no_operator_notice(
    make_env: Callable[..., _Env], file_state: str, toml_text: str
) -> None:
    """Valid, valid-empty, and missing files never notify the operator at startup."""
    env = make_env(toml_text, file_state=file_state)
    env.sched.start()
    env.notify.assert_not_called()


def test_missing_file_first_add_creates_file(make_env: Callable[..., _Env]) -> None:
    """From a missing-file cold start, the first explicit add creates the file."""
    env = make_env(file_state="missing")
    result = env.sched.add_job(
        tag="first", schedule_type="cron", task="first job", cron="0 4 * * *",
    )
    assert result["success"] is True
    assert env.toml_path.is_file()
    reopened = make_env()
    assert _tags(reopened.sched) == ["first"]


def test_unparseable_file_bytes_survive_startup(make_env: Callable[..., _Env]) -> None:
    """Construction plus start never rewrites an unparseable scheduler file."""
    env = make_env(file_state="unparseable")
    env.sched.start()
    assert env.toml_path.read_bytes() == _UNPARSEABLE.encode()


@pytest.mark.parametrize("file_state", ["unreadable", "unparseable"])
def test_known_bad_file_refuses_add_before_attempt(
    make_env: Callable[..., _Env], file_state: str
) -> None:
    """A mutation against a known-bad file is refused before ``_on_attempt``, file untouched."""
    env = make_env(file_state=file_state)
    before = _snapshot(env.toml_path)
    attempt = MagicMock()
    result = env.sched.add_job(
        tag="new", schedule_type="cron", task="new job", cron="0 4 * * *",
        _on_attempt=attempt,
    )
    assert result["success"] is False
    assert str(env.toml_path) in result["error"]
    attempt.assert_not_called()
    assert _snapshot(env.toml_path) == before


# ---------------------------------------------------------------------------
# Operator notice for an unreadable / unparseable file
# ---------------------------------------------------------------------------

_BAD_FILE_CASES = [
    pytest.param("unreadable", id="unreadable"),
    pytest.param("unparseable", id="unparseable"),
]


def _assert_actionable_safe_notice(text: str, env: _Env, status: str) -> None:
    """Assert *text* names path, status, and a repair step without file content."""
    lowered = text.lower()
    assert str(env.toml_path) in text
    assert status in lowered
    assert "reload" in lowered or "restart" in lowered
    assert _SECRET not in text
    assert "call the api" not in lowered


@pytest.mark.parametrize("status", _BAD_FILE_CASES)
def test_bad_file_sends_one_operator_notice(
    make_env: Callable[..., _Env], status: str
) -> None:
    """Startup with a bad file sends exactly one actionable, content-free notice."""
    env = make_env(file_state=status)
    env.sched.start()
    messages = [call.args[0] for call in env.notify.call_args_list]
    assert len(messages) == 1
    _assert_actionable_safe_notice(messages[0], env, status)


@pytest.mark.parametrize("status", _BAD_FILE_CASES)
def test_bad_file_notice_visible_in_tool_list(
    make_env: Callable[..., _Env], status: str
) -> None:
    """The file notice appears in ``schedule list`` without a fabricated job entry."""
    env = make_env(file_state=status)
    assert env.sched.list_jobs() == []
    _assert_actionable_safe_notice(_tool_text(env.sched, {"action": "list"}), env, status)


@pytest.mark.parametrize("status", _BAD_FILE_CASES)
def test_bad_file_notice_visible_in_tool_show(
    make_env: Callable[..., _Env], status: str
) -> None:
    """The file notice appears in the ``schedule show`` response."""
    env = make_env(file_state=status)
    text = _tool_text(env.sched, {"action": "show", "tag": "alpha"})
    _assert_actionable_safe_notice(text, env, status)


# ---------------------------------------------------------------------------
# Per-job containment
# ---------------------------------------------------------------------------

_CRON_BASE = 'schedule = "cron"\ncron = "0 4 * * *"\n'

# Bad time/scheduling fields: the job must be visibly unscheduled.
_SCHEDULING_BAD = [
    pytest.param('schedule = "cron"\ncron = "not a cron"\ntask = "t"\n', id="bad_cron"),
    pytest.param('schedule = "weekly"\ncron = "0 4 * * *"\ntask = "t"\n', id="unknown_schedule_type"),
    pytest.param('schedule = "once"\ntask = "t"\n', id="once_without_run_at"),
    pytest.param('schedule = "interval"\nhours = "six"\ntask = "t"\n', id="legacy_interval_bad_hours"),
    pytest.param('enabled = "yes"\n' + _CRON_BASE + 'task = "t"\n', id="enabled_not_bool"),
]

# Other bad fields: the job stays scheduled (fire-time behavior is task 12.23).
_OTHER_BAD = [
    pytest.param(_CRON_BASE + 'task = "t"\nnotify = "yes"\n', id="notify_str"),
    pytest.param(_CRON_BASE + 'task = "t"\npreserve_context = "true"\n', id="preserve_context_str"),
    pytest.param(
        _CRON_BASE + 'task = "t"\npreserve_context = true\ncontext_max_messages = "lots"\n',
        id="context_max_messages_str",
    ),
    pytest.param(_CRON_BASE + 'task = "t"\nmax_iterations = "many"\n', id="max_iterations_str"),
    pytest.param(_CRON_BASE + "task = 42\n", id="task_int"),
    pytest.param(_CRON_BASE + 'task = "t"\noverlap_policy = "sometimes"\n', id="overlap_policy_invalid"),
]


def _with_bad_job(body: str) -> str:
    """Return the healthy-job TOML plus a ``[jobs.bad]`` table with *body*."""
    return _TOML + "\n[jobs.bad]\n" + body


@pytest.mark.parametrize("body", _SCHEDULING_BAD + _OTHER_BAD)
def test_bad_job_leaves_healthy_sibling_registered(
    make_env: Callable[..., _Env], body: str
) -> None:
    """One malformed job never prevents its healthy sibling from loading and registering."""
    env = make_env(_with_bad_job(body))
    env.sched.reload()
    assert "healthy" in _tags(env.sched)
    assert _is_scheduled(env.sched, "healthy")


@pytest.mark.parametrize("body", _SCHEDULING_BAD)
def test_bad_scheduling_field_shows_config_error(
    make_env: Callable[..., _Env], body: str
) -> None:
    """A bad scheduling field keeps the job listed with a visible ``config_error``."""
    env = make_env(_with_bad_job(body))
    assert "bad" in _tags(env.sched)
    assert "config_error" in _tool_text(env.sched, {"action": "list"})
    assert "config_error" in _tool_text(env.sched, {"action": "show", "tag": "bad"})


@pytest.mark.parametrize("body", _SCHEDULING_BAD)
def test_bad_scheduling_field_leaves_job_unscheduled(
    make_env: Callable[..., _Env], body: str
) -> None:
    """A job with a bad scheduling field is never registered to fire."""
    env = make_env(_with_bad_job(body))
    env.sched.reload()
    assert not _is_scheduled(env.sched, "bad")


@pytest.mark.parametrize("body", _SCHEDULING_BAD)
def test_bad_scheduling_field_run_now_returns_config_error(
    make_env: Callable[..., _Env], body: str
) -> None:
    """``run_now`` of an unscheduled bad job returns the failure result with its error."""
    env = make_env(_with_bad_job(body))
    job = env.sched.show_job("bad") or {}
    config_error = job.get("config_error")
    assert config_error, "show_job should expose the job's config_error"
    result = env.sched.run_now("bad")
    assert result["success"] is False
    assert config_error in result["error"]


@pytest.mark.parametrize("body", _OTHER_BAD)
def test_non_scheduling_bad_field_job_stays_registered(
    make_env: Callable[..., _Env], body: str
) -> None:
    """A bad non-scheduling field leaves the job loaded and registered."""
    env = make_env(_with_bad_job(body))
    env.sched.reload()
    assert _is_scheduled(env.sched, "bad")


@pytest.mark.parametrize(
    "model_line",
    [
        pytest.param('model = ""\n', id="blank"),
        pytest.param('model = "   "\n', id="whitespace"),
        pytest.param("model = 42\n", id="integer"),
        pytest.param('model = ["a", "b"]\n', id="array"),
        pytest.param('model = { provider = "x" }\n', id="inline_table"),
    ],
)
def test_raw_model_value_loads_without_config_error(
    make_env: Callable[..., _Env], caplog: pytest.LogCaptureFixture, model_line: str
) -> None:
    """No raw ``model`` value produces a load-time warning or ``config_error``."""
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        env = make_env(_with_bad_job(_CRON_BASE + 'task = "t"\n' + model_line))
        env.sched.reload()
    assert _scheduler_warnings(caplog) == []
    assert (env.sched.show_job("bad") or {}).get("config_error") is None
    assert _is_scheduled(env.sched, "bad")


def test_legacy_job_fallback_models_loads_without_warning(
    make_env: Callable[..., _Env], caplog: pytest.LogCaptureFixture
) -> None:
    """A legacy per-job ``fallback_models`` key is retained silently at load."""
    toml_text = _with_bad_job(_CRON_BASE + 'task = "t"\nfallback_models = ["model-b"]\n')
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        env = make_env(toml_text)
        env.sched.start()
    assert _scheduler_warnings(caplog) == []
    assert (env.sched.show_job("bad") or {}).get("config_error") is None
    assert env.toml_path.read_text() == toml_text


def test_agent_fallback_setting_remains_config_error() -> None:
    """The agent-level fallback setting is still rejected by config parsing (distinct path)."""
    with pytest.raises(ConfigError, match=r"agent\.fallback_models"):
        parse_config({"agent": {"fallback_models": ["model-b"]}})


# ---------------------------------------------------------------------------
# State, storage, and registration containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state_text",
    [
        pytest.param("{not json", id="not_json"),
        pytest.param("[]", id="top_level_list"),
        pytest.param('{"history": {"healthy": "oops"}}', id="history_entry_not_table"),
    ],
)
def test_malformed_state_file_does_not_escape_construction(
    make_env: Callable[..., _Env], state_text: str
) -> None:
    """A malformed scheduler state file is contained; the healthy job still loads."""
    env = make_env(state_text=state_text)
    assert _tags(env.sched) == ["healthy"]


def test_non_json_toml_value_does_not_escape_state_save(make_env: Callable[..., _Env]) -> None:
    """A TOML-only value (datetime) cannot break construction through the state save."""
    toml_text = _with_bad_job(_CRON_BASE + 'task = "t"\ncreated_at = 2026-01-01T00:00:00Z\n')
    env = make_env(toml_text)
    assert "healthy" in _tags(env.sched)


def test_unavailable_state_home_does_not_escape_construction(
    make_env: Callable[..., _Env],
) -> None:
    """A state directory that cannot be created is contained; the healthy job still loads."""
    env = make_env(state_home_is_file=True)
    assert _tags(env.sched) == ["healthy"]


@pytest.mark.parametrize(
    "legacy_text",
    [
        pytest.param("{not json", id="not_json"),
        pytest.param("[]", id="top_level_list"),
        pytest.param('{"old": "not a table"}', id="job_not_table"),
    ],
)
def test_malformed_legacy_job_file_does_not_escape_construction(
    make_env: Callable[..., _Env], legacy_text: str
) -> None:
    """A malformed legacy job file is contained; TOML jobs still load."""
    env = make_env(legacy_text=legacy_text)
    assert "healthy" in _tags(env.sched)


def test_registration_failure_does_not_escape_start(make_env: Callable[..., _Env]) -> None:
    """A trigger-library registration failure is contained to its job at start."""
    toml_text = (
        '[jobs.reminder]\nenabled = true\nschedule = "once"\nrun_at = "10:00"\ntask = "r"\n'
        + _FAR_HEALTHY
    )
    env = make_env(toml_text, enabled=True)
    with patch("scheduler.schedule.every", side_effect=ValueError("registration boom")):
        env.sched.start()
    assert _is_scheduled(env.sched, "healthy")


# ---------------------------------------------------------------------------
# Load never writes: legacy daily/interval derivation stays in memory
# ---------------------------------------------------------------------------

_LEGACY_SCHEDULES = [
    pytest.param('schedule = "daily"\ntime = "03:30"\n', "30 3 * * *", id="daily"),
    pytest.param('schedule = "interval"\nhours = 6\n', "0 */6 * * *", id="interval"),
]

_LEGACY_SCHEDULE_LINES = [
    pytest.param('schedule = "daily"\ntime = "03:30"\n', id="daily"),
    pytest.param('schedule = "interval"\nhours = 6\n', id="interval"),
]


def _legacy_toml(schedule_lines: str) -> str:
    """Return the healthy job plus a legacy-schedule job with comments and raw extras."""
    return (
        "# operator notes: keep this comment\n"
        + _TOML
        + "\n[jobs.legacy]  # legacy schedule\n"
        + "enabled = true\n"
        + schedule_lines
        + 'task = "legacy job"\n'
        + 'owner = "ops"  # unknown field\n'
        + 'fallback_models = ["model-b"]\n'
        + 'model = ""\n'
    )


def _side_files(toml_path: Path) -> list[str]:
    """Return backup/temp artifacts written next to the scheduler file."""
    prefix = toml_path.name + "."
    return sorted(p.name for p in toml_path.parent.iterdir() if p.name.startswith(prefix))


@pytest.mark.parametrize("schedule_lines", _LEGACY_SCHEDULE_LINES)
def test_legacy_schedule_load_leaves_file_untouched(
    make_env: Callable[..., _Env], schedule_lines: str
) -> None:
    """Constructing over a legacy schedule keeps the exact bytes and writes no backups."""
    toml_text = _legacy_toml(schedule_lines)
    env = make_env(toml_text)
    assert (env.toml_path.read_bytes(), _side_files(env.toml_path)) == (toml_text.encode(), [])


@pytest.mark.parametrize(("schedule_lines", "cron"), _LEGACY_SCHEDULES)
def test_legacy_schedule_load_derives_cron_in_memory(
    make_env: Callable[..., _Env], schedule_lines: str, cron: str
) -> None:
    """The runtime view of a legacy schedule carries the derived cron; healthy still loads."""
    env = make_env(_legacy_toml(schedule_lines))
    assert (env.sched.show_job("legacy") or {}).get("cron") == cron
    assert "healthy" in _tags(env.sched)


@pytest.mark.parametrize("schedule_lines", _LEGACY_SCHEDULE_LINES)
def test_reload_of_edited_legacy_schedule_leaves_file_untouched(
    make_env: Callable[..., _Env], schedule_lines: str
) -> None:
    """Reloading an operator edit that adds a legacy schedule keeps the new bytes, no backups."""
    env = make_env()
    edited = _legacy_toml(schedule_lines)
    env.toml_path.write_text(edited)
    env.sched.reload()
    assert (env.toml_path.read_bytes(), _side_files(env.toml_path)) == (edited.encode(), [])


@pytest.mark.parametrize(("schedule_lines", "cron"), _LEGACY_SCHEDULES)
def test_reload_of_edited_legacy_schedule_refreshes_runtime_view(
    make_env: Callable[..., _Env], schedule_lines: str, cron: str
) -> None:
    """After reloading the operator edit, the runtime view shows the derived cron."""
    env = make_env()
    env.toml_path.write_text(_legacy_toml(schedule_lines))
    env.sched.reload()
    assert (env.sched.show_job("legacy") or {}).get("cron") == cron


# Raw non-scheduling values the old runtime-only TOML writer cannot encode.
_UNENCODABLE_BAD = [
    pytest.param(_CRON_BASE + "task = 42\n", id="task_int"),
    pytest.param(_CRON_BASE + 'task = { text = "t" }\n', id="task_table"),
    pytest.param(_CRON_BASE + 'task = "t"\nmodel = { provider = "x" }\n', id="model_table"),
    pytest.param(_CRON_BASE + 'task = "t"\nmodel = 2026-01-01T00:00:00Z\n', id="model_datetime"),
]


def _legacy_with_bad_sibling(body: str) -> str:
    """Return a valid legacy-daily file plus a ``[jobs.bad]`` table with *body*."""
    return _legacy_toml('schedule = "daily"\ntime = "03:30"\n') + "\n[jobs.bad]\n" + body


@pytest.mark.parametrize("body", _UNENCODABLE_BAD)
def test_legacy_schedule_with_unencodable_sibling_does_not_escape_construction(
    make_env: Callable[..., _Env], body: str
) -> None:
    """A raw value beside a legacy schedule cannot break construction; healthy still loads."""
    env = make_env(_legacy_with_bad_sibling(body))
    assert "healthy" in _tags(env.sched)


@pytest.mark.parametrize("body", _UNENCODABLE_BAD)
def test_legacy_schedule_with_unencodable_sibling_keeps_file_bytes(
    make_env: Callable[..., _Env], body: str
) -> None:
    """Construction beside an unencodable raw value leaves the scheduler file bytes intact."""
    toml_text = _legacy_with_bad_sibling(body)
    env = make_env(toml_text)
    assert env.toml_path.read_bytes() == toml_text.encode()


# ---------------------------------------------------------------------------
# Legacy JSON job file: malformed fields inside a job object
# ---------------------------------------------------------------------------

_MALFORMED_LEGACY_FIELDS = [
    pytest.param(
        '{"old": {"task": 42, "schedule_type": "cron", "cron": "0 4 * * *", "enabled": true}}',
        id="task_int",
    ),
    pytest.param(
        '{"old": {"task": "t", "schedule_type": "cron", "cron": "0 4 * * *",'
        ' "enabled": true, "model": {"provider": "x"}}}',
        id="model_object",
    ),
]


@pytest.mark.parametrize("legacy_text", _MALFORMED_LEGACY_FIELDS)
def test_malformed_legacy_job_field_does_not_escape_construction(
    make_env: Callable[..., _Env], legacy_text: str
) -> None:
    """A legacy JSON job with a malformed field cannot break construction; TOML jobs load."""
    env = make_env(legacy_text=legacy_text)
    assert "healthy" in _tags(env.sched)


@pytest.mark.parametrize("legacy_text", _MALFORMED_LEGACY_FIELDS)
def test_malformed_legacy_job_field_keeps_legacy_file(
    make_env: Callable[..., _Env], tmp_path: Path, legacy_text: str
) -> None:
    """The legacy JSON file holding a malformed job field is kept byte-for-byte."""
    legacy_path = _paths_for(tmp_path / "data").scheduler_jobs
    make_env(legacy_text=legacy_text)
    assert _snapshot(legacy_path) == legacy_text.encode()


# ---------------------------------------------------------------------------
# Legacy daily time compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("time_str", ["2:3", "02:3"])
def test_legacy_daily_single_digit_minute_derives_cron(
    make_env: Callable[..., _Env], time_str: str
) -> None:
    """Legacy daily times with a one-digit minute stay accepted as by the baseline parser."""
    env = make_env(_TOML + f'\n[jobs.legacy]\nschedule = "daily"\ntime = "{time_str}"\ntask = "t"\n')
    assert (env.sched.show_job("legacy") or {}).get("cron") == "3 2 * * *"


# ---------------------------------------------------------------------------
# TOML origin decides treatment (legacy lookalike values)
#
# Relocated by task 2.4 from tests/test_scheduler_legacy_readonly.py (ledger
# provenance in openspec/changes/simplify-model-runtime/test-preservation-ledger).
# Deferred rewrite (task 8.3, ledger 449/450): obsolete legacy-lookalike fields
# become a visible per-job config_error instead of legacy marking.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[legacy_fx.MakeLegacyScheduler]:
    """Real legacy-beside-TOML schedulers (shared factory, see the support module)."""
    yield from legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)


# A scheduler.toml job whose raw values imitate legacy origin markers.
_SPOOF_TOML = legacy_fx.TOML + """
[jobs.spoofed]
enabled = true
schedule = "cron"
cron = "0 6 1 1 *"
task = "spoofed task"
notify = false
source = "dynamic"
_legacy_read_only = true
"""


class TestOriginSpoofGuard:
    """Read-only origin comes from the legacy file, never from raw values."""

    def test_toml_job_with_legacy_lookalike_values_is_writable(
        self, make_legacy_scheduler: legacy_fx.MakeLegacyScheduler,
    ) -> None:
        """``source = "dynamic"`` plus a raw private flag in scheduler.toml stays writable."""
        env = make_legacy_scheduler(_SPOOF_TOML, legacy=legacy_fx.UNRELATED_LEGACY)

        out = exec_schedule(env.sched, {"action": "modify", "tag": "spoofed", "task": "edited"})

        assert out["success"] is True, out
        assert read_execution(out) == "executed"
        assert legacy_fx.parse_toml(env.toml_path)["jobs"]["spoofed"]["task"] == "edited"

    def test_toml_job_with_legacy_lookalike_values_is_unmarked(
        self, make_legacy_scheduler: legacy_fx.MakeLegacyScheduler,
    ) -> None:
        """The lookalike scheduler.toml job carries no read-only marker."""
        env = make_legacy_scheduler(_SPOOF_TOML, legacy=legacy_fx.UNRELATED_LEGACY)

        assert env.sched.show_job("spoofed")["schedule"] == "cron: 0 6 1 1 *"
