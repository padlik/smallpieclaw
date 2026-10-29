"""Real /jobs handlers must report scheduler file failures truthfully."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduler import Scheduler
from telegram_commands import cmd_jobs
from telegram_interface import TelegramInterface
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
