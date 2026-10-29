"""
Fire-time dispositions for raw, type-strict scheduler job fields (task 12.23).

scheduler.toml loads raw: a wrong-typed field never stops the load and is
never coerced. The disposition is decided when the job fires (automatically
or through ``run_now``):

* an ACTIVE job with an invalid ``overlap_policy`` is conservatively skipped
  (no run ID, no record, no alert, the active run untouched); an IDLE job with
  an invalid policy mints a run ID, persists ``last_run``, and ends as a normal
  recorded launch failure;
* a non-string ``task`` and a wrong-typed ``notify``, ``preserve_context``,
  ``context_max_messages`` or ``max_iterations`` are a normal recorded launch
  failure: no spawn, a direct alert even with ``notify = false``, tracking
  released; only a blank STRING task takes the empty-task reminder path;
* a well-typed ``max_iterations`` stays silently ignored;
* a legacy per-job ``fallback_models`` key is silent at load and reload and
  stays in the document; at fire it yields one warning naming the job and the
  key (never its value) and is never passed to the spawn call.

Overlap ``skip``/``parallel`` with valid values, the ``""`` reminder with
``notify = true``, and failed-spawn records are owned by
``tests/test_job_execution_log.py`` and are not duplicated here.

Only external boundaries are substituted: the file system (``tmp_path``),
notify, agent, the spawn-capable executor, the ``schedule`` library's global
scheduler, and ``threading.excepthook`` for the manual-fire thread. The
scheduler is real; private state is read only for observation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import Scheduler
from xdg import xdg_paths


_BOUND = 10.0
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")
_SECRET = "sk-fake-secret-fallback-7d41"
_FALLBACK_TAG = "legacy_fallback_job"
_FALLBACK_VALUE = ["provider/alt-model", _SECRET]
_MISSING = object()


def _job(tag: str, extra: str = "", task: str = '"write report"') -> str:
    """Return one far-future cron job table with ``notify = false``."""
    return (
        f"[jobs.{tag}]\nenabled = true\nschedule = \"cron\"\ncron = \"0 4 1 1 *\"\n"
        f"task = {task}\nnotify = false\n{extra}"
    )


# name -> (tag, scheduler.toml job table); every job loads without config_error.
_BAD_FIELD_JOBS = {
    "invalid_overlap_policy": ("bogus_policy", _job("bogus_policy", 'overlap_policy = "bogus"\n')),
    "non_string_task": ("numeric_task", _job("numeric_task", task="123")),
    "notify_wrong_type": (
        "notify_typed",
        _job("notify_typed").replace("notify = false\n", 'notify = "nope"\n'),
    ),
    "preserve_context_wrong_type": (
        "preserve_typed", _job("preserve_typed", 'preserve_context = "yes"\n'),
    ),
    "context_max_messages_wrong_type": (
        "ctx_max_typed", _job("ctx_max_typed", 'context_max_messages = "abc"\n'),
    ),
    "max_iterations_wrong_type": (
        "iterations_typed", _job("iterations_typed", 'max_iterations = "5"\n'),
    ),
}

_FALLBACK_TOML = _job(
    _FALLBACK_TAG, f"fallback_models = {json.dumps(_FALLBACK_VALUE)}\n",
)


class _Env:
    """A real disabled scheduler with its file, paths, and captured spawn calls."""

    def __init__(self, sched: Scheduler, toml_path: Path, state_path: Path) -> None:
        self.sched = sched
        self.toml_path = toml_path
        self.state_path = state_path
        self.spawns: list[tuple[dict, object]] = []
        executor = MagicMock()

        def spawn(args: dict, options: object) -> dict:
            self.spawns.append((dict(args), options))
            return {"success": True, "agent_id": f"sa-{len(self.spawns)}"}

        executor.spawn_agent = MagicMock(side_effect=spawn)
        sched.builtin_executor = executor

    def records(self, tag: str) -> list[dict]:
        """Return the persisted execution records for *tag*."""
        return [e for e in self.sched.execution_log.read_recent() if e["tag"] == tag]

    def alerts(self) -> list[str]:
        """Return every message sent through the notify boundary."""
        return [call.args[0] for call in self.sched.notify.call_args_list]


@pytest.fixture
def isolated_schedule(monkeypatch: pytest.MonkeyPatch) -> schedule.Scheduler:
    """Replace the external library's global scheduler (and its ``jobs`` alias)."""
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    return isolated


