"""Trusted scheduled spawns forward the raw job model selector (task 12.10).

Scheduler-built ``SupervisionOptions(source=SOURCE_SCHEDULED)`` exempt ONLY the
strict model-shape guard: any non-None raw value reaches the factory's existing
``model`` keyword unchanged (classification is the root factory's job). Every
other refusal — missing task, obsolete generation keys — stays on, and the
default (model-facing) source keeps the strict shape refusal.

Only boundaries are substituted: the sub-agent factory (an accepting recording
double), the registry capacity count, the supervisor's worker pool, the
``schedule`` library's global scheduler, notify/agent callbacks, and the file
system (``tmp_path``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
import schedule

from outcome_utils import read_execution
from scheduler import Scheduler
from sub_agent_registry import SOURCE_SCHEDULED
from sub_agent_supervisor import SupervisionOptions
from xdg import xdg_paths

_SHAPE_REFUSAL = "spawn_agent: 'model' must be a non-empty string or omitted."
_BAD_SHAPES = [123, 1.5, "  ", ["p/model"], {"id": "p/model"}, 0, False]
_BAD_IDS = ["int", "float", "whitespace", "list", "dict", "zero", "false"]
_JOB_TAG = "raw_model_job"


def _job_toml(model_literal: str) -> str:
    """Return one far-future cron job whose ``model`` is the given TOML literal."""
    return (
        f"[jobs.{_JOB_TAG}]\nenabled = true\nschedule = \"cron\"\ncron = \"0 4 1 1 *\"\n"
        f"task = \"write report\"\nnotify = false\nmodel = {model_literal}\n"
    )


class _AcceptingFactory:
    """Sub-agent factory double that accepts any ``model`` value and records kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        runner = MagicMock()
        runner.agent_id = f"sa-trusted{len(self.calls)}"
        runner._model_id = "test-model"
        runner._cancel_event = MagicMock()
        runner._llm = MagicMock()
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        return runner


def _scheduled_options() -> SupervisionOptions:
    """Build options the way the scheduler fire path does (internal channel only)."""
    return SupervisionOptions(
        job_tag="nightly_report",
        finish_cb=lambda _tag: None,
        result_log_cb=lambda **_kwargs: None,
        completion_cb=lambda _completion: None,
        notify=False,
        source=SOURCE_SCHEDULED,
    )


def _registry() -> MagicMock:
    registry = MagicMock()
    registry.count_managed.return_value = 0
    return registry


def _spawn(make_builtin_executor, args: dict, options: SupervisionOptions | None):
    """Call the public ``spawn_agent`` entry; return (result, factory, submit count)."""
    factory = _AcceptingFactory()
    executor = make_builtin_executor(sub_agent_factory=factory, max_subagents=6)
    with patch("sub_agent_registry.get_registry", return_value=_registry()), \
         patch.object(executor._supervisor._pool, "submit",
                      side_effect=lambda fn, *a, **kw: MagicMock()), \
         patch.object(executor._supervisor, "submit",
                      wraps=executor._supervisor.submit) as submit:
        result = executor.spawn_agent(args, options=options)
    return result, factory, submit.call_count


# ---------------------------------------------------------------------------
# Trusted scheduled forwarding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", _BAD_SHAPES, ids=_BAD_IDS)
def test_scheduled_spawn_forwards_raw_selector_unchanged(make_builtin_executor, raw) -> None:
    result, factory, submits = _spawn(
        make_builtin_executor, {"task": "write report", "model": raw}, _scheduled_options(),
    )

    assert result.get("error") != _SHAPE_REFUSAL
    assert result["success"] is True
    assert submits == 1
    assert len(factory.calls) == 1
    assert factory.calls[0]["model"] is raw


def test_scheduled_spawn_forwards_padded_string_unchanged(make_builtin_executor) -> None:
    raw = " p/model "
    result, factory, _ = _spawn(
        make_builtin_executor, {"task": "write report", "model": raw}, _scheduled_options(),
    )

    assert result["success"] is True
    assert len(factory.calls) == 1
    assert factory.calls[0]["model"] == " p/model "


# ---------------------------------------------------------------------------
# Counter-case: the model-facing default source stays strict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", _BAD_SHAPES, ids=_BAD_IDS)
def test_default_spawn_refuses_bad_selector_shape(make_builtin_executor, raw) -> None:
    result, factory, submits = _spawn(
        make_builtin_executor, {"task": "write report", "model": raw}, None,
    )

    assert result["success"] is False
    assert result["error"] == _SHAPE_REFUSAL
    assert result["error_type"] == "validation_error"
    assert read_execution(result) == "refused"
    assert factory.calls == []
    assert submits == 0


