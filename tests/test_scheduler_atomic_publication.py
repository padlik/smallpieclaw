"""
Atomic mutation publication (task 12.4, review remediation).

Each case targets one publication gap of the atomic mutation commit:

* a type-only change (``true`` → ``1``, ``1`` → ``1.0``) is published to the
  live job view, the stored document, and the file alike;
* a modify whose canonical schedule triple was computed before a later
  committed modify must not restore the older ``run_at``;
* an add held in trigger preparation while ``start()`` completes, and a cron
  advance computed by the real loop before a committed cron change, must not
  leave the new job unregistered or overwrite the newer cron-derived fields;
* a fault in the external registration container (an unrelated
  ``schedule.Job``'s ``tags``) while the replacement registrations are built
  must fail before the scheduler file is replaced.

Only external boundaries are gated or faulted: ``schedule.Job.do``,
``croniter.get_next``, an unrelated job's ``tags`` container, and an
observational ``os.replace``. The scheduler, its validators, and its commit
are real. Every test runs on an isolated ``schedule.default_scheduler`` (and
``schedule.jobs``), so a real loop never runs another module's jobs. Paths
come from the real ``xdg_paths`` with every XDG directory under ``tmp_path``.
Every wait and join is bounded and every gate is released in ``finally``.
"""

from __future__ import annotations

import copy
import os
import sys
import threading
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Callable, Iterator, Union
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from scheduler import Scheduler
from xdg import xdg_paths


_BOUND = 10.0
_SECRET = "fake-secret-sentinel-5c1e"

# Far-future cron (1 January) so nothing fires during a test.
_CRON_TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha"
notify = {notify}
"""

_ONCE_TOML = """\
[jobs.reminder]
enabled = true
schedule = "once"
run_at = "03:00"
task = "once reminder"
notify = false
"""

# An empty task: the due loop firing only records last_run (no executor, no notify).
_DUE_TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = ""
notify = false
"""

