"""
Post-commit publication failure (task 12.6): a replaced file is irreversible.

Once ``os.replace`` has installed the new scheduler.toml, the change is on
disk. An ordinary exception raised while the committed candidate is being
published (here: the external ``schedule`` library's job list refusing the
slice assignment) must not roll the file back, retry, or claim nothing was
changed. The scheduler sets the private ``_reload_required`` latch, quiesces
automatic registrations, keeps a coherent stored document/hash pair, and
reports a safe "reload required" failure naming the file. While latched every
public mutation and the ``schedule`` tool's tag actions refuse before any
attempt, automatic admission is skipped (already admitted runs continue), and
only a successful reload clears the latch.

Only external boundaries are faulted or observed: the default scheduler's job
list (a ``list`` subclass armed only after the observed ``os.replace`` of
scheduler.toml), ``os.replace`` itself (observed, never altered), and the
library's ``run_pending`` (signals a completed loop tick). The scheduler,
its commit, its reload, and the ``schedule`` tool are real. Every test runs
on an isolated ``schedule.default_scheduler`` (and ``schedule.jobs``) over
real ``xdg_paths`` under ``tmp_path``; every wait and join is bounded.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from builtin_tools.schedule import exec_schedule
from outcome_utils import read_execution, strip_execution
from scheduler import Scheduler
from xdg import xdg_paths


_BOUND = 10.0
_SECRET = "fake-publication-secret-5c1e"
_MISSING = object()

# Far-future crons (1 January) so nothing fires unless a test makes it due.
_TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha"
notify = false

[jobs.paused]
enabled = false
schedule = "cron"
cron = "0 3 1 1 *"
task = "job paused"
notify = false

[jobs.reminder]
enabled = true
schedule = "once"
run_at = "03:00"
task = "once reminder"
notify = false
"""

MakeScheduler = Callable[..., tuple[Scheduler, Path]]


# ---------------------------------------------------------------------------
# External boundaries
# ---------------------------------------------------------------------------


class _FakePublishFault(Exception):
    """Ordinary exception raised by the faulted external job list (test data only)."""


class _FaultList(list):
    """The default scheduler's job list; slice assignment raises only while armed."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.raised = 0

    def __setitem__(self, key: object, value: object) -> None:
        if self.armed:
            self.raised += 1
            raise _FakePublishFault(_SECRET)
        super().__setitem__(key, value)


class _IsolatedScheduler(schedule.Scheduler):
    """Isolated library scheduler with a faultable job list and a tick signal."""

    def __init__(self) -> None:
        super().__init__()
        self.jobs = _FaultList()
        self.ticked = threading.Event()

    def run_pending(self) -> None:
        super().run_pending()
        self.ticked.set()


class _ReplaceObserver:
    """Observe ``os.replace``; arm the fault right after scheduler.toml is replaced."""

    def __init__(self, target: Path, fault: _FaultList) -> None:
        self.target = os.path.abspath(target)
        self.fault = fault
        self.replaced = 0
        self.committed: bytes | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_replace = os.replace

        def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
            real_replace(src, dst, *args, **kwargs)
            if os.path.abspath(os.fspath(dst)) == self.target:
                self.replaced += 1
                self.committed = Path(self.target).read_bytes()
                self.fault.armed = True

        monkeypatch.setattr(scheduler_module.os, "replace", replace)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(data: bytes) -> dict:
    return scheduler_module.tomli.loads(data.decode("utf-8"))


def _call(call: Callable[[], object]) -> dict:
    """Run *call*; its result or exception lands in the dict."""
    try:
        return {"result": call()}
    except Exception as exc:  # noqa: BLE001 - captured, then asserted absent
        return {"error": exc}


def _result(outcome: dict) -> object:
    assert "error" not in outcome, f"the public call raised {type(outcome['error']).__name__}"
    return outcome["result"]


def _assert_reload_required(text: object, path: Path) -> None:
    """A safe reload-required failure: names the file, never claims nothing changed."""
    assert isinstance(text, str), f"no reload-required failure text: {text!r}"
    assert str(path) in text
    assert "reload" in text.lower()
    assert "was not changed" not in text
    assert "previous file" not in text
    assert _SECRET not in text


def _register_reminder(sched: Scheduler, isolated: _IsolatedScheduler) -> schedule.Job:
    """Publish the once job's trigger through the library API (append, never slice-set)."""
    return isolated.every().day.at("03:00").do(sched._run_job, tag="reminder").tag("reminder")


