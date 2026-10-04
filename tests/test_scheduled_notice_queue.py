"""Telegram-owned idle queue for scheduled run-result notices.

Scheduled notices are deferred until the operator turn is idle and typing has
stopped, serialized against new turn admission, and sent without sound.
Delivery is best-effort; job history is authoritative.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import ParseMode

from telegram_formatter import ResultNotice, details_callback_data, format_result_notice
from telegram_interface import TelegramInterface

_CONFIG = {
    "telegram": {
        "bot_token": "fake:token",
        "security_mode": "allowlist",
        "allowed_user_ids": [42],
    }
}


def _make_iface(scheduler=None) -> TelegramInterface:
    """Real interface (no bot built) with a recording send_message."""
    iface = TelegramInterface(_CONFIG, MagicMock(return_value="done"), scheduler=scheduler)
    iface._app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    iface._loop = asyncio.get_running_loop()
    return iface


async def _until(predicate, timeout: float = 2.0) -> None:
    """Yield to the loop until ``predicate()`` holds or fail after ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.005)


async def _quiet(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


def _sent(iface: TelegramInterface) -> list:
    return iface._app.bot.send_message.call_args_list


# ---------------------------------------------------------------------------
# Idle delivery and soundless sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_notice_is_sent_silently_as_html():
    iface = _make_iface()

    iface.enqueue_scheduled_notice(ResultNotice("<b>✅ Scheduled job finished: nightly</b>"))

    await _until(lambda: len(_sent(iface)) == 1)
    kwargs = _sent(iface)[0].kwargs
    assert kwargs["chat_id"] == 42
    assert "Scheduled job finished: nightly" in kwargs["text"]
    assert kwargs["parse_mode"] == ParseMode.HTML
    assert kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_every_queued_notice_is_soundless():
    iface = _make_iface()
    await iface._begin_turn()

    iface.enqueue_scheduled_notice(ResultNotice("<b>one</b>"))
    iface.enqueue_scheduled_notice(ResultNotice("<b>two</b>"))
    await iface._end_turn()

    await _until(lambda: len(_sent(iface)) == 2)
    assert [c.kwargs["disable_notification"] for c in _sent(iface)] == [True, True]
    assert [c.kwargs["text"] for c in _sent(iface)] == ["<b>one</b>", "<b>two</b>"]


@pytest.mark.asyncio
async def test_notice_enqueued_from_another_thread_is_delivered():
    iface = _make_iface()

    worker = threading.Thread(
        target=iface.enqueue_scheduled_notice, args=(ResultNotice("<b>from scheduler</b>"),)
    )
    worker.start()
    worker.join()

    await _until(lambda: len(_sent(iface)) == 1)
    assert _sent(iface)[0].kwargs["text"] == "<b>from scheduler</b>"


_RUN_ID = "run-" + "ab12cd34" * 4


@pytest.mark.asyncio
async def test_details_notice_is_sent_with_details_button_for_its_execution():
    iface = _make_iface()
    notice = format_result_notice("<b>done</b>", "y" * 10_000, _RUN_ID)

    iface.enqueue_scheduled_notice(notice)

    await _until(lambda: len(_sent(iface)) == 1)
    markup = _sent(iface)[0].kwargs["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert button.callback_data == details_callback_data(_RUN_ID)


@pytest.mark.asyncio
async def test_notice_without_details_is_sent_without_reply_markup():
    iface = _make_iface()

    iface.enqueue_scheduled_notice(ResultNotice("<b>plain</b>"))

    await _until(lambda: len(_sent(iface)) == 1)
    assert "reply_markup" not in _sent(iface)[0].kwargs


@pytest.mark.asyncio
async def test_fitting_expandable_notice_is_sent_as_one_message():
    iface = _make_iface()
    notice = format_result_notice("<b>done</b>", "&" * 3500, _RUN_ID)

    iface.enqueue_scheduled_notice(notice)
    await _quiet()

    assert [c.kwargs["text"] for c in _sent(iface)] == [notice.text]


@pytest.mark.asyncio
async def test_oversized_notice_is_sent_as_a_fitting_message():
    from telegram_formatter import rendered_length

    iface = _make_iface()

    iface.enqueue_scheduled_notice(ResultNotice("<b>" + "x" * 5000 + "</b>"))

    await _until(lambda: len(_sent(iface)) == 1)
    assert rendered_length(_sent(iface)[0].kwargs["text"]) <= 4096


# ---------------------------------------------------------------------------
# Startup: notices queued before the event loop runs
# ---------------------------------------------------------------------------


def _make_startup_iface() -> TelegramInterface:
    """Interface whose loop has not started yet, so enqueue can only queue."""
    iface = TelegramInterface(_CONFIG, MagicMock(return_value="done"))
    iface._app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(), set_my_commands=AsyncMock()),
    )
    return iface


@pytest.mark.asyncio
async def test_notice_queued_before_startup_is_sent_after_post_init():
    iface = _make_startup_iface()
    iface.enqueue_scheduled_notice(ResultNotice("<b>queued at init</b>"))
    assert _sent(iface) == []
    iface._loop = asyncio.get_running_loop()

    await iface._post_init(iface._app)

    await _until(lambda: len(_sent(iface)) == 1)
    assert _sent(iface)[0].kwargs["text"] == "<b>queued at init</b>"


