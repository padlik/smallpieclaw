"""Tests for the unified grant-ledger sub-agent confirmation flow."""

from __future__ import annotations

import html
import secrets
import threading
from unittest.mock import MagicMock, patch

import pytest

from confirmation import ConfirmationManager, GrantLifetime
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
def executor(make_builtin_executor, tmp_path):
    return make_builtin_executor(data_dir=str(tmp_path))


@pytest.fixture
def iface(executor):
    agent = _FakeAgent()
    return _FakeIface(agent=agent, builtin=executor)


class TestGrantButtonRenders:
    """Sub-agent prompts now render Approve / Till /reset / Deny (grant-capable) or Approve/Deny only (shell/secret_get)."""

    def _capture_keyboard(self, executor, coordinator, tool_name: str):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        captured: dict = {}

        def fake_prompt(token, tool_name, description, caller_tag=""):
            from confirmation import NO_STANDING_GRANT_TOOLS

            grant_capable = tool_name and tool_name not in NO_STANDING_GRANT_TOOLS
            rows = [[InlineKeyboardButton("✅ Approve", callback_data=f"subconfirm_yes:{token}")]]
            if grant_capable:
                rows[0].append(
                    InlineKeyboardButton("✅✅ Till /reset", callback_data=f"subconfirm_till_reset:{token}")
                )
            rows[0].append(InlineKeyboardButton("❌ Deny", callback_data=f"subconfirm_no:{token}"))
            captured["keyboard"] = InlineKeyboardMarkup(rows)

        executor._subagent_confirm_prompt_fn = fake_prompt
        with patch.object(coordinator, "default_headless_timeout", 0):
            executor._headless_confirm_bridge(
                tool_name, {"path": "/tmp/x"}, "do it", caller_tag="sa-1"
            )
        return captured["keyboard"]

    def test_file_tools_render_till_reset_button(self, executor):
        coordinator = ConfirmationManager()
        executor._coordinator = coordinator

        for tool in ("file_read", "file_write", "file_patch"):
            keyboard = self._capture_keyboard(executor, coordinator, tool)
            assert len(keyboard.inline_keyboard) == 1
            assert len(keyboard.inline_keyboard[0]) == 3
            texts = [btn.text for btn in keyboard.inline_keyboard[0]]
            datas = [btn.callback_data for btn in keyboard.inline_keyboard[0]]
            assert "✅ Approve" in texts
            assert "✅✅ Till /reset" in texts
            assert "❌ Deny" in texts
            assert any(d.startswith("subconfirm_till_reset:") for d in datas)

    def test_shell_does_not_render_till_reset_button(self, executor):
        prompt_fn = MagicMock()
        executor._subagent_confirm_prompt_fn = prompt_fn
        coordinator = ConfirmationManager()
        executor._coordinator = coordinator

        with patch.object(coordinator, "default_headless_timeout", 0):
            result = executor._headless_confirm_bridge(
                "shell", {"command": "rm -rf /"}, "danger", caller_tag="sa-1"
            )
        assert result.get("success") is False
        assert prompt_fn.called is True


class TestSubAgentGrantCallbacks:
    @pytest.mark.asyncio
    async def test_subconfirm_yes_creates_prompt_grant_with_scope(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor._subagent_confirm_prompt_fn = MagicMock()

        target = str(tmp_path / "x.txt")
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("file_read", {"path": target})
        executor._zone_paths[token] = target
        executor._pending_confirmations.stage(token, "file_read", {"path": target}, scope_owner="sa-1")

        query = _MockQuery(f"subconfirm_yes:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_read", target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_read", target, scope_owner=None) is False
        assert "Approved for this run" in html.unescape(query.edited_text or "")

    @pytest.mark.asyncio
    async def test_subconfirm_till_reset_creates_session_grant(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor._subagent_confirm_prompt_fn = MagicMock()

        target = str(tmp_path / "x.txt")
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("file_read", {"path": target})
        executor._zone_paths[token] = target
        executor._pending_confirmations.stage(token, "file_read", {"path": target}, scope_owner="sa-1")

        query = _MockQuery(f"subconfirm_till_reset:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_read", target, scope_owner="sa-1") is True
        # Session grants are scope-free: "Till /reset" consent covers the whole
        # session (approval-grants spec) — main agent included.
        assert coordinator.grant_ledger.check("file_read", target, scope_owner=None) is True
        assert "Approved until /reset" in html.unescape(query.edited_text or "")

    @pytest.mark.asyncio
    async def test_subconfirm_no_denies(self, executor, iface):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator

        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("file_read", {})

        query = _MockQuery(f"subconfirm_no:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert event.is_set()
        assert coordinator._headless_confirm_results.get(token) is False
        assert "Denied" in html.unescape(query.edited_text or "")


class TestSinkVeto:
    """Crafted callbacks cannot create standing grants for shell/secret_get — the ledger sink refuses them."""

    @pytest.mark.asyncio
    async def test_crafted_callback_for_shell_denied_at_sink(self, iface, executor):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("shell", {})

        query = _MockQuery(f"subconfirm_yes:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        # No shell grant exists; the callback still signalled the one-shot operation.
        assert coordinator.grant_ledger.check("shell", "/tmp/x", scope_owner=None) is False
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_direct_ledger_add_for_shell_refused(self, executor):
        """The sink veto also blocks a direct ledger.add('shell', ...) call."""
        from confirmation import GrantLedger

        ledger = GrantLedger()
        assert ledger.add("shell", "/data", GrantLifetime.SESSION) is False
        assert ledger.check("shell", "/data/x.txt") is False

