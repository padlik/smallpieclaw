"""Tests for Telegram command surface after streamline-telegram-commands.

Spec assertions:
  - BotCommand discovery omits /health and /compress; includes /reset
  - CommandHandler registration omits health; keeps compress
  - cmd_help() omits /health and /compress; includes /status and /reset
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
# Tests: /mcp auth commands
# ---------------------------------------------------------------------------

class TestMcpAuthCommands:
    """Telegram surface for MCP OAuth flows."""

    def test_mcp_auth_unknown_server(self):
        """/mcp auth <name> surfaces errors from start_oauth_flow."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.server_has_oauth = MagicMock(return_value=False)
        iface.mcp_manager.get_oauth_timeout = MagicMock(return_value=300)
        iface.mcp_manager.start_oauth_flow = MagicMock(
            return_value={"success": False, "error": "Server 'unknown' not found"}
        )

        sent_texts: list[str] = []

        async def _run():
            mock_message = MagicMock()
            mock_message.reply_text = AsyncMock(side_effect=lambda text, **kw: sent_texts.append(text))

            mock_user = MagicMock()
            mock_user.id = 42

            mock_update = MagicMock()
            mock_update.effective_user = mock_user
            mock_update.effective_message = mock_message
            mock_update.effective_chat = MagicMock()
            mock_update.effective_chat.id = 123

            mock_ctx = MagicMock()
            mock_ctx.args = ["auth", "unknown"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        assert any("unknown" in t and "not found" in t for t in sent_texts), sent_texts

    def test_mcp_auth_no_oauth(self):
        """/mcp auth <name> reports missing OAuth configuration."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.server_has_oauth = MagicMock(return_value=True)
        iface.mcp_manager.get_oauth_timeout = MagicMock(return_value=300)
        iface.mcp_manager.start_oauth_flow = MagicMock(
            return_value={"success": False, "error": "Server 'local' has no OAuth configuration"}
        )

        sent_texts: list[str] = []

        async def _run():
            mock_message = MagicMock()
            mock_message.reply_text = AsyncMock(side_effect=lambda text, **kw: sent_texts.append(text))

            mock_user = MagicMock()
            mock_user.id = 42

            mock_update = MagicMock()
            mock_update.effective_user = mock_user
            mock_update.effective_message = mock_message
            mock_update.effective_chat = MagicMock()
            mock_update.effective_chat.id = 123

            mock_ctx = MagicMock()
            mock_ctx.args = ["auth", "local"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        assert any("no OAuth configuration" in t for t in sent_texts), sent_texts

    def test_mcp_auth_status_format(self):
        """/mcp auth status lists servers with OAuth details."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.server_has_oauth = MagicMock(side_effect=lambda name: name == "gmail")
        iface.mcp_manager.list_servers = MagicMock(
            return_value=[
                {"name": "gmail", "status": "needs_auth"},
                {"name": "stdio_srv", "status": "active"},
            ]
        )

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
            mock_ctx.args = ["auth", "status"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        text = "\n".join(sent_texts)
        assert "gmail" in text
        assert "stdio_srv" in text
        assert "needs_auth" in text
        assert "active" in text
        assert "no OAuth" in text or "needs authentication" in text

    def test_mcp_auth_status_shows_expiry_and_refresh(self):
        """/mcp auth status shows token expiry and refresh-token availability."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.server_has_oauth = MagicMock(return_value=True)
        iface.mcp_manager.list_servers = MagicMock(
            return_value=[{"name": "gmail", "status": "active"}]
        )
        iface.mcp_manager.get_token_info = MagicMock(
            return_value={
                "has_token": True,
                "expires_in": 3600,
                "has_refresh": True,
                "scope": "read_mail",
            }
        )

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
            mock_ctx.args = ["auth", "status"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        text = "\n".join(sent_texts)
        assert "gmail" in text
        assert "expires in 3600s" in text
        assert "refresh: available" in text

    def test_mcp_auth_revoke_deletes_token(self):
        """/mcp auth revoke <name> delegates to revoke_server and reports success."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.revoke_server = MagicMock(return_value=True)

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
            mock_ctx.args = ["auth", "revoke", "gmail"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        iface.mcp_manager.revoke_server.assert_called_once_with("gmail")
        assert any("Token revoked" in t and "gmail" in t for t in sent_texts), sent_texts

    def test_mcp_auth_cancel_success(self):
        """/mcp auth cancel requests cancellation of in-progress flow."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.cancel_oauth_flow = MagicMock(
            return_value={"success": True}
        )

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
            mock_ctx.args = ["auth", "cancel"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        iface.mcp_manager.cancel_oauth_flow.assert_called_once()
        assert any("cancellation requested" in t.lower() for t in sent_texts)

    def test_mcp_auth_cancel_no_flow(self):
        """/mcp auth cancel when no flow is in progress reports error."""
        from telegram_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.cancel_oauth_flow = MagicMock(
            return_value={"success": False, "error": "No OAuth flow in progress"}
        )

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
            mock_ctx.args = ["auth", "cancel"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        iface.mcp_manager.cancel_oauth_flow.assert_called_once()
        assert any("No OAuth flow in progress" in t for t in sent_texts)


# ---------------------------------------------------------------------------
# Tests: Handler registration
# ---------------------------------------------------------------------------
# Tests: send_oauth_prompt
# ---------------------------------------------------------------------------

class TestSendOauthPrompt:
    """Direct tests for TelegramInterface.send_oauth_prompt."""

    @pytest.fixture(autouse=True)
    def _patch_run_coroutine_threadsafe(self, monkeypatch):
        """Capture coroutines submitted via run_coroutine_threadsafe so tests can run them."""
        self._captured: list[tuple[Any, asyncio.AbstractEventLoop]] = []

        def _fake_run_coroutine_threadsafe(coro, loop):
            self._captured.append((coro, loop))
            return MagicMock()

        monkeypatch.setattr(
            asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe
        )

    def _make_iface_with_app(self):
        from telegram_interface import TelegramInterface

        iface = TelegramInterface.__new__(TelegramInterface)
        iface._app = MagicMock()
        iface._app.bot.send_message = AsyncMock()
        iface._loop = MagicMock()
        iface._loop.is_running.return_value = True
        return iface

    def test_successful_delivery(self, caplog):
        """A running loop schedules send_message with the expected payload."""
        from telegram.constants import ParseMode

        iface = self._make_iface_with_app()
        caplog.set_level(logging.INFO, logger="telegram_interface")

        iface.send_oauth_prompt(123, "gmail", "https://auth.example.com", timeout=300)

        assert len(self._captured) == 1
        coro, loop = self._captured[0]
        asyncio.run(coro)
        loop.is_running.assert_called_once()

        call_kwargs = iface._app.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert "gmail" in call_kwargs["text"]
        assert "5 min" in call_kwargs["text"]
        assert "<b>Authorize</b>" in call_kwargs["text"]
        assert call_kwargs["parse_mode"] == ParseMode.HTML
        assert call_kwargs["reply_markup"] is not None
        assert any(
            "OAuth prompt delivered to chat 123 (MCP [gmail])" in rec.message
            for rec in caplog.records
        )

    def test_app_not_built_logs_warning(self, caplog):
        """Without _app, the prompt is not sent and a warning is logged."""
        from telegram_interface import TelegramInterface

        iface = TelegramInterface.__new__(TelegramInterface)
        iface._app = None
        iface._loop = MagicMock()
        caplog.set_level(logging.WARNING, logger="telegram_interface")

        iface.send_oauth_prompt(123, "gmail", "https://auth.example.com")

        assert len(self._captured) == 0
        assert any("app not built" in rec.message for rec in caplog.records)

    def test_loop_not_running_logs_warning(self, caplog):
        """Without a running loop, the prompt is not sent and a warning is logged."""
        iface = self._make_iface_with_app()
        iface._loop.is_running.return_value = False
        caplog.set_level(logging.WARNING, logger="telegram_interface")

        iface.send_oauth_prompt(123, "gmail", "https://auth.example.com")

        assert len(self._captured) == 0
        iface._app.bot.send_message.assert_not_called()
        assert any("loop not running" in rec.message for rec in caplog.records)

    def test_send_message_failure_logs_warning(self, caplog):
        """An exception from bot.send_message is caught, logged, and re-raised."""
        iface = self._make_iface_with_app()
        iface._app.bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        caplog.set_level(logging.WARNING, logger="telegram_interface")

        iface.send_oauth_prompt(123, "gmail", "https://auth.example.com")

        assert len(self._captured) == 1
        coro, _loop = self._captured[0]
        # The coroutine must re-raise so the redirect handler can abort the flow.
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(coro)
        assert any(
            "Failed to send OAuth prompt for MCP [gmail] to chat 123" in rec.message
            for rec in caplog.records
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
