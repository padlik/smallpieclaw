"""Tests for the mode-status-selector feature.

Covers:
  (a) /status output includes the current creativity-mode line.
  (b) /mode with no args renders an inline selector with a button per
      non-active mode and none for the active mode.
  (c) cb_mode_switch applies a valid mode to config + live agent and edits
      the callback message.
  (d) cb_mode_switch rejects an unknown mode without mutating config.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_commands import (  # noqa: E402
    _MODES,
    _MODE_DESCRIPTIONS,
    cb_mode_switch,
    cmd_mode,
    cmd_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_iface(mode: str | None = None):
    """Build a minimal TelegramInterface without real I/O.

    Args:
        mode: When provided, seeds ``config["agent"]["mode"]``.
    """
    from telegram_interface import TelegramInterface
    import time

    config: dict = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    if mode is not None:
        config["agent"] = {"mode": mode}

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


def _make_command_update():
    """Return (update, ctx, captured) for a slash-command invocation by user 42.

    ``captured`` collects the ``text`` and ``reply_markup`` of the reply.
    """
    captured: dict = {}

    async def _reply(text, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = kwargs.get("reply_markup")
        return MagicMock()

    mock_message = MagicMock()
    mock_message.reply_text = AsyncMock(side_effect=_reply)

    mock_user = MagicMock()
    mock_user.id = 42

    update = MagicMock()
    update.effective_user = mock_user
    update.effective_message = mock_message

    ctx = MagicMock()
    ctx.args = []
    return update, ctx, captured


def _make_callback_update(data: str):
    """Return (update, query) for an inline-button callback with the given data."""
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    return update, query


# ---------------------------------------------------------------------------
# (a) /status includes the mode line
# ---------------------------------------------------------------------------

class TestStatusModeLine:
    """cmd_status must render the current creativity mode and its description."""

    def test_status_contains_mode_line(self):
        iface = _make_iface(mode="planner")
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_status(iface, update, ctx))
        text = captured["text"]
        assert "🎭 Mode:" in text
        assert "planner" in text
        assert _MODE_DESCRIPTIONS["planner"] in text

    def test_status_defaults_when_mode_absent(self):
        iface = _make_iface()  # no agent config → default
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_status(iface, update, ctx))
        text = captured["text"]
        assert "🎭 Mode:" in text
        assert "default" in text


# ---------------------------------------------------------------------------
# (b) /mode with no args renders a selector
# ---------------------------------------------------------------------------

class TestModeSelector:
    """No-arg /mode shows a button per non-active mode and none for the active one."""

    def test_no_arg_mode_shows_buttons_for_non_active_modes(self):
        iface = _make_iface(mode="default")
        update, ctx, captured = _make_command_update()
        ctx.args = []
        asyncio.run(cmd_mode(iface, update, ctx))

        markup = captured["reply_markup"]
        assert markup is not None, "no-arg /mode must attach an inline keyboard"

        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        expected = [f"mode:{m}" for m in _MODES if m != "default"]
        assert callbacks == expected
        assert "mode:default" not in callbacks
        assert len(callbacks) == len(_MODES) - 1

    def test_active_mode_excluded_when_non_default(self):
        iface = _make_iface(mode="explorer")
        update, ctx, captured = _make_command_update()
        ctx.args = []
        asyncio.run(cmd_mode(iface, update, ctx))

        markup = captured["reply_markup"]
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "mode:explorer" not in callbacks
        assert callbacks == [f"mode:{m}" for m in _MODES if m != "explorer"]


# ---------------------------------------------------------------------------
# (c) + (d) cb_mode_switch callback
# ---------------------------------------------------------------------------

class TestCbModeSwitch:
    """The inline-button callback applies valid modes and rejects unknown ones."""

    def test_valid_mode_updates_config_and_agent(self):
        iface = _make_iface(mode="default")
        iface.agent = MagicMock()  # live-apply target
        update, query = _make_callback_update("mode:planner")
        asyncio.run(cb_mode_switch(iface, update, MagicMock()))

        assert iface._config["agent"]["mode"] == "planner"
        assert iface._config["agent"]["creativity_mode"] == "planner"
        assert iface.agent.creativity_mode == "planner"
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        sent = query.edit_message_text.call_args.args[0]
        assert "planner" in sent
        assert _MODE_DESCRIPTIONS["planner"] in sent

    def test_unknown_mode_does_not_change_config(self):
        iface = _make_iface(mode="default")  # iface.agent stays None
        update, query = _make_callback_update("mode:bogus")
        asyncio.run(cb_mode_switch(iface, update, MagicMock()))

        assert iface._config["agent"]["mode"] == "default"
        assert "creativity_mode" not in iface._config["agent"]
        query.edit_message_text.assert_awaited_once()
        sent = query.edit_message_text.call_args.args[0]
        assert "Unknown mode" in sent
        assert "bogus" in sent


# ---------------------------------------------------------------------------
# Explicit-arg /mode path
# ---------------------------------------------------------------------------

class TestModeExplicitArg:
    """`/mode <name>` sets a valid mode and rejects an unknown one."""

    def test_explicit_valid_arg_applies_mode(self):
        iface = _make_iface(mode="default")
        iface.agent = MagicMock()  # live-apply target
        update, ctx, captured = _make_command_update()
        ctx.args = ["planner"]
        asyncio.run(cmd_mode(iface, update, ctx))

        assert iface._config["agent"]["mode"] == "planner"
        assert iface._config["agent"]["creativity_mode"] == "planner"
        assert iface.agent.creativity_mode == "planner"
        update.effective_message.reply_text.assert_awaited()
        assert captured["reply_markup"] is None  # confirmation, not a selector
        assert "planner" in captured["text"]

    def test_explicit_unknown_arg_errors_without_mutation(self):
        iface = _make_iface(mode="default")  # iface.agent stays None
        update, ctx, captured = _make_command_update()
        ctx.args = ["bogus"]
        asyncio.run(cmd_mode(iface, update, ctx))

        assert iface._config["agent"]["mode"] == "default"
        assert "creativity_mode" not in iface._config["agent"]
        assert "Unknown mode" in captured["text"]
        assert "bogus" in captured["text"]
