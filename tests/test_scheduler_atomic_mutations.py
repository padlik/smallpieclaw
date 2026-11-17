"""
Mutation freshness guard against external scheduler-file edits (task 12.4, first slice).

``add_job``, ``modify_job``, ``remove_job``, ``pause_job`` and ``resume_job``
compare the scheduler file on disk with the stored hash (the hash of the last
bytes this scheduler parsed or wrote) — or, for a file recorded as missing,
require that it is still missing — before writing. A valid TOML edit made
outside the scheduler since the last load is an explicit failure: the file is
not overwritten and the runtime view, the authoritative document and hash, and
the registrations stay exactly as they were. The scheduler's own successful
write refreshes the stored hash, so the next ordinary mutation succeeds.

Validation refusals keep preceding ``_on_attempt`` (owned by
``tests/test_scheduler_attempt_evidence.py``). An external-edit mismatch can
only be discovered after the attempt, so add/modify call the hook exactly once
and never replay it. ``remove_job``/``pause_job``/``resume_job`` keep their
existing boolean API without an ``_on_attempt`` parameter.

Preparation and commit failures (second slice): a fault in the external
trigger libraries (``croniter.get_next``, ``schedule.Job.do``) while the
affected registrations are prepared, an ``os.replace`` failure on the
scheduler file, and a file that became unparseable or unreadable after load
all return a controlled failure with the full prior state intact; errors never
echo the exception text. Two independent updates prepared from the same base
both persist, the later one re-prepared without a second ``_on_attempt``.

Concurrency cases (final section) exercise specific interleavings with
``_run_job`` history recording, state save, and reload: each gate records
whether it was released or expired, and where a contender must block, a
delegating lock probe shows it reached the intended lock before the holder is
released. They are scenario coverage of those interleavings, not a formal
lock-order proof. Publication gaps found in review (type-only changes, stale
schedule triples, start()/loop races, registration-container faults) are
covered in ``tests/test_scheduler_atomic_publication.py``.

Only external boundaries (the file system via ``tmp_path``, notify, agent) are
substituted; the scheduler, its parser, encoder, and registration are real.
Cron registration is observable as the job's ``next_run``; the scheduler is
constructed with ``scheduler.enabled = false`` so no loop thread runs, and
enabled jobs are registered through the real ``_register_job`` where a test
needs a registered baseline.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional, Union
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from builtin_tools.schedule import exec_schedule
from outcome_utils import read_execution
from scheduler import Scheduler
from tests import scheduler_legacy_fixtures as legacy_fx
from xdg import XDGPaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paths_for(state_dir: Path) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
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


def _parse(text: str) -> dict:
    """Parse TOML text with the scheduler's own parser."""
    return scheduler_module.tomli.loads(text)


def _sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


# Far-future cron schedules (1 January) so nothing fires during a test. The
# unknown and raw fields must survive untouched.
_TOML = """\
title = "operator scheduler"

[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha"
model = "provider/model-a"
custom_field = "keep me"
max_iterations = 7

[jobs.dormant]
enabled = false
schedule = "cron"
cron = "0 4 1 1 *"
task = "dormant job"
"""

# A valid operator edit: one changed value and one new job table.
_EXTERNAL_TOML = _TOML.replace('task = "job alpha"', 'task = "operator edited alpha"') + """
[jobs.operator_added]
enabled = true
schedule = "cron"
cron = "0 5 1 1 *"
task = "added by the operator"
"""

# Every schedule tag a test in this module can register; cleanup clears only these.
_TAGS = ("alpha", "dormant", "fresh", "operator_added")

Result = Union[dict, bool]


def _state(sched: Scheduler) -> dict:
    """Capture the runtime view, authoritative document/hash, and registrations."""
    load_result = sched._load_result
    return {
        "meta": copy.deepcopy(sched._jobs_meta),
        "jobs": copy.deepcopy(sched.list_jobs()),
        "status": load_result.status,
        "document": copy.deepcopy(load_result.document),
        "hash": load_result.content_hash,
        "registrations": {tag: [repr(job) for job in schedule.get_jobs(tag)] for tag in _TAGS},
    }


def _snapshot(sched: Scheduler, toml_path: Path) -> dict:
    """Capture :func:`_state` plus the scheduler file bytes (``None`` when missing)."""
    return {
        **_state(sched),
        "file": toml_path.read_bytes() if toml_path.exists() else None,
    }


def _failed(result: Result) -> bool:
    """Return True when a public mutation result reports failure."""
    if isinstance(result, bool):
        return result is False
    return result.get("success") is False


def _add(sched: Scheduler, hook: MagicMock) -> Result:
    return sched.add_job("fresh", "cron", "fresh task", cron="0 3 1 1 *", _on_attempt=hook)


def _modify(sched: Scheduler, hook: MagicMock) -> Result:
    return sched.modify_job("alpha", {"task": "patched alpha"}, _on_attempt=hook)


# remove/pause/resume keep their boolean API without an _on_attempt parameter.
def _remove(sched: Scheduler, _hook: MagicMock) -> Result:
    return sched.remove_job("alpha")


def _pause(sched: Scheduler, _hook: MagicMock) -> Result:
    return sched.pause_job("alpha")


