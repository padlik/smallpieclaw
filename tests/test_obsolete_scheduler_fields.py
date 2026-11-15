"""Obsolete scheduler job fields are contained per job, never converted (task 8.3, ADR-0037/0044).

Spec ``scheduled-job-execution``: *Job configuration errors are contained to the offending job* and
*Mutation refusals precede the attempt hook only when knowable*.

* Obsolete SCHEDULING fields (``schedule`` = ``daily``/``interval``, ``time``, ``hours``, ``minutes``)
  leave the job visibly unscheduled with a ``config_error`` that names the field and points at
  ``cron`` / ``once``; no cron is derived and the file is never rewritten. Presence is the trigger,
  whatever the value type, even next to ``cron``/``once``.
* A per-job ``fallback_models`` key keeps the job scheduled but is a visible ``config_error``
  (remove-the-key guidance) and, when the job fires, ONE recorded launch failure with the direct
  alert; nothing is admitted, no credential/client/provider work, no model fallback.
* ``add``/``modify`` (scheduler API and the real ``schedule`` tool) refuse every obsolete input
  before ``_on_attempt`` and before any coercion, changing nothing.

Real ``Scheduler`` objects; only the ``schedule`` library's global, the file system, notify/agent
and the spawn entry point are substituted. The root-level case drives the real ``main._run`` chain.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builtin_tools.schedule import exec_schedule
from outcome_utils import read_execution
from scheduler import Scheduler
from telegram_formatter import format_jobs_list
from tests.root_startup_harness import MAIN_ID, RootHarness, ScheduledRootProbe, scheduled_job_toml
from tests.test_scheduled_model_admission import scheduled_root  # noqa: F401 - the real-root fixture
from tests.test_scheduler_load_containment import _paths_for

CANARY = "QZXobsolete-canary-8-3"
_HEALTHY = (
    '[jobs.healthy]\nenabled = true\nschedule = "cron"\ncron = "0 2 * * *"\ntask = "healthy job"\n'
    '\n[jobs.healthy_once]\nenabled = true\nschedule = "once"\nrun_at = "23:30"\ntask = "once job"\n'
)


class _Env:
    """A real disabled Scheduler plus the boundaries a test observes."""

    def __init__(self, sched: Scheduler, notify: MagicMock, toml_path: Path, spawns: list, state_file: Path) -> None:
        self.sched, self.notify, self.toml_path, self.spawns, self.state_file = sched, notify, toml_path, spawns, state_file

    def snapshot(self) -> tuple:
        """Everything a refused mutation or a load must leave alone."""
        state_file = self.state_file
        return (
            self.toml_path.read_bytes(),
            json.dumps(self.sched.list_jobs(), sort_keys=True, default=str),
            self.sched._config_generation,
            hashlib.sha256(state_file.read_bytes()).hexdigest() if state_file and state_file.is_file() else None,
        )


@pytest.fixture
def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., _Env]]:
    """Factory of real Schedulers over ``scheduler.toml`` text; every one is stopped on teardown."""
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    created: list[Scheduler] = []

    def _make(toml_text: str, *, enabled: bool = False) -> _Env:
        toml_path = tmp_path / "scheduler.toml"
        toml_path.write_text(toml_text, encoding="utf-8")
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        notify = MagicMock()
        sched = Scheduler(
            config={"scheduler": {"enabled": enabled}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=notify, agent_fn=MagicMock(), paths=_paths_for(data_dir),
            scheduler_config_path=str(toml_path),
        )
        spawns: list = []

        def spawn(args: dict, options: object) -> dict:
            spawns.append((dict(args), options))
            return {"success": True, "agent_id": f"sa-{len(spawns)}"}

        sched.builtin_executor = MagicMock()
        sched.builtin_executor.spawn_agent = MagicMock(side_effect=spawn)
        created.append(sched)
        return _Env(sched, notify, toml_path, spawns, _paths_for(data_dir).scheduler_state)

    yield _make
    for sched in created:
        sched.stop()


def _job(body: str, tag: str = "legacy") -> str:
    """One job table named ``tag`` with ``body`` lines and a task, after the healthy siblings."""
    return _HEALTHY + f"\n[jobs.{tag}]\nenabled = true\n{body}task = \"t\"\n"


def _is_scheduled(sched: Scheduler, tag: str) -> bool:
    """True when ``tag`` has a computed next run or a ``schedule`` library trigger."""
    job = sched.show_job(tag)
    return bool(job and job.get("next_run")) or bool(schedule.get_jobs(tag))


# ---------------------------------------------------------------------------
# A. Obsolete scheduling fields: unscheduled, visible, never converted
# ---------------------------------------------------------------------------

_SCHEDULING_CASES = [
    pytest.param('schedule = "daily"\ntime = "03:30"\n', ["daily", "time"], id="daily"),
    pytest.param('schedule = "daily"\n', ["daily"], id="daily-no-time"),
    pytest.param('schedule = "interval"\nhours = 6\n', ["interval", "hours"], id="interval-hours"),
    pytest.param('schedule = "interval"\nminutes = 15\n', ["interval", "minutes"], id="interval-minutes"),
    pytest.param('schedule = "interval"\n', ["interval"], id="interval-bare"),
    pytest.param('schedule = "cron"\ncron = "0 4 * * *"\ntime = "03:30"\n', ["time"], id="cron-with-time"),
    pytest.param('schedule = "cron"\ncron = "0 4 * * *"\nhours = 6\n', ["hours"], id="cron-with-hours"),
    pytest.param('schedule = "cron"\ncron = "0 4 * * *"\nminutes = 15\n', ["minutes"], id="cron-with-minutes"),
    pytest.param('schedule = "once"\ntime = "03:30"\n', ["time"], id="once-with-time-only"),
    pytest.param('schedule = "once"\nrun_at = "03:30"\ntime = "03:30"\n', ["time"], id="once-run_at-and-time"),
    pytest.param('schedule = "cron"\ncron = "0 4 * * *"\nhours = true\nminutes = 1.5\n', ["hours", "minutes"], id="cron-bool-and-float"),
    pytest.param(f'schedule = "cron"\ncron = "0 4 * * *"\ntime = "{CANARY}"\n', ["time"], id="cron-canary-time"),
    pytest.param("schedule = \"cron\"\ncron = \"0 4 * * *\"\ntime = 2026-01-01T00:00:00Z\n", ["time"], id="cron-datetime-time"),
]


def _assert_obsolete_unscheduled(env: _Env, fields: list[str], *, before: tuple, raw_cron: object = None) -> None:
    """The job is visibly unscheduled with a field-naming cron/once error; nothing was derived or written."""
    sched = env.sched
    job = sched.show_job("legacy")
    assert job is not None
    error = job["config_error"]
    assert isinstance(error, str) and error
    for field in fields:
        assert field in error, (field, error)
    assert "cron" in error and "once" in error, error
    assert CANARY not in error and "2026" not in error
    assert job["cron"] == raw_cron and job["next_run"] is None and sched.config_blocks_schedule("legacy") is True  # never derived
    listed = {entry["tag"]: entry for entry in sched.list_jobs()}
    assert listed["legacy"]["config_error"] == error and listed["legacy"]["next_run"] is None
    assert not _is_scheduled(sched, "legacy")
    refusal = sched.run_now("legacy")
    assert refusal["success"] is False and error in refusal["error"]
    # Healthy siblings are loaded, listed and untouched.
    for sibling in ("healthy", "healthy_once"):
        assert listed[sibling]["config_error"] in (None, "")
    assert env.toml_path.read_bytes() == before[0]  # raw bytes: no rewrite, no derived cron
    assert env.spawns == [] and sched.agent.call_count == 0


@pytest.mark.parametrize(("body", "fields"), _SCHEDULING_CASES)
def test_obsolete_scheduling_field_leaves_the_job_unscheduled_with_cron_once_guidance(
    make_env: Callable[..., _Env], body: str, fields: list[str], caplog: pytest.LogCaptureFixture,
) -> None:
    """Load: presence of an obsolete scheduling field (any value type, even beside cron/once) is a
    visible per-job config error naming the field; no cron derived, file untouched, siblings healthy."""
    caplog.set_level(logging.DEBUG)
    text = _job(body)
    env = make_env(text)
    before = (text.encode(),)

    _assert_obsolete_unscheduled(env, fields, before=before, raw_cron="0 4 * * *" if "cron = " in body else None)

    document = tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]["legacy"]
    assert "cron" not in document or document["cron"] == "0 4 * * *"  # never derived into the file
    assert CANARY not in caplog.text


@pytest.mark.parametrize(("body", "fields"), _SCHEDULING_CASES)
def test_reload_of_obsolete_scheduling_field_matches_load_and_never_writes(
    make_env: Callable[..., _Env], body: str, fields: list[str],
) -> None:
    """Reload applies the same per-job containment to an operator edit; bytes stay exactly as written."""
    env = make_env(_HEALTHY)
    edited = _job(body)
    env.toml_path.write_text(edited, encoding="utf-8")

    result = env.sched.reload()

    assert "error" not in result, result
    _assert_obsolete_unscheduled(env, fields, before=(edited.encode(),), raw_cron="0 4 * * *" if "cron = " in body else None)


def test_obsolete_scheduling_job_does_not_stop_siblings_from_scheduling(make_env: Callable[..., _Env]) -> None:
    """With the scheduler started, healthy cron/once siblings register while the obsolete job does not."""
    env = make_env(_job('schedule = "interval"\nhours = 6\n'), enabled=True)
    env.sched.start()

    assert _is_scheduled(env.sched, "healthy") and _is_scheduled(env.sched, "healthy_once")
    assert not _is_scheduled(env.sched, "legacy")


# ---------------------------------------------------------------------------
# C. fallback_models: visible, stays scheduled, fires as ONE recorded launch failure
# ---------------------------------------------------------------------------

_FALLBACK_VALUES = [
    pytest.param("[]", id="empty-list"),
    pytest.param(f'["{CANARY}"]', id="canary-list"),
    pytest.param("false", id="false"),
    pytest.param(f'"{CANARY}"', id="canary-string"),
    pytest.param("7", id="int"),
]


def _fallback_toml(value: str, notify: str = "false") -> str:
    """Healthy siblings plus one cron job carrying ``fallback_models = value``."""
    return _job(
        f'schedule = "cron"\ncron = "0 4 * * *"\nnotify = {notify}\nfallback_models = {value}\n',
    )


@pytest.mark.parametrize("value", _FALLBACK_VALUES)
def test_fallback_models_is_a_visible_config_error_but_the_job_stays_scheduled(
    make_env: Callable[..., _Env], value: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """List/show expose a remove-the-key ``config_error``; the job keeps its trigger; bytes unchanged."""
    caplog.set_level(logging.DEBUG)
    text = _fallback_toml(value)
    env = make_env(text, enabled=True)
    env.sched.start()

    job = env.sched.show_job("legacy")
    assert job is not None and "fallback_models" in job["config_error"] and "remove" in job["config_error"].lower()
    assert env.sched.config_blocks_schedule("legacy") is False
    assert _is_scheduled(env.sched, "legacy"), "a fallback_models job is not an unscheduled job"
    listed = {entry["tag"]: entry for entry in env.sched.list_jobs()}
    assert listed["legacy"]["config_error"] == job["config_error"]
    assert listed["healthy"]["config_error"] in (None, "")
    assert env.toml_path.read_bytes() == text.encode()
    assert CANARY not in job["config_error"] and CANARY not in caplog.text


@pytest.mark.parametrize("value", _FALLBACK_VALUES)
def test_fallback_models_job_fires_as_one_recorded_launch_failure_without_any_model_work(
    make_env: Callable[..., _Env], value: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """Firing records ONE failed launch (run id, last_run), sends ONE direct alert although notify=false,
    releases tracking once, spawns nothing and never touches a model; a healthy sibling still runs."""
    caplog.set_level(logging.DEBUG)
    text = _fallback_toml(value)
    env = make_env(text)
    sched = env.sched

    sched._run_job("legacy")

    records = [r for r in sched.execution_log.read_recent() if r.get("tag") == "legacy"]
    assert len(records) == 1
    (record,) = records
    assert (record["status"], record["success"]) == ("failed", False)
    assert re_run_id(record["run_id"]) and "fallback_models" in record["result"] and "remove" in record["result"].lower()
    assert CANARY not in json.dumps(record) and CANARY not in caplog.text
    alerts = [call.args[0] for call in env.notify.call_args_list]
    assert len(alerts) == 1 and "legacy" in alerts[0] and "fallback_models" in alerts[0]
    assert CANARY not in alerts[0]
    assert env.spawns == [] and sched.agent.call_count == 0  # no spawn, no model attempt
    assert "legacy" not in sched._running_jobs and not sched._active_runs.get("legacy")
    assert sched.show_job("legacy")["last_run"], "last_run must be persisted for an admitted launch failure"
    assert env.toml_path.read_bytes() == text.encode()

    sched._run_job("healthy")  # a sibling fires normally (its own spawn)
    assert len(env.spawns) == 1


def re_run_id(value: object) -> bool:
    """True for a minted ``run-<32 hex>`` execution id."""
    import re

    return isinstance(value, str) and re.fullmatch(r"run-[0-9a-f]{32}", value) is not None


# ---------------------------------------------------------------------------
# E. add/modify refuse every obsolete input before the attempt hook
# ---------------------------------------------------------------------------

_BASE_ADD = {"action": "add", "tag": "newjob", "task": "do work", "cron": "0 5 * * *"}
_OBSOLETE_ADD_ARGS = [
    pytest.param({"schedule_type": "daily", "time": "03:30"}, ["daily"], id="schedule_type-daily"),
    pytest.param({"schedule_type": "interval", "hours": 6}, ["interval"], id="schedule_type-interval"),
    pytest.param({"hours": 6}, ["hours"], id="hours"),
    pytest.param({"hours": "six"}, ["hours"], id="hours-non-numeric"),
    pytest.param({"minutes": 15}, ["minutes"], id="minutes"),
    pytest.param({"minutes": None}, ["minutes"], id="minutes-null"),
    pytest.param({"time": "03:30"}, ["time"], id="time"),
    pytest.param({"fallback_models": []}, ["fallback_models"], id="fallback-empty"),
    pytest.param({"fallback_models": False}, ["fallback_models"], id="fallback-false"),
    pytest.param({"fallback_models": [CANARY]}, ["fallback_models"], id="fallback-list"),
    pytest.param({"schedule": "interval", "minutes": 5}, ["interval"], id="schedule-alias-interval"),
]
_OBSOLETE_MODIFY_ARGS = [
    pytest.param({"hours": 6}, ["hours"], id="hours"),
    pytest.param({"minutes": 15}, ["minutes"], id="minutes"),
    pytest.param({"time": "03:30"}, ["time"], id="time"),
    pytest.param({"schedule_type": "interval"}, ["interval"], id="schedule_type-interval"),
    pytest.param({"schedule_type": "daily"}, ["daily"], id="schedule_type-daily"),
    pytest.param({"fallback_models": []}, ["fallback_models"], id="fallback-empty"),
    pytest.param({"fallback_models": [CANARY]}, ["fallback_models"], id="fallback-list"),
    pytest.param({"hours": None}, ["hours"], id="hours-null"),
]


def _assert_refused_before_attempt(result: dict, fields: list[str], env: _Env, before: tuple) -> None:
    """A refused tool result with field-naming cron/once or remove-key guidance and no change at all."""
    assert result["success"] is False and read_execution(result) == "refused", result
    error = result["error"]
    for field in fields:
        assert field in error, (field, error)
    assert ("cron" in error and "once" in error) or "remove" in error.lower(), error
    assert CANARY not in error
    assert env.snapshot() == before


@pytest.mark.parametrize(("extra", "fields"), _OBSOLETE_ADD_ARGS)
def test_tool_add_refuses_obsolete_input_before_the_attempt_hook(
    make_env: Callable[..., _Env], extra: dict, fields: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``schedule add`` refuses before coercion/hook; no file, state, history or generation change."""
    env = make_env(_HEALTHY)
    attempts: list[int] = []
    original = env.sched.add_job

    def spy(*args: object, **kwargs: object) -> dict:
        hook = kwargs.get("_on_attempt")
        if callable(hook):
            kwargs["_on_attempt"] = lambda: (attempts.append(1), hook())[1]
        return original(*args, **kwargs)

    monkeypatch.setattr(env.sched, "add_job", spy)
    before = env.snapshot()

    result = exec_schedule(env.sched, {**_BASE_ADD, **extra})

    _assert_refused_before_attempt(result, fields, env, before)
    assert attempts == [] and env.sched.show_job("newjob") is None


