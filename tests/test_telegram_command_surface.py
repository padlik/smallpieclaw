"""Tests for Telegram command surface after streamline-telegram-commands.

Spec assertions:
  - BotCommand discovery omits /health and /compress; includes /reset
  - CommandHandler registration omits health; keeps compress
  - cmd_help() omits /health and /compress; includes /status and /reset
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import os
import threading
from collections import deque
from pathlib import Path
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
    iface._notice_queue = deque()
    iface._notice_gate = asyncio.Lock()
    iface._active_turns = 0
    iface._flush_tasks = set()
    iface.model_registry = None  # optional provider-model-catalog injection (task 8.1)
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

    def test_context_in_bot_commands(self):
        iface = _make_iface()
        commands = _collect_bot_commands(iface)
        assert "context" in commands, (
            "/context must remain in BotCommand registration"
        )

    def test_models_is_the_only_catalog_command(self):
        """Task 8.1: catalog features extend /models; no second command is added."""
        iface = _make_iface()
        bot_commands = _collect_bot_commands(iface)
        handler_commands = _collect_handler_command_names(iface)
        assert bot_commands.count("models") == 1
        assert handler_commands.count("models") == 1
        catalog_ish = {"catalog", "modelscatalog", "models_catalog", "modellist", "providers"}
        assert not (catalog_ish & set(bot_commands))
        assert not (catalog_ish & set(handler_commands))


# ---------------------------------------------------------------------------
# Tests: /mcp auth commands
# ---------------------------------------------------------------------------

class TestMcpAuthCommands:
    """Telegram surface for MCP OAuth flows."""

    def test_mcp_auth_unknown_server(self):
        """/mcp auth <name> surfaces errors from start_oauth_flow."""
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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
        from telegram_mcp_commands import cmd_mcp

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

    def test_mcp_auth_success_registers_tools(self):
        """After a successful OAuth flow, discovered tools are registered."""
        from telegram_mcp_commands import cmd_mcp

        iface = _make_iface()
        iface.mcp_manager = MagicMock()
        iface.mcp_manager.server_has_oauth = MagicMock(return_value=True)
        iface.mcp_manager.get_oauth_timeout = MagicMock(return_value=300)
        iface.mcp_manager.start_oauth_flow = MagicMock(
            return_value={"success": True}
        )
        discovered_tools = [MagicMock(name="tool1")]
        iface.mcp_manager.get_server_info = MagicMock(
            return_value={"tools": discovered_tools}
        )
        iface.tool_registry = MagicMock()
        iface.agent = MagicMock()
        iface.agent.context_monitor = MagicMock()
        iface.agent.context_monitor.read = MagicMock(return_value=None)

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
            mock_update.effective_chat = MagicMock()
            mock_update.effective_chat.id = 123

            mock_ctx = MagicMock()
            mock_ctx.args = ["auth", "gmail"]

            await cmd_mcp(iface, mock_update, mock_ctx)

        asyncio.run(_run())
        iface.mcp_manager.get_server_info.assert_called_once_with("gmail")
        iface.tool_registry.register_mcp_tools.assert_called_once_with(
            "gmail", discovered_tools
        )
        assert any("OAuth flow completed" in t and "gmail" in t for t in sent_texts), sent_texts


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

    def test_help_includes_context(self):
        iface = _make_iface()
        text = _get_help_text(iface)
        assert "/context" in text, (
            "cmd_help must list /context"
        )


# ---------------------------------------------------------------------------
# Tests: scheduled-result Details callback
# ---------------------------------------------------------------------------

_DETAILS_RUN_ID = "run-" + "ab12cd34" * 4


def _details_iface(record: dict | None):
    """Interface whose scheduler retains ``record`` and whose agent side is spied."""
    iface = _make_iface()
    iface.scheduler = MagicMock(spec=["execution_log"])
    iface.scheduler.execution_log = MagicMock()
    iface.scheduler.execution_log.get_result = MagicMock(return_value=record)
    iface.agent = MagicMock()
    iface._run_agent_task = AsyncMock()
    return iface


def _press_details(iface, data: str, user_id: int = 42):
    """Drive cb_job_details with a button press; return (query, message)."""
    from telegram_callbacks import cb_job_details

    message = MagicMock()
    message.reply_text = AsyncMock()
    message.reply_document = AsyncMock()

    query = MagicMock()
    query.data = data
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.message = message
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    asyncio.run(cb_job_details(iface, update, MagicMock()))
    return query, message


def _record(result: str) -> dict:
    return {
        "run_id": _DETAILS_RUN_ID,
        "tag": "nightly",
        "status": "done",
        "ts": "2026-10-05T01:00:00",
        "result": result,
    }


def _alert_text(query) -> str:
    return " ".join(str(c.args[0]) for c in query.answer.call_args_list if c.args)


class TestJobDetailsAuthorization:
    def test_unauthenticated_user_is_denied(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("secret result"))
        query, _message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID), user_id=999)
        assert "Not authorized" in _alert_text(query)

    def test_unauthenticated_user_gets_no_result_lookup(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("secret result"))
        _press_details(iface, details_callback_data(_DETAILS_RUN_ID), user_id=999)
        iface.scheduler.execution_log.get_result.assert_not_called()

    def test_unauthenticated_user_receives_no_message_or_document(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("secret result"))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID), user_id=999)
        message.reply_text.assert_not_called()
        message.reply_document.assert_not_called()


class TestJobDetailsRealScheduler:
    def test_retained_result_is_returned_from_real_execution_log(self, tmp_path):
        from telegram_formatter import details_callback_data

        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _DETAILS_RUN_ID, result="all good")])
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert message.reply_text.call_args.args[0] == "all good"

    def test_scheduler_without_execution_log_reports_unavailable(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(None)
        iface.scheduler = MagicMock(spec=[])
        query, _message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert "unavailable" in _alert_text(query).lower()


class TestJobDetailsExpiry:
    def test_unknown_or_expired_result_reports_unavailable(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(None)
        query, _message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert "unavailable" in _alert_text(query).lower()

    def test_unavailable_result_sends_no_message_or_document(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(None)
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        message.reply_text.assert_not_called()
        message.reply_document.assert_not_called()

    def test_lookup_uses_execution_id_from_callback_data(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(None)
        _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        iface.scheduler.execution_log.get_result.assert_called_once_with(_DETAILS_RUN_ID)

    def test_missing_scheduler_reports_unavailable(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(None)
        iface.scheduler = None
        query, _message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert "unavailable" in _alert_text(query).lower()

    def test_empty_execution_id_reports_unavailable(self):
        iface = _details_iface(_record("anything"))
        query, _message = _press_details(iface, "jobdetails:")
        assert "unavailable" in _alert_text(query).lower()
        iface.scheduler.execution_log.get_result.assert_not_called()


class TestJobDetailsDelivery:
    def test_short_result_is_sent_complete_as_text(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("all <b>good</b> & done"))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert message.reply_text.call_args.args[0] == "all <b>good</b> & done"

    def test_short_result_is_not_sent_as_document(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("all good"))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        message.reply_document.assert_not_called()

    def test_oversized_result_is_sent_as_complete_utf8_document(self):
        from telegram_formatter import details_callback_data, format_result_document

        result = "日本語🙂" * 3000
        record = _record(result)
        iface = _details_iface(record)
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        document = message.reply_document.call_args.kwargs["document"]
        assert document.read() == format_result_document(record)

    def test_oversized_result_document_contains_untruncated_result(self):
        from telegram_formatter import details_callback_data

        result = "line\n" * 5000 + "END-MARKER"
        iface = _details_iface(_record(result))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        document = message.reply_document.call_args.kwargs["document"]
        assert document.read().decode("utf-8").endswith("END-MARKER")

    def test_oversized_result_document_is_named_after_execution_id(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("x" * 10_000))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert message.reply_document.call_args.kwargs["filename"] == f"{_DETAILS_RUN_ID}.txt"

    def test_oversized_result_is_not_sent_as_text(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("x" * 10_000))
        _query, message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        message.reply_text.assert_not_called()

    def test_button_press_is_acknowledged(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("ok"))
        query, _message = _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        query.answer.assert_called()


class TestJobDetailsIsolation:
    @pytest.mark.parametrize("result", ["short result", "x" * 10_000], ids=["short", "oversized"])
    def test_agent_handler_is_never_invoked(self, result):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record(result))
        _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        iface.agent_handler.assert_not_called()

    @pytest.mark.parametrize(
        "record",
        [None, _record("short result"), _record("x" * 10_000)],
        ids=["expired", "short", "oversized"],
    )
    def test_agent_and_memory_are_never_touched(self, record):
        from telegram_formatter import details_callback_data

        iface = _details_iface(record)
        _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        assert iface.agent.mock_calls == []

    def test_no_agent_task_is_started(self):
        from telegram_formatter import details_callback_data

        iface = _details_iface(_record("short result"))
        _press_details(iface, details_callback_data(_DETAILS_RUN_ID))
        iface._run_agent_task.assert_not_called()


class TestJobDetailsRegistration:
    def _registered_callback_patterns(self) -> list[str]:
        from telegram.ext import CallbackQueryHandler

        patterns: list[str] = []

        def _fake_add_handler(handler):
            if isinstance(handler, CallbackQueryHandler):
                patterns.append(handler.pattern.pattern)

        iface = _make_iface()
        mock_app = MagicMock()
        mock_app.add_handler = _fake_add_handler
        mock_app.add_error_handler = MagicMock()
        iface._app = mock_app
        iface._register_handlers()
        return patterns

    def test_details_callback_handler_is_registered(self):
        from telegram_formatter import details_callback_data

        data = details_callback_data(_DETAILS_RUN_ID)
        import re

        assert any(re.search(p, data) for p in self._registered_callback_patterns())

    def test_details_callback_data_fits_telegram_limit(self):
        from telegram_formatter import CALLBACK_DATA_LIMIT, details_callback_data

        assert len(details_callback_data(_DETAILS_RUN_ID).encode("utf-8")) <= CALLBACK_DATA_LIMIT


# ---------------------------------------------------------------------------
# Tests: /jobs history <tag> and existing /jobs subcommands
# ---------------------------------------------------------------------------

def _hours_ago_ts(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id(char: str) -> str:
    return "run-" + char * 32


def _complete_entry(tag: str, run_id: str, hours_ago: float = 1.0, status: str = "done",
                    result: str = "full result") -> dict:
    return {
        "ts": _hours_ago_ts(hours_ago), "tag": tag, "task": "t", "result": result,
        "success": status == "done", "elapsed_s": 1, "model": "",
        "run_id": run_id, "status": status,
    }


def _legacy_entry(tag: str, success: bool, hours_ago: float = 1.0) -> dict:
    return {
        "ts": _hours_ago_ts(hours_ago), "tag": tag, "task": "t", "result": "trunc" * 10,
        "success": success, "elapsed_s": 1, "model": "",
    }


def _jobs_iface(tmp_path, entries: list[dict], active_tags: tuple[str, ...] = ("nightly",)):
    """Interface backed by a real execution log; only the scheduler shell is a double."""
    import json

    from scheduler import JobExecutionLog

    log = JobExecutionLog(str(tmp_path / "job_execution_log.jsonl"))
    with open(log._log_file, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")

    iface = _make_iface()
    iface.scheduler = MagicMock()
    iface.scheduler.execution_log = log
    iface.scheduler.list_jobs = MagicMock(return_value=[
        {"tag": t, "schedule": "daily at 02:00", "enabled": True} for t in active_tags
    ])
    iface.agent = MagicMock()
    iface._run_agent_task = AsyncMock()
    return iface


def _run_jobs(iface, args: list[str], user_id: int = 42):
    """Drive cmd_jobs; return the message whose replies were captured."""
    from telegram_commands import cmd_jobs

    message = MagicMock()
    message.reply_text = AsyncMock()
    message.reply_document = AsyncMock()

    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = message

    ctx = MagicMock()
    ctx.args = args
    asyncio.run(cmd_jobs(iface, update, ctx))
    return message


def _reply_text(message) -> str:
    return "\n".join(str(c.args[0]) for c in message.reply_text.call_args_list if c.args)


class TestJobsHistoryListing:
    def test_lists_every_retained_execution_id(self, tmp_path):
        entries = [_complete_entry("nightly", _run_id("1"), 3), _complete_entry("nightly", _run_id("2"), 2)]
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, entries), ["history", "nightly"]))
        assert _run_id("1") in text and _run_id("2") in text

    def test_lists_each_status(self, tmp_path):
        entries = [
            _complete_entry("nightly", _run_id("1"), 3, status="cancelled"),
            _complete_entry("nightly", _run_id("2"), 2, status="failed"),
        ]
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, entries), ["history", "nightly"]))
        assert "cancelled" in text and "failed" in text

    def test_lists_each_timestamp(self, tmp_path):
        entry = _complete_entry("nightly", _run_id("1"), 3)
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, [entry]), ["history", "nightly"]))
        assert entry["ts"] in text

    def test_lists_newest_run_first(self, tmp_path):
        entries = [_complete_entry("nightly", _run_id("1"), 3), _complete_entry("nightly", _run_id("2"), 2)]
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, entries), ["history", "nightly"]))
        assert text.index(_run_id("2")) < text.index(_run_id("1"))

    def test_omits_other_tags_runs(self, tmp_path):
        entries = [_complete_entry("nightly", _run_id("1")), _complete_entry("hourly", _run_id("9"))]
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, entries, ("nightly", "hourly")), ["history", "nightly"]))
        assert _run_id("9") not in text


class TestJobsHistoryRemovedOnceJob:
    def test_removed_job_with_retained_runs_is_listed(self, tmp_path):
        entries = [_complete_entry("once-report", _run_id("3"), status="failed")]
        iface = _jobs_iface(tmp_path, entries, active_tags=())
        text = _reply_text(_run_jobs(iface, ["history", "once-report"]))
        assert _run_id("3") in text and "failed" in text

    def test_removed_job_with_retained_runs_is_not_reported_unknown(self, tmp_path):
        entries = [_complete_entry("once-report", _run_id("3"))]
        iface = _jobs_iface(tmp_path, entries, active_tags=())
        assert "unknown" not in _reply_text(_run_jobs(iface, ["history", "once-report"])).lower()


class TestJobsHistoryLegacy:
    def test_legacy_record_is_identified_as_legacy(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_legacy_entry("nightly", True)])
        assert "legacy" in _reply_text(_run_jobs(iface, ["history", "nightly"])).lower()

    def test_legacy_record_is_identified_as_partial(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_legacy_entry("nightly", True)])
        assert "partial" in _reply_text(_run_jobs(iface, ["history", "nightly"])).lower()

    @pytest.mark.parametrize(("success", "status"), [(True, "done"), (False, "failed")])
    def test_legacy_record_shows_mapped_status(self, tmp_path, success, status):
        iface = _jobs_iface(tmp_path, [_legacy_entry("nightly", success)])
        assert status in _reply_text(_run_jobs(iface, ["history", "nightly"]))

    def test_legacy_record_shows_its_timestamp(self, tmp_path):
        entry = _legacy_entry("nightly", True)
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, [entry]), ["history", "nightly"]))
        assert entry["ts"] in text

    def test_legacy_record_gets_no_fabricated_execution_id(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_legacy_entry("nightly", True)])
        assert "run-" not in _reply_text(_run_jobs(iface, ["history", "nightly"]))

    def test_legacy_record_does_not_expose_truncated_output(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_legacy_entry("nightly", True)])
        assert "trunctrunc" not in _reply_text(_run_jobs(iface, ["history", "nightly"]))


class TestJobsHistoryUnknownVersusEmpty:
    def test_tag_without_definition_or_runs_is_reported_unknown(self, tmp_path):
        iface = _jobs_iface(tmp_path, [], active_tags=("nightly",))
        assert "unknown" in _reply_text(_run_jobs(iface, ["history", "ghost"])).lower()

    def test_unknown_tag_is_not_reported_as_empty_history(self, tmp_path):
        iface = _jobs_iface(tmp_path, [], active_tags=("nightly",))
        assert "no retained runs" not in _reply_text(_run_jobs(iface, ["history", "ghost"])).lower()

    def test_active_job_without_runs_is_reported_as_empty_history(self, tmp_path):
        iface = _jobs_iface(tmp_path, [], active_tags=("nightly",))
        assert "no retained runs" in _reply_text(_run_jobs(iface, ["history", "nightly"])).lower()

    def test_empty_history_is_not_reported_unknown(self, tmp_path):
        iface = _jobs_iface(tmp_path, [], active_tags=("nightly",))
        assert "unknown" not in _reply_text(_run_jobs(iface, ["history", "nightly"])).lower()

    def test_expired_runs_of_removed_job_make_the_tag_unknown(self, tmp_path):
        entries = [_complete_entry("once-report", _run_id("3"), hours_ago=500)]
        iface = _jobs_iface(tmp_path, entries, active_tags=())
        assert "unknown" in _reply_text(_run_jobs(iface, ["history", "once-report"])).lower()


class TestJobsHistoryUsage:
    def test_missing_tag_returns_history_usage(self, tmp_path):
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, []), ["history"]))
        assert "/jobs history" in text and "usage" in text.lower()

    def test_missing_tag_performs_no_lookup(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        _run_jobs(iface, ["history"])
        iface.scheduler.list_jobs.assert_not_called()


class TestJobsHistoryAuthentication:
    def test_unauthenticated_user_sees_no_run_ids(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        message = _run_jobs(iface, ["history", "nightly"], user_id=999)
        assert _run_id("1") not in _reply_text(message)

    def test_unauthenticated_user_triggers_no_scheduler_lookup(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        _run_jobs(iface, ["history", "nightly"], user_id=999)
        iface.scheduler.list_jobs.assert_not_called()


class TestJobsHistoryIsolation:
    def test_agent_handler_is_never_invoked(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        _run_jobs(iface, ["history", "nightly"])
        iface.agent_handler.assert_not_called()

    @pytest.mark.parametrize("tag", ["nightly", "ghost"], ids=["known", "unknown"])
    def test_agent_and_memory_are_never_touched(self, tmp_path, tag):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        _run_jobs(iface, ["history", tag])
        assert iface.agent.mock_calls == []

    def test_no_agent_task_is_started(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _run_id("1"))])
        _run_jobs(iface, ["history", "nightly"])
        iface._run_agent_task.assert_not_called()


class TestExistingJobsSubcommandsUnchanged:
    def _iface(self, tmp_path):
        iface = _jobs_iface(tmp_path, [])
        iface.scheduler.list_jobs = MagicMock(return_value=[])
        iface.scheduler.reload = MagicMock(return_value={"reloaded": 3, "failed": 1})
        iface.scheduler.remove_job = MagicMock(return_value=True)
        iface.scheduler.pause_job = MagicMock(return_value=True)
        iface.scheduler.resume_job = MagicMock(return_value=True)
        return iface

    def test_bare_jobs_lists_jobs(self, tmp_path):
        iface = self._iface(tmp_path)
        _run_jobs(iface, [])
        iface.scheduler.list_jobs.assert_called_once_with()

    def test_reload_reloads_and_reports_counts(self, tmp_path):
        iface = self._iface(tmp_path)
        text = _reply_text(_run_jobs(iface, ["reload"]))
        iface.scheduler.reload.assert_called_once_with()
        assert "Scheduler reloaded" in text and "<b>3</b>" in text and "<b>1</b>" in text

    @pytest.mark.parametrize(
        ("sub", "method", "confirmation"),
        [("pause", "pause_job", "paused"), ("resume", "resume_job", "resumed"), ("remove", "remove_job", "removed")],
    )
    def test_mutating_subcommand_acts_on_tag_and_confirms(self, tmp_path, sub, method, confirmation):
        iface = self._iface(tmp_path)
        text = _reply_text(_run_jobs(iface, [sub, "nightly"]))
        getattr(iface.scheduler, method).assert_called_once_with("nightly")
        assert confirmation in text

    @pytest.mark.parametrize(
        ("sub", "method"), [("pause", "pause_job"), ("resume", "resume_job"), ("remove", "remove_job")]
    )
    def test_mutating_subcommand_reports_not_found(self, tmp_path, sub, method):
        iface = self._iface(tmp_path)
        getattr(iface.scheduler, method).return_value = False
        assert "not found" in _reply_text(_run_jobs(iface, [sub, "ghost"]))

    @pytest.mark.parametrize("sub", ["pause", "resume", "remove"])
    def test_mutating_subcommand_without_tag_returns_usage(self, tmp_path, sub):
        iface = self._iface(tmp_path)
        text = _reply_text(_run_jobs(iface, [sub]))
        assert f"/jobs {sub}" in text and "Usage" in text

    def test_unavailable_scheduler_is_reported(self, tmp_path):
        iface = self._iface(tmp_path)
        iface.scheduler = None
        assert "Scheduler not available" in _reply_text(_run_jobs(iface, []))


# ---------------------------------------------------------------------------
# Tests: /jobs result <execution-id>
# ---------------------------------------------------------------------------

def _result_iface(tmp_path, result: str, run_id: str = _DETAILS_RUN_ID):
    return _jobs_iface(tmp_path, [_complete_entry("nightly", run_id, result=result)])


class TestJobsResultShort:
    def test_short_result_is_returned_complete(self, tmp_path):
        iface = _result_iface(tmp_path, "all good")
        message = _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        assert message.reply_text.call_args.args[0] == "all good"

    def test_result_at_message_limit_is_returned_untruncated(self, tmp_path):
        from telegram_formatter import TELEGRAM_RENDERED_LIMIT

        result = "x" * TELEGRAM_RENDERED_LIMIT
        message = _run_jobs(_result_iface(tmp_path, result), ["result", _DETAILS_RUN_ID])
        assert message.reply_text.call_args.args[0] == result

    def test_result_markup_is_sent_without_html_parsing(self, tmp_path):
        iface = _result_iface(tmp_path, "a <b>bold</b> & <unclosed")
        message = _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        assert message.reply_text.call_args.args[0] == "a <b>bold</b> & <unclosed"
        assert "parse_mode" not in message.reply_text.call_args.kwargs

    def test_short_result_is_not_sent_as_document(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "all good"), ["result", _DETAILS_RUN_ID])
        message.reply_document.assert_not_called()

    def test_empty_result_still_gets_a_non_empty_reply(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, ""), ["result", _DETAILS_RUN_ID])
        assert message.reply_text.call_args.args[0].strip()


class TestJobsResultDocument:
    def test_oversized_result_is_sent_as_complete_utf8_document(self, tmp_path):
        from telegram_formatter import format_result_document

        result = "日本語🙂" * 3000
        iface = _result_iface(tmp_path, result)
        message = _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        record = iface.scheduler.execution_log.get_result(_DETAILS_RUN_ID)
        assert message.reply_document.call_args.kwargs["document"].read() == format_result_document(record)

    def test_oversized_result_document_is_untruncated(self, tmp_path):
        iface = _result_iface(tmp_path, "line\n" * 5000 + "END-MARKER")
        message = _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        document = message.reply_document.call_args.kwargs["document"]
        assert document.read().decode("utf-8").endswith("END-MARKER")

    def test_oversized_result_document_is_named_after_execution_id(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "x" * 10_000), ["result", _DETAILS_RUN_ID])
        assert message.reply_document.call_args.kwargs["filename"] == f"{_DETAILS_RUN_ID}.txt"

    def test_result_just_over_the_limit_is_sent_as_document(self, tmp_path):
        from telegram_formatter import TELEGRAM_RENDERED_LIMIT

        result = "x" * (TELEGRAM_RENDERED_LIMIT + 1)
        message = _run_jobs(_result_iface(tmp_path, result), ["result", _DETAILS_RUN_ID])
        message.reply_document.assert_called_once()

    def test_oversized_result_is_not_sent_as_text(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "x" * 10_000), ["result", _DETAILS_RUN_ID])
        message.reply_text.assert_not_called()


class TestJobsResultUnavailable:
    def test_unknown_execution_id_reports_unavailable(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "r"), ["result", _run_id("f")])
        assert "unavailable" in _reply_text(message).lower()

    def test_expired_execution_id_reports_unavailable(self, tmp_path):
        iface = _jobs_iface(tmp_path, [_complete_entry("nightly", _DETAILS_RUN_ID, hours_ago=500)])
        message = _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        assert "unavailable" in _reply_text(message).lower()

    def test_unavailable_result_sends_no_document(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "r"), ["result", _run_id("f")])
        message.reply_document.assert_not_called()

    def test_unavailable_reply_does_not_disclose_other_results(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "SECRET-BODY"), ["result", _run_id("f")])
        assert "SECRET-BODY" not in _reply_text(message)


class TestJobsResultUsage:
    def test_missing_execution_id_returns_result_usage(self, tmp_path):
        text = _reply_text(_run_jobs(_result_iface(tmp_path, "r"), ["result"]))
        assert "/jobs result" in text and "Usage" in text

    def test_missing_execution_id_performs_no_lookup(self, tmp_path):
        from unittest.mock import patch

        iface = _result_iface(tmp_path, "r")
        with patch.object(iface.scheduler.execution_log, "get_result") as lookup:
            _run_jobs(iface, ["result"])
        lookup.assert_not_called()


class TestJobsResultAuthentication:
    def test_unauthenticated_user_receives_no_result_text(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "SECRET-BODY"), ["result", _DETAILS_RUN_ID], user_id=999)
        assert "SECRET-BODY" not in _reply_text(message)

    def test_unauthenticated_user_receives_no_document(self, tmp_path):
        message = _run_jobs(_result_iface(tmp_path, "x" * 10_000), ["result", _DETAILS_RUN_ID], user_id=999)
        message.reply_document.assert_not_called()

    def test_unauthenticated_user_triggers_no_lookup(self, tmp_path):
        from unittest.mock import patch

        iface = _result_iface(tmp_path, "r")
        with patch.object(iface.scheduler.execution_log, "get_result") as lookup:
            _run_jobs(iface, ["result", _DETAILS_RUN_ID], user_id=999)
        lookup.assert_not_called()


class TestJobsResultIsolation:
    @pytest.mark.parametrize("result", ["short result", "x" * 10_000], ids=["short", "oversized"])
    def test_agent_handler_is_never_invoked(self, tmp_path, result):
        iface = _result_iface(tmp_path, result)
        _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        iface.agent_handler.assert_not_called()

    @pytest.mark.parametrize("run_id", [_DETAILS_RUN_ID, _run_id("f")], ids=["found", "unavailable"])
    def test_agent_and_memory_are_never_touched(self, tmp_path, run_id):
        iface = _result_iface(tmp_path, "short result")
        _run_jobs(iface, ["result", run_id])
        assert iface.agent.mock_calls == []

    def test_no_agent_task_is_started(self, tmp_path):
        iface = _result_iface(tmp_path, "short result")
        _run_jobs(iface, ["result", _DETAILS_RUN_ID])
        iface._run_agent_task.assert_not_called()


class TestJobLookupsDoNotBlockTheEventLoop:
    """Execution-log reads take a threading lock, so they must leave the loop thread."""

    @staticmethod
    def _recording_log(lookup_threads: list[int], *, record: dict | None = None):
        from types import SimpleNamespace

        def history(_tag: str) -> list:
            lookup_threads.append(threading.get_ident())
            return []

        def get_result(_run_id: str) -> dict | None:
            lookup_threads.append(threading.get_ident())
            return record

        return SimpleNamespace(history=history, get_result=get_result)

    def test_jobs_history_lookup_runs_off_the_loop_thread(self):
        threads: list[int] = []
        iface = _make_iface()
        iface.scheduler = MagicMock()
        iface.scheduler.execution_log = self._recording_log(threads)
        iface.scheduler.list_jobs = MagicMock(return_value=[])

        _run_jobs(iface, ["history", "nightly"])

        assert threads and threads[0] != threading.get_ident()

    def test_jobs_result_lookup_runs_off_the_loop_thread(self):
        threads: list[int] = []
        iface = _make_iface()
        iface.scheduler = MagicMock()
        iface.scheduler.execution_log = self._recording_log(threads, record=_record("short"))

        _run_jobs(iface, ["result", _DETAILS_RUN_ID])

        assert threads and threads[0] != threading.get_ident()

    def test_details_lookup_runs_off_the_loop_thread(self):
        threads: list[int] = []
        iface = _details_iface(None)
        iface.scheduler.execution_log = self._recording_log(threads, record=_record("short"))

        _press_details(iface, f"jobdetails:{_DETAILS_RUN_ID}")

        assert threads and threads[0] != threading.get_ident()

    def test_loop_stays_responsive_while_a_result_lookup_blocks(self):
        from types import SimpleNamespace

        release = threading.Event()
        entered = threading.Event()

        def blocking_get_result(_run_id: str) -> dict:
            entered.set()
            release.wait(timeout=5)
            return _record("short")

        iface = _make_iface()
        iface.scheduler = MagicMock()
        iface.scheduler.execution_log = SimpleNamespace(get_result=blocking_get_result)

        async def scenario() -> bool:
            from telegram_commands import cmd_jobs

            message = MagicMock()
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.effective_user.id = 42
            update.effective_message = message
            ctx = MagicMock()
            ctx.args = ["result", _DETAILS_RUN_ID]
            handler = asyncio.ensure_future(cmd_jobs(iface, update, ctx))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            responsive = False
            try:
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.5)
                responsive = entered.is_set() and not handler.done()
            finally:
                release.set()
                await handler
            return responsive

        assert asyncio.run(scenario()) is True


# ---------------------------------------------------------------------------
# Tests: help discovery of the history and result views
# ---------------------------------------------------------------------------

class TestJobsHelpDiscovery:
    def test_help_documents_history_view(self):
        assert "/jobs history" in _get_help_text(_make_iface())

    def test_help_documents_result_view(self):
        assert "/jobs result" in _get_help_text(_make_iface())

    def test_help_describes_the_views_as_read_only(self):
        assert "read-only" in _get_help_text(_make_iface())

    def test_help_says_scheduled_run_notices_are_deferred_and_silent(self):
        text = _get_help_text(_make_iface())
        assert "deferred" in text and "silent" in text

    def test_help_keeps_existing_jobs_guidance(self):
        assert "list scheduled jobs" in _get_help_text(_make_iface())

    def test_jobs_usage_documents_history_view(self, tmp_path):
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, []), []))
        assert "/jobs history" in text

    def test_jobs_usage_documents_result_view(self, tmp_path):
        text = _reply_text(_run_jobs(_jobs_iface(tmp_path, []), []))
        assert "/jobs result" in text

    @pytest.mark.parametrize("hint", ["/jobs reload", "/jobs remove", "/jobs pause", "/jobs resume"])
    def test_jobs_usage_keeps_existing_management_hints(self, tmp_path, hint):
        assert hint in _reply_text(_run_jobs(_jobs_iface(tmp_path, []), []))


# ---------------------------------------------------------------------------
# Registry-backed /status and /context (no standing llm_client)
# ---------------------------------------------------------------------------
#
# A real ModelRegistry/ModelSettings with the FakeDiscovery boundary from
# tests/test_telegram_models.py supplies the current selection. The legacy
# client below stands in for a stale pre-registry ``iface.llm_client``.

_SEL_ACTIVE_ID = "p/alpha"
_SEL_GONE_ID = "p/gone"
_SEL_STATES = ("active", "no_selection", "saved_selection_unavailable")
#: Fragments that must name the current selection state, per registry state.
_SEL_EXPECTED = {
    "active": (_SEL_ACTIVE_ID,),
    "no_selection": ("no selection",),
    "saved_selection_unavailable": ("saved selection unavailable", _SEL_GONE_ID),
}


class _LegacyClient:
    """Stale legacy chat client: the pre-registry ``llm_cfg`` surface only."""

    llm_cfg = {"name": "legacy", "model": "legacy-model"}

    @staticmethod
    def get_today_usage() -> dict[str, int]:
        """Return zero usage, as the legacy client reports it."""
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class _MonitoredAgent:
    """Agent stand-in exposing only the shared ``context_monitor``."""

    def __init__(self, monitor: Any) -> None:
        """Bind the monitor read by ``/context``."""
        self.context_monitor = monitor


def _selection_registry(tmp_path: Path, state: str):
    """Return ``(registry, settings_path)`` restored into *state* after a real refresh."""
    from model_registry import CatalogDocument, ProviderState

    from tests.test_telegram_models import _inventory, _make_registry, _model, _provider_config

    provider = _provider_config("p")
    alpha = _model("alpha", "p", context_tokens=1000)
    gone = _model("gone", "p", context_tokens=1000)
    default = {"active": _SEL_ACTIVE_ID, "no_selection": None,
               "saved_selection_unavailable": _SEL_GONE_ID}[state]
    seed = CatalogDocument(
        1, 1, default,
        (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
        (alpha, gone),
    )
    registry, _settings, discovery = _make_registry(tmp_path, (provider,), seed_document=seed)
    discovery.responses["p"] = _inventory("p", (alpha,))  # the refresh drops p/gone
    registry.refresh()
    registry.restore_selection()
    assert registry.view().selection_status == state
    return registry, tmp_path / "models-config.toml"


def _run_registry_command(handler, iface) -> str:
    """Run an authorized command handler and return every reply text."""
    sent: list[str] = []

    async def _run():
        message = MagicMock()
        message.reply_text = AsyncMock(side_effect=lambda text, **kw: sent.append(text))
        update = MagicMock()
        update.effective_user.id = 42
        update.effective_message = message
        await handler(iface, update, MagicMock())

    asyncio.run(_run())
    return "\n".join(sent)


def _line_with(text: str, marker: str) -> str:
    """Return the single reply line containing *marker*."""
    lines = [line for line in text.splitlines() if marker in line]
    assert len(lines) == 1, f"expected one {marker!r} line, got {lines!r}"
    return lines[0]


def _published_monitor():
    """Return a real ContextMonitor holding one idle snapshot (no model field)."""
    from context_monitor import ContextMonitor, ContextSnapshot

    monitor = ContextMonitor()
    monitor.publish(ContextSnapshot(
        system_prompt_tokens=1_000, chat_history_tokens=2_000, tool_defs_tokens=500,
        tool_defs_by_server={"builtin": 500}, completion_reserve=4_096,
        effective_window=100_000, compaction_threshold=85_000,
        headroom_nominal=92_404, headroom_real=7_404,
        danger_level="safe", is_live=False, turn=3,
    ))
    return monitor


class TestRegistryBackedStatus:
    """/status shows the registry's current selection, never a stale client."""

    @pytest.mark.parametrize("state", _SEL_STATES)
    @pytest.mark.parametrize("standing_client", [False, True], ids=["no_standing_client", "stale_client"])
    def test_status_shows_registry_selection_without_standing_client(self, tmp_path, state, standing_client):
        from telegram_commands import cmd_status

        registry, settings_path = _selection_registry(tmp_path, state)
        before = settings_path.read_bytes()
        iface = _make_iface()
        iface.model_registry = registry
        iface.llm_client = _LegacyClient() if standing_client else None

        text = _run_registry_command(cmd_status, iface)

        llm_line = _line_with(text, "LLM")
        for fragment in _SEL_EXPECTED[state]:
            assert fragment in llm_line, (state, llm_line)
        assert "legacy-model" not in text
        if state != "active":
            assert _SEL_ACTIVE_ID not in llm_line
        # Presentation never selects a model or rewrites the saved selection.
        assert registry.view().selection_status == state
        assert settings_path.read_bytes() == before

    def test_status_without_registry_keeps_legacy_client_label(self):
        from telegram_commands import cmd_status

        iface = _make_iface()
        iface.llm_client = _LegacyClient()

        text = _run_registry_command(cmd_status, iface)

        assert "legacy / legacy-model" in _line_with(text, "LLM")

    @pytest.mark.parametrize(
        ("embeddings", "provider"),
        [
            ({"provider": "openai", "model": "embed-small"}, "openai"),
            ({"provider": "acme", "model": "embed-small"}, "acme"),
            ({"provider": "openai", "model": "embed-small",
              "api_key": "sk-inline-embed-secret-987654"}, "openai"),
        ],
        ids=["adapter_without_inline_key", "named_provider_connection", "inline_key"],
    )
    def test_status_embeddings_line_is_independent_of_active_model(
        self, tmp_path, embeddings, provider,
    ):
        from telegram_commands import cmd_status

        registry, _ = _selection_registry(tmp_path, "active")
        iface = _make_iface()
        iface.model_registry = registry
        iface._config["providers"] = {
            "acme": {"type": "anthropic", "api_key": "env:ACME_EMBED_KEY"},
        }
        iface._config["embeddings"] = embeddings

        text = _run_registry_command(cmd_status, iface)

        line = _line_with(text, "Embeddings")
        assert "embed-small" in line and provider in line
        assert "active model key" not in text and "fallback" not in line.lower()
        # A missing inline key alone is not proof that credentials are unavailable.
        assert "unavailable" not in line.lower()
        assert "sk-inline-embed-secret-987654" not in text
        assert "ACME_EMBED_KEY" not in text