def _resume(sched: Scheduler, _hook: MagicMock) -> Result:
    return sched.resume_job("dormant")


_ALL_OPERATIONS = [
    pytest.param(_add, id="add"),
    pytest.param(_modify, id="modify"),
    pytest.param(_remove, id="remove"),
    pytest.param(_pause, id="pause"),
    pytest.param(_resume, id="resume"),
]

_HOOKED_OPERATIONS = [
    pytest.param(_add, id="add"),
    pytest.param(_modify, id="modify"),
]


@pytest.fixture
def make_scheduler(
    tmp_path: Path,
) -> Iterator[Callable[[Optional[str]], tuple[Scheduler, Path]]]:
    """Construct a real disabled Scheduler; ``None`` text records the file as missing.

    No loop thread is started (``scheduler.enabled = false``), so cleanup only
    clears this module's own schedule tags instead of every schedule job.
    """

    def _make(text: Optional[str]) -> tuple[Scheduler, Path]:
        toml_path = tmp_path / "scheduler.toml"
        if text is not None:
            toml_path.write_text(text, encoding="utf-8")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        sched = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=_paths_for(state_dir),
            scheduler_config_path=str(toml_path),
        )
        return sched, toml_path

    yield _make
    for tag in _TAGS:
        schedule.clear(tag)


@pytest.fixture
def edited_externally(
    make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
) -> tuple[Scheduler, Path]:
    """A loaded scheduler with alpha registered, then a valid external edit on disk."""
    sched, toml_path = make_scheduler(_TOML)
    sched._register_job("alpha", sched._jobs_meta["alpha"])
    assert sched.show_job("alpha")["next_run"] is not None
    assert sched.show_job("dormant")["enabled"] is False

    toml_path.write_text(_EXTERNAL_TOML, encoding="utf-8")
    assert _sha256(toml_path.read_bytes()) != sched._load_result.content_hash
    return sched, toml_path


# ---------------------------------------------------------------------------
# External edit since the last load
# ---------------------------------------------------------------------------


class TestExternalEditRefused:
    """A mutation never overwrites a file edited outside the scheduler."""

    @pytest.mark.parametrize("operation", _ALL_OPERATIONS)
    def test_mutation_fails_and_leaves_state_and_file_unchanged(
        self,
        edited_externally: tuple[Scheduler, Path],
        operation: Callable[[Scheduler, MagicMock], Result],
    ) -> None:
        """Runtime, document, hash, registrations, and the edited bytes all survive."""
        sched, toml_path = edited_externally
        before = _snapshot(sched, toml_path)

        result = operation(sched, MagicMock())

        assert _failed(result), result
        assert _snapshot(sched, toml_path) == before

    @pytest.mark.parametrize("operation", _HOOKED_OPERATIONS)
    def test_mismatch_after_attempt_calls_hook_exactly_once(
        self,
        edited_externally: tuple[Scheduler, Path],
        operation: Callable[[Scheduler, MagicMock], Result],
    ) -> None:
        """The mismatch is found after the attempt: one hook call, no replay."""
        sched, _ = edited_externally
        hook = MagicMock()

        result = operation(sched, hook)

        assert _failed(result), result
        assert hook.call_count == 1

    @pytest.mark.parametrize("operation", _HOOKED_OPERATIONS)
    def test_failure_names_the_file_and_asks_for_a_reload(
        self,
        edited_externally: tuple[Scheduler, Path],
        operation: Callable[[Scheduler, MagicMock], Result],
    ) -> None:
        """The error is actionable and never claims the change was applied."""
        sched, toml_path = edited_externally

        result = operation(sched, MagicMock())

        assert _failed(result), result
        error = result["error"]
        assert str(toml_path) in error
        assert "reload" in error.lower()
        assert "for this session" not in error


# ---------------------------------------------------------------------------
# File recorded as missing at load
# ---------------------------------------------------------------------------


class TestRecordedMissingFile:
    """A file recorded as missing must still be missing before the first create."""

    def test_externally_created_file_is_not_overwritten_by_add(
        self, make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
    ) -> None:
        """A file that appeared after load refuses the add and keeps its bytes."""
        sched, toml_path = make_scheduler(None)
        assert sched._load_result.status == "missing"
        toml_path.write_text(_EXTERNAL_TOML, encoding="utf-8")
        before = _snapshot(sched, toml_path)
        hook = MagicMock()

        result = _add(sched, hook)

        assert _failed(result), result
        assert hook.call_count == 1
        assert "fresh" not in {job["tag"] for job in sched.list_jobs()}
        assert _snapshot(sched, toml_path) == before

    def test_first_create_then_modify_both_succeed(
        self, make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
    ) -> None:
        """The scheduler's own first create is not mistaken for an external edit."""
        sched, toml_path = make_scheduler(None)

        added = _add(sched, MagicMock())

        assert added == {"success": True}
        assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())

        modified = sched.modify_job("fresh", {"task": "second task"})

        assert modified == {"success": True}
        assert _parse(toml_path.read_text(encoding="utf-8"))["jobs"]["fresh"]["task"] == (
            "second task"
        )
        assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())


