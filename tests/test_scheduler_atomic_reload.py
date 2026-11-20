"""
Atomic reload (task 12.5): a reload publishes only the candidate it parsed.

A reload reads, hashes, and parses its own scheduler.toml bytes and legacy
JSON bytes, prepares the complete map, view, and registrations outside the
commit locks, then rechecks both inputs under the locks against its OWN
candidate input (never the previously stored hash). A legitimate operator
edit loads; a mutation or external change that landed between the parse and
the publish fails with an actionable "reload again" result, keeps the
last-good map/document/hash/registrations, and is never retried. A failed
reload leaves the private ``_reload_required`` latch as it was; a successful
reload clears it.

Only external boundaries are gated or faulted: ``croniter.get_next`` and
``schedule.Job.do`` in the reload thread, and real files on disk. The
scheduler, its parser, and its commit are real. Every test runs on an
isolated ``schedule.default_scheduler`` (and ``schedule.jobs``). Paths come
from the real ``xdg_paths`` with every XDG directory under ``tmp_path``.
Every wait and join is bounded and every gate is released in ``finally``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from scheduler import Scheduler
from xdg import xdg_paths


_BOUND = 10.0
_SECRET = "fake-secret-sentinel-9d2a"
_RELOAD_WORKER = "atomic-reload-worker"
_MISSING = object()

# Far-future crons (1 January) so nothing fires during a test.
_BASE_TOML = f"""\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha {_SECRET}"
notify = false
"""

_EDITED_TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha edited by the operator"
notify = false

[jobs.beta]
enabled = true
schedule = "cron"
cron = "0 3 1 1 *"
task = "job beta"
notify = false
"""

_ONCE_SIBLING_TOML = """\
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "job alpha"
notify = false

[jobs.reminder]
enabled = true
schedule = "once"
run_at = "03:00"
task = "once reminder"
notify = false
"""

_LEGACY_JOBS = {
    "legacyjob": {
        "schedule_type": "cron",
        "cron": "0 4 1 1 *",
        "task": "legacy task",
        "enabled": True,
        "notify": False,
    },
}

MakeScheduler = Callable[..., tuple[Scheduler, Path, Path]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _parse(data: bytes) -> dict:
    """Parse scheduler bytes with the scheduler's own parser."""
    return scheduler_module.tomli.loads(data.decode("utf-8"))


def _snapshot(sched: Scheduler) -> dict:
    """Capture the published map, document, hash, registrations, and generation."""
    with sched._running_lock:
        return {
            "jobs_meta": copy.deepcopy(sched._jobs_meta),
            "document": copy.deepcopy(sched._load_result.document),
            "content_hash": sched._load_result.content_hash,
            "status": sched._load_result.status,
            "registrations": list(schedule.default_scheduler.jobs),
            "generation": sched._config_generation,
        }


class _ReloadGate:
    """Hold the reload thread's first ``croniter.get_next``; count every reload-thread call."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.timed_out = False
        self.calls = 0
        self._guard = threading.Lock()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap the external driver; other threads and later calls delegate unheld."""
        real_get_next = scheduler_module.croniter.get_next

        def get_next(driver: object, *args: object, **kwargs: object) -> object:
            if threading.current_thread().name == _RELOAD_WORKER:
                with self._guard:
                    self.calls += 1
                    first = self.calls == 1
                if first:
                    self.reached.set()
                    try:
                        self.timed_out = not self.release.wait(timeout=_BOUND)
                    finally:
                        self.exited.set()
            return real_get_next(driver, *args, **kwargs)

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


def _join(*threads: threading.Thread) -> list[threading.Thread]:
    """Join every thread (bounded) and return those still alive."""
    for thread in threads:
        thread.join(timeout=_BOUND)
    return [thread for thread in threads if thread.is_alive()]


def _held_reload(
    sched: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
    while_held: Callable[[], object],
) -> dict:
    """Hold reload in its first trigger preparation, run *while_held*, then release.

    Returns the reload outcome, *while_held*'s return value, whether reload
    was still held (not released, not expired) after *while_held* returned,
    the reload-thread driver call count, whether the hold expired, and the
    threads still alive after bounded joins.
    """
    gate = _ReloadGate()
    gate.install(monkeypatch)
    reloader = None
    observed: object = None
    still_held = False
    try:
        reloader, reloaded = _start(_RELOAD_WORKER, sched.reload)
        assert gate.reached.wait(_BOUND), "reload never reached its trigger preparation"
        observed = while_held()
        still_held = reloader.is_alive() and not gate.exited.is_set()
    finally:
        gate.release.set()
        alive = _join(reloader) if reloader is not None else []
    return {
        "reloaded": reloaded,
        "observed": observed,
        "still_held": still_held,
        "driver_calls": gate.calls,
        "expired": gate.timed_out,
        "alive": alive,
    }


def _assert_reload_again(outcome: dict, path: Path) -> str:
    """Assert a controlled reload-again failure naming *path* and no file contents."""
    assert "error" not in outcome, f"reload raised {outcome.get('error')!r}"
    result = outcome["result"]
    error = result.get("error")
    assert isinstance(error, str), f"reload did not fail: {result!r}"
    assert "reload again" in error.lower()
    assert str(path) in error
    assert _SECRET not in error
    return error


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
    """Construct real Schedulers (loop off) over temporary XDG paths.

    Returns ``(scheduler, scheduler.toml path, legacy JSON path)``; the
    legacy file is written only when *legacy* is given.
    """
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("atomic-reload-test")
    assert str(paths.model_selection_file).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(text: str, *, legacy: dict | None = None) -> tuple[Scheduler, Path, Path]:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.state_home.mkdir(parents=True, exist_ok=True)
        paths.scheduler_config.write_text(text, encoding="utf-8")
        if legacy is not None:
            paths.scheduler_jobs.write_text(json.dumps(legacy), encoding="utf-8")
        sched = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(paths.scheduler_config),
        )
        created.append(sched)
        return sched, paths.scheduler_config, paths.scheduler_jobs

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()
            assert not sched._thread.is_alive(), "the scheduler loop did not stop"


