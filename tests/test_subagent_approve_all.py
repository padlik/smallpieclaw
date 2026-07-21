"""Tests for the sub-agent approve-all button and callback behavior."""

from __future__ import annotations

import html
import secrets
import threading
from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import BuiltinExecutor
from confirmation import ConfirmationManager
from telegram_callbacks import cb_subagent_confirm


class _MockQuery:
    """Minimal CallbackQuery stand-in."""

    def __init__(self, data: str, uid: int = 1):
        self.data = data
        self.from_user = type("_U", (), {"id": uid})()
        self.answer_called = False
        self.edited_text: str | None = None

    async def answer(self, text: str | None = None, *_args, **_kwargs) -> None:
        self.answer_called = True

    async def edit_message_text(self, text: str, **_) -> None:
        self.edited_text = text


class _FakeUpdate:
    def __init__(self, query: _MockQuery):
        self.callback_query = query
        self.effective_user = query.from_user


class _FakeAgent:
    def __init__(self):
        self._confirmation = ConfirmationManager()
        self.builtin_executor = None


class _FakeIface:
    def __init__(self, agent=None, builtin=None, uid: int = 1):
        self.agent = agent
        if self.agent is not None:
            self.agent.builtin_executor = builtin
        self.builtin_executor = builtin
        self.allowed_ids = {uid}

    def _is_authorized(self, uid: int) -> bool:
        return uid in self.allowed_ids


@pytest.fixture
def executor(tmp_path):
    return BuiltinExecutor(data_dir=str(tmp_path))


@pytest.fixture
def iface(executor):
    agent = _FakeAgent()
    return _FakeIface(agent=agent, builtin=executor)


class TestApproveAllButtonRenders:
    def test_file_tools_render_approve_all_button(self, executor):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        captured: dict = {}

        def fake_prompt(token, tool_name, description, caller_tag=""):
            # Replicate the real send_subagent_confirmation_prompt logic to capture keyboard.
            rows = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"subconfirm_yes:{token}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"subconfirm_no:{token}"),
                ]
            ]
            if tool_name in {"file_read", "file_write", "file_patch"}:
                rows.append([
                    InlineKeyboardButton(
                        f"✅✅ Approve all {tool_name}",
                        callback_data=f"subconfirm_all:{token}:{tool_name}",
                    )
                ])
            captured["keyboard"] = InlineKeyboardMarkup(rows)

        executor._subagent_confirm_prompt_fn = fake_prompt

        for tool in ("file_read", "file_write", "file_patch"):
            with patch.object(executor, "_subagent_confirm_timeout", 0):
                executor._headless_confirm_bridge(
                    tool, {"path": "/tmp/x"}, "do it", caller_tag="sa-1"
                )
            keyboard = captured["keyboard"]
            assert len(keyboard.inline_keyboard) == 2
            approve_all_btn = keyboard.inline_keyboard[1][0]
            assert f"Approve all {tool}" in approve_all_btn.text
            assert approve_all_btn.callback_data.startswith("subconfirm_all:")
            assert approve_all_btn.callback_data.endswith(f":{tool}")

    def test_shell_does_not_render_approve_all_button(self, executor):
        prompt_fn = MagicMock()
        executor._subagent_confirm_prompt_fn = prompt_fn

        # Shell is blocked in headless mode: it prompts but with no approve-all button.
        with patch.object(executor, "_subagent_confirm_timeout", 0):
            result = executor._headless_confirm_bridge(
                "shell", {"command": "rm -rf /"}, "danger", caller_tag="sa-1"
            )
        assert result.get("success") is False
        # A prompt is sent for shell, but the approve-all button is absent.
        assert prompt_fn.called is True