_API_ADD_KWARGS = [
    pytest.param({"schedule_type": "daily", "time_str": "03:30", "cron": None}, ["daily"], id="daily"),
    pytest.param({"schedule_type": "interval", "hours": 6, "cron": None}, ["interval"], id="interval"),
    pytest.param({"hours": 6}, ["hours"], id="hours-beside-cron"),
    pytest.param({"hours": None}, ["hours"], id="hours-explicit-none"),
    pytest.param({"minutes": 15}, ["minutes"], id="minutes-beside-cron"),
    pytest.param({"minutes": None}, ["minutes"], id="minutes-explicit-none"),
    pytest.param({"time_str": "03:30"}, ["time"], id="time-beside-cron"),
    pytest.param({"time_str": None}, ["time"], id="time-explicit-none"),
    pytest.param({"schedule_type": "once", "cron": None, "time_str": "03:30"}, ["time"], id="once-time-as-run_at"),
]


@pytest.mark.parametrize(("extra", "fields"), _API_ADD_KWARGS)
def test_scheduler_api_add_refuses_obsolete_input_before_the_attempt_hook(
    make_env: Callable[..., _Env], extra: dict, fields: list[str],
) -> None:
    """``Scheduler.add_job`` itself refuses (the tool is not the only guard) without calling ``_on_attempt``."""
    env = make_env(_HEALTHY)
    before = env.snapshot()
    attempts: list[int] = []
    kwargs: dict = {"tag": "newjob", "schedule_type": "cron", "task": "do work", "cron": "0 5 * * *"}
    kwargs.update(extra)

    result = env.sched.add_job(**kwargs, _on_attempt=lambda: attempts.append(1))

    assert result["success"] is False
    for field in fields:
        assert field in result["error"]
    assert attempts == [] and env.snapshot() == before and env.sched.show_job("newjob") is None


