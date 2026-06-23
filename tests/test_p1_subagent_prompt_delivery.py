"""P1 regression: sub-agent confirmation prompt must stay deliverable.

The headless confirmation bridge blocks the sub-agent thread until the operator
presses Approve/Deny. If the prompt message itself cannot be delivered (e.g.
Telegram rejects the HTML because a file_patch diff contains raw <, >, &), the
sub-agent would block until the confirmation timeout. These tests verify that
untrusted description content (sensitive paths, file_patch diffs) is HTML-escaped
so the outgoing message is always valid Telegram HTML.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock


def _make_iface(allowed_id: int = 42):
    """Build a TelegramInterface with a fake bot, bypassing real I/O."""
    from telegram_interface import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.token = "fake:token"
    iface.security_mode = "allowlist"
    iface.allowed_ids = {allowed_id}
    iface._start_time = time.time()
    # Fake app/bot — capture send_message calls
    bot = MagicMock()
    bot.send_message = AsyncMock()
    app = MagicMock()
    app.bot = bot
    iface._app = app
    iface._loop = None  # forces the asyncio.run(_send()) fallback path
    return iface, bot


def _sent_text(bot) -> str:
    """Return the text= kwarg of the first send_message call."""
    assert bot.send_message.await_count >= 1
    _, kwargs = bot.send_message.await_args
    return kwargs["text"]


class TestSubagentPromptEscaping:
    def test_file_patch_diff_with_html_chars_is_escaped(self):
        iface, bot = _make_iface()
        # Mimics a file_patch description containing raw XML/HTML content
        description = (
            "Patch file: <code>/srv/app/config.xml</code>\n"
            "  - <setting name=\"debug\">false</setting>\n"
            "  + <setting name=\"debug\">true</setting>"
        )
        iface.send_subagent_confirmation_prompt("tok123", "file_patch", description)

        text = _sent_text(bot)
        # Raw, unescaped angle-bracket diff content must NOT appear (would break Telegram HTML).
        assert "<setting" not in text
        # The escaped form must be present instead.
        assert "&lt;setting" in text

    def test_ampersand_in_description_is_escaped(self):
        iface, bot = _make_iface()
        description = "Read file: <code>/data/a&b&c.txt</code>"
        iface.send_subagent_confirmation_prompt("tok", "file_read", description)
        text = _sent_text(bot)
        # A bare & would be rejected by Telegram's HTML parser.
        assert "a&b&c" not in text
        assert "a&amp;b&amp;c" in text

    def test_tool_name_is_escaped(self):
        iface, bot = _make_iface()
        iface.send_subagent_confirmation_prompt("tok", "file<>_write", "desc")
        text = _sent_text(bot)
        assert "file<>_write" not in text
        assert "file&lt;&gt;_write" in text

    def test_caller_tag_is_escaped(self):
        iface, bot = _make_iface()
        iface.send_subagent_confirmation_prompt("tok", "file_write", "desc", caller_tag="sa<x>")
        text = _sent_text(bot)
        assert "sa<x>" not in text
        assert "sa&lt;x&gt;" in text

    def test_buttons_carry_token(self):
        iface, bot = _make_iface()
        iface.send_subagent_confirmation_prompt("abc999", "file_write", "desc")
        _, kwargs = bot.send_message.await_args
        markup = kwargs["reply_markup"]
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "subconfirm_yes:abc999" in callbacks
        assert "subconfirm_no:abc999" in callbacks

    def test_no_app_raises(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface._app = None
        try:
            iface.send_subagent_confirmation_prompt("t", "file_write", "d")
            assert False, "expected RuntimeError when app not built"
        except RuntimeError:
            pass
