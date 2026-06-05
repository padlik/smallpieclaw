"""
tests/test_madplan_classifier.py
Tests for TelegramInterface._classify_madplan_intent() routing logic,
per-user _UserMadPlanState isolation (fixes #2 and #9),
and /mad_plan show command.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock


def _make_iface(llm_response: str):
    """Build a minimal TelegramInterface stub with a mock llm_client."""
    from telegram_interface import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.llm_client = MagicMock()
    iface.llm_client.chat.return_value = llm_response
    iface._user_state = {}
    return iface


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestClassifyMadplanIntent:
    def test_plan_task_returned(self):
        iface = _make_iface("PLAN_TASK")
        result = _run(iface._classify_madplan_intent("Write a script to check disk usage", False))
        assert result == "PLAN_TASK"

    def test_plan_feedback_with_pending(self):
        iface = _make_iface("PLAN_FEEDBACK")
        result = _run(iface._classify_madplan_intent("Add a fallback step using Docker", True))
        assert result == "PLAN_FEEDBACK"

    def test_plan_feedback_without_pending_becomes_plan_task(self):
        """PLAN_FEEDBACK is impossible without a pending plan — must be rewritten to PLAN_TASK."""
        iface = _make_iface("PLAN_FEEDBACK")
        result = _run(iface._classify_madplan_intent("some feedback-ish text", False))
        assert result == "PLAN_TASK"

    def test_question_returned(self):
        iface = _make_iface("QUESTION")
        result = _run(iface._classify_madplan_intent("How many plans do I have?", False))
        assert result == "QUESTION"

    def test_conversational_returned(self):
        iface = _make_iface("CONVERSATIONAL")
        result = _run(iface._classify_madplan_intent("ok", False))
        assert result == "CONVERSATIONAL"

    def test_llm_error_falls_back_to_plan_task(self):
        iface = _make_iface("")
        iface.llm_client.chat.side_effect = RuntimeError("LLM unavailable")
        result = _run(iface._classify_madplan_intent("some task", False))
        assert result == "PLAN_TASK"

    def test_llm_error_with_pending_falls_back_to_plan_feedback(self):
        iface = _make_iface("")
        iface.llm_client.chat.side_effect = RuntimeError("LLM unavailable")
        result = _run(iface._classify_madplan_intent("some input", True))
        assert result == "PLAN_FEEDBACK"

    def test_no_llm_client_falls_back_to_plan_task(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface.llm_client = None
        iface._user_state = {}
        result = _run(iface._classify_madplan_intent("some task", False))
        assert result == "PLAN_TASK"

    def test_no_llm_client_with_pending_falls_back_to_plan_feedback(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface.llm_client = None
        iface._user_state = {}
        result = _run(iface._classify_madplan_intent("some input", True))
        assert result == "PLAN_FEEDBACK"

    def test_unknown_label_falls_back(self):
        """Unrecognised LLM output → fallback."""
        iface = _make_iface("HALLUCINATED_LABEL")
        result = _run(iface._classify_madplan_intent("something", False))
        assert result == "PLAN_TASK"

    def test_label_case_insensitive(self):
        """LLM may return lowercase — should still match."""
        iface = _make_iface("question")
        result = _run(iface._classify_madplan_intent("what tools do you have?", False))
        assert result == "QUESTION"

    def test_label_with_trailing_whitespace(self):
        iface = _make_iface("  CONVERSATIONAL  \n")
        result = _run(iface._classify_madplan_intent("ok thanks", False))
        assert result == "CONVERSATIONAL"


class TestPerUserMadPlanState:
    """Tests for per-user state isolation and plan_id verification (fixes #2 and #9)."""

    def _make_iface(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface._user_state = {}
        return iface

    def test_get_user_state_creates_on_first_access(self):
        iface = self._make_iface()
        state = iface._get_user_state(42)
        assert state.agent_mode == "agent"
        assert state.pending_plan is None
        assert state.plan_id == ""

    def test_get_user_state_returns_same_object(self):
        iface = self._make_iface()
        s1 = iface._get_user_state(42)
        s2 = iface._get_user_state(42)
        assert s1 is s2

    def test_different_users_have_isolated_state(self):
        iface = self._make_iface()
        s1 = iface._get_user_state(1)
        s2 = iface._get_user_state(2)
        s1.agent_mode = "madplan"
        s1.plan_id = "aabbccdd"
        assert s2.agent_mode == "agent"
        assert s2.plan_id == ""

    def test_user_state_mutations_are_persistent(self):
        iface = self._make_iface()
        state = iface._get_user_state(99)
        state.agent_mode = "madplan"
        state.pending_plan = {"task": "test"}
        state.plan_id = "abcd1234"
        retrieved = iface._get_user_state(99)
        assert retrieved.agent_mode == "madplan"
        assert retrieved.pending_plan == {"task": "test"}
        assert retrieved.plan_id == "abcd1234"

    def test_plan_id_field_exists_on_dataclass(self):
        from telegram_interface import _UserState
        s = _UserState()
        assert s.plan_id == ""
        s.plan_id = "deadbeef"
        assert s.plan_id == "deadbeef"

    def test_pending_plan_name_override_defaults_empty(self):
        from telegram_interface import _UserState
        s = _UserState()
        assert s.pending_plan_name_override == ""


def _make_cmd_update(text: str, user_id: int = 1):
    """Build a minimal Update mock for command handler tests."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.text = text
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.reply_document = AsyncMock()
    return update


def _make_cmd_iface(authorized: bool = True):
    """Build a minimal TelegramInterface stub for command handler tests."""
    from telegram_interface import TelegramInterface
    iface = TelegramInterface.__new__(TelegramInterface)
    iface._user_state = {}
    iface._config = {}
    iface._is_authorized = MagicMock(return_value=authorized)
    iface._send_unauthorized = AsyncMock()
    return iface


class TestMadPlanShowCommand:
    """Tests for /mad_plan show <plan_name>."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_show_no_name_returns_usage(self):
        from telegram_commands import cmd_mad_plan
        iface = _make_cmd_iface()
        update = _make_cmd_update("/mad_plan show")
        ctx = MagicMock()
        self._run(cmd_mad_plan(iface, update, ctx))
        update.effective_message.reply_text.assert_called_once()
        assert "Usage" in update.effective_message.reply_text.call_args[0][0]

    def test_show_path_traversal_rejected(self):
        from telegram_commands import cmd_mad_plan
        iface = _make_cmd_iface()
        for bad_name in ("../etc", "foo/bar", "back\\slash"):
            update = _make_cmd_update(f"/mad_plan show {bad_name}")
            ctx = MagicMock()
            self._run(cmd_mad_plan(iface, update, ctx))
            reply = update.effective_message.reply_text.call_args[0][0]
            assert "Invalid" in reply

    def test_show_nonexistent_plan(self):
        from telegram_commands import cmd_mad_plan
        iface = _make_cmd_iface()
        update = _make_cmd_update("/mad_plan show no_such_plan_xyz")
        ctx = MagicMock()
        self._run(cmd_mad_plan(iface, update, ctx))
        reply = update.effective_message.reply_text.call_args[0][0]
        assert "not found" in reply

    def test_show_existing_plan_sends_document(self):
        from telegram_commands import cmd_mad_plan
        iface = _make_cmd_iface()
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_dir = os.path.join(tmpdir, "plans", "my_plan")
            os.makedirs(plan_dir)
            plan_md_content = "# My Plan\n\nSome content here."
            with open(os.path.join(plan_dir, "plan.md"), "w", encoding="utf-8") as f:
                f.write(plan_md_content)

            original_getcwd = os.getcwd
            os.getcwd = lambda: tmpdir  # patch cwd to use tmpdir plans_dir
            try:
                update = _make_cmd_update("/mad_plan show my_plan")
                ctx = MagicMock()
                self._run(cmd_mad_plan(iface, update, ctx))
            finally:
                os.getcwd = original_getcwd

        update.effective_message.reply_document.assert_called_once()
        call_kwargs = update.effective_message.reply_document.call_args[1]
        assert call_kwargs.get("filename") == "my_plan.md"
        assert "my_plan" in call_kwargs.get("caption", "")

    def test_show_document_content_matches_plan(self):
        from telegram_commands import cmd_mad_plan
        import io as _io
        iface = _make_cmd_iface()
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_dir = os.path.join(tmpdir, "plans", "test_plan")
            os.makedirs(plan_dir)
            plan_md_content = "# Test Plan\n\nHello world."
            with open(os.path.join(plan_dir, "plan.md"), "w", encoding="utf-8") as f:
                f.write(plan_md_content)

            original_getcwd = os.getcwd
            os.getcwd = lambda: tmpdir
            try:
                update = _make_cmd_update("/mad_plan show test_plan")
                ctx = MagicMock()
                self._run(cmd_mad_plan(iface, update, ctx))
            finally:
                os.getcwd = original_getcwd

        doc_arg = update.effective_message.reply_document.call_args[1]["document"]
        assert isinstance(doc_arg, _io.BytesIO)
        assert doc_arg.read().decode("utf-8") == plan_md_content


# ---------------------------------------------------------------------------
# _build_review_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    def test_with_plan_and_trace(self):
        from telegram_commands import _build_review_prompt
        plan = {"task": "test task", "subtasks": []}
        trace = {"subtasks": [{"id": "s1", "traces": []}]}
        prompt = _build_review_prompt(trace, plan)
        assert "## Plan" in prompt
        assert "test task" in prompt
        assert "## Execution Trace" in prompt

    def test_without_plan(self):
        from telegram_commands import _build_review_prompt
        trace = {"subtasks": []}
        prompt = _build_review_prompt(trace, None)
        assert "(plan not available)" in prompt
        assert "## Execution Trace" in prompt

    def test_large_plan_truncated(self):
        from telegram_commands import _build_review_prompt
        plan = {"task": "x" * 5000, "subtasks": []}
        trace = {"small": True}
        prompt = _build_review_prompt(trace, plan)
        # Full 5000-char string shouldn't appear (3000 limit)
        assert "x" * 4000 not in prompt

    def test_large_trace_truncated(self):
        from telegram_commands import _build_review_prompt
        trace = {"data": "y" * 10000}
        prompt = _build_review_prompt(trace, None)
        # Full 10000-char string shouldn't appear (8000 limit)
        assert "y" * 9000 not in prompt


# ---------------------------------------------------------------------------
# cmd_agent — is_executing guard
# ---------------------------------------------------------------------------

class TestCmdAgentExecutingGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_blocked_when_executing(self):
        from telegram_commands import cmd_agent
        from mad_plan import MadPlanState

        iface = _make_cmd_iface()
        user_state = iface._get_user_state(1)
        user_state.session.state = MadPlanState.EXECUTING

        update = _make_cmd_update("/agent", user_id=1)
        ctx = MagicMock()
        self._run(cmd_agent(iface, update, ctx))

        reply = update.effective_message.reply_text.call_args[0][0]
        assert "stop" in reply.lower() or "executing" in reply.lower()

    def test_switches_when_not_executing(self):
        from telegram_commands import cmd_agent
        from mad_plan import MadPlanState

        iface = _make_cmd_iface()
        user_state = iface._get_user_state(1)
        user_state.session.transition(MadPlanState.PLANNING)

        update = _make_cmd_update("/agent", user_id=1)
        ctx = MagicMock()
        self._run(cmd_agent(iface, update, ctx))

        reply = update.effective_message.reply_text.call_args[0][0]
        assert "Agent mode" in reply
        assert user_state.session.state == MadPlanState.OFF


# ---------------------------------------------------------------------------
# plans_dir property
# ---------------------------------------------------------------------------

class TestPlansDirProperty:
    def test_from_config(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface._config = {"paths": {"plans_dir": "/custom/plans"}}
        iface._user_state = {}
        assert iface.plans_dir == "/custom/plans"

    def test_fallback_to_cwd(self, monkeypatch):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface._config = {}
        iface._user_state = {}
        monkeypatch.setattr(os, "getcwd", lambda: "/fake/cwd")
        assert iface.plans_dir == "/fake/cwd/plans"

    def test_empty_config_uses_fallback(self, monkeypatch):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface._config = {"paths": {"plans_dir": ""}}
        iface._user_state = {}
        monkeypatch.setattr(os, "getcwd", lambda: "/work")
        assert iface.plans_dir == "/work/plans"