@pytest.fixture
def make_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_schedule: schedule.Scheduler,
) -> Iterator[Callable[[str], _Env]]:
    """Construct a real disabled Scheduler over *toml* with every XDG dir in ``tmp_path``."""
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("fire-dispositions-test")
    assert str(paths.scheduler_state).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(toml: str) -> _Env:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.state_home.mkdir(parents=True, exist_ok=True)
        paths.scheduler_config.write_text(toml, encoding="utf-8")
        sched = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(paths.scheduler_config),
        )
        created.append(sched)
        return _Env(sched, paths.scheduler_config, paths.scheduler_state)

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()


def _assert_recorded_launch_failure(env: _Env, tag: str) -> None:
    """No spawn; one failed record with a run ID; one direct alert; tracking released."""
    assert env.spawns == [], "a bad-field fire reached the spawn call"
    records = env.records(tag)
    assert len(records) == 1, records
    assert _RUN_ID_RE.match(records[0]["run_id"])
    assert records[0]["status"] == "failed"
    assert records[0]["success"] is False
    assert records[0]["result"]
    alerts = env.alerts()
    assert len(alerts) == 1, alerts
    assert tag in alerts[0] and "failed to" in alerts[0]
    assert "Reminder" not in alerts[0]
    assert tag not in env.sched._running_jobs
    assert not env.sched._active_runs.get(tag)


# ---------------------------------------------------------------------------
# Invalid overlap policy
# ---------------------------------------------------------------------------


def test_active_job_with_invalid_policy_is_skipped(make_scheduler: Callable[[str], _Env]) -> None:
    """An active job is not admitted again: no spawn, no run ID, no record, no alert."""
    tag, toml = _BAD_FIELD_JOBS["invalid_overlap_policy"]
    env = make_scheduler(toml)
    env.sched._running_jobs.add(tag)

    env.sched._run_job(tag)

    assert env.spawns == []
    assert not env.sched._active_runs.get(tag)
    assert env.records(tag) == []
    assert env.alerts() == []
    assert tag in env.sched._running_jobs, "the active run's marker was released"


def test_idle_job_with_invalid_policy_persists_last_run(
    make_scheduler: Callable[[str], _Env],
) -> None:
    """The admitted run's ``last_run`` reaches the saved scheduler state."""
    tag, toml = _BAD_FIELD_JOBS["invalid_overlap_policy"]
    env = make_scheduler(toml)

    env.sched._run_job(tag)

    history = json.loads(env.state_path.read_text(encoding="utf-8"))["history"]
    assert history[tag]["last_run"] is not None
    assert env.records(tag), "the invalid-policy run left no execution record"


# ---------------------------------------------------------------------------
# Wrong-typed fields are recorded launch failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(_BAD_FIELD_JOBS))
def test_bad_field_fire_is_recorded_launch_failure(
    make_scheduler: Callable[[str], _Env], case: str,
) -> None:
    """An idle automatic fire ends as a normal recorded launch failure."""
    tag, toml = _BAD_FIELD_JOBS[case]
    env = make_scheduler(toml)

    env.sched._run_job(tag)

    _assert_recorded_launch_failure(env, tag)