def _trip(
    sched: Scheduler, toml_path: Path, monkeypatch: pytest.MonkeyPatch,
    fault: _FaultList, call: Callable[[], object],
) -> tuple[dict, _ReplaceObserver]:
    """Run a real public mutation whose publication faults after the file was replaced."""
    observer = _ReplaceObserver(toml_path, fault)
    observer.install(monkeypatch)
    outcome = _call(call)
    assert observer.replaced == 1, "scheduler.toml was not replaced exactly once"
    assert fault.raised == 1, "the post-commit publication fault was not reached exactly once"
    return outcome, observer


def _start_loop(sched: Scheduler, name: str) -> threading.Thread:
    """Run the real loop under a unique thread name (no start() re-registration)."""
    loop = threading.Thread(target=sched._run_loop, name=name, daemon=True)
    sched._thread = loop
    loop.start()
    return loop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_schedule(monkeypatch: pytest.MonkeyPatch) -> Iterator[_IsolatedScheduler]:
    """Replace the library's global scheduler (and its ``jobs`` alias) with a faultable one."""
    isolated = _IsolatedScheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    try:
        yield isolated
    finally:
        isolated.jobs.armed = False


@pytest.fixture
def make_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_schedule: _IsolatedScheduler,
) -> Iterator[MakeScheduler]:
    """Construct real Schedulers (loop off unless a test runs it) over temporary XDG paths."""
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("publication-failure-test")
    assert str(paths.models_config_file).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(text: str = _TOML, *, enabled: bool = False) -> tuple[Scheduler, Path]:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.state_home.mkdir(parents=True, exist_ok=True)
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
    isolated_schedule.jobs.armed = False
    for sched in created:
        if sched._thread is not None:
            sched.stop()
            assert not sched._thread.is_alive(), "the scheduler loop did not stop"


# ---------------------------------------------------------------------------
# 1. Post-commit fault matrix (real public mutations that publish registrations)
# ---------------------------------------------------------------------------

# name -> (public call, committed-document predicate, returns a result dict)
_FAULTED: dict[str, tuple[Callable[[Scheduler, MagicMock], object], Callable[[dict], bool], bool]] = {
    "resume": (
        lambda s, _hook: s.resume_job("paused"),
        lambda doc: doc["jobs"]["paused"]["enabled"] is True,
        False,
    ),
    "modify_cron": (
        lambda s, hook: s.modify_job("alpha", {"cron": "0 6 1 1 *"}, _on_attempt=hook),
        lambda doc: doc["jobs"]["alpha"]["cron"] == "0 6 1 1 *",
        True,
    ),
    "remove": (
        lambda s, _hook: s.remove_job("alpha"),
        lambda doc: "alpha" not in doc["jobs"],
        False,
    ),
}


