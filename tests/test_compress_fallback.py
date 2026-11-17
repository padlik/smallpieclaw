"""Tests for manual /compress failure handling.

Historical memory item F applied a deterministic head+tail fallback; task 7.3
retired that behaviour for manual compression (history is preserved on failure).
``reset_task`` keeps its own fallback (covered in test_controller_summary_admission).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from llm_client import LLMError
from memory_store import ShortTermMemory


def _controller(short_term, llm, make_agent_controller):
    # Manual compression admits its own summary client (task 7.2); the scripted
    # client is handed out as that owned product.
    return make_agent_controller(
        llm=None,
        run_client_factory=lambda event: llm,
        short_term=short_term,
    )


def _stm(turns):
    stm = ShortTermMemory(max_turns=50)
    for role, content in turns:
        stm.add(role, content)
    return stm


def _failing_llm():
    llm = MagicMock()
    llm.chat.side_effect = LLMError("provider unavailable")
    return llm


def _summary_llm(summary="- did things\n- finished"):
    llm = MagicMock()
    llm.chat.return_value = summary
    return llm


class TestCompressFallback:
    """Manual /compress no longer has a deterministic fallback (task 7.3)."""

    def test_llm_failure_keeps_history_and_reports_failure(self, make_agent_controller):
        turns = [("user", f"message {i} " + "x" * 200) for i in range(20)]
        stm = _stm(turns)
        before = stm.get_messages()
        failing = MagicMock()
        failing.chat.side_effect = LLMError("provider unavailable")
        ctrl = _controller(stm, failing, make_agent_controller)

        msg = ctrl.compress_context()

        assert stm.get_messages() == before
        assert msg.startswith("❌") and "unchanged" in msg.lower()
        assert "deterministic" not in msg.lower() and "unavailable" not in msg
        failing.close.assert_called_once()

    def test_failure_never_publishes_head_tail_truncation(self, make_agent_controller):
        turns = (
            [("user", "FIRST_MESSAGE_MARKER")]
            + [("assistant", "filler " * 200) for _ in range(20)]
            + [("user", "LAST_MESSAGE_MARKER")]
        )
        stm = _stm(turns)
        failing = MagicMock()
        failing.chat.side_effect = LLMError("down")
        ctrl = _controller(stm, failing, make_agent_controller)

        ctrl.compress_context()

        assert len(stm.get_messages()) == len(turns)
        assert not any("deterministic" in m["content"] for m in stm.get_messages())

    def test_successful_llm_uses_normal_summary_path(self, make_agent_controller):
        turns = [("user", "hello there"), ("assistant", "general kenobi")]
        stm = _stm(turns)
        llm = _summary_llm("- greeting exchanged")
        ctrl = _controller(stm, llm, make_agent_controller)

        msg = ctrl.compress_context()

        llm.chat.assert_called_once()
        remaining = stm.get_messages()
        assert len(remaining) == 1
        assert "Compressed context summary]" in remaining[0]["content"]
        assert "deterministic" not in remaining[0]["content"]
        assert "✅" in msg

    def test_empty_short_term_unchanged(self, make_agent_controller):
        stm = _stm([])
        ctrl = _controller(stm, _failing_llm(), make_agent_controller)
        msg = ctrl.compress_context()
        # Nothing to compress — no LLM call, no fallback message.
        assert "No short-term" in msg or "already minimal" in msg
        assert stm.get_messages() == []

    def test_single_message_not_compressed(self, make_agent_controller):
        stm = _stm([("user", "only one")])
        ctrl = _controller(stm, _failing_llm(), make_agent_controller)
        msg = ctrl.compress_context()
        assert "already minimal" in msg
        assert len(stm.get_messages()) == 1
