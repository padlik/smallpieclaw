"""
Legacy ``scheduler_jobs.json`` jobs are runnable and read-only (task 12.7).

The legacy JSON file is read non-destructively into the runtime view: it is
never written, deleted, renamed, adopted into scheduler.toml, or tombstoned,
and a scheduler.toml job wins a duplicate tag. A JSON-only job runs through
normal admission, and only its returned schedule prose carries the literal
``legacy, read-only`` marker (no new public fields). Modify, remove, pause,
and resume of a JSON-only job are refused before the attempt with the legacy
file path and manual-edit/reload guidance. Removing a scheduler.toml job is
refused while the CURRENT legacy file on disk shadows its tag or cannot be
ruled out (unreadable or malformed); the legacy fingerprint read by that one
preflight is rechecked under the commit locks, and a change after the
preflight is an executed failure with no change, even across an internal
re-preparation of a stale candidate. A bad legacy file raises an operator
notice without blocking ordinary scheduler.toml changes.

Only external boundaries are gated: ``open`` on the legacy path in the
removal worker thread, and the external TOML parser in that thread. The
scheduler, builtin tool, Telegram handler, parser, and commit are real; tests
never call or patch private scheduler methods (private state is read only for
observation snapshots). Every test runs on an isolated
``schedule.default_scheduler`` (and ``schedule.jobs``) with every XDG
directory under ``tmp_path``. Every wait and join is bounded, every gate is
released in ``finally``, and an expired gate fails the test.
"""

from __future__ import annotations

import asyncio
import builtins
import copy
import html
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from builtin_tools.schedule import exec_schedule
from outcome_utils import read_execution
from scheduler import Scheduler
from xdg import xdg_paths


_BOUND = 10.0
_SECRET = "fake-secret-sentinel-legacy-4c1e"
_MARKER = "legacy, read-only"
_REMOVE_WORKER = "legacy-removal-worker"

# Sentinel: create the legacy path as a directory (unreadable as a file).
_LEGACY_DIR = object()

# Far-future crons (1 January) so nothing fires during a test.
_TOML = """\
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

# A scheduler.toml job whose raw values imitate legacy origin markers.
_SPOOF_TOML = _TOML + """
[jobs.spoofed]
enabled = true
schedule = "cron"
cron = "0 6 1 1 *"
task = "spoofed task"
notify = false
source = "dynamic"
_legacy_read_only = true
"""

_LEGACY = {
    "legacyjob": {
        "schedule_type": "cron",
        "cron": "0 4 1 1 *",
        "task": f"legacy task {_SECRET}",
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

_UNRELATED_LEGACY = {"legacyjob": _LEGACY["legacyjob"]}

_SHADOW_ENTRY = {
    "schedule_type": "cron",
    "cron": "0 7 1 1 *",
    "task": f"legacy shadow {_SECRET}",
    "enabled": True,
    "notify": False,
}

_SHADOW_LEGACY = {"alpha": _SHADOW_ENTRY, "dormant": _SHADOW_ENTRY}

_MALFORMED_LEGACY = b'{"legacyjob": {"task": "' + _SECRET.encode() + b'"'

_SHOW_FIELDS = frozenset({
    "tag", "task", "schedule", "schedule_type", "cron", "run_at", "next_run",
    "last_run", "last_error", "notify", "model", "preserve_context",
    "context_max_messages", "overlap_policy", "source", "enabled", "is_running",
    "created_at",
})

_LIST_FIELDS = frozenset({
    "tag", "schedule", "schedule_type", "cron", "next_run", "enabled", "last_run",
    "last_error", "task", "notify", "source", "model", "preserve_context",
    "is_running", "config_error",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_bytes(jobs: dict) -> bytes:
    """Return operator-formatted legacy JSON bytes (two-space indent)."""
    return (json.dumps(jobs, indent=2) + "\n").encode("utf-8")


def _whitespace_variant(jobs: dict) -> bytes:
    """Return different bytes that parse to the identical legacy JSON value."""
    return (json.dumps(jobs, indent=4) + "\n\n").encode("utf-8")


def _write_legacy(path: Path, legacy: object) -> None:
    """Write *legacy* (None: absent, dict: JSON, bytes: raw, _LEGACY_DIR: directory)."""
    if legacy is None:
        return
    if legacy is _LEGACY_DIR:
        path.mkdir()
    elif isinstance(legacy, bytes):
        path.write_bytes(legacy)
    else:
        path.write_bytes(_legacy_bytes(legacy))


def _file_state(path: Path) -> tuple:
    """Return the kind of *path* and, for a regular file, its exact bytes."""
    if path.is_dir():
        return ("dir",)
    if not path.exists():
        return ("missing",)
    return ("file", path.read_bytes())


def _parse_toml(path: Path) -> dict:
    """Parse the scheduler file with the scheduler's own parser."""
    return scheduler_module.tomli.loads(path.read_text(encoding="utf-8"))