class TestPostCommitFault:
    """A publication fault after os.replace latches RELOAD_REQUIRED; nothing rolls back."""

    @pytest.mark.parametrize("mutation", sorted(_FAULTED))
    def test_reports_reload_required_and_latches(
        self,
        make_scheduler: MakeScheduler,
        monkeypatch: pytest.MonkeyPatch,
        isolated_schedule: _IsolatedScheduler,
        caplog: pytest.LogCaptureFixture,
        mutation: str,
    ) -> None:
        """Safe public failure naming the file, latch set, load notice says reload."""
        sched, toml_path = make_scheduler()
        call, _, returns_dict = _FAULTED[mutation]
        hook = MagicMock()

        outcome, _ = _trip(
            sched, toml_path, monkeypatch, isolated_schedule.jobs, lambda: call(sched, hook),
        )

        result = _result(outcome)
        if returns_dict:
            assert result["success"] is False
            _assert_reload_required(result["error"], toml_path)
            assert hook.call_count == 1
        else:
            assert result is False
        assert getattr(sched, "_reload_required", _MISSING) is True
        _assert_reload_required(sched.load_notice(), toml_path)
        assert _SECRET not in caplog.text

    @pytest.mark.parametrize("mutation", sorted(_FAULTED))
    def test_keeps_committed_file_and_quiesces_registrations(
        self,
        make_scheduler: MakeScheduler,
        monkeypatch: pytest.MonkeyPatch,
        isolated_schedule: _IsolatedScheduler,
        mutation: str,
    ) -> None:
        """Disk stays new, the stored doc/hash pair is coherent, the job list is cleared in place."""
        sched, toml_path = make_scheduler()
        call, committed_change, _ = _FAULTED[mutation]
        _register_reminder(sched, isolated_schedule)
        assert len(isolated_schedule.jobs) == 1
        old_bytes = toml_path.read_bytes()
        old_pair = (sched._load_result.content_hash, sched._load_result.document)

        _, observer = _trip(
            sched, toml_path, monkeypatch, isolated_schedule.jobs, lambda: call(sched, MagicMock()),
        )
        isolated_schedule.jobs.armed = False

        disk = toml_path.read_bytes()
        assert disk == observer.committed and disk != old_bytes, "the committed file was rolled back"
        assert committed_change(_parse(disk))
        pair = (sched._load_result.content_hash, sched._load_result.document)
        assert pair in ((_sha256(disk), _parse(disk)), old_pair), "stored hash/document are mixed"
        assert schedule.default_scheduler is isolated_schedule
        assert schedule.default_scheduler.jobs is isolated_schedule.jobs is schedule.jobs
        assert list(isolated_schedule.jobs) == [], "automatic registrations were not quiesced"


# ---------------------------------------------------------------------------
# 2. Latch refusal matrix
# ---------------------------------------------------------------------------

_LATCHED_CALLS: dict[str, Callable[[Scheduler, MagicMock], object]] = {
    "add": lambda s, hook: s.add_job("gamma", "cron", "t", cron="0 5 1 1 *", _on_attempt=hook),
    "modify": lambda s, hook: s.modify_job("alpha", {"task": "changed"}, _on_attempt=hook),
    "remove": lambda s, _hook: s.remove_job("alpha"),
    "pause": lambda s, _hook: s.pause_job("alpha"),
    "resume": lambda s, _hook: s.resume_job("paused"),
}


@pytest.mark.parametrize("action", sorted(_LATCHED_CALLS))
def test_latched_scheduler_refuses_every_public_mutation(
    make_scheduler: MakeScheduler, action: str,
) -> None:
    """While latched no mutation attempts: no hook, no write, a reload-required refusal."""
    sched, toml_path = make_scheduler()
    sched._reload_required = True
    before = toml_path.read_bytes()
    hook = MagicMock()

    result = _LATCHED_CALLS[action](sched, hook)

    if isinstance(result, dict):
        assert result["success"] is False
        _assert_reload_required(result["error"], toml_path)
    else:
        assert result is False
    assert hook.call_count == 0
    assert toml_path.read_bytes() == before
    assert sched._reload_required is True


@pytest.mark.parametrize("action", ["remove", "pause", "resume"])
def test_latched_builtin_tag_action_is_refused(make_scheduler: MakeScheduler, action: str) -> None:
    """The schedule tool's latch preflight marks remove/pause/resume refused, file untouched."""
    sched, toml_path = make_scheduler()
    sched._reload_required = True
    before = toml_path.read_bytes()
    tag = "paused" if action == "resume" else "alpha"

    out = exec_schedule(sched, {"action": action, "tag": tag})

    public = strip_execution(out)
    assert public["success"] is False
    _assert_reload_required(public["error"], toml_path)
    assert read_execution(out) == "refused"
    assert toml_path.read_bytes() == before


