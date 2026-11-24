"""Telegram final-status classification and inert stale LLM Retry/Cancel callbacks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from checkpoint_store import CheckpointStore
from telegram_callbacks import cb_llm_retry
from telegram_interface import TelegramInterface, _classify_final_status


def test_classify_final_status_failed() -> None:
    assert _classify_final_status("❌ LLM error: TimeoutException: timed out") == "failed"


def test_classify_final_status_cancelled() -> None:
    assert _classify_final_status("[Cancelled]") == "cancelled"


def test_classify_final_status_done() -> None:
    assert _classify_final_status("Here is your answer.") == "done"


def test_classify_final_status_empty() -> None:
    assert _classify_final_status("") == "done"


def test_no_live_llm_error_card_surface_exists() -> None:
    """New executions publish no LLM retry card, token or panel handler."""
    assert not hasattr(TelegramInterface, "_send_llm_error_prompt")
    from telegram_interface import _ProgressPanel

    panel = _ProgressPanel(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    assert "__LLM_ERROR__:" not in panel._progress_dispatch
    assert not hasattr(panel, "_handle_llm_error_progress")


def _stale_update(data: str, *, authorized: bool = True):
    iface = MagicMock()
    iface._is_authorized.return_value = authorized
    query = MagicMock()
    query.data = data
    query.from_user.id = 7
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return iface, query, update


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "cancel"])
async def test_authenticated_stale_button_is_expired_with_resume_guidance(
    tmp_path: Path, action: str,
) -> None:
    store = CheckpointStore(str(tmp_path))
    assert store.save("r-aaaa0001", {"created_at": "2026-08-21T14:30:00Z"})
    path = Path(store._checkpoint_dir) / "r-aaaa0001.json"
    before = path.read_bytes()
    iface, query, update = _stale_update(f"llm_retry:abc123:{action}")
    iface.agent.checkpoint_store = store

    await cb_llm_retry(iface, update, MagicMock())

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    text = query.edit_message_text.call_args.args[0]
    assert "expired" in text.lower() and "/resume" in text
    assert "Retrying" not in text and "Cancelled" not in text
    assert "reply_markup" not in query.edit_message_text.call_args.kwargs  # no replacement buttons
    assert iface.agent.method_calls == []  # no run, authorization or signal reached the agent
    assert path.read_bytes() == before  # checkpoint neither deleted nor rewritten


@pytest.mark.asyncio
async def test_unauthenticated_stale_button_is_denied_without_disclosure() -> None:
    iface, query, update = _stale_update("llm_retry:abc123:retry", authorized=False)

    await cb_llm_retry(iface, update, MagicMock())

    query.answer.assert_awaited_once_with("⛔ Not authorized.", show_alert=True)
    query.edit_message_text.assert_not_called()
    assert iface.agent.method_calls == []


@pytest.mark.asyncio
async def test_stale_button_edit_failure_is_swallowed() -> None:
    iface, query, update = _stale_update("llm_retry:abc123:retry")
    query.edit_message_text.side_effect = RuntimeError("message gone")
    await cb_llm_retry(iface, update, MagicMock())
    query.answer.assert_awaited_once()