Result = Union[dict, bool]
MakeScheduler = Callable[..., tuple[Scheduler, Path]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> dict:
    """Parse the scheduler file with the scheduler's own parser."""
    return scheduler_module.tomli.loads(path.read_text(encoding="utf-8"))


def _failed(result: Result) -> bool:
    """Return True when a public mutation result reports failure."""
    if isinstance(result, bool):
        return result is False
    return isinstance(result, dict) and result.get("success") is False


def _typed(value: object) -> tuple:
    """Return ``(type, value)`` so equal values of different types compare unequal."""
    return type(value), value


class _Hold:
    """Hold the first call that reaches it until released, with visible expiry."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.timed_out = False
        self._guard = threading.Lock()
        self._used = False

    def hold(self, *_args: object, **_kwargs: object) -> None:
        """Hold the first caller (bounded); later callers pass straight through."""
        with self._guard:
            first = not self._used
            self._used = True
        if not first:
            return
        self.reached.set()
        try:
            self.timed_out = not self.release.wait(timeout=_BOUND)
        finally:
            self.exited.set()


def _hold_job_do(monkeypatch: pytest.MonkeyPatch, gate: _Hold, thread_name: str) -> None:
    """Hold the external ``schedule.Job.do`` in *thread_name* only; delegate always."""
    real_do = schedule.Job.do

    def do(self: schedule.Job, job_func: Callable, *args: object, **kwargs: object) -> object:
        if threading.current_thread().name == thread_name:
            gate.hold()
        return real_do(self, job_func, *args, **kwargs)

    monkeypatch.setattr(schedule.Job, "do", do)


def _hold_cron_next(monkeypatch: pytest.MonkeyPatch, gate: _Hold, thread_name: str) -> None:
    """Hold the external ``croniter.get_next`` in *thread_name* only; delegate always."""
    real_get_next = scheduler_module.croniter.get_next

    def get_next(self: object, *args: object, **kwargs: object) -> object:
        if threading.current_thread().name == thread_name:
            gate.hold()
        return real_get_next(self, *args, **kwargs)

    monkeypatch.setattr(scheduler_module.croniter, "get_next", get_next)


def _start(name: str, call: Callable[[], object]) -> tuple[threading.Thread, dict]:
    """Run *call* in a named daemon thread; its result or exception lands in the dict."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except Exception as exc:  # noqa: BLE001 - captured, then asserted absent
            outcome["error"] = exc

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, outcome


def _finish(gate: _Hold, *threads: threading.Thread | None) -> list[threading.Thread]:
    """Release *gate*, join every thread (bounded), and return those still alive."""
    gate.release.set()
    started = [thread for thread in threads if thread is not None]
    for thread in started:
        thread.join(timeout=_BOUND)
    return [thread for thread in started if thread.is_alive()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
) -> Iterator[MakeScheduler]:
    """Construct real Schedulers over temporary XDG paths; stop any started loop."""
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("atomic-publication-test")
    assert str(paths.model_selection_file).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(text: str, *, enabled: bool = False) -> tuple[Scheduler, Path]:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.scheduler_config.write_text(text, encoding="utf-8")
        sched = Scheduler(
            config={"scheduler": {"enabled": enabled}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(paths.scheduler_config),
        )
        created.append(sched)
        return sched, paths.scheduler_config

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()
            assert not sched._thread.is_alive(), "the scheduler loop did not stop"


# ---------------------------------------------------------------------------
# Finding 1: a type-only change is a change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "new_value"),
    [
        pytest.param("true", 1, id="bool_true_to_int_1"),
        pytest.param("1", 1.0, id="int_1_to_float_1_0"),
    ],
)
def test_type_only_notify_change_is_published_everywhere(
    make_scheduler: MakeScheduler, raw: str, new_value: object,
) -> None:
    """The live view, the stored document, and the file all hold the exact new type."""
    sched, toml_path = make_scheduler(_CRON_TOML.format(notify=raw))
    live = sched._jobs_meta["alpha"]
    assert live["notify"] == new_value and _typed(live["notify"]) != _typed(new_value)

    result = sched.modify_job("alpha", {"notify": new_value}, _on_attempt=MagicMock())

    assert result == {"success": True}
    assert sched._jobs_meta["alpha"] is live
    observed = {
        "meta": live["notify"],
        "show": sched.show_job("alpha")["notify"],
        "document": sched._load_result.document["jobs"]["alpha"]["notify"],
        "disk": _parse(toml_path)["jobs"]["alpha"]["notify"],
    }
    assert {k: _typed(v) for k, v in observed.items()} == {
        k: _typed(new_value) for k in observed
    }


# ---------------------------------------------------------------------------
# Finding 2: a canonical schedule triple computed before a later commit
# ---------------------------------------------------------------------------


def test_held_schedule_type_modify_keeps_later_run_at(make_scheduler: MakeScheduler) -> None:
    """A is held in its attempt hook; B commits run_at 04:00; A must not restore 03:00."""
    sched, toml_path = make_scheduler(_ONCE_TOML)
    gate = _Hold()
    hook_a = MagicMock(side_effect=gate.hold)
    hook_b = MagicMock()
    first = second = None
    try:
        first, outcome_a = _start(
            "atomic-publication-modify-a",
            lambda: sched.modify_job("reminder", {"schedule_type": "once"}, _on_attempt=hook_a),
        )
        assert gate.reached.wait(_BOUND), "modify A never reached its attempt hook"
        second, outcome_b = _start(
            "atomic-publication-modify-b",
            lambda: sched.modify_job("reminder", {"run_at": "04:00"}, _on_attempt=hook_b),
        )
        second.join(timeout=_BOUND)
        assert not second.is_alive(), "modify B waited on the held modify A"
        assert not gate.exited.is_set(), "modify A was no longer held when B committed"
    finally:
        alive = _finish(gate, first, second)

    assert alive == []
    assert not gate.timed_out
    assert outcome_a == {"result": {"success": True}}
    assert outcome_b == {"result": {"success": True}}
    assert (hook_a.call_count, hook_b.call_count) == (1, 1)
    assert sched._jobs_meta["reminder"]["run_at"] == "04:00"
    assert sched.show_job("reminder")["run_at"] == "04:00"
    assert sched._load_result.document["jobs"]["reminder"]["run_at"] == "04:00"
    assert _parse(toml_path)["jobs"]["reminder"]["run_at"] == "04:00"
    published = schedule.get_jobs("reminder")
    assert [job.at_time for job in published] == [dt_time(4, 0)]


# ---------------------------------------------------------------------------
# Finding 4: lifecycle races with start() and the real loop's cron advance
# ---------------------------------------------------------------------------

_ADD_WORKER = "atomic-publication-add-worker"


def _add_once(sched: Scheduler) -> Result:
    return sched.add_job("fresh", "once", "fresh reminder", run_at="03:00")


def _add_cron(sched: Scheduler) -> Result:
    return sched.add_job("fresh", "cron", "fresh task", cron="0 3 1 1 *")


def _once_registered(sched: Scheduler) -> bool:
    jobs = schedule.get_jobs("fresh")
    return len(jobs) == 1 and jobs[0].job_func.func == sched._run_job


def _cron_registered(sched: Scheduler) -> bool:
    return sched.show_job("fresh")["next_run"] is not None


@pytest.mark.parametrize(
    ("install_hold", "add", "registered"),
    [
        pytest.param(_hold_job_do, _add_once, _once_registered, id="once"),
        pytest.param(_hold_cron_next, _add_cron, _cron_registered, id="cron"),
    ],
)
def test_add_held_across_start_is_registered(
    make_scheduler: MakeScheduler,
    monkeypatch: pytest.MonkeyPatch,
    install_hold: Callable[[pytest.MonkeyPatch, _Hold, str], None],
    add: Callable[[Scheduler], Result],
    registered: Callable[[Scheduler], bool],
) -> None:
    """An add prepared while the loop was stopped, committed after start(), is active."""
    sched, _ = make_scheduler(_CRON_TOML.format(notify="true"), enabled=True)
    gate = _Hold()
    install_hold(monkeypatch, gate, _ADD_WORKER)
    worker = None
    try:
        worker, outcome = _start(_ADD_WORKER, lambda: add(sched))
        assert gate.reached.wait(_BOUND), "the add never reached its trigger preparation"
        assert not sched._loop_running()
        sched.start()
        assert sched._loop_running() and not gate.exited.is_set()
    finally:
        alive = _finish(gate, worker)

    assert alive == []
    assert not gate.timed_out
    assert outcome == {"result": {"success": True}}
    assert "fresh" in sched._jobs_meta
    assert registered(sched), "the enabled job added across start() is not registered"


_CRON_FIELDS = ("_next_run", "_natural_next_run")


def test_loop_cron_advance_does_not_overwrite_committed_cron(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop's held old-cron advance must not replace the committed new-cron fields.

    alpha is made due before the real ``_run_loop`` starts; the loop's
    ``croniter.get_next`` (loop thread only) is held while a cron modify commits.
    """
    sched, _ = make_scheduler(_DUE_TOML, enabled=True)
    live = sched._jobs_meta["alpha"]
    due = datetime(2000, 1, 1).isoformat()
    live["_next_run"] = live["_natural_next_run"] = due
    gate = _Hold()
    _hold_cron_next(monkeypatch, gate, "scheduler")
    before = set(threading.enumerate())
    loop = threading.Thread(target=sched._run_loop, name="scheduler", daemon=True)
    sched._thread = loop
    mutation = None
    try:
        loop.start()
        assert gate.reached.wait(_BOUND), "the loop never advanced the due cron job"
        mutation, outcome = _start(
            "atomic-publication-cron-modify",
            lambda: sched.modify_job("alpha", {"cron": "0 6 1 1 *"}, _on_attempt=MagicMock()),
        )
        mutation.join(timeout=_BOUND)
        assert not mutation.is_alive(), "the modify waited on the held loop"
        assert not gate.exited.is_set()
        committed = {field: live.get(field) for field in _CRON_FIELDS}
    finally:
        alive = _finish(gate, mutation)
        sched.stop()
        runs = [
            thread for thread in threading.enumerate()
            if thread not in before and thread.name.endswith("(_run_job)")
        ]
        for thread in runs:
            thread.join(timeout=_BOUND)

    assert alive == [] and not loop.is_alive()
    assert [thread for thread in runs if thread.is_alive()] == []
    assert not gate.timed_out
    assert outcome == {"result": {"success": True}}
    assert sched._jobs_meta["alpha"] is live and live["cron"] == "0 6 1 1 *"
    assert datetime.fromisoformat(committed["_natural_next_run"]).hour == 6
    assert {field: live.get(field) for field in _CRON_FIELDS} == committed


# ---------------------------------------------------------------------------
# Finding 5: registration replacement fault before the file is replaced
# ---------------------------------------------------------------------------


class _FaultingTags(set):
    """An external ``Job.tags`` set whose membership test raises while armed."""

    def __init__(self, items: set, reached: list) -> None:
        super().__init__(items)
        self.armed = False
        self.reached = reached

    def __contains__(self, item: object) -> bool:
        if self.armed:
            self.reached.append(item)
            raise RuntimeError(_SECRET)
        return super().__contains__(item)


def _observe_replace(monkeypatch: pytest.MonkeyPatch, toml_path: Path) -> list:
    """Record every real ``os.replace`` onto the scheduler file; always delegate."""
    replaced: list = []
    real_replace = os.replace
    target = os.path.realpath(toml_path)

    def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if os.path.realpath(os.fspath(dst)) == target:
            replaced.append(dst)
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return replaced


def _publication_state(sched: Scheduler, toml_path: Path) -> dict:
    """Capture the view, document, hash, and file without touching any job's tags."""
    return {
        "meta": copy.deepcopy(sched._jobs_meta),
        "document": copy.deepcopy(sched._load_result.document),
        "hash": sched._load_result.content_hash,
        "generation": sched._config_generation,
        "file": toml_path.read_bytes(),
    }


def _noop() -> None:
    """Callback of the unrelated registration; never run."""


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda s: s.modify_job("alpha", {"cron": "0 6 1 1 *"}, _on_attempt=MagicMock()),
            id="modify_cron",
        ),
        pytest.param(lambda s: s.pause_job("alpha"), id="pause"),
    ],
)
def test_registration_container_fault_never_replaces_file(
    make_scheduler: MakeScheduler,
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[Scheduler], Result],
) -> None:
    """A tags-membership fault while building replacements fails before ``os.replace``."""
    sched, toml_path = make_scheduler(_CRON_TOML.format(notify="true"))
    sched._register_job("alpha", sched._jobs_meta["alpha"])
    unrelated = schedule.every(1).hours.do(_noop)
    reached: list = []
    tags = _FaultingTags({"unrelated"}, reached)
    unrelated.tags = tags
    before = _publication_state(sched, toml_path)
    jobs_before = list(schedule.default_scheduler.jobs)
    replaced = _observe_replace(monkeypatch, toml_path)
    escaped = None
    result: Result = {}
    tags.armed = True
    try:
        result = operation(sched)
    except Exception as exc:  # noqa: BLE001 - asserted absent below
        escaped = exc
    finally:
        tags.armed = False

    assert escaped is None, f"{type(escaped).__name__} escaped the mutation"
    assert reached, "the registration container fault was never reached"
    assert _failed(result), result
    if isinstance(result, dict):
        assert _SECRET not in result["error"]
    assert replaced == [], "the scheduler file was replaced before the failure"
    assert _publication_state(sched, toml_path) == before
    current = list(schedule.default_scheduler.jobs)
    assert len(current) == len(jobs_before)
    assert all(a is b for a, b in zip(current, jobs_before))