# ---------------------------------------------------------------------------
# Latch foundation
# ---------------------------------------------------------------------------


def test_fresh_scheduler_reload_required_latch_starts_clear(
    make_scheduler: MakeScheduler,
) -> None:
    """A constructed scheduler carries the private in-memory latch, initially clear."""
    sched, _, _ = make_scheduler(_BASE_TOML)

    assert getattr(sched, "_reload_required", _MISSING) is False


# ---------------------------------------------------------------------------
# A legitimate operator edit loads from its own bytes
# ---------------------------------------------------------------------------


def test_operator_edit_reload_publishes_own_bytes_without_rewrite(
    make_scheduler: MakeScheduler,
) -> None:
    """Document, hash, and view come from the edited bytes; the file is not rewritten."""
    sched, toml_path, _ = make_scheduler(_BASE_TOML)
    stored_before = sched._load_result.content_hash
    toml_path.write_text(_EDITED_TOML, encoding="utf-8")
    edited = toml_path.read_bytes()
    assert _sha256(edited) != stored_before

    result = sched.reload()

    assert result == {"reloaded": 2, "failed": 0}
    assert sched._load_result.content_hash == _sha256(edited)
    assert sched._load_result.document == _parse(edited)
    assert set(sched._jobs_meta) == {"alpha", "beta"}
    assert sched._jobs_meta["alpha"]["task"] == "job alpha edited by the operator"
    assert toml_path.read_bytes() == edited
    assert sorted(p.name for p in toml_path.parent.glob("scheduler.toml.bak.*")) == []


def test_mutation_after_operator_edit_reload_is_not_an_external_edit(
    make_scheduler: MakeScheduler,
) -> None:
    """After loading an operator edit, the next ordinary mutation commits."""
    sched, toml_path, _ = make_scheduler(_BASE_TOML)
    toml_path.write_text(_EDITED_TOML, encoding="utf-8")
    assert "error" not in sched.reload()

    result = sched.modify_job("beta", {"task": "changed after reload"}, _on_attempt=MagicMock())

    assert result == {"success": True}
    on_disk = toml_path.read_bytes()
    assert _parse(on_disk)["jobs"]["beta"]["task"] == "changed after reload"
    assert sched._load_result.content_hash == _sha256(on_disk)


