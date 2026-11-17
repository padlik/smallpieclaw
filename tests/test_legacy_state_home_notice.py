"""The state-home ``scheduler_jobs.json`` is detected but never loaded (task 8.4, ADR-0044).

Spec ``scheduled-job-execution``: *Legacy state-home job file is detected but never loaded*.

The scheduler's only contact with the file is an existence check at load and reload. When it
exists (whatever it holds) one actionable notice names its path and directs manual conversion to
``cron``/``once`` jobs in ``scheduler.toml``; it is visible in job listings. The file is never
opened, read, hashed, executed, converted, written, renamed or deleted, its jobs never register
or run, and it never blocks TOML jobs, mutations or removals.

Real ``Scheduler`` objects over the shared legacy fixtures; an access guard on ``open`` and the
mutating ``os`` calls records any touch of the legacy path.
"""

from __future__ import annotations

import asyncio
import builtins
import html
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import schedule

from builtin_tools.schedule import exec_schedule
from telegram_commands import cmd_jobs
from telegram_interface import TelegramInterface
from tests import scheduler_legacy_fixtures as legacy_fx

SECRET = legacy_fx.SECRET

_CONTENTS = [
    pytest.param(legacy_fx.LEGACY, id="valid-jobs"),
    pytest.param(legacy_fx.SHADOW_LEGACY, id="same-tags-as-toml"),
    pytest.param(b"", id="empty"),
    pytest.param(legacy_fx.MALFORMED_LEGACY, id="malformed"),
    pytest.param(b'["not", "an", "object"]', id="not-an-object"),
    pytest.param(legacy_fx.LEGACY_DIR, id="directory"),
]


@pytest.fixture
def make_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., legacy_fx.LegacyEnv]]:
    """Factory of real schedulers whose legacy-path accesses are recorded in ``touched``."""
    touched: list[str] = []
    state_dir = str(tmp_path)

    def is_legacy(path: object) -> bool:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        text = os.fsdecode(path)
        return text.endswith("scheduler_jobs.json") and state_dir in text

    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_legacy(file):
            touched.append("open")
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_legacy(path):
            touched.append("os.open")
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("replace", "rename", "remove", "unlink"):
        real = getattr(os, name)

        def _wrap(*args, _real=real, _name=name, **kwargs):  # type: ignore[no-untyped-def]
            if any(is_legacy(a) for a in args):
                touched.append(f"os.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, name, _wrap)

    factory_gen = legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)
    make = next(factory_gen)

    def _make(*args: object, **kwargs: object) -> legacy_fx.LegacyEnv:
        env = make(*args, **kwargs)
        env.touched = touched  # type: ignore[attr-defined]
        return env

    yield _make
    for _ in factory_gen:
        pass


def _notice(env: legacy_fx.LegacyEnv) -> str:
    return env.sched.load_notice() or ""


def _assert_notice(env: legacy_fx.LegacyEnv) -> None:
    text = _notice(env)
    path = str(env.legacy_path)
    assert path in text and "scheduler.toml" in text and "cron" in text and "once" in text, text
    assert SECRET not in text


@pytest.mark.parametrize("content", _CONTENTS)
def test_existing_file_of_any_content_gives_one_path_naming_notice(
    make_env: Callable[..., legacy_fx.LegacyEnv], content: object,
) -> None:
    """Whatever the file holds, the notice names its path and the cron/once conversion guidance."""
    env = make_env(legacy_fx.TOML, legacy=content)

    _assert_notice(env)
    assert _notice(env).count(str(env.legacy_path)) == 1


@pytest.mark.parametrize("content", _CONTENTS)
def test_the_file_is_never_opened_changed_or_removed(
    make_env: Callable[..., legacy_fx.LegacyEnv], content: object,
) -> None:
    """Load, start, list, reload and a TOML mutation never touch the legacy path beyond existence."""
    env = make_env(legacy_fx.TOML, legacy=content)
    before = legacy_fx.file_state(env.legacy_path)

    env.sched.start()
    env.sched.list_jobs()
    env.sched.reload()
    env.sched.modify_job("alpha", {"task": "changed"})
    env.sched.remove_job("dormant")

    assert env.touched == []  # type: ignore[attr-defined]
    assert legacy_fx.file_state(env.legacy_path) == before


def test_absent_file_has_no_notice_and_sends_nothing(make_env: Callable[..., legacy_fx.LegacyEnv]) -> None:
    """No file, no notice, no startup notification."""
    env = make_env(legacy_fx.TOML)

    env.sched.start()

    assert env.sched.load_notice() is None
    env.notify.assert_not_called()