# ---------------------------------------------------------------------------
# Own writes refresh the stored hash
# ---------------------------------------------------------------------------


def test_sequential_own_writes_all_succeed(
    make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
) -> None:
    """Each successful write publishes its hash, so the next mutation is not refused."""
    sched, toml_path = make_scheduler(_TOML)

    results = [
        _add(sched, MagicMock()),
        sched.pause_job("alpha"),
        sched.resume_job("dormant"),
        sched.modify_job("fresh", {"task": "fresh again"}),
        sched.remove_job("alpha"),
    ]

    assert results == [{"success": True}, True, True, {"success": True}, True]
    written = _parse(toml_path.read_text(encoding="utf-8"))
    assert set(written["jobs"]) == {"dormant", "fresh"}
    assert written["jobs"]["dormant"]["enabled"] is True
    assert written["jobs"]["fresh"]["task"] == "fresh again"
    assert written["title"] == "operator scheduler"
    assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())


# ---------------------------------------------------------------------------
# Preparation and commit faults (external library and file-system seams)
# ---------------------------------------------------------------------------

# Fake exception text that must never reach a user-facing error.
_SECRET = "fake-secret-sentinel-7f3a"

FaultInstaller = Callable[[pytest.MonkeyPatch, Path], list]


def _fail_cron_next(monkeypatch: pytest.MonkeyPatch, _toml_path: Path) -> list:
    """Make the external ``croniter.get_next`` raise; return the list of reached faults."""
    reached: list = []

    def get_next(self: object, *_args: object, **_kwargs: object) -> object:
        reached.append(self)
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(scheduler_module.croniter, "get_next", get_next)
    return reached


def _fail_job_do(monkeypatch: pytest.MonkeyPatch, _toml_path: Path) -> list:
    """Make the external ``schedule.Job.do`` raise; return the list of reached faults."""
    reached: list = []

    def do(self: object, *_args: object, **_kwargs: object) -> object:
        reached.append(self)
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(schedule.Job, "do", do)
    return reached


def _fail_replace_onto(monkeypatch: pytest.MonkeyPatch, toml_path: Path) -> list:
    """Make ``os.replace`` fail only onto the scheduler file; delegate every other path."""
    reached: list = []
    real_replace = os.replace
    target = os.path.realpath(toml_path)

    def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if os.path.realpath(os.fspath(dst)) == target:
            reached.append(dst)
            raise OSError(errno.EIO, _SECRET)
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return reached


def _modify_cron(sched: Scheduler, hook: MagicMock) -> Result:
    return sched.modify_job("alpha", {"cron": "0 6 1 1 *"}, _on_attempt=hook)


def _add_once(sched: Scheduler, hook: MagicMock) -> Result:
    return sched.add_job("fresh", "once", "fresh reminder", run_at="03:00", _on_attempt=hook)


@pytest.fixture
def loaded(
    make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
) -> tuple[Scheduler, Path]:
    """A scheduler over the unchanged valid file with alpha registered for real."""
    sched, toml_path = make_scheduler(_TOML)
    sched._register_job("alpha", sched._jobs_meta["alpha"])
    assert sched.show_job("alpha")["next_run"] is not None
    assert sched.show_job("dormant")["enabled"] is False
    return sched, toml_path


