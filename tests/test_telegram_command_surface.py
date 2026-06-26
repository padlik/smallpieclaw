"""Tests for Telegram command surface after streamline-telegram-commands.

Spec assertions:
  - BotCommand discovery omits /health and /compress; includes /reset
  - CommandHandler registration omits health; keeps compress
  - cmd_help() omits /health and /compress; includes /status and /reset
"""

from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_iface():
    """Build a minimal TelegramInterface without real I/O."""
    from telegram_interface import TelegramInterface
    import time

    config = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
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
    iface._deferred_messages = {}
    iface._current_deferred_token = {}
    iface._verbose = False
    iface._app = None
    iface._loop = None
    return iface


def _collect_bot_commands(iface) -> list[str]:
    """Run _post_init with a mocked app and return registered command names."""
    registered: list[str] = []

    async def _run():
        async def _fake_set_my_commands(commands):
            for cmd in commands:
                registered.append(cmd.command)

        mock_bot = MagicMock()
        mock_bot.set_my_commands = _fake_set_my_commands

        mock_app = MagicMock()
        mock_app.bot = mock_bot

        await iface._post_init(mock_app)

    asyncio.run(_run())
    return registered


def _collect_handler_command_names(iface) -> list[str]:
    """Run _register_handlers with a mocked app and return CommandHandler command names."""
    from telegram.ext import CommandHandler

    registered_commands: list[str] = []

    def _fake_add_handler(handler):
        if isinstance(handler, CommandHandler):
            # CommandHandler stores commands as a frozenset
            for cmd in handler.commands:
                registered_commands.append(cmd)

    mock_app = MagicMock()
    mock_app.add_handler = _fake_add_handler
    mock_app.add_error_handler = MagicMock()

    iface._app = mock_app
    iface._register_handlers()
    return registered_commands


def _get_help_text(iface) -> str:
    """Call cmd_help and capture the text sent."""
    from telegram_commands import cmd_help

    sent_texts: list[str] = []

    async def _run():
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(side_effect=lambda text, **kw: sent_texts.append(text))

        mock_user = MagicMock()
        mock_user.id = 42

        mock_update = MagicMock()
        mock_update.effective_user = mock_user
        mock_update.effective_message = mock_message

        mock_ctx = MagicMock()
        await cmd_help(iface, mock_update, mock_ctx)

    asyncio.run(_run())
    return "\n".join(sent_texts)


# ---------------------------------------------------------------------------
# Tests: BotCommand discovery
# ---------------------------------------------------------------------------

class TestBotCommandDiscovery:
    """BotCommand list passed to Telegram must omit health/compress and keep reset."""

    def test_health_not_in_bot_commands(self):
        iface = _make_iface()
        commands = _collect_bot_commands(iface)
        assert "health" not in commands, (
            "/health must not appear in BotCommand registration"
        )

    def test_compress_not_in_bot_commands(self):
        iface = _make_iface()
        commands = _collect_bot_commands(iface)
        assert "compress" not in commands, (
            "/compress must not appear in BotCommand registration (hidden command)"
        )

    def test_reset_in_bot_commands(self):
        iface = _make_iface()
        commands = _collect_bot_commands(iface)
        assert "reset" in commands, (
            "/reset must remain in BotCommand registration"
        )

    def test_status_in_bot_commands(self):
        iface = _make_iface()
        commands = _collect_bot_commands(iface)
        assert "status" in commands, (
            "/status must remain in BotCommand registration"
        )


# ---------------------------------------------------------------------------
# Tests: Handler registration
# ---------------------------------------------------------------------------

class TestHandlerRegistration:
    """CommandHandler registration must omit health and keep compress."""

    def test_health_handler_not_registered(self):
        iface = _make_iface()
        commands = _collect_handler_command_names(iface)
        assert "health" not in commands, (
            "health CommandHandler must not be registered"
        )

    def test_compress_handler_still_registered(self):
        iface = _make_iface()
        commands = _collect_handler_command_names(iface)
        assert "compress" in commands, (
            "compress CommandHandler must remain registered as a hidden command"
        )

    def test_reset_handler_registered(self):
        iface = _make_iface()
        commands = _collect_handler_command_names(iface)
        assert "reset" in commands, (
            "reset CommandHandler must remain registered"
        )

    def test_status_handler_registered(self):
        iface = _make_iface()
        commands = _collect_handler_command_names(iface)
        assert "status" in commands, (
            "status CommandHandler must remain registered"
        )


# ---------------------------------------------------------------------------
# Tests: cmd_help() output
# ---------------------------------------------------------------------------

class TestCmdHelpOutput:
    """cmd_help() must omit health/compress but keep status/reset."""

    def test_help_omits_health(self):
        iface = _make_iface()
        text = _get_help_text(iface)
        # Should not appear as a slash command in help
        assert "/health" not in text, (
            "cmd_help must not list /health"
        )

    def test_help_omits_compress(self):
        iface = _make_iface()
        text = _get_help_text(iface)
        assert "/compress" not in text, (
            "cmd_help must not list /compress"
        )

    def test_help_includes_status(self):
        iface = _make_iface()
        text = _get_help_text(iface)
        assert "/status" in text, (
            "cmd_help must still list /status"
        )

    def test_help_includes_reset(self):
        iface = _make_iface()
        text = _get_help_text(iface)
        assert "/reset" in text, (
            "cmd_help must still list /reset"
        )
