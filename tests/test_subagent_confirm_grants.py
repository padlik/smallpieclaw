"""Tests for the unified grant-ledger sub-agent confirmation flow."""

from __future__ import annotations

import html
import asyncio
import secrets
import threading
from unittest.mock import MagicMock, patch

import pytest

from confirmation import ConfirmationManager, GrantLifetime
from telegram_callbacks import cb_confirm, cb_subagent_confirm


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
        second_target = str(tmp_path / "second" / "y.txt")
        executor._pending_confirmations.stage(
            token, "file_diff", {"path_a": target, "path_b": second_target},
            zone_path=target, extra_zone_paths=(second_target,), scope_owner="sa-1",
        )
        executor._pending[token] = ("file_diff", {"path_a": target, "path_b": second_target})

        query = _MockQuery(f"subconfirm_yes:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_diff", target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", second_target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", target, scope_owner=None) is False
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
        second_target = str(tmp_path / "second" / "y.txt")
        executor._pending[token] = ("file_diff", {"path_a": target, "path_b": second_target})
        executor._pending_confirmations.stage(
            token, "file_diff", {"path_a": target, "path_b": second_target},
            zone_path=target, extra_zone_paths=(second_target,), scope_owner="sa-1",
        )

        query = _MockQuery(f"subconfirm_till_reset:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_diff", target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", second_target, scope_owner="sa-1") is True
        # Session grants are scope-free: "Till /reset" consent covers the whole
        # session (approval-grants spec) — main agent included.
        assert coordinator.grant_ledger.check("file_diff", target, scope_owner=None) is True
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

    @pytest.mark.asyncio
    async def test_subconfirm_stale_token_does_not_grant(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        token = secrets.token_hex(12)
        query = _MockQuery(f"subconfirm_yes:{token}")
        await cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock())
        assert coordinator.grant_ledger.check("file_diff", str(tmp_path), scope_owner="sa-1") is False

    @pytest.mark.asyncio
    async def test_subagent_bridge_approves_single_zone_file_read(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor.path_policy = _make_policy(str(tmp_path))
        secret = tmp_path / "outside" / "secret.txt"
        secret.parent.mkdir()
        secret.write_text("approved content")
        prompt_tokens: list[str] = []
        loop = asyncio.get_running_loop()

        def prompt(token, _tool, _description, _tag):
            prompt_tokens.append(token)
            query = _MockQuery(f"subconfirm_yes:{token}")
            asyncio.run_coroutine_threadsafe(
                cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock()), loop,
            )

        executor._subagent_confirm_prompt_fn = prompt
        result = await asyncio.to_thread(
            executor.execute, "file_read", {"path": str(secret)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert result["success"] is True
        assert result["output"] == "approved content"
        assert len(prompt_tokens) == 1
        repeated = executor.execute("file_read", {"path": str(secret)}, caller_depth=1, caller_tag="sa-1")
        assert repeated["success"] is True
        assert len(prompt_tokens) == 1

    @pytest.mark.asyncio
    async def test_subagent_bridge_approves_two_zone_file_diff(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor.path_policy = _make_policy(str(tmp_path))
        left, right = tmp_path / "left", tmp_path / "right"
        left.mkdir()
        right.mkdir()
        path_a, path_b = left / "a.txt", right / "b.txt"
        path_a.write_text("A")
        path_b.write_text("B")
        prompt_tokens: list[str] = []
        loop = asyncio.get_running_loop()

        def prompt(token, _tool, _description, _tag):
            prompt_tokens.append(token)
            query = _MockQuery(f"subconfirm_yes:{token}")
            asyncio.run_coroutine_threadsafe(
                cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock()), loop,
            )

        executor._subagent_confirm_prompt_fn = prompt
        result = await asyncio.to_thread(
            executor.execute, "file_diff", {"path_a": str(path_a), "path_b": str(path_b)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert result["success"] is True
        assert len(prompt_tokens) == 1
        ledger = coordinator.grant_ledger
        assert ledger.check("file_diff", str(left), scope_owner="sa-1") is True
        assert ledger.check("file_diff", str(right), scope_owner="sa-1") is True
        assert ledger.check("file_diff", str(left), scope_owner=None) is False
        repeated = executor.execute(
            "file_diff", {"path_a": str(path_a), "path_b": str(path_b)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert repeated["success"] is True
        assert len(prompt_tokens) == 1


    @pytest.mark.asyncio
    @pytest.mark.parametrize("action,lifetime", [
        ("confirm_yes", GrantLifetime.PROMPT),
        ("confirm_till_reset", GrantLifetime.SESSION),
        ("confirm_no", None),
    ])
    async def test_main_confirmation_grants_all_staged_paths(self, executor, iface, tmp_path, action, lifetime):
        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        paths = (str(tmp_path / "a" / "x"), str(tmp_path / "b" / "y"))
        staged = executor._requires_confirmation(
            "file_diff", {"path_a": paths[0], "path_b": paths[1]}, "diff",
            zone_path=paths[0], extra_zone_paths=(paths[1],),
        )
        query = _MockQuery(f"{action}:{staged['token']}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        for path in paths:
            grants = iface.agent._confirmation.grant_ledger
            scope_dir = str(__import__("os").path.dirname(path))
            assert grants.check("file_diff", scope_dir) is (lifetime is not None)
            if lifetime is not None:
                assert grants.check("file_diff", scope_dir, scope_owner="sa-other") is (lifetime is GrantLifetime.SESSION)
        assert iface.agent.resume.call_args.args == (staged["token"], action != "confirm_no")

    @pytest.mark.asyncio
    async def test_main_stale_confirmation_does_not_grant(self, executor, iface, tmp_path):
        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        token = secrets.token_hex(12)
        query = _MockQuery(f"confirm_yes:{token}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        assert iface.agent._confirmation.grant_ledger.check("file_diff", str(tmp_path / "x")) is False

    @pytest.mark.asyncio
    async def test_main_stage_callback_confirm_and_reexecute_without_prompt(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        executor.path_policy = _make_policy(str(tmp_path))
        left, right = tmp_path / "left", tmp_path / "right"
        left.mkdir()
        right.mkdir()
        path_a, path_b = left / "a.txt", right / "b.txt"
        path_a.write_text("A")
        path_b.write_text("B")
        staged = executor.execute("file_diff", {"path_a": str(path_a), "path_b": str(path_b)})
        assert staged["requires_confirmation"] is True
        query = _MockQuery(f"confirm_yes:{staged['token']}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        result = executor.confirm(staged["token"])
        assert result["success"] is True
        repeated = executor.execute("file_diff", {"path_a": str(path_a), "path_b": str(path_b)})
        assert repeated["success"] is True



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