@pytest.mark.asyncio
async def test_post_init_does_not_flush_while_an_operator_turn_is_active():
    iface = _make_startup_iface()
    iface._loop = asyncio.get_running_loop()
    await iface._begin_turn()
    iface.enqueue_scheduled_notice(ResultNotice("<b>held</b>"))

    await iface._post_init(iface._app)
    await _quiet()

    assert _sent(iface) == []


# ---------------------------------------------------------------------------
# Deferral while the operator turn is busy or typing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_waits_for_active_turn_then_sends():
    iface = _make_iface()
    await iface._begin_turn()

    iface.enqueue_scheduled_notice(ResultNotice("<b>held</b>"))
    await _quiet()
    assert _sent(iface) == []

    await iface._end_turn()
    await _until(lambda: len(_sent(iface)) == 1)


@pytest.mark.asyncio
async def test_notice_waits_for_final_response_and_typing_stop():
    iface = _make_iface()
    events: list[str] = []
    typing_started = threading.Event()

    class FakePanel:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def typing_loop(self) -> None:
            typing_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                events.append("typing_stopped")
                raise

        def dispatch_progress(self, *_a, **_k) -> None:
            pass

        def milestone_notify(self, *_a, **_k) -> None:
            pass

        def build_panel(self) -> str:
            return "panel"

    def handler(*_args, **_kwargs) -> str:
        assert typing_started.wait(timeout=2.0)
        iface.enqueue_scheduled_notice(ResultNotice("<b>mid-turn</b>"))
        return "final answer"

    async def record_final(*_args, **_kwargs) -> None:
        events.append("final_response")

    async def record_send(**_kwargs) -> None:
        events.append("notice")

    iface.agent_handler = handler
    iface._safe_edit = AsyncMock()
    iface._safe_edit_html = AsyncMock()
    iface._send_safe = AsyncMock(side_effect=record_final)
    iface._app.bot.send_message = AsyncMock(side_effect=record_send)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(
            reply_text=AsyncMock(return_value=MagicMock())
        ),
    )

    with patch("telegram_interface._ProgressPanel", FakePanel):
        await iface._run_agent_task(update, MagicMock(), "hi")

    await _until(lambda: "notice" in events)
    assert events == ["final_response", "typing_stopped", "notice"]


# ---------------------------------------------------------------------------
# Serialization against new turn admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_transition_is_serialized_against_turn_admission():
    iface = _make_iface()
    in_flight = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_first_send(**_kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            in_flight.set()
            await release.wait()

    iface._app.bot.send_message = AsyncMock(side_effect=slow_first_send)
    iface.enqueue_scheduled_notice(ResultNotice("<b>first</b>"))
    iface.enqueue_scheduled_notice(ResultNotice("<b>second</b>"))
    await _until(in_flight.is_set)

    admission = asyncio.create_task(iface._begin_turn())
    await _quiet()
    assert not admission.done()

    release.set()
    await admission
    sent_at_admission = calls

    iface.enqueue_scheduled_notice(ResultNotice("<b>third</b>"))
    await _quiet()
    assert calls == sent_at_admission

    await iface._end_turn()
    await _until(lambda: calls == sent_at_admission + 1)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_interface_installs_itself_as_scheduler_notice_sink():
    scheduler = MagicMock()

    iface = TelegramInterface(_CONFIG, MagicMock(), scheduler=scheduler)

    scheduler.set_result_notice_sink.assert_called_once_with(iface.enqueue_scheduled_notice)


# ---------------------------------------------------------------------------
# Cancellation and /stop during an active turn
# ---------------------------------------------------------------------------


def _stop_inputs():
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )
    return update, MagicMock()


@pytest.mark.asyncio
async def test_cancellation_notice_after_stop_waits_for_turn_end():
    from telegram_commands import cmd_stop

    iface = _make_iface()
    iface.agent = MagicMock()
    await iface._begin_turn()
    update, ctx = _stop_inputs()

    await cmd_stop(iface, update, ctx)
    iface.enqueue_scheduled_notice(ResultNotice("<b>🛑 Scheduled job cancelled: nightly</b>"))
    await _quiet()

    iface.agent.cancel.assert_called_once()
    assert _sent(iface) == []

    await iface._end_turn()
    await _until(lambda: len(_sent(iface)) == 1)
    kwargs = _sent(iface)[0].kwargs
    assert "Scheduled job cancelled: nightly" in kwargs["text"]
    assert kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_stop_is_not_blocked_by_an_in_flight_notice_send():
    from telegram_commands import cmd_stop

    iface = _make_iface()
    iface.agent = MagicMock()
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def blocked_send(**_kwargs) -> None:
        in_flight.set()
        await release.wait()

    iface._app.bot.send_message = AsyncMock(side_effect=blocked_send)
    iface.enqueue_scheduled_notice(ResultNotice("<b>slow</b>"))
    await _until(in_flight.is_set)
    update, ctx = _stop_inputs()

    try:
        await asyncio.wait_for(cmd_stop(iface, update, ctx), timeout=1.0)
    finally:
        release.set()

    iface.agent.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Restart: the queue is memory-only and best-effort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_drops_pending_notices_and_claims_no_redelivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = _make_iface()
    await before._begin_turn()
    before.enqueue_scheduled_notice(ResultNotice("<b>lost on restart</b>"))
    await _quiet()
    assert _sent(before) == []

    after = _make_iface()
    await after._begin_turn()
    await after._end_turn()
    await _quiet()

    assert _sent(after) == []
    assert _sent(before) == []
    assert list(tmp_path.rglob("*")) == []