@pytest.mark.parametrize(
    ("schedule_type", "schedule_fields"),
    [
        pytest.param("cron", {"cron": "0 5 * * *"}, id="cron"),
        pytest.param("once", {"run_at": "23:45"}, id="once"),
    ],
)
def test_scheduler_api_add_omitting_obsolete_arguments_still_succeeds(
    make_env: Callable[..., _Env], schedule_type: str, schedule_fields: dict,
) -> None:
    """An omitted optional legacy argument is not an explicit null; valid jobs commit normally."""
    env = make_env(_HEALTHY)
    attempts: list[int] = []

    result = env.sched.add_job(
        "newjob", schedule_type, "do work", **schedule_fields,
        _on_attempt=lambda: attempts.append(1),
    )

    assert result["success"] is True and attempts == [1]
    job = env.sched.show_job("newjob")
    assert job is not None and job.get("config_error") is None
    assert "newjob" in tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize(("extra", "fields"), _OBSOLETE_MODIFY_ARGS)
def test_tool_modify_refuses_obsolete_input_before_the_attempt_hook(
    make_env: Callable[..., _Env], extra: dict, fields: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``schedule modify`` refuses obsolete keys by PRESENCE (never silently dropped), changing nothing."""
    env = make_env(_HEALTHY)
    attempts: list[int] = []
    original = env.sched.modify_job

    def spy(tag: str, updates: dict, **kwargs: object) -> dict:
        hook = kwargs.get("_on_attempt")
        if callable(hook):
            kwargs["_on_attempt"] = lambda: (attempts.append(1), hook())[1]
        return original(tag, updates, **kwargs)

    monkeypatch.setattr(env.sched, "modify_job", spy)
    before = env.snapshot()

    result = exec_schedule(env.sched, {"action": "modify", "tag": "healthy", "task": "changed", **extra})

    _assert_refused_before_attempt(result, fields, env, before)
    assert attempts == []
    assert env.sched.show_job("healthy")["task"] == "healthy job"


@pytest.mark.parametrize(("extra", "fields"), [p for p in _OBSOLETE_MODIFY_ARGS if "hours-null" not in str(p.id)])
def test_scheduler_api_modify_refuses_obsolete_input_before_the_attempt_hook(
    make_env: Callable[..., _Env], extra: dict, fields: list[str],
) -> None:
    """``Scheduler.modify_job`` refuses a present obsolete key even next to a valid patch."""
    env = make_env(_HEALTHY)
    before = env.snapshot()
    attempts: list[int] = []

    result = env.sched.modify_job("healthy", {"task": "changed", **copy.deepcopy(extra)}, _on_attempt=lambda: attempts.append(1))

    assert result["success"] is False
    for field in fields:
        assert field in result["error"]
    assert attempts == [] and env.snapshot() == before


def test_legitimate_add_and_modify_still_work_and_keep_strict_types(make_env: Callable[..., _Env]) -> None:
    """The refusals are narrow: cron/once adds and ordinary modifies succeed, raw values stay type-strict."""
    env = make_env(_HEALTHY)

    assert exec_schedule(env.sched, {**_BASE_ADD})["success"] is True
    assert exec_schedule(env.sched, {"action": "add", "tag": "later", "task": "x", "schedule_type": "once", "run_at": "23:45"})["success"] is True
    assert exec_schedule(env.sched, {"action": "modify", "tag": "newjob", "task": "renamed", "notify": False})["success"] is True

    document = tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]
    assert document["newjob"]["task"] == "renamed" and document["newjob"]["notify"] is False
    assert document["later"]["run_at"] == "23:45" and "time" not in document["later"]
    assert not any(key in document["newjob"] for key in ("hours", "minutes", "time", "fallback_models"))


def test_modify_of_a_valid_patch_keeps_an_existing_obsolete_key_raw(make_env: Callable[..., _Env]) -> None:
    """Characterization: there is no clearing semantics in the frozen contract, so a valid patch on a job
    that already carries ``fallback_models`` preserves the raw key and the error stays visible."""
    env = make_env(_fallback_toml("[]"))

    result = exec_schedule(env.sched, {"action": "modify", "tag": "legacy", "task": "patched"})

    assert result["success"] is True, result
    document = tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]["legacy"]
    assert document["fallback_models"] == [] and document["task"] == "patched"
    assert "fallback_models" in env.sched.show_job("legacy")["config_error"]


def test_obsolete_scheduling_job_survives_a_valid_sibling_mutation_byte_for_byte(make_env: Callable[..., _Env]) -> None:
    """Mutating a healthy sibling never converts or rewrites the obsolete job's raw values."""
    env = make_env(_job('schedule = "interval"\nhours = 6\n'))
    raw_before = tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]["legacy"]

    assert exec_schedule(env.sched, {"action": "modify", "tag": "healthy", "task": "sibling change"})["success"] is True

    document = tomllib.loads(env.toml_path.read_text(encoding="utf-8"))["jobs"]
    assert document["legacy"] == raw_before and "cron" not in document["legacy"]
    assert "hours" in env.sched.show_job("legacy")["config_error"]