def _tags(sched: Scheduler) -> list[str]:
    """Return the public list of job tags."""
    return [job["tag"] for job in sched.list_jobs()]


class _Env:
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
        state["toml"] = _file_state(self.toml_path)
        state["legacy"] = _file_state(self.legacy_path)
        state["backups"] = sorted(
            p.name for p in self.toml_path.parent.glob(self.toml_path.name + ".bak.*")
        )
        return state


MakeScheduler = Callable[..., _Env]


def _start(
    name: str, call: Callable[[], object], done: Optional[threading.Event] = None,
) -> tuple[threading.Thread, dict]:
    """Run *call* in a named daemon thread; its result or exception lands in the dict."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except Exception as exc:  # noqa: BLE001 - captured, then asserted absent
            outcome["error"] = exc
        finally:
            if done is not None:
                done.set()

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, outcome


def _join(*threads: threading.Thread) -> list[threading.Thread]:
    """Join every thread (bounded) and return those still alive."""
    for thread in threads:
        thread.join(timeout=_BOUND)
    return [thread for thread in threads if thread.is_alive()]


def _assert_legacy_guidance(error: str, legacy_path: Path) -> None:
    """Require the legacy path plus manual-edit and reload steps, no file content."""
    lower = error.lower()
    assert str(legacy_path) in error, error
    assert "reload" in lower, error
    assert "edit" in lower or "manual" in lower, error
    assert "not found" not in lower, error
    assert _SECRET not in error


def _assert_actionable_unresolved(error: str, legacy_path: Path) -> None:
    """Require the legacy path and a repair step for an unreadable/malformed file."""
    lower = error.lower()
    assert str(legacy_path) in error, error
    assert any(word in lower for word in ("repair", "fix", "edit", "check", "remove")), error
    assert "not found" not in lower, error
    assert _SECRET not in error


def _run_telegram_jobs(sched: Scheduler, args: list[str]) -> str:
    """Drive the authorized real /jobs handler with external Telegram replies captured."""
    from telegram_commands import cmd_jobs
    from telegram_interface import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.scheduler = sched
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_message=message)
    asyncio.run(cmd_jobs(iface, update, SimpleNamespace(args=args)))
    return "\n".join(call.args[0] for call in message.reply_text.await_args_list)


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
    """Construct real Schedulers over temporary XDG paths (loop off unless *enabled*).

    ``toml=None`` leaves scheduler.toml missing; *legacy* is written as
    described by :func:`_write_legacy`. Started loops are stopped and joined.
    """
    assert schedule.default_scheduler is isolated_schedule
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("legacy-readonly-test")
    assert str(paths.models_config_file).startswith(str(tmp_path))
    assert str(paths.scheduler_jobs).startswith(str(tmp_path))
    created: list[Scheduler] = []

    def _make(
        toml: Optional[str] = _TOML, *, legacy: object = None, enabled: bool = False,
    ) -> _Env:
        paths.config_home.mkdir(parents=True, exist_ok=True)
        paths.state_home.mkdir(parents=True, exist_ok=True)
        if toml is not None:
            paths.scheduler_config.write_text(toml, encoding="utf-8")
        _write_legacy(paths.scheduler_jobs, legacy)
        notify = MagicMock()
        sched = Scheduler(
            config={"scheduler": {"enabled": enabled}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=notify,
            agent_fn=MagicMock(),
            paths=paths,
            scheduler_config_path=str(paths.scheduler_config),
        )
        created.append(sched)
        return _Env(sched, paths.scheduler_config, paths.scheduler_jobs, notify)

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()
            assert not sched._thread.is_alive(), "the scheduler loop did not stop"


# ---------------------------------------------------------------------------
# A. Non-destructive load, runnable legacy jobs, and the read-only marker
# ---------------------------------------------------------------------------


class TestNonDestructiveLoad:
    """Loading and reloading legacy jobs never writes either file."""

    @pytest.mark.parametrize("toml", [_TOML, None], ids=["toml_present", "toml_missing"])
    def test_load_and_reload_keep_legacy_bytes_without_adoption(
        self, make_scheduler: MakeScheduler, toml: Optional[str],
    ) -> None:
        """Legacy jobs are visible; both files keep their bytes; no TOML is created."""
        env = make_scheduler(toml, legacy=_LEGACY)

        reloaded = env.sched.reload()

        assert "error" not in reloaded, reloaded
        assert {"legacyjob", "legacypaused"} <= set(_tags(env.sched))
        assert env.legacy_path.read_bytes() == _legacy_bytes(_LEGACY)
        if toml is None:
            assert not env.toml_path.exists()
        else:
            assert env.toml_path.read_text(encoding="utf-8") == toml
        assert list(env.toml_path.parent.glob(env.toml_path.name + ".bak.*")) == []

    def test_toml_job_wins_duplicate_tag(self, make_scheduler: MakeScheduler) -> None:
        """The scheduler.toml definition is the only one visible, unmarked."""
        duplicate = {"alpha": dict(_SHADOW_ENTRY, task="legacy alpha duplicate")}
        env = make_scheduler(_TOML, legacy=duplicate)

        shown = env.sched.show_job("alpha")

        assert _tags(env.sched).count("alpha") == 1
        assert shown["task"] == "toml alpha"
        assert shown["cron"] == "0 2 1 1 *"
        assert shown["source"] == "config"
        assert _MARKER not in shown["schedule"]
        assert env.legacy_path.read_bytes() == _legacy_bytes(duplicate)


def test_run_now_admits_legacy_reminder_normally(make_scheduler: MakeScheduler) -> None:
    """A JSON-only empty-task job runs through admission and delivers its reminder."""
    reminder = {
        "legacy_reminder": {
            "schedule_type": "cron", "cron": "0 4 1 1 *", "task": "",
            "enabled": True, "notify": True,
        },
    }
    env = make_scheduler(_TOML, legacy=reminder)
    delivered = threading.Event()
    messages: list[str] = []
    runners: list[threading.Thread] = []

    def notify(message: str) -> None:
        messages.append(message)
        runners.append(threading.current_thread())
        delivered.set()

    env.notify.side_effect = notify

    result = env.sched.run_now("legacy_reminder")

    assert result == {"success": True}
    assert delivered.wait(_BOUND), "the legacy reminder run never completed"
    assert _join(*runners) == []
    assert any("Reminder" in m and "legacy reminder" in m for m in messages), messages
    shown = env.sched.show_job("legacy_reminder")
    assert shown["is_running"] is False
    assert shown["last_run"] is not None
    assert env.legacy_path.read_bytes() == _legacy_bytes(reminder)


def _native_list_schedule(env: _Env) -> str:
    return next(job["schedule"] for job in env.sched.list_jobs() if job["tag"] == "legacyjob")


def _builtin_list_line(env: _Env) -> str:
    output = exec_schedule(env.sched, {"action": "list"})["output"]
    return next(line for line in output.splitlines() if " legacyjob (" in line)


def _builtin_show_line(env: _Env) -> str:
    output = exec_schedule(env.sched, {"action": "show", "tag": "legacyjob"})["output"]
    return next(line for line in output.splitlines() if line.startswith("schedule:"))


_MARKED_SURFACES: dict[str, Callable[[_Env], str]] = {
    "native_list": _native_list_schedule,
    "builtin_list": _builtin_list_line,
    "builtin_show": _builtin_show_line,
}


@pytest.mark.parametrize("surface", sorted(_MARKED_SURFACES))
def test_json_only_job_schedule_prose_carries_marker(
    make_scheduler: MakeScheduler, surface: str,
) -> None:
    """The native schedule prose keeps its text and appends the literal marker."""
    env = make_scheduler(_TOML, legacy=_LEGACY)

    rendered = _MARKED_SURFACES[surface](env)

    assert "cron: 0 4 1 1 *" in rendered
    assert _MARKER in rendered


@pytest.mark.parametrize("tag", ["alpha", "legacyjob"])
def test_public_job_field_sets_are_unchanged(make_scheduler: MakeScheduler, tag: str) -> None:
    """No new public list/show fields are introduced for either origin."""
    env = make_scheduler(_TOML, legacy=_LEGACY)

    listed = next(job for job in env.sched.list_jobs() if job["tag"] == tag)

    assert set(env.sched.show_job(tag)) == _SHOW_FIELDS
    assert set(listed) == _LIST_FIELDS


def test_healthy_toml_schedule_prose_is_unmarked(make_scheduler: MakeScheduler) -> None:
    """A scheduler.toml job keeps its compact native prose beside legacy jobs."""
    env = make_scheduler(_TOML, legacy=_LEGACY)

    listed = next(job for job in env.sched.list_jobs() if job["tag"] == "alpha")

    assert env.sched.show_job("alpha")["schedule"] == "cron: 0 2 1 1 *"
    assert listed["schedule"] == "cron: 0 2 1 1 *"


class TestOriginSpoofGuard:
    """Read-only origin comes from the legacy file, never from raw values."""

    def test_toml_job_with_legacy_lookalike_values_is_writable(
        self, make_scheduler: MakeScheduler,
    ) -> None:
        """``source = "dynamic"`` plus a raw private flag in scheduler.toml stays writable."""
        env = make_scheduler(_SPOOF_TOML, legacy=_UNRELATED_LEGACY)

        out = exec_schedule(env.sched, {"action": "modify", "tag": "spoofed", "task": "edited"})

        assert out["success"] is True, out
        assert read_execution(out) == "executed"
        assert _parse_toml(env.toml_path)["jobs"]["spoofed"]["task"] == "edited"

    def test_toml_job_with_legacy_lookalike_values_is_unmarked(
        self, make_scheduler: MakeScheduler,
    ) -> None:
        """The lookalike scheduler.toml job carries no read-only marker."""
        env = make_scheduler(_SPOOF_TOML, legacy=_UNRELATED_LEGACY)

        assert env.sched.show_job("spoofed")["schedule"] == "cron: 0 6 1 1 *"

    def test_legacy_raw_flag_cannot_disable_protection(
        self, make_scheduler: MakeScheduler,
    ) -> None:
        """A JSON-only job claiming ``source = config`` and a false flag stays read-only."""
        flagged = {
            "flagged": dict(
                _LEGACY["legacyjob"], source="config", _legacy_read_only=False,
            ),
        }
        env = make_scheduler(_TOML, legacy=flagged)
        before = env.snapshot()
        hook = MagicMock()

        result = env.sched.modify_job("flagged", {"task": "changed"}, _on_attempt=hook)

        assert result["success"] is False, result
        assert hook.call_count == 0
        assert env.snapshot() == before


# ---------------------------------------------------------------------------
# B. JSON-only jobs are refused before the attempt
# ---------------------------------------------------------------------------

_JSON_ONLY_TARGETS = {
    "modify": "legacyjob",
    "remove": "legacyjob",
    "pause": "legacyjob",
    "resume": "legacypaused",
}


def _native_change(sched: Scheduler, action: str, hook: MagicMock) -> object:
    """Call the real native API for *action* on its JSON-only target."""
    tag = _JSON_ONLY_TARGETS[action]
    if action == "modify":
        return sched.modify_job(tag, {"task": "changed"}, _on_attempt=hook)
    if action == "remove":
        return sched.remove_job(tag)
    if action == "pause":
        return sched.pause_job(tag)
    return sched.resume_job(tag)


def _builtin_args(action: str) -> dict:
    """Return the builtin tool arguments for *action* on its JSON-only target."""
    args = {"action": action, "tag": _JSON_ONLY_TARGETS[action]}
    if action == "modify":
        args["task"] = "changed"
    return args


class TestJsonOnlyMutationRefused:
    """Modify/remove/pause/resume of a JSON-only job change nothing anywhere."""

    @pytest.mark.parametrize("action", sorted(_JSON_ONLY_TARGETS))
    def test_native_api_refuses_without_change(
        self, make_scheduler: MakeScheduler, action: str,
    ) -> None:
        """The native call fails, never reaches the modify hook, and changes no state."""
        env = make_scheduler(_TOML, legacy=_LEGACY)
        before = env.snapshot()
        hook = MagicMock()

        result = _native_change(env.sched, action, hook)

        if isinstance(result, bool):
            assert result is False
        else:
            assert result["success"] is False, result
        assert hook.call_count == 0
        assert env.snapshot() == before

    @pytest.mark.parametrize("action", sorted(_JSON_ONLY_TARGETS))
    def test_builtin_refuses_with_legacy_guidance(
        self, make_scheduler: MakeScheduler, action: str,
    ) -> None:
        """The tool result is a refused failure naming the legacy file and the manual steps."""
        env = make_scheduler(_TOML, legacy=_LEGACY)
        before = env.snapshot()

        out = exec_schedule(env.sched, _builtin_args(action))

        assert out["success"] is False, out
        assert read_execution(out) == "refused"
        _assert_legacy_guidance(out["error"], env.legacy_path)
        assert env.snapshot() == before


def test_telegram_remove_of_json_only_job_reports_legacy_guidance(
    make_scheduler: MakeScheduler,
) -> None:
    """``/jobs remove`` neither claims removal nor misreports the job as not found."""
    env = make_scheduler(_TOML, legacy=_LEGACY)

    text = _run_telegram_jobs(env.sched, ["remove", "legacyjob"])

    assert html.escape(str(env.legacy_path)) in text
    assert "not found" not in text.lower()
    assert "legacyjob</code> removed" not in text
    assert env.legacy_path.read_bytes() == _legacy_bytes(_LEGACY)


# ---------------------------------------------------------------------------
# C. Removing a scheduler.toml job checks the CURRENT legacy file
# ---------------------------------------------------------------------------

# name -> (legacy at construction, legacy written after construction)
_REMOVAL_ALLOWED = {
    "legacy_missing": (None, None),
    "unrelated_legacy_job": (_UNRELATED_LEGACY, None),
}

_REMOVAL_REFUSED = {
    "matching_tag_at_load": (_SHADOW_LEGACY, None),
    "matching_tag_added_after_load": (None, {"alpha": _SHADOW_ENTRY}),
    "matching_raw_tag_null": ({"alpha": None}, None),
    "malformed_json": (_MALFORMED_LEGACY, None),
    "unreadable_directory": (_LEGACY_DIR, None),
}

_SHADOW_REFUSALS = {"matching_tag_at_load", "matching_tag_added_after_load", "matching_raw_tag_null"}


class TestTomlRemovalShadowPreflight:
    """The removal preflight reads the legacy file on disk, never the cached map."""

    @pytest.mark.parametrize("case", sorted(_REMOVAL_ALLOWED))
    def test_removal_proceeds_without_current_shadow(
        self, make_scheduler: MakeScheduler, case: str,
    ) -> None:
        """A missing legacy file or an unrelated legacy job allows a normal removal."""
        initial, late = _REMOVAL_ALLOWED[case]
        env = make_scheduler(_TOML, legacy=initial)
        _write_legacy(env.legacy_path, late)
        legacy_before = _file_state(env.legacy_path)

        out = exec_schedule(env.sched, {"action": "remove", "tag": "alpha"})

        assert out["success"] is True, out
        assert read_execution(out) == "executed"
        assert "alpha" not in _parse_toml(env.toml_path)["jobs"]
        assert "alpha" not in _tags(env.sched)
        assert _file_state(env.legacy_path) == legacy_before

    @pytest.mark.parametrize("case", sorted(_REMOVAL_REFUSED))
    def test_removal_refused_by_current_legacy_file(
        self, make_scheduler: MakeScheduler, case: str,
    ) -> None:
        """A shadowing, unreadable, or malformed legacy file refuses with no change."""
        initial, late = _REMOVAL_REFUSED[case]
        env = make_scheduler(_TOML, legacy=initial)
        _write_legacy(env.legacy_path, late)
        before = env.snapshot()

        out = exec_schedule(env.sched, {"action": "remove", "tag": "alpha"})

        assert out["success"] is False, out
        assert read_execution(out) == "refused"
        if case in _SHADOW_REFUSALS:
            _assert_legacy_guidance(out["error"], env.legacy_path)
        else:
            _assert_actionable_unresolved(out["error"], env.legacy_path)
        assert env.snapshot() == before

    def test_native_removal_refused_by_late_shadow(
        self, make_scheduler: MakeScheduler,
    ) -> None:
        """The boolean API also refuses a shadow the operator added without reloading."""
        env = make_scheduler(_TOML, legacy=None)
        _write_legacy(env.legacy_path, {"alpha": _SHADOW_ENTRY})
        before = env.snapshot()

        assert env.sched.remove_job("alpha") is False
        assert env.snapshot() == before


_SHADOW_CHANGES = {
    "modify": ({"action": "modify", "tag": "alpha", "task": "shadow modified"},
               ("alpha", "task", "shadow modified")),
    "pause": ({"action": "pause", "tag": "alpha"}, ("alpha", "enabled", False)),
    "resume": ({"action": "resume", "tag": "dormant"}, ("dormant", "enabled", True)),
}


@pytest.mark.parametrize("change", sorted(_SHADOW_CHANGES))
def test_shadowing_toml_job_change_proceeds(make_scheduler: MakeScheduler, change: str) -> None:
    """Modify, pause, and resume of a shadowing scheduler.toml job are applied normally."""
    args, (tag, field, value) = _SHADOW_CHANGES[change]
    env = make_scheduler(_TOML, legacy=_SHADOW_LEGACY)

    out = exec_schedule(env.sched, args)

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    assert _parse_toml(env.toml_path)["jobs"][tag][field] == value
    assert env.legacy_path.read_bytes() == _legacy_bytes(_SHADOW_LEGACY)


# ---------------------------------------------------------------------------
# D. The preflight's legacy fingerprint is rechecked under the commit locks
# ---------------------------------------------------------------------------


class _LegacyOpenGate:
    """Hold the removal worker's Nth open of the legacy file, then delegate the real open.

    Other threads and other paths always delegate unheld. ``signal`` is set
    when the hold is reached (or by the caller when the worker finishes), so
    a removal that never re-reads the legacy file is reported, not waited out.
    """

    def __init__(self, sched: Scheduler, legacy_path: Path, hold_at: int) -> None:
        self._sched = sched
        self._target = os.path.realpath(legacy_path)
        self._hold_at = hold_at
        self._guard = threading.Lock()
        self.opens = 0
        self.locked_at_hold = False
        self.timed_out = False
        self.reached = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.signal = threading.Event()

    def _is_target(self, file: object) -> bool:
        if not isinstance(file, (str, bytes, os.PathLike)):
            return False
        return os.path.realpath(os.fsdecode(os.fspath(file))) == self._target

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap ``builtins.open`` and ``io.open`` (the external file boundary)."""
        real_open = builtins.open

        def gated_open(file: object, *args: object, **kwargs: object) -> object:
            if threading.current_thread().name == _REMOVE_WORKER and self._is_target(file):
                with self._guard:
                    self.opens += 1
                    hold = self.opens == self._hold_at
                if hold:
                    self.locked_at_hold = self._sched._running_lock._is_owned()
                    self.reached.set()
                    self.signal.set()
                    try:
                        self.timed_out = not self.release.wait(timeout=_BOUND)
                    finally:
                        self.exited.set()
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", gated_open)
        monkeypatch.setattr(io, "open", gated_open)


