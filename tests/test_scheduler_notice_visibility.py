"""Operator-visible registration failures and last-good reload containment."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import schedule

from builtin_tools.schedule import exec_schedule
from scheduler import Scheduler
from xdg import xdg_paths


_SECRET = "sk-test-never-publish-file-content"
_BROKEN = f'secret = "{_SECRET}"\nbroken = [\n'
_JOBS = '''
[jobs.bad]
schedule = "once"
run_at = "23:59"
task = "reminder"

[jobs.healthy]
schedule = "cron"
cron = "0 2 1 1 *"
task = "healthy task"
'''


@pytest.fixture
def sched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scheduler]:
    """Build a real scheduler with isolated XDG storage and no model execution."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "scheduler.toml"
    path.write_text(_JOBS, encoding="utf-8")
    instance = Scheduler(
        config={"scheduler": {"enabled": True}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        paths=xdg_paths("notice-test"),
        scheduler_config_path=str(path),
    )
    try:
        yield instance
    finally:
        instance.stop()


def _register_with_failure(sched: Scheduler, phase: str) -> dict | None:
    """Fail the shared once-job driver for global and isolated schedulers."""
    with patch.object(schedule.Job, "do", side_effect=ValueError("trigger unavailable")) as driver:
        if phase == "start":
            sched.start()
            result = None
        else:
            result = sched.reload()
        driver.assert_called_once()
        return result


def _break_file(sched: Scheduler, status: str) -> Path:
    """Make the configured file fail without host permission changes."""
    path = Path(sched._scheduler_config_path)
    if status == "unreadable":
        path.unlink()
        path.mkdir()
    else:
        path.write_text(_BROKEN, encoding="utf-8")
    return path


def _assert_notice(text: str, path: Path, status: str) -> None:
    """Require safe, actionable file failure information."""
    assert str(path) in text
    assert status in text.lower()
    assert "repair" in text.lower() or "fix" in text.lower()
    assert "reload" in text.lower()
    assert _SECRET not in text
    assert "secret =" not in text


@pytest.mark.parametrize("phase", ["start", "reload"])
@pytest.mark.parametrize("action", ["list", "show"])
def test_registration_failure_is_visible(
    sched: Scheduler, phase: str, action: str,
) -> None:
    """A trigger failure is a visible config_error, not merely a log warning."""
    _register_with_failure(sched, phase)
    result = exec_schedule(sched, {"action": action, "tag": "bad"})
    assert "config_error" in result["output"]
    assert "bad" in result["output"]


@pytest.mark.parametrize("phase", ["start", "reload"])
def test_registration_failure_is_unscheduled(sched: Scheduler, phase: str) -> None:
    """Failed registration leaves no live trigger or computed next run."""
    # Begin with a real registration to catch stale-trigger retention too.
    sched.reload()
    _register_with_failure(sched, phase)
    assert schedule.get_jobs("bad") == []
    assert sched.show_job("bad")["next_run"] is None


@pytest.mark.parametrize("phase", ["start", "reload"])
def test_registration_failure_refuses_run_now(sched: Scheduler, phase: str) -> None:
    """The failed job cannot bypass its config_error via manual admission."""
    _register_with_failure(sched, phase)
    assert sched.show_job("bad").get("config_error"), "registration failure must block admission"
    result = sched.run_now("bad")
    assert result["success"] is False
    assert "config_error" in result["error"]


@pytest.mark.parametrize("phase", ["start", "reload"])
def test_registration_failure_preserves_healthy_sibling(
    sched: Scheduler, phase: str,
) -> None:
    """A different job still registers without acquiring error-only output fields."""
    _register_with_failure(sched, phase)
    healthy = sched.show_job("healthy")
    assert healthy["next_run"] is not None
    assert "config_error" not in healthy


def test_reload_registration_counts_only_successful_jobs(sched: Scheduler) -> None:
    """Reload counts the single registered sibling separately from the failed job."""
    result = _register_with_failure(sched, "reload")
    assert result["reloaded"] == 1
    assert result["failed"] == 1


@pytest.mark.parametrize("status", ["unreadable", "unparseable"])
def test_failed_reload_preserves_last_good_state(sched: Scheduler, status: str) -> None:
    """A file failure preserves the published map, raw document, hash and triggers."""
    sched.reload()
    jobs = copy.deepcopy(sched.list_jobs())
    shown = {job["tag"]: copy.deepcopy(sched.show_job(job["tag"])) for job in jobs}
    document = copy.deepcopy(sched._load_result.document)
    content_hash = sched._load_result.content_hash
    triggers = list(schedule.get_jobs())
    _break_file(sched, status)
    sched.reload()
    assert sched.list_jobs() == jobs
    assert {tag: sched.show_job(tag) for tag in shown} == shown
    assert sched._load_result.document == document
    assert sched._load_result.content_hash == content_hash
    assert schedule.get_jobs() == triggers


@pytest.mark.parametrize("status", ["unreadable", "unparseable"])
def test_failed_reload_returns_safe_repair_notice(sched: Scheduler, status: str) -> None:
    """The reload caller receives the file error without parser content leaking."""
    sched.reload()
    path = _break_file(sched, status)
    result = sched.reload()
    _assert_notice(result["error"], path, status)


@pytest.mark.parametrize("status", ["unreadable", "unparseable"])
@pytest.mark.parametrize("action", ["list", "show"])
def test_failed_reload_notice_remains_visible_with_last_good_jobs(
    sched: Scheduler, status: str, action: str,
) -> None:
    """Old usable jobs do not hide the current file failure in tool responses."""
    sched.reload()
    path = _break_file(sched, status)
    sched.reload()
    result = exec_schedule(sched, {"action": action, "tag": "healthy"})
    _assert_notice(result["output"] + result["error"], path, status)


@pytest.mark.parametrize("status", ["unreadable", "unparseable"])
@pytest.mark.parametrize("action", ["add", "modify"])
def test_failed_reload_refuses_mutation_before_attempt(
    sched: Scheduler, status: str, action: str,
) -> None:
    """Known-bad input is untouched when a last-good job map still exists."""
    sched.reload()
    path = _break_file(sched, status)
    sched.reload()
    before = None if path.is_dir() else path.read_bytes()
    attempt = MagicMock()
    if action == "add":
        result = sched.add_job(
            "new", "cron", "new task", cron="0 4 * * *", _on_attempt=attempt,
        )
    else:
        result = sched.modify_job("healthy", {"task": "changed"}, _on_attempt=attempt)
    assert result["success"] is False
    _assert_notice(result["error"], path, status)
    attempt.assert_not_called()
    assert sched.show_job("healthy")["task"] == "healthy task"
    assert path.is_dir() if before is None else path.read_bytes() == before


_BAD_CRON = "0 3 1 1 *"
_DRIVER_SECRET = "/private/driver/sk-test-cron-exception-secret"


@pytest.fixture
def cron_sched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scheduler]:
    """Build two real cron jobs whose next scheduled dates are far in the future."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "cron-scheduler.toml"
    path.write_text(
        f'[jobs.bad]\nschedule = "cron"\ncron = "{_BAD_CRON}"\ntask = "bad task"\n'
        '[jobs.healthy]\nschedule = "cron"\ncron = "0 2 1 1 *"\ntask = "healthy task"\n',
        encoding="utf-8",
    )
    instance = Scheduler(
        config={"scheduler": {"enabled": True}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(), paths=xdg_paths("cron-notice-test"),
        scheduler_config_path=str(path),
    )
    try:
        yield instance
    finally:
        instance.stop()


def _register_with_cron_failure(
    sched: Scheduler, phase: str, error_type: type[Exception],
) -> dict | None:
    """Fail only the bad external iterator's get_next, not constructor validation."""
    from croniter import croniter as real_driver

    def driver(expression: str, *args: object, **kwargs: object) -> real_driver:
        iterator = real_driver(expression, *args, **kwargs)
        if expression == _BAD_CRON:
            iterator.get_next = MagicMock(side_effect=error_type(_DRIVER_SECRET))
        return iterator

    # Validation still constructs a real iterator successfully; the fault only
    # occurs when registration asks this particular iterator for its next date.
    # wraps preserves driver attributes for consumers such as croniter.is_valid.
    with patch("scheduler.croniter", wraps=real_driver, side_effect=driver):
        if phase == "start":
            sched.start()
            return None
        return sched.reload()