# ---------------------------------------------------------------------------
# D. The real root: a fallback_models job fails its launch before ANY model work
# ---------------------------------------------------------------------------


def test_root_fallback_models_job_fails_launch_before_any_admission_or_client(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - imported fixture
) -> None:
    """Through the actual root chain a ``fallback_models`` job is ONE recorded launch failure with the direct
    alert: no admission, credential, runtime, client or provider request and no fallback to Main; a healthy
    sibling on the same Scheduler completes on Main, and Main/preference/TOML bytes are unchanged."""
    scheduled_root.serve_provider_chat()
    probes: list[ScheduledRootProbe] = []
    snapshot: dict[str, object] = {}
    toml = (
        scheduled_job_toml(None, tag="badjob") + 'fallback_models = ["acme/claude-b"]\n'
        + "\n" + scheduled_job_toml(None, tag="goodjob")
    )

    def hold(started: RootHarness) -> None:
        """Fire the obsolete job, then the healthy sibling."""
        started.registry.refresh()
        for tag in ("badjob", "goodjob"):
            probe = ScheduledRootProbe(started, monkeypatch, tag=tag)
            probes.append(probe)
            probe.fire()
            if tag == "badjob":  # the sibling's probe wraps the same registry spies: snapshot first
                snapshot.update(
                    admits={k: list(v) for k, v in probe.admits.items()}, resolved=list(probe.resolved),
                    pins=list(probe.pins), creates=list(probe.creates), registered=list(probe.registered),
                )

    scheduled_root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)

    bad, good = probes
    assert snapshot["admits"] == {"admit": [], "admit_scheduled": []}
    assert snapshot["resolved"] == [] and snapshot["pins"] == [] and snapshot["creates"] == [] and snapshot["registered"] == []
    (record,) = bad.after.records
    assert (record["status"], record["success"]) == ("failed", False)
    assert "fallback_models" in record["result"] and "remove" in record["result"].lower()
    (alert,) = bad.after.notices  # notify = false: the launch-failure alert still goes out
    assert "badjob" in alert and "fallback_models" in alert
    assert bad.after.llm_clients == 0 and bad.after.provider_requests == [] and bad.after.tracked is False
    (good_record,) = good.after.records
    assert (good_record["status"], good_record["success"], good_record["model"]) == ("done", True, MAIN_ID)
    assert good.after.notices == [] and good.after.view == good.view_before
    assert good.view_before.active_model_id == MAIN_ID and good.after.toml == bad.toml_before
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []


# ---------------------------------------------------------------------------
# G. The operator-facing tool text tells a blocking error from a launch-time one
# ---------------------------------------------------------------------------


def test_tool_list_and_show_label_blocking_and_launch_time_config_errors(make_env: Callable[..., _Env]) -> None:
    """``schedule list/show``: an obsolete scheduling field says "not scheduled", fallback_models says launches fail."""
    env = make_env(_job('schedule = "interval"\nhours = 6\n') + '\n[jobs.fb]\nenabled = true\nschedule = "cron"\ncron = "0 4 * * *"\ntask = "t"\nfallback_models = []\n')

    listing = exec_schedule(env.sched, {"action": "list"})["output"]
    shown_blocked = exec_schedule(env.sched, {"action": "show", "tag": "legacy"})["output"]
    shown_launch = exec_schedule(env.sched, {"action": "show", "tag": "fb"})["output"]

    assert "config_error (not scheduled)" in listing and "config_error (launches fail until fixed)" in listing
    assert "not scheduled; fix the job, then reload" in shown_blocked
    assert "every launch fails until fixed" in shown_launch


def test_telegram_jobs_list_shows_obsolete_job_guidance_without_exposing_values(
    make_env: Callable[..., _Env],
) -> None:
    """The operator-facing /jobs listing shows both error types, not raw fallback values."""
    env = make_env(
        _job('schedule = "interval"\nhours = 6\n')
        + f'\n[jobs.fb]\nschedule = "cron"\ncron = "0 4 * * *"\n'
        f'task = "t"\nfallback_models = ["{CANARY}"]\n'
    )

    listing = format_jobs_list(env.sched.list_jobs())

    assert "interval" in listing and "cron" in listing and "once" in listing
    assert "fallback_models" in listing and "Remove" in listing
    assert CANARY not in listing