def test_once_add_held_across_expired_stop_is_not_published(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An add prepared before stop() must not publish its trigger after stop() returned.

    The real ``_run_loop`` is held in its own ``croniter.get_next`` (loop
    thread only), so the real ``stop()`` waits out its five-second join and
    returns with the loop still alive. The once add, held in ``schedule.Job.do``
    (add worker only) since before stop(), is then released: the add is saved,
    but no trigger may be published for a scheduler that was stopped.
    """
    sched, toml_path = make_scheduler(_DUE_TOML, enabled=True)
    live = sched._jobs_meta["alpha"]
    live["_next_run"] = live["_natural_next_run"] = datetime(2000, 1, 1).isoformat()
    loop_gate = _Hold()
    add_gate = _Hold()
    _hold_cron_next(monkeypatch, loop_gate, "scheduler")
    _hold_job_do(monkeypatch, add_gate, _ADD_WORKER)
    before = set(threading.enumerate())
    loop = threading.Thread(target=sched._run_loop, name="scheduler", daemon=True)
    sched._thread = loop
    worker = None
    try:
        loop.start()
        assert loop_gate.reached.wait(_BOUND), "the loop never advanced the due cron job"
        worker, outcome = _start(_ADD_WORKER, lambda: _add_once(sched))
        assert add_gate.reached.wait(_BOUND), "the add never reached its trigger preparation"

        sched.stop()

        assert loop.is_alive() and not loop_gate.exited.is_set(), "stop() did not outlive the loop"
        assert not add_gate.exited.is_set()
        add_gate.release.set()
        worker.join(timeout=_BOUND)
        assert not worker.is_alive(), "the add did not finish after stop() returned"
        assert outcome == {"result": {"success": True}}
        assert "fresh" in sched._jobs_meta
        assert "fresh" in _parse(toml_path)["jobs"]
        published = schedule.get_jobs("fresh")
        assert published == [], "a trigger was published after stop() returned"
    finally:
        add_gate.release.set()
        alive = _finish(loop_gate, worker, loop)
        runs = [
            thread for thread in threading.enumerate()
            if thread not in before and thread.name.endswith("(_run_job)")
        ]
        for thread in runs:
            thread.join(timeout=_BOUND)

    assert alive == []
    assert [thread for thread in runs if thread.is_alive()] == []
    assert not loop_gate.timed_out and not add_gate.timed_out