@pytest.mark.parametrize("phase", ["start", "reload"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_cron_registration_error_is_safe_visible(
    cron_sched: Scheduler, phase: str, error_type: type[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A driver failure exposes safe repair information rather than exception content."""
    _register_with_cron_failure(cron_sched, phase, error_type)
    error = cron_sched.show_job("bad").get("config_error")
    assert error
    assert error_type.__name__ in error
    assert "reload" in error.lower()
    for action in ("list", "show"):
        result = exec_schedule(cron_sched, {"action": action, "tag": "bad"})
        text = result["output"] + result["error"]
        assert "config_error" in text
        assert _DRIVER_SECRET not in text
    assert _DRIVER_SECRET not in error
    assert _DRIVER_SECRET not in caplog.text


@pytest.mark.parametrize("phase", ["start", "reload"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_cron_registration_error_refuses_manual_admission(
    cron_sched: Scheduler, phase: str, error_type: type[Exception],
) -> None:
    """A failed cron registration cannot be bypassed with run_now."""
    _register_with_cron_failure(cron_sched, phase, error_type)
    # Fail before attempting a background run when the containment is missing.
    assert cron_sched.show_job("bad").get("config_error")
    result = cron_sched.run_now("bad")
    assert result["success"] is False
    assert "config_error" in result["error"]
    assert _DRIVER_SECRET not in result["error"]


@pytest.mark.parametrize("phase", ["start", "reload"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_cron_registration_error_removes_next_run(
    cron_sched: Scheduler, phase: str, error_type: type[Exception],
) -> None:
    """A failing driver cannot leave the previous cron next-run timestamp active."""
    cron_sched.reload()
    assert cron_sched.show_job("bad")["next_run"] is not None
    _register_with_cron_failure(cron_sched, phase, error_type)
    assert cron_sched.show_job("bad")["next_run"] is None
    assert schedule.get_jobs("bad") == []


@pytest.mark.parametrize("phase", ["start", "reload"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_cron_registration_error_preserves_sibling(
    cron_sched: Scheduler, phase: str, error_type: type[Exception],
) -> None:
    """The healthy external iterator still registers its cron job normally."""
    _register_with_cron_failure(cron_sched, phase, error_type)
    healthy = cron_sched.show_job("healthy")
    assert healthy["next_run"] is not None
    assert "config_error" not in healthy


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_cron_registration_error_reload_counts(
    cron_sched: Scheduler, error_type: type[Exception],
) -> None:
    """Reload counts exactly one healthy cron registration and one driver failure."""
    result = _register_with_cron_failure(cron_sched, "reload", error_type)
    assert result["reloaded"] == 1
    assert result["failed"] == 1
