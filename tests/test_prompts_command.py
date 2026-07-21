"""Tests for the /prompts Telegram command."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from prompt_registry import PromptRegistry
from telegram_commands import cmd_help, cmd_prompts


def _make_iface(registry=None):
    """Build a minimal TelegramInterface stand-in."""
    from telegram_interface import TelegramInterface

    config = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
    iface._config = config
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface._prompt_registry = registry
    return iface


def _run_cmd(iface):
    """Invoke cmd_prompts and return all reply_text calls."""
    sent_texts: list[str] = []

    async def _run():
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=lambda text, **kw: sent_texts.append(text)
        )
        mock_user = MagicMock()
        mock_user.id = 42
        mock_update = MagicMock()
        mock_update.effective_user = mock_user
        mock_update.effective_message = mock_message
        mock_ctx = MagicMock()
        await cmd_prompts(iface, mock_update, mock_ctx)

    asyncio.run(_run())
    return sent_texts


class TestPromptsCommand:
    def test_lists_recent_prompts(self, tmp_path):
        registry = PromptRegistry(data_dir=str(tmp_path))
        r1 = registry.start("r-aaaa", "first task")
        registry.add_sub_agent(r1.prompt_id, "sa-1")
        registry.finish(r1.prompt_id, "done")
        registry.start("r-bbbb", "second task")

        iface = _make_iface(registry)
        texts = _run_cmd(iface)

        full = "\n".join(texts)
        assert "Prompt #2" in full
        assert "Prompt #1" in full
        assert "done" in full
        assert "running" in full
        assert "1 sub-agent" in full
        assert "0 sub-agent" not in full  # second prompt has none

    def test_empty_registry(self, tmp_path):
        registry = PromptRegistry(data_dir=str(tmp_path))
        iface = _make_iface(registry)
        texts = _run_cmd(iface)
        assert "No prompts recorded yet" in "\n".join(texts)

    def test_registry_unavailable(self):
        iface = _make_iface(registry=None)
        texts = _run_cmd(iface)
        assert "Prompt registry not available" in "\n".join(texts)

    def test_status_icons(self, tmp_path):
        registry = PromptRegistry(data_dir=str(tmp_path))
        registry.start("r-a", "task")
        r2 = registry.start("r-b", "task")
        registry.finish(r2.prompt_id, "failed")
        r3 = registry.start("r-c", "task")
        registry.finish(r3.prompt_id, "cancelled")

        iface = _make_iface(registry)
        texts = _run_cmd(iface)
        full = "\n".join(texts)
        assert "🔄" in full or "running" in full
        assert "❌" in full or "failed" in full
        assert "🛑" in full or "cancelled" in full

    def test_help_includes_prompts(self):
        iface = _make_iface()
        sent_texts: list[str] = []

        async def _run():
            mock_message = MagicMock()
            mock_message.reply_text = AsyncMock(
                side_effect=lambda text, **kw: sent_texts.append(text)
            )
            mock_user = MagicMock()
            mock_user.id = 42
            mock_update = MagicMock()
            mock_update.effective_user = mock_user
            mock_update.effective_message = mock_message
            await cmd_help(iface, mock_update, MagicMock())

        asyncio.run(_run())
        assert any("/prompts" in t for t in sent_texts)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