def test_the_notice_is_sent_once_at_start(make_env: Callable[..., legacy_fx.LegacyEnv]) -> None:
    """The startup notification carries the legacy notice exactly once."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY)

    env.sched.start()
    env.sched.start()

    assert len(env.notify.call_args_list) == 1
    call = env.notify.call_args_list[0]
    assert str(env.legacy_path) in call.args[0]


@pytest.mark.parametrize("content", [legacy_fx.LEGACY, legacy_fx.SHADOW_LEGACY])
def test_no_legacy_job_is_listed_scheduled_or_runnable(
    make_env: Callable[..., legacy_fx.LegacyEnv], content: object,
) -> None:
    """Only scheduler.toml jobs exist: legacy tags are unknown and nothing of theirs registers or runs."""
    env = make_env(legacy_fx.TOML, legacy=content, enabled=True)
    env.sched.start()

    assert sorted(legacy_fx.tags(env.sched)) == ["alpha", "dormant"]
    for tag in ("legacyjob", "legacypaused"):
        assert env.sched.show_job(tag) is None
        assert env.sched.run_now(tag)["success"] is False
        assert not schedule.get_jobs(tag)
    alpha = env.sched.show_job("alpha") or {}
    assert alpha["task"] == "toml alpha" and alpha["cron"] == "0 2 1 1 *"
    assert alpha["source"] == "config"


@pytest.mark.parametrize("toml", [None, "[jobs.broken\n"], ids=["toml-missing", "toml-unparseable"])
def test_a_missing_or_bad_toml_never_falls_back_to_the_legacy_file(
    make_env: Callable[..., legacy_fx.LegacyEnv], toml: object,
) -> None:
    """A valid legacy file is no substitute for scheduler.toml; the notice still appears."""
    env = make_env(toml, legacy=legacy_fx.LEGACY)

    assert legacy_fx.tags(env.sched) == []
    assert str(env.legacy_path) in _notice(env)


def test_reload_detects_a_new_file_and_clears_the_notice_when_it_is_gone(
    make_env: Callable[..., legacy_fx.LegacyEnv],
) -> None:
    """Reload re-checks existence only: appears -> notice, disappears -> cleared, jobs unchanged."""
    env = make_env(legacy_fx.TOML)
    assert env.sched.load_notice() is None

    legacy_fx.write_legacy(env.legacy_path, legacy_fx.LEGACY)
    assert "error" not in env.sched.reload()
    _assert_notice(env)
    assert sorted(legacy_fx.tags(env.sched)) == ["alpha", "dormant"]

    env.legacy_path.unlink()
    assert "error" not in env.sched.reload()
    assert env.sched.load_notice() is None


def test_an_unreadable_legacy_file_never_fails_a_reload(make_env: Callable[..., legacy_fx.LegacyEnv]) -> None:
    """A directory at the legacy path used to fail the reload; now it is just the notice."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY_DIR)

    result = env.sched.reload()

    assert "error" not in result and result["failed"] == 0
    _assert_notice(env)


@pytest.mark.parametrize("action", ["modify", "pause", "resume", "remove"])
def test_toml_jobs_sharing_a_tag_with_the_legacy_file_change_without_legacy_refusal(
    make_env: Callable[..., legacy_fx.LegacyEnv], action: str,
) -> None:
    """modify/pause/resume/remove of a TOML job proceed under the ordinary protocol; JSON bytes stay."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.SHADOW_LEGACY)
    before = legacy_fx.file_state(env.legacy_path)
    calls = {
        "modify": lambda: env.sched.modify_job("alpha", {"task": "edited"}),
        "pause": lambda: env.sched.pause_job("alpha"),
        "resume": lambda: env.sched.resume_job("dormant"),
        "remove": lambda: env.sched.remove_job("alpha"),
    }

    result = calls[action]()

    assert result is True or (isinstance(result, dict) and result.get("success") is True), result
    document = legacy_fx.parse_toml(env.toml_path)["jobs"]
    if action == "remove":
        assert "alpha" not in document and "alpha" not in legacy_fx.tags(env.sched)
    elif action == "modify":
        assert document["alpha"]["task"] == "edited"
    assert legacy_fx.file_state(env.legacy_path) == before


def test_removed_toml_job_is_never_resurrected_by_the_legacy_file(
    make_env: Callable[..., legacy_fx.LegacyEnv],
) -> None:
    """After removal and reload a same-tag JSON entry does not come back as a job."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.SHADOW_LEGACY)

    assert env.sched.remove_job("alpha") is True
    assert "error" not in env.sched.reload()

    assert env.sched.show_job("alpha") is None