class TestTriggerPreparationFault:
    """A trigger that cannot be prepared fails contained before anything is published."""

    @pytest.mark.parametrize(
        ("operation", "hook_calls"),
        [
            pytest.param(_add, 1, id="add"),
            pytest.param(_modify_cron, 1, id="modify_cron"),
            pytest.param(_resume, 0, id="resume"),
        ],
    )
    def test_cron_trigger_fault_leaves_state_unchanged(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        operation: Callable[[Scheduler, MagicMock], Result],
        hook_calls: int,
    ) -> None:
        """A failing ``croniter.get_next`` is a controlled failure with prior state intact."""
        sched, toml_path = loaded
        before = _snapshot(sched, toml_path)
        reached = _fail_cron_next(monkeypatch, toml_path)
        hook = MagicMock()

        result = operation(sched, hook)

        assert _failed(result), result
        assert reached, "the external croniter.get_next fault was never reached"
        assert hook.call_count == hook_calls
        assert _snapshot(sched, toml_path) == before

    def test_once_trigger_fault_leaves_state_unchanged(
        self, loaded: tuple[Scheduler, Path], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing ``schedule.Job.do`` on a once add publishes no job anywhere."""
        sched, toml_path = loaded
        before = _snapshot(sched, toml_path)
        global_jobs = list(schedule.get_jobs())
        reached = _fail_job_do(monkeypatch, toml_path)
        hook = MagicMock()

        result = _add_once(sched, hook)

        assert _failed(result), result
        assert reached, "the external schedule.Job.do fault was never reached"
        assert hook.call_count == 1
        assert _snapshot(sched, toml_path) == before
        assert list(schedule.get_jobs()) == global_jobs


@pytest.mark.parametrize(
    ("install_fault", "operation"),
    [
        pytest.param(_fail_cron_next, _modify_cron, id="cron_trigger_modify"),
        pytest.param(_fail_job_do, _add_once, id="once_trigger_add"),
        pytest.param(_fail_replace_onto, _modify, id="replace_modify"),
    ],
)
def test_fault_error_is_safe_and_actionable(
    loaded: tuple[Scheduler, Path],
    monkeypatch: pytest.MonkeyPatch,
    install_fault: FaultInstaller,
    operation: Callable[[Scheduler, MagicMock], Result],
) -> None:
    """The error names the file, hides the exception text, and claims no applied change."""
    sched, toml_path = loaded
    install_fault(monkeypatch, toml_path)

    result = operation(sched, MagicMock())

    assert _failed(result), result
    error = result["error"]
    assert _SECRET not in error
    assert str(toml_path) in error
    assert "for this session" not in error


class TestWriteFailure:
    """A failed atomic replace keeps the last-good file and every published state."""

    @pytest.mark.parametrize("operation", _ALL_OPERATIONS)
    def test_replace_failure_leaves_state_and_file_unchanged(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        operation: Callable[[Scheduler, MagicMock], Result],
    ) -> None:
        """``os.replace`` onto scheduler.toml fails: public failure, nothing published."""
        sched, toml_path = loaded
        before = _snapshot(sched, toml_path)
        reached = _fail_replace_onto(monkeypatch, toml_path)

        result = operation(sched, MagicMock())

        assert _failed(result), result
        assert reached, "the os.replace fault on the scheduler file was never reached"
        assert _snapshot(sched, toml_path) == before


def _make_unparseable(toml_path: Path) -> None:
    """Overwrite the scheduler file with invalid TOML."""
    toml_path.write_text("[jobs.alpha\nbroken = \n", encoding="utf-8")


def _make_unreadable(toml_path: Path) -> None:
    """Replace the scheduler file with a directory (no permission changes)."""
    toml_path.unlink()
    toml_path.mkdir()


def _bad_path_state(toml_path: Path) -> tuple:
    """Describe the damaged scheduler path: a directory listing or file bytes."""
    if toml_path.is_dir():
        return ("directory", sorted(os.listdir(toml_path)))
    return ("file", toml_path.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_make_unparseable, id="unparseable"),
        pytest.param(_make_unreadable, id="unreadable"),
    ],
)
def test_commit_time_bad_file_fails_modify_without_change(
    loaded: tuple[Scheduler, Path], damage: Callable[[Path], None],
) -> None:
    """A file damaged after load is found at commit: one attempt, nothing changes."""
    sched, toml_path = loaded
    before = _state(sched)
    damage(toml_path)
    bad_before = _bad_path_state(toml_path)
    hook = MagicMock()

    result = _modify(sched, hook)

    assert _failed(result), result
    assert hook.call_count == 1
    assert _state(sched) == before
    assert _bad_path_state(toml_path) == bad_before


# ---------------------------------------------------------------------------
# Two independent updates prepared from the same base
# ---------------------------------------------------------------------------

_BOTH_ACTIVE = _TOML.replace("enabled = false", "enabled = true")
_NEW_CRONS = {"alpha": "0 6 1 1 *", "dormant": "0 7 1 1 *"}
_WORKER_PREFIX = "atomic-mutation-worker-"


def test_independent_updates_from_same_base_both_persist(
    make_scheduler: Callable[[Optional[str]], tuple[Scheduler, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both concurrently prepared patches persist; the later one is re-prepared once.

    Each worker's first ``croniter.get_next`` waits on a 2-party barrier
    (bounded at 3s) so both candidates are prepared from the same base before
    either commits; retries delegate straight through. Validation only
    constructs ``croniter``, so no barrier wait precedes ``_on_attempt``.
    """
    assert _BOTH_ACTIVE.count("enabled = true") == 2
    sched, toml_path = make_scheduler(_BOTH_ACTIVE)
    for tag in _NEW_CRONS:
        sched._register_job(tag, sched._jobs_meta[tag])
        assert sched.show_job(tag)["next_run"] is not None

    real_get_next = scheduler_module.croniter.get_next
    barrier = threading.Barrier(2, timeout=3)
    guard = threading.Lock()
    gated: set = set()
    worker_calls: list = []

    def get_next(self: object, *args: object, **kwargs: object) -> object:
        name = threading.current_thread().name
        first = False
        if name.startswith(_WORKER_PREFIX):
            with guard:
                worker_calls.append(name)
                first = name not in gated
                gated.add(name)
        if first:
            barrier.wait()
        return real_get_next(self, *args, **kwargs)

    monkeypatch.setattr(scheduler_module.croniter, "get_next", get_next)
    hooks = {tag: MagicMock() for tag in _NEW_CRONS}
    results: dict = {}
    errors: dict = {}

    def worker(tag: str) -> None:
        try:
            results[tag] = sched.modify_job(
                tag, {"cron": _NEW_CRONS[tag]}, _on_attempt=hooks[tag],
            )
        except Exception as exc:  # noqa: BLE001 - captured, then asserted absent below
            errors[tag] = exc

    threads = [
        threading.Thread(target=worker, args=(tag,), name=_WORKER_PREFIX + tag, daemon=True)
        for tag in _NEW_CRONS
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        barrier.abort()
        for thread in threads:
            thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == {}, errors
    assert results == {tag: {"success": True} for tag in _NEW_CRONS}
    assert {tag: hook.call_count for tag, hook in hooks.items()} == {"alpha": 1, "dormant": 1}
    assert len(worker_calls) >= 3, worker_calls
    written = _parse(toml_path.read_text(encoding="utf-8"))
    assert {tag: written["jobs"][tag]["cron"] for tag in _NEW_CRONS} == _NEW_CRONS
    assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())
    assert {tag: sched.show_job(tag)["cron"] for tag in _NEW_CRONS} == _NEW_CRONS


# ---------------------------------------------------------------------------
# Concurrency (final 12.4 slice): no deadlock against runs, history, state
# save, and reload; once-job publication; unrelated registrations preserved.
#
# Critical sections are held open at external boundaries only: ``os.replace``
# onto one target file (the caller keeps every lock it holds) or
# ``croniter.get_next`` in one named thread. Every wait is bounded and every
# gate is released in ``finally``. Runs go through the real ``run_now`` →
# ``_run_job`` path; with no builtin executor a run fails closed through
# ``_fail_launch`` (state save, then ``_record_execution``), with only
# ``notify`` substituted.
#
# A held gate records whether it was released (``exited`` after ``release``)
# or expired (``timed_out``); tests assert it never expired, so a thread that
# is "still alive" is never a silent auto-timeout. ``_LockProbe`` delegates
# to the scheduler's own lock (same reentrancy and order) and only signals
# that a named thread is about to acquire it.
# ---------------------------------------------------------------------------

_BOUND = 10.0
_RELOAD_WORKER = "atomic-reload-worker"


class _ReplaceGate:
    """Hold the first ``os.replace`` onto one target file until released.

    With ``fail`` every replace onto the target then raises ``OSError`` (the
    caller's write fails); otherwise it delegates to the real ``os.replace``.
    ``exited`` is set once the held call stops waiting; ``timed_out`` is True
    when that happened because the bounded wait expired, not a release.
    """

    def __init__(self, target: Path, *, fail: bool = False) -> None:
        self.target = os.path.realpath(target)
        self.fail = fail
        self.reached = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.timed_out = False
        self._guard = threading.Lock()
        self._held = False

    def enter(self) -> None:
        """Hold the first call (bounded), then fail or let the caller delegate."""
        with self._guard:
            first = not self._held
            self._held = True
        if first:
            self.reached.set()
            try:
                self.timed_out = not self.release.wait(timeout=_BOUND)
            finally:
                self.exited.set()
        if self.fail:
            raise OSError(errno.EIO, "held replace failed")


class _LockProbe:
    """Delegate to an existing scheduler lock; signal a watched thread's acquire attempt.

    Acquire, release, reentrancy, and ordering are the wrapped lock's own.
    ``watch(name)`` returns an event set when the thread named *name* next
    tries to acquire (it is then at, or blocked on, the real lock).
    """

    def __init__(self, lock: object) -> None:
        self._lock = lock
        self._guard = threading.Lock()
        self._watched: dict = {}

    def watch(self, thread_name: str) -> threading.Event:
        """Return an event set on *thread_name*'s next acquire attempt."""
        event = threading.Event()
        with self._guard:
            self._watched[thread_name] = event
        return event

    def acquire(self, *args: object, **kwargs: object) -> bool:
        """Signal a watched caller, then acquire the wrapped lock."""
        with self._guard:
            event = self._watched.pop(threading.current_thread().name, None)
        if event is not None:
            event.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        """Release the wrapped lock."""
        self._lock.release()

    def __enter__(self) -> "_LockProbe":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _assert_no_expiry(*gates: _ReplaceGate) -> None:
    """Fail when any gate stopped holding because its bounded wait expired."""
    assert [gate.target for gate in gates if gate.timed_out] == [], "a held gate expired"


def _install_replace_gates(monkeypatch: pytest.MonkeyPatch, *gates: _ReplaceGate) -> None:
    """Route ``os.replace`` onto each gate's target through that gate; delegate the rest."""
    real_replace = os.replace
    by_target = {gate.target: gate for gate in gates}

    def replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        gate = by_target.get(os.path.realpath(os.fspath(dst)))
        if gate is not None:
            gate.enter()
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


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


def _start_held_run(sched: Scheduler, tag: str, gate: _ReplaceGate) -> threading.Thread:
    """``run_now`` *tag* and return its run thread once it is held at *gate*."""
    before = set(threading.enumerate())
    assert sched.run_now(tag) == {"success": True}
    assert gate.reached.wait(_BOUND), "the run never reached its held write"
    spawned = [
        thread for thread in threading.enumerate()
        if thread not in before and thread.name.endswith("(_run_job)")
    ]
    assert len(spawned) == 1, spawned
    return spawned[0]


def _release(*gates: _ReplaceGate) -> None:
    """Release every gate so held threads can finish."""
    for gate in gates:
        gate.release.set()


def _join(*threads: threading.Thread) -> list[threading.Thread]:
    """Join each thread (bounded) and return the ones still alive."""
    for thread in threads:
        thread.join(timeout=_BOUND)
    return [thread for thread in threads if thread.is_alive()]


@pytest.fixture
def thread_errors(monkeypatch: pytest.MonkeyPatch) -> list:
    """Collect exceptions escaping any thread (the run threads ``run_now`` starts)."""
    errors: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    return errors


class TestNoDeadlockWithRunHistory:
    """Mutations never wait on history recording; history waits only briefly on commits."""

    @pytest.mark.parametrize("operation", _ALL_OPERATIONS)
    def test_mutation_completes_while_run_holds_history_lock(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        thread_errors: list,
        operation: Callable[[Scheduler, MagicMock], Result],
    ) -> None:
        """A run held inside ``_record_execution`` does not block a public mutation.

        The run's execution-log write is held, so the run thread keeps
        ``_history_lock`` (and the log's own lock) while the mutation runs.
        """
        sched, _ = loaded
        history_gate = _ReplaceGate(Path(sched.execution_log._log_file))
        _install_replace_gates(monkeypatch, history_gate)
        run_thread = mutation = None
        try:
            run_thread = _start_held_run(sched, "alpha", history_gate)
            mutation, outcome = _start(
                "atomic-mutation-under-history", lambda: operation(sched, MagicMock()),
            )
            mutation.join(timeout=_BOUND)
            assert not mutation.is_alive(), "the mutation waited on the held history record"
            assert run_thread.is_alive() and not history_gate.release.is_set()
            assert not history_gate.exited.is_set(), "the history record was no longer held"
        finally:
            _release(history_gate)
            alive = _join(*(t for t in (run_thread, mutation) if t is not None))

        assert alive == []
        _assert_no_expiry(history_gate)
        assert thread_errors == []
        assert "error" not in outcome, outcome
        assert not _failed(outcome["result"]), outcome["result"]

    def test_history_and_mutation_crossing_lock_order_both_complete(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        thread_errors: list,
    ) -> None:
        """History holding ``_history_lock`` and a commit holding its locks both finish.

        The run is held in its (failing) execution-log write with
        ``_history_lock`` taken; the mutation is then held in its
        scheduler-file write with ``_running_lock`` and ``_save_lock`` taken.
        Released in that order, the failed record needs the commit locks for
        its persistence marker, and the commit never needs ``_history_lock``.
        The history gate is released first; the commit gate only after the
        run thread, still holding ``_history_lock``, reached ``_running_lock``
        (held by the commit), so the crossing really happens.
        """
        sched, toml_path = loaded
        running_probe = _LockProbe(sched._running_lock)
        monkeypatch.setattr(sched, "_running_lock", running_probe)
        history_gate = _ReplaceGate(Path(sched.execution_log._log_file), fail=True)
        commit_gate = _ReplaceGate(toml_path)
        _install_replace_gates(monkeypatch, history_gate, commit_gate)
        run_thread = mutation = None
        try:
            run_thread = _start_held_run(sched, "alpha", history_gate)
            mutation, outcome = _start(
                "atomic-mutation-crossing", lambda: _modify(sched, MagicMock()),
            )
            assert commit_gate.reached.wait(_BOUND), "the commit waited on the held history record"
            run_crossing = running_probe.watch(run_thread.name)
            history_gate.release.set()
            assert run_crossing.wait(_BOUND), "the released history record never reached _running_lock"
            assert run_thread.is_alive() and not commit_gate.exited.is_set()
            commit_gate.release.set()
            alive = _join(run_thread, mutation)
        finally:
            _release(history_gate, commit_gate)
            _join(*(t for t in (run_thread, mutation) if t is not None))

        assert alive == []
        _assert_no_expiry(history_gate, commit_gate)
        assert thread_errors == []
        assert outcome == {"result": {"success": True}}
        assert scheduler_module._HISTORY_PERSIST_MARKER in sched.show_job("alpha")["last_error"]


class TestNoDeadlockWithStateSave:
    """A state save in progress and a mutation commit interleave without deadlock."""

    def test_mutation_behind_held_state_save_keeps_hash_equal_to_disk(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        thread_errors: list,
    ) -> None:
        """A run's state save holds ``_save_lock``; the mutation commits after it.

        After both finish (the mutation saves state again after its commit),
        the stored hash is the hash of the bytes on disk. The state save is
        released only after the mutation's commit reached ``_save_lock``.
        """
        sched, toml_path = loaded
        save_probe = _LockProbe(sched._save_lock)
        monkeypatch.setattr(sched, "_save_lock", save_probe)
        state_gate = _ReplaceGate(Path(sched._state_file))
        _install_replace_gates(monkeypatch, state_gate)
        mutation_name = "atomic-mutation-behind-state-save"
        run_thread = mutation = None
        try:
            run_thread = _start_held_run(sched, "alpha", state_gate)
            mutation_at_save = save_probe.watch(mutation_name)
            mutation, outcome = _start(mutation_name, lambda: _modify(sched, MagicMock()))
            assert mutation_at_save.wait(_BOUND), "the mutation never reached _save_lock"
            assert mutation.is_alive() and not state_gate.exited.is_set()
            state_gate.release.set()
            alive = _join(run_thread, mutation)
        finally:
            _release(state_gate)
            _join(*(t for t in (run_thread, mutation) if t is not None))

        assert alive == []
        _assert_no_expiry(state_gate)
        assert thread_errors == []
        assert outcome == {"result": {"success": True}}
        assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())


