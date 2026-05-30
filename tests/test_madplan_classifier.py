"""
tests/test_madplan_classifier.py
Tests for TelegramInterface._classify_madplan_intent() routing logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


def _make_iface(llm_response: str):
    """Build a minimal TelegramInterface stub with a mock llm_client."""
    from telegram_interface import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.llm_client = MagicMock()
    iface.llm_client.chat.return_value = llm_response
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
        result = _run(iface._classify_madplan_intent("some task", False))
        assert result == "PLAN_TASK"

    def test_no_llm_client_with_pending_falls_back_to_plan_feedback(self):
        from telegram_interface import TelegramInterface
        iface = TelegramInterface.__new__(TelegramInterface)
        iface.llm_client = None
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