def _native_remove(env: _Env) -> object:
    return env.sched.remove_job("alpha")


def _builtin_remove(env: _Env) -> object:
    return exec_schedule(env.sched, {"action": "remove", "tag": "alpha"})


def _assert_native_failure(result: object) -> None:
    assert result is False, result


def _assert_builtin_executed_failure(result: object) -> None:
    assert isinstance(result, dict) and result["success"] is False, result
    assert read_execution(result) == "executed"


_RACE_SURFACES = {
    "native": (_native_remove, _assert_native_failure),
    "builtin": (_builtin_remove, _assert_builtin_executed_failure),
}


@pytest.mark.parametrize("surface", sorted(_RACE_SURFACES))
def test_legacy_change_after_preflight_fails_removal(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch, surface: str,
) -> None:
    """A whitespace-only legacy rewrite before the locked re-read is an executed failure."""
    call, assert_failed = _RACE_SURFACES[surface]
    env = make_scheduler(_TOML, legacy=_UNRELATED_LEGACY)
    before = env.snapshot()
    variant = _whitespace_variant(_UNRELATED_LEGACY)
    gate = _LegacyOpenGate(env.sched, env.legacy_path, hold_at=2)
    gate.install(monkeypatch)
    worker = None
    still_held = False
    try:
        worker, outcome = _start(_REMOVE_WORKER, lambda: call(env), done=gate.signal)
        assert gate.signal.wait(_BOUND), "the removal neither finished nor re-read the legacy file"
        if not gate.reached.is_set():
            pytest.fail(
                "the removal never opened the legacy file a second time (one preflight "
                f"read plus the locked fingerprint recheck); legacy opens={gate.opens}"
            )
        env.legacy_path.write_bytes(variant)
        still_held = worker.is_alive() and not gate.exited.is_set()
    finally:
        gate.release.set()
        alive = _join(worker) if worker is not None else []

    assert alive == []
    assert not gate.timed_out, "the held removal expired instead of being released"
    assert still_held, "the operator write did not land while the removal was held"
    assert gate.locked_at_hold, "the held re-read did not run under the commit locks"
    assert "error" not in outcome, f"removal raised {outcome.get('error')!r}"
    assert_failed(outcome["result"])
    after = env.snapshot()
    assert after.pop("legacy") == ("file", variant)
    before.pop("legacy")
    assert after == before