def _reload_overlapping_modify(
    sched: Scheduler, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Hold ``reload()`` in its candidate preparation, run a modify, then let reload finish.

    Reload has parsed its own input bytes but has not published anything when
    its first ``croniter.get_next`` (in the reload thread only, preparing the
    candidate's registrations) is held. Returns the generation before, both
    outcomes, whether the modify finished while reload was still held (not
    released, not expired), whether the hold expired, and the threads still
    alive after bounded joins.
    """
    real_get_next = scheduler_module.croniter.get_next
    reached = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    guard = threading.Lock()
    held: list = []
    expired: list = []

    def get_next(self: object, *args: object, **kwargs: object) -> object:
        if threading.current_thread().name == _RELOAD_WORKER:
            with guard:
                first = not held
                held.append(self)
            if first:
                reached.set()
                try:
                    expired.append(not release.wait(timeout=_BOUND))
                finally:
                    exited.set()
        return real_get_next(self, *args, **kwargs)

    monkeypatch.setattr(scheduler_module.croniter, "get_next", get_next)
    generation_before = sched._config_generation
    reloader = mutation = None
    try:
        reloader, reloaded = _start(_RELOAD_WORKER, sched.reload)
        assert reached.wait(_BOUND), "reload never reached its re-registration"
        mutation, modified = _start(
            "atomic-mutation-during-reload", lambda: _modify(sched, MagicMock()),
        )
        mutation.join(timeout=_BOUND)
        finished_while_held = (
            not mutation.is_alive() and reloader.is_alive() and not exited.is_set()
        )
    finally:
        release.set()
        alive = _join(*(t for t in (reloader, mutation) if t is not None))
    return {
        "generation_before": generation_before,
        "reloaded": reloaded,
        "modified": modified,
        "finished_while_held": finished_while_held,
        "expired": any(expired),
        "alive": alive,
    }


class TestNoDeadlockWithReload:
    """A public reload and a mutation both complete; the outcome stays coherent.

    The modify commits while reload is held before publishing its candidate,
    so the reload's locked recheck finds the file changed and fails with a
    controlled "reload again" result; the committed mutation is kept and only
    its generation is published. ``reload()`` clears every ``schedule`` job
    when it publishes (its real behavior); this module re-registers its own.
    """

    def test_reload_and_mutation_complete_with_stored_hash_equal_to_disk(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
        thread_errors: list,
    ) -> None:
        """The modify finishes during the held reload, which then fails; hash == disk bytes."""
        sched, toml_path = loaded

        run = _reload_overlapping_modify(sched, monkeypatch)

        assert run["alive"] == []
        assert not run["expired"], "the held reload expired instead of being released"
        assert thread_errors == []
        assert run["finished_while_held"]
        reloaded = run["reloaded"]["result"]
        assert (reloaded["reloaded"], reloaded["failed"]) == (0, 0)
        assert "reload again" in reloaded["error"].lower()
        assert str(toml_path) in reloaded["error"]
        assert run["modified"] == {"result": {"success": True}}
        assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())
        assert _parse(toml_path.read_text(encoding="utf-8")) == sched._load_result.document

    def test_failed_reload_does_not_publish_generation(
        self,
        loaded: tuple[Scheduler, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the committed mutation publishes a generation; the failed reload publishes none."""
        sched, toml_path = loaded

        run = _reload_overlapping_modify(sched, monkeypatch)

        assert run["alive"] == []
        assert not run["expired"], "the held reload expired instead of being released"
        assert run["modified"] == {"result": {"success": True}}
        reloaded = run["reloaded"]["result"]
        assert (reloaded["reloaded"], reloaded["failed"]) == (0, 0)
        assert "reload again" in reloaded["error"].lower()
        assert str(toml_path) in reloaded["error"]
        assert sched._config_generation == run["generation_before"] + 1


# ---------------------------------------------------------------------------
# Once-job publication and unrelated registrations
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_running(loaded: tuple[Scheduler, Path]) -> Iterator[tuple[Scheduler, Path]]:
    """``loaded`` with the loop-running condition met by a parked stand-in thread.

    No real ``_run_loop`` starts (it would run every global ``schedule`` job)
    and ``stop()`` is never called (it clears every global ``schedule`` job);
    the stand-in is released and joined, and ``make_scheduler`` clears this
    module's tags.
    """
    sched, toml_path = loaded
    parked = threading.Event()
    stand_in = threading.Thread(
        target=parked.wait, args=(_BOUND * 6,), name="atomic-loop-stand-in", daemon=True,
    )
    sched.enabled = True
    sched._thread = stand_in
    stand_in.start()
    try:
        assert sched._loop_running()
        yield sched, toml_path
    finally:
        parked.set()
        stand_in.join(timeout=_BOUND)


def test_once_add_is_not_published_while_loop_stopped(
    loaded: tuple[Scheduler, Path],
) -> None:
    """With no loop running the once trigger is prepared but never published."""
    sched, _ = loaded

    result = _add_once(sched, MagicMock())

    assert result == {"success": True}
    assert "fresh" in {job["tag"] for job in sched.list_jobs()}
    assert schedule.get_jobs("fresh") == []


def test_once_add_is_published_with_tag_and_run_callback_while_loop_runs(
    loop_running: tuple[Scheduler, Path],
) -> None:
    """With the loop running one ``fresh`` job is published to the default scheduler."""
    sched, _ = loop_running

    result = _add_once(sched, MagicMock())

    assert result == {"success": True}
    published = schedule.get_jobs("fresh")
    assert len(published) == 1
    job = published[0]
    assert job.tags == {"fresh"}
    assert job.scheduler is schedule.default_scheduler
    assert job.job_func.func == sched._run_job
    assert job.job_func.keywords == {"tag": "fresh"}


def test_mutation_on_dormant_keeps_unrelated_registration_identity(
    loop_running: tuple[Scheduler, Path],
) -> None:
    """Resuming dormant replaces only its own registration; fresh's Job object is kept.

    Cron registrations are next-run fields, not ``schedule`` jobs, so the
    unrelated registration is a once job published while the loop runs.
    """
    sched, _ = loop_running
    assert _add_once(sched, MagicMock()) == {"success": True}
    before = schedule.get_jobs("fresh")
    assert len(before) == 1

    assert sched.resume_job("dormant") is True

    after = schedule.get_jobs("fresh")
    assert len(after) == 1 and after[0] is before[0]
    assert sched.show_job("dormant")["next_run"] is not None


# ---------------------------------------------------------------------------
# TOML mutations beside a legacy job file
#
# Relocated by task 2.4 from tests/test_scheduler_legacy_readonly.py (ledger
# provenance in openspec/changes/simplify-model-runtime/test-preservation-ledger).
# Task 8.4 (ledger 451-455/458): the atomic TOML change and legacy byte
# preservation are kept; the legacy shadow preflight is gone (the legacy file
# is never read, so it can neither shadow nor refuse a TOML job change).
# ---------------------------------------------------------------------------


@pytest.fixture
def make_legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[legacy_fx.MakeLegacyScheduler]:
    """Real legacy-beside-TOML schedulers (shared factory, see the support module)."""
    yield from legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)


# name -> (legacy at construction, legacy written after construction)
_REMOVAL_ALLOWED = {
    "legacy_missing": (None, None),
    "unrelated_legacy_job": (legacy_fx.UNRELATED_LEGACY, None),
}


class TestTomlRemovalShadowPreflight:
    """A removal never consults the legacy file: a missing or unrelated one changes nothing."""

    @pytest.mark.parametrize("case", sorted(_REMOVAL_ALLOWED))
    def test_removal_proceeds_without_current_shadow(
        self, make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, case: str,
    ) -> None:
        """A missing legacy file or an unrelated legacy job allows a normal removal."""
        initial, late = _REMOVAL_ALLOWED[case]
        env = make_legacy_scheduler(legacy_fx.TOML, legacy=initial)
        legacy_fx.write_legacy(env.legacy_path, late)
        legacy_before = legacy_fx.file_state(env.legacy_path)

        out = exec_schedule(env.sched, {"action": "remove", "tag": "alpha"})

        assert out["success"] is True, out
        assert read_execution(out) == "executed"
        assert "alpha" not in legacy_fx.parse_toml(env.toml_path)["jobs"]
        assert "alpha" not in legacy_fx.tags(env.sched)
        assert legacy_fx.file_state(env.legacy_path) == legacy_before


_SHADOW_CHANGES = {
    "modify": ({"action": "modify", "tag": "alpha", "task": "shadow modified"},
               ("alpha", "task", "shadow modified")),
    "pause": ({"action": "pause", "tag": "alpha"}, ("alpha", "enabled", False)),
    "resume": ({"action": "resume", "tag": "dormant"}, ("dormant", "enabled", True)),
}


@pytest.mark.parametrize("change", sorted(_SHADOW_CHANGES))
def test_shadowing_toml_job_change_proceeds(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, change: str,
) -> None:
    """Modify, pause, and resume of a shadowing scheduler.toml job are applied normally."""
    args, (tag, field, value) = _SHADOW_CHANGES[change]
    env = make_legacy_scheduler(legacy_fx.TOML, legacy=legacy_fx.SHADOW_LEGACY)

    out = exec_schedule(env.sched, args)

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    assert legacy_fx.parse_toml(env.toml_path)["jobs"][tag][field] == value
    assert env.legacy_path.read_bytes() == legacy_fx.legacy_bytes(legacy_fx.SHADOW_LEGACY)


def test_toml_change_succeeds_beside_bad_legacy_file(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler,
) -> None:
    """A legacy warning never blocks an ordinary scheduler.toml modify."""
    env = make_legacy_scheduler(legacy_fx.TOML, legacy=legacy_fx.MALFORMED_LEGACY, enabled=True)
    env.sched.start()

    out = exec_schedule(env.sched, {"action": "modify", "tag": "alpha", "task": "still editable"})

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    assert legacy_fx.parse_toml(env.toml_path)["jobs"]["alpha"]["task"] == "still editable"
    assert env.sched.show_job("alpha")["next_run"] is not None
    assert env.legacy_path.read_bytes() == legacy_fx.MALFORMED_LEGACY