def test_run_now_with_non_string_task_is_recorded_launch_failure(
    make_scheduler: Callable[[str], _Env], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual fire thread records the launch failure instead of raising."""
    tag, toml = _BAD_FIELD_JOBS["non_string_task"]
    env = make_scheduler(toml)
    finished = threading.Event()
    runners: list[threading.Thread] = []
    escaped: list[type] = []

    def notify(_message: str) -> None:
        runners.append(threading.current_thread())
        finished.set()

    def excepthook(args: threading.ExceptHookArgs) -> None:
        escaped.append(args.exc_type)
        if args.thread is not None:
            runners.append(args.thread)
        finished.set()

    env.sched.notify.side_effect = notify
    monkeypatch.setattr(threading, "excepthook", excepthook)

    assert env.sched.run_now(tag) == {"success": True}
    assert finished.wait(_BOUND), "the manual fire neither alerted nor raised"
    for runner in runners:
        runner.join(timeout=_BOUND)
    assert [runner for runner in runners if runner.is_alive()] == []

    assert escaped == [], f"the manual fire raised {escaped!r}"
    _assert_recorded_launch_failure(env, tag)


# ---------------------------------------------------------------------------
# Characterizations: reminder, well-typed max_iterations, healthy spawn
# ---------------------------------------------------------------------------


def test_blank_string_task_takes_notify_gated_reminder(
    make_scheduler: Callable[[str], _Env],
) -> None:
    """A whitespace-only task gets no run ID or record; ``notify = false`` sends nothing."""
    env = make_scheduler(_job("blank_task", task='"   "'))

    env.sched._run_job("blank_task")

    assert env.spawns == []
    assert not env.sched._active_runs.get("blank_task")
    assert env.records("blank_task") == []
    assert env.alerts() == []
    assert env.sched.show_job("blank_task")["last_run"] is not None


def test_well_typed_max_iterations_runs_normally(make_scheduler: Callable[[str], _Env]) -> None:
    """An integer ``max_iterations`` is ignored: one spawn, no failure, no alert."""
    env = make_scheduler(_job("iterations_ok", "max_iterations = 5\n"))

    env.sched._run_job("iterations_ok")

    assert len(env.spawns) == 1
    assert env.spawns[0][0]["task"] == "write report"
    assert env.records("iterations_ok") == []
    assert env.alerts() == []


def test_healthy_job_spawns_without_fallback_warning(
    make_scheduler: Callable[[str], _Env], caplog: pytest.LogCaptureFixture,
) -> None:
    """A job without ``fallback_models`` spawns once and warns about nothing."""
    env = make_scheduler(_job("healthy"))
    caplog.set_level(logging.DEBUG, logger="scheduler")

    env.sched._run_job("healthy")

    assert len(env.spawns) == 1
    assert [r for r in caplog.records if "fallback_models" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# Legacy per-job fallback_models
# ---------------------------------------------------------------------------


def test_load_is_silent_about_fallback_models(
    make_scheduler: Callable[[str], _Env], caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup logs nothing about the key and keeps it raw in the document."""
    caplog.set_level(logging.DEBUG, logger="scheduler")

    env = make_scheduler(_FALLBACK_TOML)

    assert [r for r in caplog.records if "fallback_models" in r.getMessage()] == []
    assert _SECRET not in caplog.text
    document = env.sched._load_result.document
    assert document["jobs"][_FALLBACK_TAG]["fallback_models"] == _FALLBACK_VALUE
    assert env.toml_path.read_text(encoding="utf-8") == _FALLBACK_TOML


def test_reload_is_silent_about_fallback_models(
    make_scheduler: Callable[[str], _Env], caplog: pytest.LogCaptureFixture,
) -> None:
    """A reload logs nothing about the key and does not rewrite the file."""
    env = make_scheduler(_FALLBACK_TOML)
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="scheduler")

    result = env.sched.reload()

    assert "error" not in result, result
    assert [r for r in caplog.records if "fallback_models" in r.getMessage()] == []
    assert _SECRET not in caplog.text
    assert env.toml_path.read_text(encoding="utf-8") == _FALLBACK_TOML


def test_fire_warns_once_about_fallback_models(
    make_scheduler: Callable[[str], _Env], caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning names the job and the ignored key."""
    env = make_scheduler(_FALLBACK_TOML)
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="scheduler")

    env.sched._run_job(_FALLBACK_TAG)

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "fallback_models" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert _FALLBACK_TAG in warnings[0].getMessage()


def test_fire_never_reveals_fallback_models_value(
    make_scheduler: Callable[[str], _Env], caplog: pytest.LogCaptureFixture,
) -> None:
    """The secret-bearing value is in no log record, alert, or spawn argument."""
    env = make_scheduler(_FALLBACK_TOML)
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="scheduler")

    env.sched._run_job(_FALLBACK_TAG)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING], "no fire warning"
    assert all(_SECRET not in r.getMessage() for r in caplog.records)
    assert _SECRET not in caplog.text
    assert all(_SECRET not in message for message in env.alerts())
    assert all(_SECRET not in repr(args) for args, _options in env.spawns)


def test_fire_spawns_once_without_fallback_models(
    make_scheduler: Callable[[str], _Env],
) -> None:
    """Exactly one spawn; the key is absent from its arguments and options."""
    env = make_scheduler(_FALLBACK_TOML)

    env.sched._run_job(_FALLBACK_TAG)

    assert len(env.spawns) == 1
    args, options = env.spawns[0]
    assert "fallback_models" not in args
    assert getattr(options, "fallback_models", _MISSING) is _MISSING


def test_fire_leaves_fallback_models_file_bytes_unchanged(
    make_scheduler: Callable[[str], _Env],
) -> None:
    """The fire never rewrites scheduler.toml; the key stays in the document."""
    env = make_scheduler(_FALLBACK_TOML)
    before = env.toml_path.read_bytes()

    env.sched._run_job(_FALLBACK_TAG)

    assert env.toml_path.read_bytes() == before
    document = env.sched._load_result.document
    assert document["jobs"][_FALLBACK_TAG]["fallback_models"] == _FALLBACK_VALUE