class TestRegistryBackedContext:
    """/context labels the current selection without attributing the snapshot to it."""

    _LAYOUT = (
        "<b>Context Profile</b>", "Window: 100,000 tokens", "⚪ idle (last run turn 3)",
        "<pre>", "System prompt", "Chat history", "Tool defs", "Completion reserve",
        "Danger: 🟢 safe", "Headroom: 7,404 tokens", "<b>Tool defs by server:</b>", "builtin: 500",
    )

    @pytest.mark.parametrize("state", _SEL_STATES)
    @pytest.mark.parametrize("standing_client", [False, True], ids=["no_standing_client", "stale_client"])
    def test_context_labels_current_registry_selection(self, tmp_path, state, standing_client):
        from telegram_commands import cmd_context

        registry, settings_path = _selection_registry(tmp_path, state)
        before = settings_path.read_bytes()
        iface = _make_iface()
        iface.model_registry = registry
        iface.llm_client = _LegacyClient() if standing_client else None
        iface.agent = _MonitoredAgent(_published_monitor())

        text = _run_registry_command(cmd_context, iface)

        header = text.splitlines()[0]
        for fragment in _SEL_EXPECTED[state]:
            assert fragment in header, (state, header)
        assert "selection" in header.lower(), "label the current selection, not the snapshot's model"
        assert f"model: {_SEL_ACTIVE_ID}" not in text
        assert "legacy-model" not in text
        for fragment in self._LAYOUT:
            assert fragment in text, fragment
        assert registry.view().selection_status == state
        assert settings_path.read_bytes() == before

    def test_context_without_registry_keeps_legacy_client_label(self):
        from telegram_commands import cmd_context

        iface = _make_iface()
        iface.llm_client = _LegacyClient()
        iface.agent = _MonitoredAgent(_published_monitor())

        text = _run_registry_command(cmd_context, iface)

        assert "model: legacy / legacy-model" in text.splitlines()[0]
        for fragment in self._LAYOUT:
            assert fragment in text, fragment