# ---------------------------------------------------------------------------
# No bypass: every other refusal stays on for scheduled options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["max_tokens", "temperature", "top_p"])
def test_obsolete_key_refusal_is_identical_for_scheduled_source(
    make_builtin_executor, key: str,
) -> None:
    args = {"task": "write report", "model": 123, key: None}
    default_result, default_factory, _ = _spawn(make_builtin_executor, dict(args), None)
    scheduled_result, scheduled_factory, submits = _spawn(
        make_builtin_executor, dict(args), _scheduled_options(),
    )

    assert scheduled_result["success"] is False
    assert scheduled_result["error_type"] == "validation_error"
    assert key in scheduled_result["error"]
    assert scheduled_result == default_result
    assert scheduled_factory.calls == default_factory.calls == []
    assert submits == 0


def test_scheduled_spawn_without_task_is_refused(make_builtin_executor) -> None:
    result, factory, submits = _spawn(make_builtin_executor, {"model": 123}, _scheduled_options())

    assert result["success"] is False
    assert result["error"] == "spawn_agent: 'task' is required."
    assert read_execution(result) == "refused"
    assert factory.calls == []
    assert submits == 0


# ---------------------------------------------------------------------------
# Scheduler passthrough: a real fire carries the raw job model to the factory
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler_env(
    request: pytest.FixtureRequest,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
) -> Iterator[tuple[Scheduler, _AcceptingFactory]]:
    """A real disabled Scheduler over one job and a real executor.

    The job's ``model`` TOML literal comes from indirect parametrization.
    """
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("trusted-scheduled-spawn-test")
    assert str(paths.scheduler_state).startswith(str(tmp_path))
    paths.config_home.mkdir(parents=True, exist_ok=True)
    paths.state_home.mkdir(parents=True, exist_ok=True)
    paths.scheduler_config.write_text(_job_toml(request.param), encoding="utf-8")
    sched = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=paths,
        scheduler_config_path=str(paths.scheduler_config),
    )
    factory = _AcceptingFactory()
    sched.builtin_executor = make_builtin_executor(sub_agent_factory=factory, max_subagents=6)
    yield sched, factory
    if sched._thread is not None:
        sched.stop()


def _fire(sched: Scheduler) -> None:
    """Fire the job once with capacity open and the worker pool held."""
    executor = sched.builtin_executor
    with patch("sub_agent_registry.get_registry", return_value=_registry()), \
         patch.object(executor._supervisor._pool, "submit",
                      side_effect=lambda fn, *a, **kw: MagicMock()):
        sched._run_job(_JOB_TAG)


@pytest.mark.parametrize(
    ("scheduler_env", "expected", "expected_type"),
    [("123", 123, int), ("0", 0, int), ("false", False, bool), ("0.0", 0.0, float)],
    ids=["int", "zero", "false", "zero-float"],
    indirect=["scheduler_env"],
)
def test_scheduler_fire_forwards_raw_job_model_to_factory(
    scheduler_env, expected, expected_type,
) -> None:
    sched, factory = scheduler_env

    _fire(sched)

    assert len(factory.calls) == 1
    assert factory.calls[0]["model"] == expected
    assert type(factory.calls[0]["model"]) is expected_type
    assert sched.notify.call_args_list == []


@pytest.mark.parametrize("scheduler_env", ['""'], ids=["empty"], indirect=True)
def test_scheduler_fire_empty_model_inherits(scheduler_env) -> None:
    """A raw ``""`` selector reaches the factory unchanged; admission classifies it.

    Task 7.3 supersedes the former ``model is None`` assertion: the scheduler
    must not normalize ``""`` into silent inheritance (scheduled spec,
    "Blank or whitespace selector warns and inherits"). The retained inheritance
    intent — one bounded WARN, then the valid Main is pinned — is proven through
    the actual root in ``tests/test_scheduled_model_admission.py``
    (``test_root_scheduled_selector_matrix[blank]``).
    """
    sched, factory = scheduler_env

    _fire(sched)

    assert len(factory.calls) == 1
    assert factory.calls[0]["model"] == ""
    assert type(factory.calls[0]["model"]) is str
    assert sched.notify.call_args_list == []
