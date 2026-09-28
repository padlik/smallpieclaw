"""Admission and deferred-message handoff tests for /reset and /compress.

These session commands must reject while the per-user agent lock is held,
run exactly once under the lock when idle, and complete the shared
deferred-prompt handoff after releasing the lock — the same handoff
``_run_agent_task`` performs — so a message that arrives during a command is
not stranded.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_commands import cmd_compress, cmd_reset
from telegram_interface import TelegramInterface


def make_authorized_command_inputs(args=None):
    """Return an authorized lightweight interface, update, and context."""
    iface = MagicMock()
    iface._is_authorized.return_value = True
    lock = asyncio.Lock()
    iface._get_agent_lock.return_value = lock
    iface.agent_reset_fn = MagicMock(return_value="Reset done")
    iface.agent_compress_fn = MagicMock(return_value="Compress done")
    iface._safe_edit = AsyncMock()
    iface._offer_pending_deferred_prompt = AsyncMock()
    iface._pending_resume = {}
    iface._current_deferred_token = {}
    iface._deferred_messages = {}
    iface._send_deferred_prompt = AsyncMock()
    iface._run_agent_task_locked = AsyncMock()
    iface._run_agent_task = TelegramInterface._run_agent_task.__get__(iface)
    user = MagicMock(id=42)
    message = MagicMock(reply_text=AsyncMock(return_value=MagicMock()))
    update = MagicMock(effective_user=user, effective_message=message)
    ctx = MagicMock(args=[] if args is None else args)
    return iface, update, ctx


@pytest.mark.parametrize(
    ("command", "args", "callback_name"),
    [
        (cmd_reset, [], "agent_reset_fn"),
        (cmd_reset, ["discard"], "agent_reset_fn"),
        (cmd_compress, [], "agent_compress_fn"),
    ],
)
@pytest.mark.asyncio
async def test_busy_command_does_not_mutate_context(command, args, callback_name):
    iface, update, ctx = make_authorized_command_inputs(args)
    lock = iface._get_agent_lock(update.effective_user.id)
    async with lock:
        await command(iface, update, ctx)
    getattr(iface, callback_name).assert_not_called()
    iface._safe_edit.assert_not_awaited()
    iface._offer_pending_deferred_prompt.assert_not_awaited()
    assert "busy" in update.effective_message.reply_text.call_args.args[0].lower()


@pytest.mark.parametrize(
    ("command", "args", "callback_name", "expected_save"),
    [
        (cmd_reset, [], "agent_reset_fn", True),
        (cmd_reset, ["discard"], "agent_reset_fn", False),
        (cmd_compress, [], "agent_compress_fn", None),
    ],
)
@pytest.mark.asyncio
async def test_idle_command_runs_once_and_releases_lock(
    command, args, callback_name, expected_save,
):
    iface, update, ctx = make_authorized_command_inputs(args)
    await command(iface, update, ctx)
    callback = getattr(iface, callback_name)
    if expected_save is None:
        callback.assert_called_once_with()
    else:
        callback.assert_called_once_with(save=expected_save)
    assert not iface._get_agent_lock(update.effective_user.id).locked()
    iface._safe_edit.assert_awaited_once()


@pytest.mark.parametrize(
    ("command", "args", "callback_name"),
    [
        (cmd_reset, [], "agent_reset_fn"),
        (cmd_reset, ["discard"], "agent_reset_fn"),
        (cmd_compress, [], "agent_compress_fn"),
    ],
)
@pytest.mark.asyncio
async def test_worker_runs_while_per_user_lock_is_held(command, args, callback_name):
    """The worker callback must execute inside ``async with lock``.

    Instruments the real callback (dispatched through ``run_in_executor``) to
    record ``lock.locked()`` at the moment it runs.  If the worker were hoisted
    out of the ``async with lock`` block this observation would be ``False`` and
    the test would fail, guarding against a regression the status-reply-paused
    concurrency test cannot catch.
    """
    iface, update, ctx = make_authorized_command_inputs(args)
    lock = iface._get_agent_lock(update.effective_user.id)
    observed: dict[str, bool] = {}

    def record(*a, **k):
        observed["locked"] = lock.locked()
        return "worker done"

    setattr(iface, callback_name, MagicMock(side_effect=record))
    await command(iface, update, ctx)
    assert observed["locked"] is True
    assert not lock.locked()


@pytest.mark.parametrize(
    ("command", "callback_name", "fallback_fragment"),
    [
        (cmd_reset, "agent_reset_fn", "cleared"),
        (cmd_compress, "agent_compress_fn", "not available"),
    ],
)
@pytest.mark.asyncio
async def test_idle_command_without_callback_shows_fallback(
    command, callback_name, fallback_fragment,
):
    iface, update, ctx = make_authorized_command_inputs()
    setattr(iface, callback_name, None)
    await command(iface, update, ctx)
    iface._safe_edit.assert_awaited_once()
    assert fallback_fragment in iface._safe_edit.await_args.args[1].lower()
    assert not iface._get_agent_lock(update.effective_user.id).locked()


@pytest.mark.parametrize("command", [cmd_reset, cmd_compress])
@pytest.mark.asyncio
async def test_unauthorized_command_does_not_touch_lock_or_worker(command):
    iface, update, ctx = make_authorized_command_inputs()
    iface._is_authorized.return_value = False
    iface._send_unauthorized = AsyncMock()
    await command(iface, update, ctx)
    iface._send_unauthorized.assert_awaited_once()
    iface._get_agent_lock.assert_not_called()
    iface.agent_reset_fn.assert_not_called()
    iface.agent_compress_fn.assert_not_called()


@pytest.mark.parametrize("command", [cmd_reset, cmd_compress])
@pytest.mark.asyncio
async def test_message_during_command_is_deferred_and_offered_after_release(command):
    iface, update, ctx = make_authorized_command_inputs()
    iface._offer_pending_deferred_prompt = (
        TelegramInterface._offer_pending_deferred_prompt.__get__(iface)
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_status(*args, **kwargs):
        entered.set()
        await release.wait()
        return MagicMock()

    update.effective_message.reply_text.side_effect = pause_status
    command_task = asyncio.create_task(command(iface, update, ctx))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert iface._get_agent_lock(42).locked()
        message_update = MagicMock(
            effective_user=MagicMock(id=42),
            effective_message=MagicMock(reply_text=AsyncMock()),
        )
        await iface._run_agent_task(message_update, ctx, "new task")
        token = iface._current_deferred_token[42]
        deferred = iface._deferred_messages[token]
        iface._run_agent_task_locked.assert_not_awaited()
        assert "busy" in message_update.effective_message.reply_text.call_args.args[0].lower()
    finally:
        release.set()
        await command_task
    assert not iface._get_agent_lock(42).locked()
    iface._send_deferred_prompt.assert_awaited_once_with(deferred)


@pytest.mark.parametrize(
    ("command", "args", "callback_name", "expected_status"),
    [
        (cmd_reset, [], "agent_reset_fn", "❌ Reset failed."),
        (cmd_reset, ["discard"], "agent_reset_fn", "❌ Reset failed."),
        (cmd_compress, [], "agent_compress_fn", "❌ Compression failed."),
    ],
)
@pytest.mark.asyncio
async def test_worker_failure_propagates_after_deferred_prompt(
    command, args, callback_name, expected_status,
):
    iface, update, ctx = make_authorized_command_inputs(args)
    iface._offer_pending_deferred_prompt = (
        TelegramInterface._offer_pending_deferred_prompt.__get__(iface)
    )
    error = RuntimeError("worker failed secret detail")
    getattr(iface, callback_name).side_effect = error
    status_msg = update.effective_message.reply_text.return_value
    deferred = MagicMock()
    token = "seeded-token"
    iface._deferred_messages[token] = deferred
    iface._current_deferred_token[42] = token

    with pytest.raises(RuntimeError) as raised:
        await command(iface, update, ctx)

    assert raised.value is error
    iface._safe_edit.assert_awaited_once_with(status_msg, expected_status)
    assert "secret detail" not in iface._safe_edit.await_args.args[1]
    update.effective_message.reply_text.assert_awaited_once()
    iface._send_deferred_prompt.assert_awaited_once_with(deferred)
    assert not iface._get_agent_lock(42).locked()