def test_parsed_candidate_is_published_only_after_preparation(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While reload prepares, last-good state stays published; release then loads the edit."""
    sched, toml_path, _ = make_scheduler(_BASE_TOML)
    last_good = _snapshot(sched)
    toml_path.write_text(_EDITED_TOML, encoding="utf-8")
    edited = toml_path.read_bytes()

    run = _held_reload(sched, monkeypatch, lambda: _snapshot(sched))

    assert run["alive"] == []
    assert not run["expired"], "the held reload expired instead of being released"
    assert run["still_held"]
    assert run["observed"] == last_good
    assert run["reloaded"] == {"result": {"reloaded": 2, "failed": 0}}
    assert sched._load_result.content_hash == _sha256(edited)
    assert set(sched._jobs_meta) == {"alpha", "beta"}
    assert sched._config_generation > last_good["generation"]


# ---------------------------------------------------------------------------
# A mutation committed between parse and publish is never overwritten
# ---------------------------------------------------------------------------


_MUTATIONS: dict[str, Callable[[Scheduler], object]] = {
    "modify_task": lambda s: s.modify_job(
        "alpha", {"task": "committed during reload"}, _on_attempt=MagicMock(),
    ),
    "add_job": lambda s: s.add_job(
        "gamma", "cron", "added during reload", cron="0 5 1 1 *", _on_attempt=MagicMock(),
    ),
}


class TestMutationDuringReloadPreparation:
    """A mutation that commits while reload prepares makes reload fail; nothing retries."""

    @pytest.mark.parametrize("mutation", sorted(_MUTATIONS))
    def test_reload_fails_reload_again_and_keeps_committed_state(
        self,
        make_scheduler: MakeScheduler,
        monkeypatch: pytest.MonkeyPatch,
        mutation: str,
    ) -> None:
        """The committed mutation's file and published state survive; one preparation only."""
        sched, toml_path, _ = make_scheduler(_BASE_TOML)
        latch_before = getattr(sched, "_reload_required", _MISSING)

        def mutate_then_snapshot() -> dict:
            worker, outcome = _start(
                "atomic-mutation-during-reload", lambda: _MUTATIONS[mutation](sched),
            )
            alive = _join(worker)
            return {
                "alive": alive,
                "outcome": outcome,
                "state": _snapshot(sched),
                "disk": toml_path.read_bytes(),
                "notice": sched.load_notice(),
            }

        run = _held_reload(sched, monkeypatch, mutate_then_snapshot)
        committed = run["observed"]

        assert run["alive"] == [] and committed["alive"] == []
        assert not run["expired"], "the held reload expired instead of being released"
        assert run["still_held"], "the mutation did not finish while reload was held"
        assert committed["outcome"] == {"result": {"success": True}}
        _assert_reload_again(run["reloaded"], toml_path)
        assert _snapshot(sched) == committed["state"]
        assert toml_path.read_bytes() == committed["disk"]
        assert sched._load_result.content_hash == _sha256(committed["disk"])
        assert sched.load_notice() == committed["notice"]
        assert run["driver_calls"] == 1, "reload re-prepared its candidate"
        assert getattr(sched, "_reload_required", _MISSING) is latch_before


# ---------------------------------------------------------------------------
# The legacy JSON is only existence-checked; changing it never fails a reload (8.4)
# ---------------------------------------------------------------------------


def _legacy_rewritten(path: Path) -> None:
    """Valid legacy JSON with different content."""
    changed = copy.deepcopy(_LEGACY_JOBS)
    changed["legacyjob"]["task"] = "legacy task changed"
    path.write_text(json.dumps(changed), encoding="utf-8")


def _legacy_whitespace_only(path: Path) -> None:
    """Different bytes that parse to the identical JSON value."""
    path.write_text(json.dumps(_LEGACY_JOBS, indent=4) + "\n", encoding="utf-8")


def _legacy_deleted(path: Path) -> None:
    """Valid legacy JSON removed."""
    path.unlink()


def _legacy_created(path: Path) -> None:
    """A legacy file appears where none existed."""
    path.write_text(json.dumps(_LEGACY_JOBS), encoding="utf-8")


_LEGACY_CHANGES = {
    "valid_to_different_valid": (True, _legacy_rewritten),
    "valid_to_whitespace_variant": (True, _legacy_whitespace_only),
    "valid_to_missing": (True, _legacy_deleted),
    "absent_to_created": (False, _legacy_created),
}


class TestLegacyChangeDuringReload:
    """Task 8.4: the legacy JSON is never fingerprinted, so changing it never fails a reload.

    The reload still publishes only the candidate it parsed from scheduler.toml; the state-home
    file is only checked for existence when the candidate is published (notice set or cleared).
    """

    @pytest.mark.parametrize("change", sorted(_LEGACY_CHANGES))
    def test_reload_still_publishes_and_the_legacy_file_is_untouched(
        self,
        make_scheduler: MakeScheduler,
        monkeypatch: pytest.MonkeyPatch,
        change: str,
    ) -> None:
        """No reload-again failure; no legacy job published; exactly one candidate preparation."""
        initially_present, apply_change = _LEGACY_CHANGES[change]
        sched, toml_path, legacy_path = make_scheduler(
            _BASE_TOML, legacy=_LEGACY_JOBS if initially_present else None,
        )
        assert "legacyjob" not in sched._jobs_meta
        generation_before = sched._config_generation
        toml_bytes = toml_path.read_bytes()

        def change_legacy() -> object:
            apply_change(legacy_path)
            return legacy_path.read_bytes() if legacy_path.exists() else None

        run = _held_reload(sched, monkeypatch, change_legacy)
        legacy_after = run["observed"]

        assert run["alive"] == []
        assert not run["expired"], "the held reload expired instead of being released"
        assert run["still_held"]
        assert "error" not in run["reloaded"], run["reloaded"]
        assert run["reloaded"]["result"] == {"reloaded": 1, "failed": 0}
        assert sched._config_generation == generation_before + 1
        assert "legacyjob" not in sched._jobs_meta
        assert toml_path.read_bytes() == toml_bytes
        assert run["driver_calls"] == 1, "reload re-prepared its candidate"
        if legacy_after is None:
            assert not legacy_path.exists()
            assert sched.load_notice() is None
        else:
            assert legacy_path.read_bytes() == legacy_after
            assert str(legacy_path) in (sched.load_notice() or "")


# ---------------------------------------------------------------------------
# The RELOAD_REQUIRED latch across failed and successful reloads
# ---------------------------------------------------------------------------


def _external_toml_edit_while_held(
    sched: Scheduler, toml_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Reload while the operator edits scheduler.toml during its preparation."""
    run = _held_reload(
        sched, monkeypatch, lambda: toml_path.write_text(_EDITED_TOML, encoding="utf-8"),
    )
    assert run["alive"] == []
    assert not run["expired"], "the held reload expired instead of being released"
    assert run["still_held"]
    _assert_reload_again(run["reloaded"], toml_path)
    return run["reloaded"]


def _unparseable_file(
    sched: Scheduler, toml_path: Path, _monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Reload a scheduler.toml that is no longer valid TOML."""
    toml_path.write_text("[jobs.alpha\n", encoding="utf-8")
    outcome = {"result": sched.reload()}
    assert isinstance(outcome["result"].get("error"), str)
    return outcome


_FAILED_RELOADS = {
    "toml_race": _external_toml_edit_while_held,
    "unparseable_file": _unparseable_file,
}


class TestFailedReloadPreservesLatch:
    """A failed reload never toggles the latch and never publishes a generation."""

    @pytest.mark.parametrize("latch", [False, True], ids=["latch_clear", "latch_set"])
    @pytest.mark.parametrize("failure", sorted(_FAILED_RELOADS))
    def test_latch_and_last_good_state_unchanged(
        self,
        make_scheduler: MakeScheduler,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
        latch: bool,
    ) -> None:
        """A clear latch stays clear, a set latch stays set; map/document/hash stay last-good."""
        sched, toml_path, _ = make_scheduler(_BASE_TOML)
        sched._reload_required = latch
        last_good = _snapshot(sched)

        _FAILED_RELOADS[failure](sched, toml_path, monkeypatch)

        assert sched._reload_required is latch
        assert _snapshot(sched) == last_good


def test_successful_reload_clears_set_latch(make_scheduler: MakeScheduler) -> None:
    """A reload that publishes its candidate clears a set latch."""
    sched, _, _ = make_scheduler(_BASE_TOML)
    sched._reload_required = True

    result = sched.reload()

    assert result == {"reloaded": 1, "failed": 0}
    assert sched._reload_required is False


# ---------------------------------------------------------------------------
# Per-job registration failure stays contained at the atomic reload boundary
# ---------------------------------------------------------------------------


def test_reload_registration_failure_is_per_job_config_error(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing trigger driver marks only that job; the healthy sibling is published."""
    sched, toml_path, _ = make_scheduler(_ONCE_SIBLING_TOML)
    real_do = schedule.Job.do
    reached: list[str] = []

    def do(job: schedule.Job, job_func: Callable, *args: object, **kwargs: object) -> object:
        if threading.current_thread().name == _RELOAD_WORKER:
            reached.append(kwargs.get("tag", ""))
            raise RuntimeError(_SECRET)
        return real_do(job, job_func, *args, **kwargs)

    monkeypatch.setattr(schedule.Job, "do", do)

    worker, outcome = _start(_RELOAD_WORKER, sched.reload)
    assert _join(worker) == []

    assert reached, "the external schedule.Job.do fault was never reached"
    assert outcome == {"result": {"reloaded": 1, "failed": 1}}
    error = sched._jobs_meta["reminder"].get("config_error")
    assert isinstance(error, str) and error.startswith("Registration failed (RuntimeError)")
    assert _SECRET not in error
    assert "config_error" not in sched._jobs_meta["alpha"]
    assert "_next_run" in sched._jobs_meta["alpha"]
    assert [j for j in schedule.default_scheduler.jobs if "reminder" in j.tags] == []
    assert sched._load_result.content_hash == _sha256(toml_path.read_bytes())
