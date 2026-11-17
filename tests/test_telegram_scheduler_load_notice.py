"""Real /jobs handlers must report scheduler file failures truthfully."""

from __future__ import annotations

import asyncio
import builtins
import html
import io
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduler import Scheduler
from telegram_commands import cmd_jobs
from telegram_interface import TelegramInterface
from tests import scheduler_legacy_fixtures as legacy_fx
from xdg import xdg_paths


_SECRET = "sk-test-private-scheduler-file"


@pytest.fixture
def failed_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> Iterator[tuple[Scheduler, Path, str]]:
    """Keep a real last-good scheduler after an unreadable/unparseable reload."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "scheduler<&>.toml"
    path.write_text(
        '[jobs.healthy]\nschedule = "cron"\ncron = "0 2 1 1 *"\ntask = "healthy"\n',
        encoding="utf-8",
    )
    sched = Scheduler(
        config={"scheduler": {"enabled": False}}, notify_fn=MagicMock(),
        paths=xdg_paths("telegram-notice-test"), scheduler_config_path=str(path),
    )
    try:
        sched.reload()
        status = request.param
        if status == "unreadable":
            path.unlink()
            path.mkdir()
        else:
            path.write_text(f'secret = "{_SECRET}"\nbroken = [\n', encoding="utf-8")
        sched.reload()
        yield sched, path, status
    finally:
        sched.stop()


def _run_jobs(sched: Scheduler, args: list[str]) -> str:
    """Drive the authorized real handler with external Telegram replies captured."""
    iface = TelegramInterface.__new__(TelegramInterface)
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.scheduler = sched
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42), effective_message=message,
    )
    asyncio.run(cmd_jobs(iface, update, SimpleNamespace(args=args)))
    return "\n".join(call.args[0] for call in message.reply_text.await_args_list)


def _assert_html_notice(text: str, path: Path, status: str) -> None:
    """Require escaped file location plus safe status and repair instructions."""
    assert html.escape(str(path)) in text
    assert str(path) not in text
    assert status in text.lower()
    assert "repair" in text.lower() or "fix" in text.lower()
    assert "reload" in text.lower()
    assert _SECRET not in text
    assert "secret =" not in text


@pytest.mark.parametrize("failed_reload", ["unreadable", "unparseable"], indirect=True)
def test_jobs_reload_reports_file_failure(
    failed_reload: tuple[Scheduler, Path, str],
) -> None:
    """A failed /jobs reload never announces that the scheduler was reloaded."""
    sched, path, status = failed_reload
    text = _run_jobs(sched, ["reload"])
    assert "Scheduler reloaded" not in text
    _assert_html_notice(text, path, status)


@pytest.mark.parametrize("failed_reload", ["unreadable", "unparseable"], indirect=True)
def test_jobs_list_displays_file_failure(
    failed_reload: tuple[Scheduler, Path, str],
) -> None:
    """The ordinary /jobs list keeps the file notice beside last-good jobs."""
    sched, path, status = failed_reload
    text = _run_jobs(sched, [])
    _assert_html_notice(text, path, status)
    assert "healthy" in text


@pytest.mark.parametrize("failed_reload", ["unreadable", "unparseable"], indirect=True)
@pytest.mark.parametrize("action", ["remove", "pause", "resume"])
def test_jobs_mutation_displays_file_refusal(
    failed_reload: tuple[Scheduler, Path, str], action: str,
) -> None:
    """A file-based refusal must not misreport an existing job as not found."""
    sched, path, status = failed_reload
    text = _run_jobs(sched, [action, "healthy"])
    assert "not found" not in text.lower()
    _assert_html_notice(text, path, status)


# ---------------------------------------------------------------------------
# Legacy state-home job file notices
#
# Relocated by task 2.4 from tests/test_scheduler_legacy_readonly.py (ledger
# provenance in openspec/changes/simplify-model-runtime/test-preservation-ledger).
# Rewritten by task 8.4 (ledger 444/445/456/457/459): the startup/reload notice
# and byte preservation are kept; detection is existence-only (never read).
# ---------------------------------------------------------------------------


@pytest.fixture
def make_legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[legacy_fx.MakeLegacyScheduler]:
    """Real legacy-beside-TOML schedulers (shared factory, see the support module)."""
    yield from legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)


class TestNonDestructiveLoad:
    """Loading and reloading with a legacy file present never writes either file."""

    @pytest.mark.parametrize("toml", [legacy_fx.TOML, None], ids=["toml_present", "toml_missing"])
    def test_load_and_reload_keep_legacy_bytes_without_adoption(
        self, make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, toml: Optional[str],
    ) -> None:
        """Legacy jobs are never listed; both files keep their bytes; no TOML is created."""
        env = make_legacy_scheduler(toml, legacy=legacy_fx.LEGACY)

        reloaded = env.sched.reload()

        assert "error" not in reloaded, reloaded
        assert not {"legacyjob", "legacypaused"} & set(legacy_fx.tags(env.sched))
        assert str(env.legacy_path) in (env.sched.load_notice() or "")
        assert env.legacy_path.read_bytes() == legacy_fx.legacy_bytes(legacy_fx.LEGACY)
        if toml is None:
            assert not env.toml_path.exists()
        else:
            assert env.toml_path.read_text(encoding="utf-8") == toml
        assert list(env.toml_path.parent.glob(env.toml_path.name + ".bak.*")) == []


_BAD_LEGACY = {
    "malformed": legacy_fx.MALFORMED_LEGACY,
    "unreadable_directory": legacy_fx.LEGACY_DIR,
}


@pytest.mark.parametrize("bad", sorted(_BAD_LEGACY))
def test_bad_legacy_file_sends_startup_notice(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, bad: str,
) -> None:
    """Startup notifies the operator with the legacy path and conversion steps, no content."""
    env = make_legacy_scheduler(legacy_fx.TOML, legacy=_BAD_LEGACY[bad], enabled=True)

    env.sched.start()

    sent = [call.args[0] for call in env.notify.call_args_list]
    naming = [m for m in sent if html.escape(str(env.legacy_path)) in m]
    assert naming, f"no startup notice names the legacy file: {sent!r}"
    text = naming[0].lower()
    assert "reload" in text
    assert "cron" in text and "once" in text and "scheduler.toml" in text
    assert legacy_fx.SECRET not in naming[0]


def test_uninspectable_legacy_file_is_never_opened_and_never_blocks_startup(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy file whose metadata cannot even be stat-ed is still never opened.

    A real valid legacy file exists, but while the scheduler is constructed
    and started the external ``os.stat`` and ``open`` on that one path raise
    ``PermissionError`` (an inaccessible parent directory) without host
    permission changes; every other path and file descriptor delegates. The
    existence check only needs metadata, so there is no read attempt and no
    fallback read; scheduler.toml jobs start normally and the bytes stay.
    """
    paths = xdg_paths("legacy-readonly-test")  # the fixture's own stable paths
    paths.state_home.mkdir(parents=True, exist_ok=True)
    legacy_path = paths.scheduler_jobs
    legacy_path.write_bytes(legacy_fx.legacy_bytes(legacy_fx.LEGACY))
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
        env = make_legacy_scheduler(legacy_fx.TOML, legacy=None, enabled=True)
        env.sched.start()
        attempts = list(open_attempts)

    assert attempts == [], "the legacy file was opened"
    assert env.sched.show_job("alpha")["next_run"] is not None
    assert legacy_fx.SECRET not in "".join(str(c) for c in env.notify.call_args_list)
    assert env.legacy_path == legacy_path
    assert legacy_path.read_bytes() == legacy_fx.legacy_bytes(legacy_fx.LEGACY)
