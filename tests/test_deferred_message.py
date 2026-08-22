"""Tests for deferred-latest message handling in TelegramInterface.

These tests verify the concurrency serialization behavior directly on the
TelegramInterface state machine without instantiating a real bot or Telegram
app.  They exercise:
  - Second message while agent is running → deferred storage (not double-run)
  - Deferred message replaced when a third message arrives
  - Run deferred → _run_agent_task_locked called exactly once
  - Discard deferred → _run_agent_task_locked not called; state cleaned up
  - Pre-pop bug: deferred entry persists in _deferred_messages until callback
  - Authorization: unauthorized presser cannot run/discard operator's task
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Minimal helpers
# ---------------------------------------------------------------------------

def _make_iface():
    """Build a TelegramInterface with fake config, bypassing all real I/O."""
    from telegram_interface import TelegramInterface

    config = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
    # Init only the fields needed by the deferred-message paths
    import time
    iface._config = config
    iface.token = "fake:token"
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.agent_handler = MagicMock(return_value="done")
    iface.agent_reset_fn = None
    iface.agent_compress_fn = None
    iface.agent = None
    iface.scheduler = None
    iface.tool_registry = None
    iface.llm_client = None
    iface._tool_index = None
    iface.skill_registry = None
    iface._usage_registry = None
    iface.mcp_manager = None
    iface._downloads_dir = "/tmp"
    iface._start_time = time.time()
    iface._pending_pairs = {}
    iface._agent_locks = {}
    iface._pending_resume = {}
    iface._deferred_messages = {}
    iface._current_deferred_token = {}
    iface._verbose = False
    iface._app = None
    iface._loop = None
    return iface


def _make_update(user_id: int = 42, text: str = "hello") -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    user = MagicMock()
    user.id = user_id
    update = MagicMock()
    update.effective_user = user
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1001
    return update


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _make_deferred(iface, user_id: int = 42, text: str = "deferred task"):
    """Store a fake deferred entry in iface and return its token."""
    import secrets
    from telegram_interface import _DeferredMessage
    token = secrets.token_hex(8)
    source_msg = MagicMock()
    source_msg.chat = MagicMock()
    source_msg.reply_text = AsyncMock()
    iface._deferred_messages[token] = _DeferredMessage(
        task_text=text,
        images=[],
        source_message=source_msg,
        user_id=user_id,
        token=token,
    )
    iface._current_deferred_token[user_id] = token
    return token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeferredMessageState:
    """Unit tests for _get_agent_lock and deferred state management."""

    def test_get_agent_lock_creates_lock_per_user(self):
        iface = _make_iface()
        lock_a = iface._get_agent_lock(1)
        lock_b = iface._get_agent_lock(2)
        assert lock_a is not lock_b
        assert lock_a is iface._get_agent_lock(1)  # same instance on second call

    def test_deferred_messages_dict_initially_empty(self):
        iface = _make_iface()
        assert iface._deferred_messages == {}

    def test_second_arrival_stores_deferred_message(self):
        """When the lock is already held, the new message is stored as deferred."""
        iface = _make_iface()

        async def _run():
            update = _make_update(user_id=42, text="second message")
            ctx = _make_ctx()

            # Manually acquire the lock to simulate active run
            lock = iface._get_agent_lock(42)
            await lock.acquire()
            try:
                await iface._run_agent_task(update, ctx, "second message")
            finally:
                lock.release()

            # A deferred entry should exist, keyed by a token
            token = iface._current_deferred_token.get(42)
            assert token is not None
            deferred = iface._deferred_messages.get(token)
            assert deferred is not None
            assert deferred.task_text == "second message"
            assert deferred.user_id == 42
            assert deferred.token == token

        asyncio.run(_run())

    def test_third_message_replaces_deferred(self):
        """The most-recent deferred message overwrites an older pending one."""
        iface = _make_iface()

        async def _run():
            update2 = _make_update(42, "second")
            update3 = _make_update(42, "third")
            ctx = _make_ctx()

            lock = iface._get_agent_lock(42)
            await lock.acquire()
            try:
                await iface._run_agent_task(update2, ctx, "second")
                await iface._run_agent_task(update3, ctx, "third")
            finally:
                lock.release()

            # Latest token points at the third message
            token = iface._current_deferred_token.get(42)
            assert token is not None
            deferred = iface._deferred_messages.get(token)
            assert deferred is not None
            assert deferred.task_text == "third"

        asyncio.run(_run())

    def test_no_deferred_message_when_lock_is_free(self):
        """When the agent is not running, no deferred message is stored."""
        iface = _make_iface()

        async def _run():
            update = _make_update(42, "normal task")
            ctx = _make_ctx()
            # Patch inner locked runner so we don't need a real agent
            iface._run_agent_task_locked = AsyncMock()
            await iface._run_agent_task(update, ctx, "normal task")
            assert len(iface._deferred_messages) == 0
            assert 42 not in iface._current_deferred_token

        asyncio.run(_run())

    def test_deferred_entry_persists_until_callback(self):
        """Deferred entry stays in _deferred_messages until cb_deferred acts (not pre-popped)."""
        iface = _make_iface()

        async def _run():
            update = _make_update(42, "current task")
            ctx = _make_ctx()

            source_msg = MagicMock()
            source_msg.chat = MagicMock()
            source_msg.reply_text = AsyncMock()

            # Populate a deferred entry as if the busy path ran
            token = _make_deferred(iface, user_id=42, text="pending task")

            # Mock the locked runner so no real agent runs
            iface._run_agent_task_locked = AsyncMock()
            await iface._run_agent_task(update, ctx, "current task")

            # After task the prompt is sent but entry must STILL be in the dict
            # (it is only removed when the operator presses Run or Discard)
            assert token in iface._deferred_messages

        asyncio.run(_run())

    def test_deferred_message_cleared_after_run_callback(self):
        """Deferred message is consumed (popped) when Run is triggered via cb_deferred."""
        iface = _make_iface()
        token = _make_deferred(iface, user_id=42, text="deferred task")

        async def _run():
            iface._run_agent_task = AsyncMock()
            from telegram_callbacks import cb_deferred
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = f"deferred_run:{token}"
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()
            update.callback_query.from_user = MagicMock()
            update.callback_query.from_user.id = 42
            ctx = _make_ctx()
            await cb_deferred(iface, update, ctx)

        asyncio.run(_run())
        # After run callback, the deferred entry must be consumed
        assert token not in iface._deferred_messages

    def test_deferred_message_cleared_on_discard(self):
        """Deferred message is consumed when Discard is pressed."""
        iface = _make_iface()
        token = _make_deferred(iface, user_id=42, text="stale message")

        async def _run():
            from telegram_callbacks import cb_deferred
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = f"deferred_discard:{token}"
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()
            update.callback_query.from_user = MagicMock()
            update.callback_query.from_user.id = 42
            ctx = _make_ctx()
            await cb_deferred(iface, update, ctx)

        asyncio.run(_run())
        assert token not in iface._deferred_messages

    def test_unauthorized_presser_cannot_run_deferred(self):
        """An unauthorized user pressing Run is rejected via alert; entry preserved, prompt untouched."""
        iface = _make_iface()
        token = _make_deferred(iface, user_id=42, text="operator task")

        async def _run():
            iface._run_agent_task = AsyncMock()
            from telegram_callbacks import cb_deferred
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = f"deferred_run:{token}"
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()
            # Unauthorized user (not in allowed_ids)
            update.callback_query.from_user = MagicMock()
            update.callback_query.from_user.id = 999
            ctx = _make_ctx()
            await cb_deferred(iface, update, ctx)
            # Rejection must use a private alert, not edit the shared prompt
            update.callback_query.answer.assert_awaited_once()
            assert update.callback_query.answer.call_args.kwargs.get("show_alert") is True
            update.callback_query.edit_message_text.assert_not_called()
            iface._run_agent_task.assert_not_called()

        asyncio.run(_run())
        # Entry must still be present; unauthorized presser cannot pop it
        assert token in iface._deferred_messages

    def test_wrong_owner_cannot_run_deferred(self):
        """An authorized user who is not the owner is rejected via alert; entry preserved."""
        iface = _make_iface()
        iface.allowed_ids = {42, 7}  # 7 is also authorized but not the owner
        token = _make_deferred(iface, user_id=42, text="operator task")

        async def _run():
            iface._run_agent_task = AsyncMock()
            from telegram_callbacks import cb_deferred
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = f"deferred_run:{token}"
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()
            update.callback_query.from_user = MagicMock()
            update.callback_query.from_user.id = 7
            ctx = _make_ctx()
            await cb_deferred(iface, update, ctx)
            update.callback_query.answer.assert_awaited_once()
            assert update.callback_query.answer.call_args.kwargs.get("show_alert") is True
            update.callback_query.edit_message_text.assert_not_called()
            iface._run_agent_task.assert_not_called()

        asyncio.run(_run())
        assert token in iface._deferred_messages

    def test_busy_reply_sent_to_user(self):
        """When the agent is busy, a 'busy' reply is sent immediately."""
        iface = _make_iface()

        async def _run():
            update = _make_update(42, "busy test")
            ctx = _make_ctx()

            lock = iface._get_agent_lock(42)
            await lock.acquire()
            try:
                await iface._run_agent_task(update, ctx, "busy test")
            finally:
                lock.release()

            # reply_text should have been called with the busy message
            update.effective_message.reply_text.assert_called_once()
            call_text = update.effective_message.reply_text.call_args[0][0]
            assert "busy" in call_text.lower() or "deferred" in call_text.lower()

        asyncio.run(_run())

    def test_deferred_prompt_sent_after_task_completes(self):
        """After a task ends, a Run/Discard prompt is sent for any pending item."""
        iface = _make_iface()

        # Pre-populate a deferred message via helper (creates token correctly)
        token = _make_deferred(iface, user_id=42, text="deferred task")
        source_msg = iface._deferred_messages[token].source_message

        async def _run():
            update = _make_update(42, "current task")
            ctx = _make_ctx()
            iface._run_agent_task_locked = AsyncMock()
            await iface._run_agent_task(update, ctx, "current task")

        asyncio.run(_run())
        # The deferred prompt should have been sent to the deferred source message
        source_msg.reply_text.assert_called_once()
        call_kwargs = source_msg.reply_text.call_args
        # Should contain Run and Discard buttons (reply_markup present)
        assert call_kwargs.kwargs.get("reply_markup") is not None

    def test_old_button_does_not_run_newer_deferred(self):
        """A replaced (stale) Run button resolves to 'expired' and does NOT run anything."""
        iface = _make_iface()

        ran = []

        async def _run():
            async def fake_run(update, ctx, text, images=None):
                ran.append(text)

            ctx = _make_ctx()

            # Hold the lock so both messages are deferred (not executed).
            lock = iface._get_agent_lock(42)
            await lock.acquire()
            try:
                update_old = _make_update(42, "old task")
                update_new = _make_update(42, "new task")
                await iface._run_agent_task(update_old, ctx, "old task")
                old_token = iface._current_deferred_token[42]
                # Second deferred message replaces the first.
                await iface._run_agent_task(update_new, ctx, "new task")
                new_token = iface._current_deferred_token[42]
            finally:
                lock.release()

            assert old_token != new_token
            # The old token must have been removed on replacement.
            assert old_token not in iface._deferred_messages
            assert new_token in iface._deferred_messages

            # Now the operator presses the OLD (stale) button.
            iface._run_agent_task = fake_run
            from telegram_callbacks import cb_deferred
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = f"deferred_run:{old_token}"
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()
            update.callback_query.from_user = MagicMock()
            update.callback_query.from_user.id = 42
            await cb_deferred(iface, update, ctx)

            return new_token

        new_token = asyncio.run(_run())
        # Nothing ran (stale button expired), and the new entry is untouched.
        assert ran == []
        assert new_token in iface._deferred_messages

    def test_concurrent_same_owner_double_press(self):
        """Pressing Run from two Telegram clients (same user_id) at once runs the task exactly once.

        asyncio is cooperative: the atomic pop before the first await ensures
        only the first callback invocation wins; the second sees None and treats
        it as 'expired'.
        """
        iface = _make_iface()
        token = _make_deferred(iface, user_id=42, text="concurrent task")

        run_count = []

        async def _run():
            async def fake_run(update, ctx, text, images=None):
                run_count.append(text)

            iface._run_agent_task = fake_run

            from telegram_callbacks import cb_deferred

            def _make_cb_update(tok):
                u = MagicMock()
                u.callback_query = MagicMock()
                u.callback_query.data = f"deferred_run:{tok}"
                u.callback_query.answer = AsyncMock()
                u.callback_query.edit_message_text = AsyncMock()
                u.callback_query.from_user = MagicMock()
                u.callback_query.from_user.id = 42
                return u

            ctx = _make_ctx()
            # Schedule both callbacks concurrently; gather lets them interleave
            # at await points, exercising the race window.
            await asyncio.gather(
                cb_deferred(iface, _make_cb_update(token), ctx),
                cb_deferred(iface, _make_cb_update(token), ctx),
            )

        asyncio.run(_run())
        # Exactly one run, not two.
        assert run_count == ["concurrent task"]
        assert token not in iface._deferred_messages
