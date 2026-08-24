"""Tests for /context command dashboard."""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context_monitor import ContextSnapshot


def _make_iface(snapshot: ContextSnapshot | None = None, *, authorized: bool = True) -> Any:
    """Build a minimal mocked TelegramInterface for cmd_context tests."""
    monitor = MagicMock()
    monitor.read.return_value = snapshot

    agent = MagicMock()
    agent.context_monitor = monitor

    llm_cfg = {"name": "openai", "model": "gpt-4"}
    llm_client = MagicMock()
    llm_client.llm_cfg = llm_cfg

    iface = MagicMock()
    iface.agent = agent
    iface.llm_client = llm_client
    iface._is_authorized = MagicMock(return_value=authorized)
    iface._send_unauthorized = AsyncMock()
    return iface


def _make_update() -> Any:
    """Build a mocked Update that captures reply_text calls."""
    sent_texts: list[str] = []
    sent_kwargs: list[dict[str, Any]] = []

    async def _reply_text(text: str, **kwargs: Any) -> Any:
        sent_texts.append(text)
        sent_kwargs.append(kwargs)
        return MagicMock()

    message = MagicMock()
    message.reply_text = _reply_text

    user = MagicMock()
    user.id = 42

    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    return update, sent_texts, sent_kwargs


@pytest.mark.asyncio
async def test_cmd_context_dashboard_live() -> None:
    """Dashboard renders all expected sections for a live snapshot."""
    from telegram_commands import cmd_context

    snapshot = ContextSnapshot(
        system_prompt_tokens=3200,
        chat_history_tokens=45000,
        tool_defs_tokens=18000,
        tool_defs_by_server={"builtin": 12000, "github": 4000, "filesystem": 2000},
        completion_reserve=1024,
        effective_window=128000,
        compaction_threshold=108800,
        headroom_nominal=61776,
        headroom_real=61776,
        danger_level="approaching",
        is_live=True,
        turn=5,
    )
    iface = _make_iface(snapshot)
    update, sent_texts, sent_kwargs = _make_update()
    ctx = MagicMock()

    await cmd_context(iface, update, ctx)

    text = "\n".join(sent_texts)
    assert sent_texts, "Expected a reply"
    assert "Context Profile" in text
    assert "gpt-4" in text
    assert "128,000" in text
    assert "LIVE" in text
    assert "turn 5" in text
    assert "3,200" in text
    assert "45,000" in text
    assert "18,000" in text
    assert "1,024" in text
    assert "approaching" in text
    assert "61,776" in text
    assert "builtin" in text
    assert "github" in text
    assert "filesystem" in text
    assert "█" in text
    assert "░" in text
    assert sent_kwargs[0].get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_cmd_context_dashboard_idle() -> None:
    """Dashboard indicates idle state when snapshot.is_live is False."""
    from telegram_commands import cmd_context

    snapshot = ContextSnapshot(
        system_prompt_tokens=1000,
        chat_history_tokens=2000,
        tool_defs_tokens=500,
        tool_defs_by_server={"builtin": 500},
        completion_reserve=256,
        effective_window=8000,
        compaction_threshold=6800,
        headroom_nominal=4244,
        headroom_real=4244,
        danger_level="safe",
        is_live=False,
        turn=3,
    )
    iface = _make_iface(snapshot)
    update, sent_texts, _sent_kwargs = _make_update()
    ctx = MagicMock()

    await cmd_context(iface, update, ctx)

    text = "\n".join(sent_texts)
    assert "idle" in text.lower()
    assert "turn 3" in text
    assert "LIVE" not in text


@pytest.mark.asyncio
async def test_cmd_context_negative_headroom() -> None:
    """Negative headroom_real renders as a signed number, not 'N/A'."""
    from telegram_commands import cmd_context

    snapshot = ContextSnapshot(
        system_prompt_tokens=3200,
        chat_history_tokens=45000,
        tool_defs_tokens=18000,
        tool_defs_by_server={"builtin": 12000},
        completion_reserve=1024,
        effective_window=128000,
        compaction_threshold=108800,
        headroom_nominal=-5120,
        headroom_real=-5120,
        danger_level="danger",
        is_live=True,
        turn=7,
    )
    iface = _make_iface(snapshot)
    update, sent_texts, _sent_kwargs = _make_update()
    ctx = MagicMock()

    await cmd_context(iface, update, ctx)

    text = "\n".join(sent_texts)
    assert "Headroom: -5,120 tokens" in text
    assert "danger" in text


@pytest.mark.asyncio
async def test_cmd_context_no_snapshot() -> None:
    """When no snapshot is published, a clear fallback message is shown."""
    from telegram_commands import cmd_context

    iface = _make_iface(None)
    update, sent_texts, sent_kwargs = _make_update()
    ctx = MagicMock()

    await cmd_context(iface, update, ctx)

    text = "\n".join(sent_texts)
    assert "Context Profile" in text
    assert "No context snapshot" in text
    assert sent_kwargs[0].get("parse_mode") == "HTML"