@pytest.mark.parametrize("action", ["remove", "resume"])
def test_unlatched_unknown_tag_keeps_executed_failure(
    make_scheduler: MakeScheduler, action: str,
) -> None:
    """Without the latch an unknown tag is still an executed not-found failure."""
    sched, _ = make_scheduler()

    out = exec_schedule(sched, {"action": action, "tag": "nope"})

    assert strip_execution(out) == {
        "success": False, "output": "", "error": "Job 'nope' not found.", "exit_code": -1,
    }
    assert read_execution(out) == "executed"


# ---------------------------------------------------------------------------
# 3. Late failures of attempted builtin add/modify stay executed
# ---------------------------------------------------------------------------

# name -> (tool args, needs the loop running to publish registrations, committed predicate)
_BUILTIN_LATE: dict[str, tuple[dict, bool, Callable[[dict], bool]]] = {
    "add": (
        {"action": "add", "tag": "gamma", "task": "t", "cron": "0 5 1 1 *"},
        True,
        lambda doc: "gamma" in doc["jobs"],
    ),
    "modify": (
        {"action": "modify", "tag": "alpha", "cron": "0 6 1 1 *"},
        False,
        lambda doc: doc["jobs"]["alpha"]["cron"] == "0 6 1 1 *",
    ),
}


@pytest.mark.parametrize("action", sorted(_BUILTIN_LATE))
def test_builtin_postcommit_failure_is_executed(
    make_scheduler: MakeScheduler,
    monkeypatch: pytest.MonkeyPatch,
    isolated_schedule: _IsolatedScheduler,
    action: str,
) -> None:
    """The attempt ran and the file changed: disposition executed, safe failure, latch set."""
    args, needs_loop, committed_change = _BUILTIN_LATE[action]
    sched, toml_path = make_scheduler(enabled=needs_loop)
    loop = None
    try:
        if needs_loop:
            loop = _start_loop(sched, f"publication-failure-{action}-loop")
            assert sched._loop_running()
        outcome, _ = _trip(
            sched, toml_path, monkeypatch, isolated_schedule.jobs,
            lambda: exec_schedule(sched, args),
        )
    finally:
        isolated_schedule.jobs.armed = False
        if loop is not None:
            sched.stop()

    out = _result(outcome)
    assert loop is None or not loop.is_alive()
    public = strip_execution(out)
    assert public["success"] is False
    _assert_reload_required(public["error"], toml_path)
    assert read_execution(out) == "executed"
    assert committed_change(_parse(toml_path.read_bytes()))
    assert sched._reload_required is True


# ---------------------------------------------------------------------------
# 4. Recovery through reload
# ---------------------------------------------------------------------------


def _latched_by_fault(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
    isolated_schedule: _IsolatedScheduler,
) -> tuple[Scheduler, Path]:
    """Latch a scheduler through a real post-commit publication fault (fault left armed)."""
    sched, toml_path = make_scheduler()
    _trip(
        sched, toml_path, monkeypatch, isolated_schedule.jobs,
        lambda: sched.modify_job("alpha", {"cron": "0 6 1 1 *"}, _on_attempt=MagicMock()),
    )
    assert getattr(sched, "_reload_required", _MISSING) is True
    return sched, toml_path


def test_reload_under_persistent_publication_fault_keeps_latch(
    make_scheduler: MakeScheduler,
    monkeypatch: pytest.MonkeyPatch,
    isolated_schedule: _IsolatedScheduler,
) -> None:
    """A reload whose publication faults is contained: safe error, latch still set."""
    sched, toml_path = _latched_by_fault(make_scheduler, monkeypatch, isolated_schedule)

    outcome = _call(sched.reload)

    assert isolated_schedule.jobs.raised == 2, "the reload publication fault was not reached"
    result = _result(outcome)
    assert isinstance(result.get("error"), str)
    assert _SECRET not in result["error"]
    assert sched._reload_required is True
    _assert_reload_required(sched.load_notice(), toml_path)
    assert schedule.default_scheduler.jobs is isolated_schedule.jobs