# ---------------------------------------------------------------------------
# Ordinary-request error presentation
# ---------------------------------------------------------------------------


class _InlineExecutor(concurrent.futures.ThreadPoolExecutor):
    """Default-executor stand-in that runs work inline: no worker thread starts."""

    def submit(self, fn, /, *args, **kwargs):
        """Run *fn* now and return its completed future."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — delivered through the future
            future.set_exception(exc)
        return future


def _run_on_inline_loop(coro):
    """Run *coro* on a fresh loop whose executor is inline (closed without threads)."""
    loop = asyncio.new_event_loop()
    loop.set_default_executor(_InlineExecutor())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ordinary_turn(tmp_path: Path, handler) -> tuple[list[str], str]:
    """Drive one real ordinary turn; return status-message edits and prompt status."""
    from prompt_registry import PromptRegistry

    iface = _make_iface()
    iface._pending_resume = {}
    iface._prompt_registry = PromptRegistry(str(tmp_path / "prompts"))
    iface.agent_handler = handler
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = MagicMock()
    message.reply_text = AsyncMock(return_value=status)
    update = MagicMock()
    update.effective_user.id = 42
    update.effective_chat.id = 42
    update.effective_message = message
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()

    _run_on_inline_loop(iface._run_agent_task_locked(update, ctx, "do work", None, 42))

    edits = [call.args[0] for call in status.edit_text.await_args_list]
    (record,) = iface._prompt_registry.list_recent(5)
    return edits, record.status


class TestOrdinaryRequestErrorPresentation:
    """no_active_selection gets /models guidance; every other error is unchanged."""

    @pytest.mark.parametrize("state", ["no_selection", "saved_selection_unavailable"])
    def test_no_active_selection_offers_models_selection_and_finalizes_failed(self, tmp_path, state):
        from model_registry import ModelRegistryError

        registry, settings_path = _selection_registry(tmp_path, state)
        before = settings_path.read_bytes()
        raised: list[BaseException] = []

        def handler(*_args, **_kwargs):
            """Ordinary admission against the real registry (raises no_active_selection)."""
            try:
                return registry.admit()
            except ModelRegistryError as exc:
                raised.append(exc)
                raise

        edits, prompt_status = _ordinary_turn(tmp_path, handler)

        assert [getattr(exc, "code", None) for exc in raised] == ["no_active_selection"]
        final = edits[-1]
        assert "/models" in final, final
        assert "select" in final.lower(), final
        assert prompt_status == "failed"
        # Guidance only: no automatic substitution or saved-selection change.
        assert registry.view().active_model_id is None
        assert settings_path.read_bytes() == before

    @pytest.mark.parametrize("kind", ["unrelated_runtime_error", "other_registry_error"])
    def test_other_errors_keep_existing_presentation(self, tmp_path, kind):
        import telegram_interface
        from model_registry import ModelRegistryError

        error: Exception = (
            RuntimeError("disk boom") if kind == "unrelated_runtime_error"
            else ModelRegistryError("credential_resolution_failed")
        )

        def handler(*_args, **_kwargs):
            """Fail the ordinary request with a non-selection error."""
            raise error

        edits, prompt_status = _ordinary_turn(tmp_path, handler)

        assert edits[-1] == telegram_interface._md_to_html(f"❌ Error: {error}")[:4096]
        assert "/models" not in edits[-1]
        assert prompt_status == "failed"