def test_the_notice_is_visible_in_the_schedule_tool_listing(make_env: Callable[..., legacy_fx.LegacyEnv]) -> None:
    """``schedule list`` shows the notice with jobs and with none."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY)
    with_jobs = exec_schedule(env.sched, {"action": "list"})["output"]
    env.sched.remove_job("alpha")
    env.sched.remove_job("dormant")
    without_jobs = exec_schedule(env.sched, {"action": "list"})["output"]

    for output in (with_jobs, without_jobs):
        assert str(env.legacy_path) in output and "cron" in output and "once" in output
        assert SECRET not in output


def test_the_legacy_file_contents_never_reach_logs(
    make_env: Callable[..., legacy_fx.LegacyEnv], caplog: pytest.LogCaptureFixture,
) -> None:
    """No file content (a secret in a task) appears in any log record."""
    caplog.set_level(logging.DEBUG)
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY)
    env.sched.reload()

    assert SECRET not in caplog.text


def _run_telegram_jobs(env: legacy_fx.LegacyEnv, args: list[str]) -> str:
    """Drive the authorized real ``/jobs`` handler with the external Telegram replies captured."""
    iface = TelegramInterface.__new__(TelegramInterface)
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.scheduler = env.sched
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_message=message)
    asyncio.run(cmd_jobs(iface, update, SimpleNamespace(args=args)))
    return "\n".join(call.args[0] for call in message.reply_text.await_args_list)


def test_the_notice_is_visible_in_the_telegram_jobs_listing(make_env: Callable[..., legacy_fx.LegacyEnv]) -> None:
    """``/jobs`` lists the TOML jobs beside the escaped legacy notice, and no notice once the file is gone."""
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY)

    listed = _run_telegram_jobs(env, [])

    assert html.escape(str(env.legacy_path)) in listed and "alpha" in listed
    assert "legacyjob" not in listed and SECRET not in listed
    env.legacy_path.unlink()
    env.sched.reload()
    assert str(env.legacy_path) not in _run_telegram_jobs(env, [])


# ---------------------------------------------------------------------------
# The notice follows existence on EVERY reload outcome, publishing nothing extra
# ---------------------------------------------------------------------------


def _bad_toml(env: legacy_fx.LegacyEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    env.toml_path.write_text("[jobs.broken\n", encoding="utf-8")


def _toml_is_a_directory(env: legacy_fx.LegacyEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    env.toml_path.unlink()
    env.toml_path.mkdir()


def _toml_changes_during_preparation(env: legacy_fx.LegacyEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator edits scheduler.toml while the reload prepares its candidate (freshness mismatch)."""
    import scheduler as scheduler_module

    real = scheduler_module.croniter.get_next
    edited = {"done": False}

    def get_next(driver, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not edited["done"]:
            edited["done"] = True
            env.toml_path.write_text(legacy_fx.TOML + "\n# edited during reload\n", encoding="utf-8")
        return real(driver, *args, **kwargs)

    monkeypatch.setattr(scheduler_module.croniter, "get_next", get_next)
    env.toml_path.write_text(legacy_fx.TOML.replace("toml alpha", "alpha reloaded"), encoding="utf-8")


class _FailingJobs(list):
    """The external ``schedule`` job list refusing the reload's slice publication."""

    def __setitem__(self, key, value):  # type: ignore[no-untyped-def]
        raise RuntimeError("driver unavailable")


def _publish_fault(env: legacy_fx.LegacyEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    env.toml_path.write_text(legacy_fx.TOML.replace("toml alpha", "alpha reloaded"), encoding="utf-8")
    monkeypatch.setattr(schedule.default_scheduler, "jobs", _FailingJobs(schedule.default_scheduler.jobs))


_FAILED_RELOADS = {
    "unparseable_toml": _bad_toml,
    "toml_is_directory": _toml_is_a_directory,
    "freshness_mismatch": _toml_changes_during_preparation,
    "publish_fault": _publish_fault,
}


def _published_state(env: legacy_fx.LegacyEnv) -> dict:
    sched = env.sched
    with sched._running_lock:
        return {
            "jobs_meta": repr(sched._jobs_meta), "document": repr(sched._load_result.document),
            "hash": sched._load_result.content_hash, "generation": sched._config_generation,
        }


@pytest.mark.parametrize("failure", sorted(_FAILED_RELOADS))
@pytest.mark.parametrize("legacy_change", ["appears", "disappears"])
def test_a_failed_reload_still_refreshes_the_legacy_notice_and_publishes_nothing_else(
    make_env: Callable[..., legacy_fx.LegacyEnv], monkeypatch: pytest.MonkeyPatch,
    failure: str, legacy_change: str,
) -> None:
    """Whatever stops the reload, the notice follows the file's existence; the last-good state is untouched.

    The legacy file is never opened or changed; jobs, document, hash and generation stay as published
    (a publish fault keeps its own existing latch semantics).
    """
    env = make_env(legacy_fx.TOML, legacy=legacy_fx.LEGACY if legacy_change == "disappears" else None)
    assert (env.sched.load_notice() is not None) is (legacy_change == "disappears")
    before = _published_state(env)
    registrations = list(schedule.default_scheduler.jobs)
    latch = env.sched._reload_required
    _FAILED_RELOADS[failure](env, monkeypatch)
    if legacy_change == "appears":
        legacy_fx.write_legacy(env.legacy_path, legacy_fx.LEGACY)
    else:
        env.legacy_path.unlink()
    legacy_before = legacy_fx.file_state(env.legacy_path)
    env.touched.clear()  # type: ignore[attr-defined]

    result = env.sched.reload()

    assert "error" in result and result["reloaded"] == 0, result
    assert _published_state(env) == before
    if failure != "publish_fault":
        assert list(schedule.default_scheduler.jobs) == registrations
        assert env.sched._reload_required is latch
    notice = env.sched.load_notice() or ""
    assert (str(env.legacy_path) in notice) is (legacy_change == "appears"), notice
    assert env.touched == []  # type: ignore[attr-defined]
    assert legacy_fx.file_state(env.legacy_path) == legacy_before