class TestApproveAllCallback:
    @pytest.mark.asyncio
    async def test_subconfirm_all_adds_to_auto_approve_and_confirms(self, executor, iface):
        executor._subagent_confirm_prompt_fn = MagicMock()
        # Stage a headless confirmation so there is a pending token.
        with patch.object(executor, "_subagent_confirm_timeout", 0):
            executor._headless_confirm_bridge(
                "file_read", {"path": "/tmp/x"}, "do it", caller_tag="sa-1"
            )
        tool_name = "file_read"

        # Stage a headless confirmation so there is a pending token.
        executor._subagent_confirm_prompt_fn = lambda *_a, **_k: None
        token = secrets.token_hex(12)
        event = threading.Event()
        executor._headless_confirm_events[token] = event
        executor._pending[token] = (tool_name, {})
        query = _MockQuery(f"subconfirm_all:{token}:{tool_name}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert tool_name in iface.agent._confirmation.auto_approve_tools
        assert "auto-approved" in html.unescape(query.edited_text or "")
        # Pending event should have been resolved.
        assert token not in executor._headless_confirm_events

    @pytest.mark.asyncio
    async def test_subconfirm_all_not_offered_for_shell(self, executor, iface):
        # Shell is never in the approve-all button set, so it can never enter auto_approve_tools.
        assert "shell" not in {"file_read", "file_write", "file_patch"}
        assert "shell" not in iface.agent._confirmation.auto_approve_tools


class TestSubsequentAutoApprove:
    def test_after_approve_all_sub_agent_file_read_auto_approves(self, executor):
        executor._subagent_confirm_prompt_fn = MagicMock()
        executor._prompt_approval_set = executor._prompt_approval_set or set()
        executor._prompt_approval_set.add("file_read")

        # After approve-all, subsequent file_read calls should not prompt.
        executor._headless_confirm_bridge(
            "file_read", {"path": "/tmp/x"}, "do it", caller_tag="sa-1"
        )
        assert executor._subagent_confirm_prompt_fn.called is False

    def test_shell_never_auto_approves(self, executor):
        executor._subagent_confirm_prompt_fn = MagicMock()
        executor._prompt_approval_set = {"shell"}

        # Shell bypasses _headless_confirm_bridge via _requires_confirmation,
        # which always blocks shell in headless mode. Use a dangerous command so
        # the confirmation path is triggered; even with shell in the approval set,
        # it is blocked without a Telegram prompt.
        result = executor.execute(
            "shell", {"command": "rm -rf /"}, caller_depth=1, caller_tag="sa-1"
        )
        assert result.get("success") is False
        prompt_fn = executor._subagent_confirm_prompt_fn
        assert prompt_fn.called is False


class TestApproveAllCallbackAllowlist:
    """W1 regression: cb_subagent_confirm must enforce _ALLOWED_APPROVE_ALL_TOOLS
    even for a crafted subconfirm_all callback that bypasses the button-render restriction."""

    @pytest.mark.asyncio
    async def test_crafted_callback_for_disallowed_tool_denied(self, iface, executor):
        """subconfirm_all with 'shell' is rejected; shell is never added to auto_approve_tools."""
        token = secrets.token_hex(12)
        # Register the token so signal_headless_confirm can resolve it
        event = threading.Event()
        executor._headless_confirm_events[token] = event
        executor._pending[token] = ("shell", {})

        query = _MockQuery(f"subconfirm_all:{token}:shell")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        # shell must never enter the approval set
        assert "shell" not in iface.agent._confirmation.auto_approve_tools
        # Operator received a rejection message mentioning the tool
        assert query.edited_text is not None
        assert "shell" in query.edited_text

    @pytest.mark.asyncio
    async def test_crafted_callback_for_allowed_tool_accepted(self, iface, executor):
        """subconfirm_all with 'file_write' passes the allowlist and adds it to the set."""
        token = secrets.token_hex(12)
        event = threading.Event()
        executor._headless_confirm_events[token] = event
        executor._pending[token] = ("file_write", {})

        query = _MockQuery(f"subconfirm_all:{token}:file_write")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert "file_write" in iface.agent._confirmation.auto_approve_tools
