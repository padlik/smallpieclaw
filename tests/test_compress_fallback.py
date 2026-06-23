"""Tests for deterministic /compress fallback on LLM failure (memory item F).

When the LLM summarization call fails, compress_context must still reduce the
short-term buffer using a deterministic head+tail truncation rather than leaving
the (often over-budget) context untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_controller import AgentController
from memory_store import ShortTermMemory


def _controller(short_term, llm):
    return AgentController(
        llm=llm,
        tool_index=MagicMock(),
        executor=MagicMock(),
        creator=MagicMock(),
        memory=MagicMock(),
        short_term=short_term,
    )


def _stm(turns):
    stm = ShortTermMemory(max_turns=50)
    for role, content in turns:
        stm.add(role, content)
    return stm


def _failing_llm():
    llm = MagicMock()
    llm.chat.side_effect = RuntimeError("provider unavailable")
    return llm


def _summary_llm(summary="- did things\n- finished"):
    llm = MagicMock()
    llm.chat.return_value = summary
    return llm


class TestCompressFallback:
    def test_llm_failure_shrinks_buffer_to_single_summary(self):
        turns = [("user", f"message {i} " + "x" * 200) for i in range(20)]
        stm = _stm(turns)
        ctrl = _controller(stm, _failing_llm())

        msg = ctrl.compress_context()

        remaining = stm.get_messages()
        assert len(remaining) == 1
        assert remaining[0]["role"] == "assistant"
        assert "deterministic fallback" in remaining[0]["content"]
        assert "deterministic truncation" in msg.lower()

    def test_fallback_preserves_head_and_tail_content(self):
        turns = (
            [("user", "FIRST_MESSAGE_MARKER")]
            + [("assistant", "filler " * 200) for _ in range(20)]
            + [("user", "LAST_MESSAGE_MARKER")]
        )
        stm = _stm(turns)
        ctrl = _controller(stm, _failing_llm())

        ctrl.compress_context()

        content = stm.get_messages()[0]["content"]
        assert "FIRST_MESSAGE_MARKER" in content
        assert "LAST_MESSAGE_MARKER" in content

    def test_successful_llm_uses_normal_summary_path(self):
        turns = [("user", "hello there"), ("assistant", "general kenobi")]
        stm = _stm(turns)
        llm = _summary_llm("- greeting exchanged")
        ctrl = _controller(stm, llm)

        msg = ctrl.compress_context()

        llm.chat.assert_called_once()
        remaining = stm.get_messages()
        assert len(remaining) == 1
        assert "Compressed context summary]" in remaining[0]["content"]
        assert "deterministic" not in remaining[0]["content"]
        assert "✅" in msg

    def test_empty_short_term_unchanged(self):
        stm = _stm([])
        ctrl = _controller(stm, _failing_llm())
        msg = ctrl.compress_context()
        # Nothing to compress — no LLM call, no fallback message.
        assert "No short-term" in msg or "already minimal" in msg
        assert stm.get_messages() == []

    def test_single_message_not_compressed(self):
        stm = _stm([("user", "only one")])
        ctrl = _controller(stm, _failing_llm())
        msg = ctrl.compress_context()
        assert "already minimal" in msg
        assert len(stm.get_messages()) == 1
