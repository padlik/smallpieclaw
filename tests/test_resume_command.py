"""Tests for the /resume Telegram command edge cases.

These tests exercise checkpoint listing, selection, refusal, and the
routing of a resumed run through ``_run_agent_task`` so it shares the same
progress-panel / result-display path as a normal message.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from telegram_commands import cmd_resume


def _make_iface(checkpoints=None, lock_locked=False):
    """Build a mock TelegramInterface for /resume tests."""
    iface = MagicMock()
    iface._is_authorized.return_value = True

    lock = MagicMock()
    lock.locked.return_value = lock_locked
    iface._get_agent_lock.return_value = lock

    checkpoint_store = MagicMock()
    checkpoint_store.list.return_value = checkpoints or []

    agent = MagicMock()
    agent.checkpoint_store = checkpoint_store
    iface.agent = agent
    iface._pending_resume = {}

    return iface


def _make_update(args=None):
    """Build a mock Update + Context for /resume tests."""
    user = MagicMock()
    user.id = 12345

    msg = MagicMock()
    msg.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user = user
    update.effective_message = msg

    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


@pytest.mark.asyncio
async def test_resume_busy_agent_rejected():
    """A running agent causes /resume to be rejected immediately."""
    iface = _make_iface(lock_locked=True)
    update, ctx = _make_update()
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    assert "currently running" in call_args[0][0]
    iface._run_agent_task.assert_not_called()


@pytest.mark.asyncio
async def test_resume_no_checkpoint_store():
    """If the agent has no checkpoint_store, /resume reports nothing available."""
    iface = _make_iface()
    iface.agent.checkpoint_store = None
    update, ctx = _make_update()
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    assert "No unfinished runs" in call_args[0][0]


@pytest.mark.asyncio
async def test_resume_no_checkpoints():
    """An empty checkpoint list reports nothing to resume."""
    iface = _make_iface(checkpoints=[])
    update, ctx = _make_update()
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    assert "No unfinished runs" in call_args[0][0]


@pytest.mark.asyncio
async def test_resume_non_retryable_refused():
    """A non-retryable checkpoint is refused and stays on disk."""
    checkpoint = {
        "trace_id": "r-test123",
        "user_goal": "test goal",
        "step": 3,
        "max_steps": 8,
        "error_info": {
            "type": "context",
            "retryable": False,
            "message": "Context too long",
            "detail": "...",
        },
        "created_at": "2026-08-21T14:30:00Z",
    }
    iface = _make_iface(checkpoints=[checkpoint])
    update, ctx = _make_update()
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    assert "non-retryable" in call_args[0][0]
    iface._run_agent_task.assert_not_called()


@pytest.mark.asyncio
async def test_resume_n_selects_specific():
    """/resume N selects the Nth checkpoint (1-indexed)."""
    cp1 = {
        "trace_id": "r-aaa",
        "user_goal": "goal 1",
        "step": 1,
        "max_steps": 8,
        "error_info": {"type": "timeout", "retryable": True, "message": "timed out", "detail": "..."},
        "created_at": "2026-08-21T10:00:00Z",
    }
    cp2 = {
        "trace_id": "r-bbb",
        "user_goal": "goal 2",
        "step": 2,
        "max_steps": 8,
        "error_info": {"type": "connection", "retryable": True, "message": "conn failed", "detail": "..."},
        "created_at": "2026-08-21T14:00:00Z",
    }
    iface = _make_iface(checkpoints=[cp2, cp1])  # sorted desc by created_at
    update, ctx = _make_update(args=["2"])
    iface._run_agent_task = AsyncMock()
    await cmd_resume(iface, update, ctx)
    assert iface._pending_resume.get(12345) == "r-aaa"
    iface._run_agent_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_multiple_lists():
    """With multiple checkpoints and no arg, /resume lists them."""
    cp1 = {
        "trace_id": "r-aaa",
        "user_goal": "goal 1",
        "step": 1,
        "max_steps": 8,
        "error_info": {"type": "timeout", "retryable": True, "message": "timed out", "detail": "..."},
        "created_at": "2026-08-21T10:00:00Z",
    }
    cp2 = {
        "trace_id": "r-bbb",
        "user_goal": "goal 2",
        "step": 2,
        "max_steps": 8,
        "error_info": {"type": "connection", "retryable": True, "message": "conn failed", "detail": "..."},
        "created_at": "2026-08-21T14:00:00Z",
    }
    iface = _make_iface(checkpoints=[cp2, cp1])
    update, ctx = _make_update()
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    text = call_args[0][0]
    assert "1." in text and "2." in text
    assert "/resume N" in text
    iface._run_agent_task.assert_not_called()


@pytest.mark.asyncio
async def test_resume_invalid_number():
    """An out-of-range checkpoint number is rejected."""
    cp = {
        "trace_id": "r-aaa",
        "user_goal": "goal 1",
        "step": 1,
        "max_steps": 8,
        "error_info": {"type": "timeout", "retryable": True, "message": "timed out", "detail": "..."},
        "created_at": "2026-08-21T10:00:00Z",
    }
    iface = _make_iface(checkpoints=[cp])
    update, ctx = _make_update(args=["5"])
    await cmd_resume(iface, update, ctx)
    call_args = update.effective_message.reply_text.call_args
    assert "Invalid checkpoint number" in call_args[0][0]
    iface._run_agent_task.assert_not_called()
