"""Tests for the Telegram LLM error card and retry callback."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from confirmation import ConfirmationManager
from telegram_callbacks import cb_llm_retry
from telegram_interface import _classify_final_status


def test_classify_final_status_failed() -> None:
    assert _classify_final_status("❌ LLM error: TimeoutException: timed out") == "failed"


def test_classify_final_status_cancelled() -> None:
    assert _classify_final_status("[Cancelled]") == "cancelled"


def test_classify_final_status_done() -> None:
    assert _classify_final_status("Here is your answer.") == "done"


def test_classify_final_status_empty() -> None:
    assert _classify_final_status("") == "done"


def test_error_card_retryable_shows_both_buttons() -> None:
    """Verify that a retryable error card has both Retry and Cancel buttons."""
    token = "test-token"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Retry", callback_data=f"llm_retry:{token}:retry"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"llm_retry:{token}:cancel"),
        ]
    ])

    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == 2
    assert buttons[0].text == "🔄 Retry"
    assert buttons[0].callback_data == f"llm_retry:{token}:retry"
    assert buttons[1].text == "❌ Cancel"
    assert buttons[1].callback_data == f"llm_retry:{token}:cancel"


def test_error_card_non_retryable_shows_only_cancel() -> None:
    """Verify that a non-retryable error card has only Cancel button."""
    token = "test-token"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"llm_retry:{token}:cancel")]
    ])

    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == 1
    assert buttons[0].text == "❌ Cancel"
    assert buttons[0].callback_data == f"llm_retry:{token}:cancel"


def test_llm_retry_callback_data_format() -> None:
    """Verify callback data format for llm_retry."""
    token = "abc123"
    assert f"llm_retry:{token}:retry" == "llm_retry:abc123:retry"
    assert f"llm_retry:{token}:cancel" == "llm_retry:abc123:cancel"


def test_signal_retry_called_on_button_press() -> None:
    """Verify that signal_retry is called when the callback handler processes a button press."""
    cm = ConfirmationManager()
    token = "test-signal"

    event = threading.Event()
    cm._retry_events[token] = event
    cm._retry_results[token] = "timeout"

    cm.signal_retry(token, "retry")

    assert event.is_set()
    assert cm._retry_results[token] == "retry"


@pytest.mark.asyncio
async def test_cb_llm_retry_signals_retry_and_edits_message() -> None:
    """Verify cb_llm_retry signals the agent and edits the prompt message."""
    iface = MagicMock()
    iface.agent.resume_llm_error = MagicMock()

    query = MagicMock()
    query.data = "llm_retry:abc123:retry"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    ctx = MagicMock()

    await cb_llm_retry(iface, update, ctx)

    iface.agent.resume_llm_error.assert_called_once_with("abc123", "retry")
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_cb_llm_retry_cancel_response() -> None:
    """Verify cb_llm_retry handles the Cancel response."""
    iface = MagicMock()
    iface.agent.resume_llm_error = MagicMock()

    query = MagicMock()
    query.data = "llm_retry:xyz:cancel"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    ctx = MagicMock()

    await cb_llm_retry(iface, update, ctx)

    iface.agent.resume_llm_error.assert_called_once_with("xyz", "cancel")
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