def test_reload_after_fault_clears_publishes_complete_unit_and_clears_latch(
    make_scheduler: MakeScheduler,
    monkeypatch: pytest.MonkeyPatch,
    isolated_schedule: _IsolatedScheduler,
) -> None:
    """Once the fault is gone a reload loads the committed file and clears the latch."""
    sched, toml_path = _latched_by_fault(make_scheduler, monkeypatch, isolated_schedule)
    isolated_schedule.jobs.armed = False

    result = sched.reload()

    disk = toml_path.read_bytes()
    assert result == {"reloaded": 2, "failed": 0}
    assert sched._reload_required is False
    assert sched.load_notice() is None
    assert sched._load_result.content_hash == _sha256(disk)
    assert sched._load_result.document == _parse(disk)
    assert sched._jobs_meta["alpha"]["cron"] == "0 6 1 1 *"
    assert schedule.default_scheduler.jobs is isolated_schedule.jobs is schedule.jobs
    assert [sorted(job.tags) for job in isolated_schedule.jobs] == [["reminder"]]


# ---------------------------------------------------------------------------
# 5. Quiesced automatic admission
# ---------------------------------------------------------------------------


def test_latched_run_pending_does_not_admit_due_once_job(
    make_scheduler: MakeScheduler, isolated_schedule: _IsolatedScheduler,
) -> None:
    """A still-registered due once trigger fires into _run_job, which admits nothing."""
    sched, _ = make_scheduler()
    job = _register_reminder(sched, isolated_schedule)
    job.next_run = datetime.now() - timedelta(minutes=1)
    sched._reload_required = True

    schedule.run_pending()

    assert sched.notify.call_count == 0
    assert "reminder" not in sched._running_jobs
    assert "reminder" not in sched._active_runs
    assert sched._jobs_meta["reminder"].get("last_run") is None
    assert "reminder" not in sched._run_history


def test_latch_does_not_cancel_already_admitted_run(
    make_scheduler: MakeScheduler, isolated_schedule: _IsolatedScheduler,
) -> None:
    """An admitted run stays tracked when a latched tick fires another trigger."""
    sched, _ = make_scheduler()
    job = _register_reminder(sched, isolated_schedule)
    job.next_run = datetime.now() - timedelta(minutes=1)
    with sched._running_lock:
        sched._running_jobs.add("alpha")
        sched._active_runs["alpha"] = {"r-seeded00"}
    sched._reload_required = True

    schedule.run_pending()

    assert "alpha" in sched._running_jobs
    assert sched._active_runs.get("alpha") == {"r-seeded00"}


def test_latched_loop_tick_does_not_admit_due_cron_job(
    make_scheduler: MakeScheduler, isolated_schedule: _IsolatedScheduler,
) -> None:
    """A real loop tick over a due cron job: every spawned _run_job admits nothing."""
    sched, _ = make_scheduler(enabled=True)
    live = sched._jobs_meta["alpha"]
    live["_next_run"] = live["_natural_next_run"] = datetime(2000, 1, 1).isoformat()
    sched._reload_required = True
    before = set(threading.enumerate())
    loop = None
    runs: list[threading.Thread] = []
    try:
        loop = _start_loop(sched, "publication-failure-admission-loop")
        assert isolated_schedule.ticked.wait(_BOUND), "the loop never completed a tick"
    finally:
        sched.stop()
        runs = [
            thread for thread in threading.enumerate()
            if thread not in before and thread.name.endswith("(_run_job)")
        ]
        for thread in runs:
            thread.join(timeout=_BOUND)

    assert loop is not None and not loop.is_alive()
    assert [thread for thread in runs if thread.is_alive()] == []
    assert sched.notify.call_count == 0
    assert live.get("last_run") is None and live.get("last_error") is None
    assert "alpha" not in sched._run_history
    assert "alpha" not in sched._running_jobs