def test_generation_retry_keeps_original_legacy_proof(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale candidate re-prepared after a commit still fails on the changed legacy file."""
    env = make_scheduler(_TOML, legacy=_UNRELATED_LEGACY)
    sched = env.sched
    variant = _whitespace_variant(_UNRELATED_LEGACY)
    real_loads = scheduler_module.tomli.loads
    guard = threading.Lock()
    outside_calls: list[int] = []
    timed_out: list[bool] = []
    reached = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    signal = threading.Event()

    def loads(text: str, *args: object, **kwargs: object) -> dict:
        # Classify by the real RLock's ownership: the locked freshness parse
        # does not count as a (re)preparation.
        if (
            threading.current_thread().name == _REMOVE_WORKER
            and not sched._running_lock._is_owned()
        ):
            with guard:
                outside_calls.append(1)
                first = len(outside_calls) == 1
            if first:
                reached.set()
                signal.set()
                try:
                    timed_out.append(not release.wait(timeout=_BOUND))
                finally:
                    exited.set()
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(scheduler_module.tomli, "loads", loads)
    worker = None
    committed: dict = {}
    still_held = False
    try:
        worker, outcome = _start(_REMOVE_WORKER, lambda: sched.remove_job("alpha"), done=signal)
        assert signal.wait(_BOUND), "the removal neither finished nor prepared a candidate"
        if not reached.is_set():
            pytest.fail("the removal finished without preparing a candidate outside the locks")
        committed["modify"] = sched.modify_job("dormant", {"task": "committed meanwhile"})
        committed["toml"] = env.toml_path.read_bytes()
        env.legacy_path.write_bytes(variant)
        still_held = worker.is_alive() and not exited.is_set()
    finally:
        release.set()
        alive = _join(worker) if worker is not None else []

    assert alive == []
    assert timed_out == [False], "the held removal expired instead of being released"
    assert still_held, "the concurrent commit did not land while the removal was held"
    assert committed["modify"] == {"success": True}
    assert len(outside_calls) >= 2, "the stale candidate was not re-prepared"
    assert "error" not in outcome, f"removal raised {outcome.get('error')!r}"
    assert outcome["result"] is False
    assert env.toml_path.read_bytes() == committed["toml"]
    jobs = _parse_toml(env.toml_path)["jobs"]
    assert "alpha" in jobs
    assert jobs["dormant"]["task"] == "committed meanwhile"
    assert "alpha" in _tags(sched)
    assert env.legacy_path.read_bytes() == variant


# ---------------------------------------------------------------------------
# E. A bad legacy file is contained; reload keeps last-good legacy jobs
# ---------------------------------------------------------------------------

_BAD_LEGACY = {
    "malformed": _MALFORMED_LEGACY,
    "unreadable_directory": _LEGACY_DIR,
}


@pytest.mark.parametrize("bad", sorted(_BAD_LEGACY))
def test_bad_legacy_file_sends_startup_notice(make_scheduler: MakeScheduler, bad: str) -> None:
    """Startup notifies the operator with the legacy path and repair steps, no content."""
    env = make_scheduler(_TOML, legacy=_BAD_LEGACY[bad], enabled=True)

    env.sched.start()

    sent = [call.args[0] for call in env.notify.call_args_list]
    naming = [m for m in sent if html.escape(str(env.legacy_path)) in m]
    assert naming, f"no startup notice names the legacy file: {sent!r}"
    text = naming[0].lower()
    assert "reload" in text
    assert "repair" in text or "fix" in text or "edit" in text
    assert _SECRET not in naming[0]


def test_toml_change_succeeds_beside_bad_legacy_file(make_scheduler: MakeScheduler) -> None:
    """A legacy warning never blocks an ordinary scheduler.toml modify."""
    env = make_scheduler(_TOML, legacy=_MALFORMED_LEGACY, enabled=True)
    env.sched.start()

    out = exec_schedule(env.sched, {"action": "modify", "tag": "alpha", "task": "still editable"})

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    assert _parse_toml(env.toml_path)["jobs"]["alpha"]["task"] == "still editable"
    assert env.sched.show_job("alpha")["next_run"] is not None
    assert env.legacy_path.read_bytes() == _MALFORMED_LEGACY


_BETA_TOML = _TOML + """
[jobs.beta]
enabled = true
schedule = "cron"
cron = "0 8 1 1 *"
task = "toml beta"
notify = false
"""


def test_malformed_legacy_reload_keeps_last_good_legacy_jobs(
    make_scheduler: MakeScheduler,
) -> None:
    """The edited scheduler.toml loads; last-good legacy jobs stay, nothing is adopted."""
    env = make_scheduler(_TOML, legacy=_LEGACY)
    legacy_task = env.sched.show_job("legacyjob")["task"]
    env.toml_path.write_text(_BETA_TOML, encoding="utf-8")
    env.legacy_path.write_bytes(_MALFORMED_LEGACY)

    env.sched.reload()

    assert {"alpha", "dormant", "beta", "legacyjob", "legacypaused"} <= set(_tags(env.sched))
    assert env.sched.show_job("legacyjob")["task"] == legacy_task
    assert set(_parse_toml(env.toml_path)["jobs"]) == {"alpha", "dormant", "beta"}
    assert env.legacy_path.read_bytes() == _MALFORMED_LEGACY


_CLEANUPS = {
    "entry_deleted": lambda path: path.write_bytes(
        _legacy_bytes({"legacypaused": _LEGACY["legacypaused"]}),
    ),
    "file_deleted": lambda path: path.unlink(),
}


@pytest.mark.parametrize("cleanup", sorted(_CLEANUPS))
def test_operator_cleanup_reload_drops_legacy_job(
    make_scheduler: MakeScheduler, cleanup: str,
) -> None:
    """After a malformed reload, a healthy or missing legacy file reloads its own state."""
    env = make_scheduler(_TOML, legacy=_LEGACY)
    env.legacy_path.write_bytes(_MALFORMED_LEGACY)
    env.sched.reload()
    toml_bytes = env.toml_path.read_bytes()
    _CLEANUPS[cleanup](env.legacy_path)
    legacy_after = _file_state(env.legacy_path)

    result = env.sched.reload()

    assert "error" not in result, result
    assert "legacyjob" not in _tags(env.sched)
    assert ("legacypaused" in _tags(env.sched)) is (cleanup == "entry_deleted")
    assert env.sched.load_notice() is None
    assert env.toml_path.read_bytes() == toml_bytes
    assert _file_state(env.legacy_path) == legacy_after


def test_uninspectable_legacy_file_sends_startup_notice(
    make_scheduler: MakeScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing legacy file that cannot be inspected is read, not treated as missing.

    A real valid legacy file exists, but while the scheduler is constructed
    and started the external ``os.stat`` and ``open`` on that one path raise
    ``PermissionError`` (an inaccessible parent directory) without host
    permission changes; every other path and file descriptor delegates.
    """
    paths = xdg_paths("legacy-readonly-test")  # the fixture's own stable paths
    paths.state_home.mkdir(parents=True, exist_ok=True)
    legacy_path = paths.scheduler_jobs
    legacy_path.write_bytes(_legacy_bytes(_LEGACY))
    assert legacy_path.is_file()
    target = os.path.abspath(legacy_path)
    real_stat = os.stat
    real_open = builtins.open
    open_attempts: list[str] = []

    def _is_target(file: object) -> bool:
        if not isinstance(file, (str, bytes, os.PathLike)):
            return False
        return os.path.abspath(os.fsdecode(os.fspath(file))) == target

    def faulting_stat(path: object, *args: object, **kwargs: object) -> object:
        if not args and kwargs.get("dir_fd") is None and _is_target(path):
            raise PermissionError(13, "Permission denied", os.fsdecode(os.fspath(path)))
        return real_stat(path, *args, **kwargs)

    def faulting_open(file: object, *args: object, **kwargs: object) -> object:
        if _is_target(file):
            open_attempts.append(threading.current_thread().name)
            raise PermissionError(13, "Permission denied", os.fsdecode(os.fspath(file)))
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "stat", faulting_stat)
        fault.setattr(builtins, "open", faulting_open)
        fault.setattr(io, "open", faulting_open)
        assert os.path.exists(legacy_path) is False, "the stat fault is not in effect"
        env = make_scheduler(_TOML, legacy=None, enabled=True)
        env.sched.start()
        attempts = list(open_attempts)

    sent = [call.args[0] for call in env.notify.call_args_list]
    naming = [m for m in sent if html.escape(str(legacy_path)) in m]
    assert naming, f"no startup notice names the uninspectable legacy file: {sent!r}"
    text = naming[0].lower()
    assert "reload" in text
    assert "repair" in text or "fix" in text or "edit" in text
    assert _SECRET not in naming[0]
    assert attempts, "the legacy file was assumed missing instead of being read"
    assert env.sched.show_job("alpha")["next_run"] is not None
    assert env.legacy_path == legacy_path
    assert legacy_path.read_bytes() == _legacy_bytes(_LEGACY)
