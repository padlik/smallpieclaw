"""P2: content-aware context compaction tests.

Covers:
- Large shell output keeps its trailing error visible to the compaction prompt.
- Large file output preserves both head and tail with an omission marker.
- Generic prose is bounded but still summarized.
- A compaction LLM failure returns a bounded deterministic fallback, not the
  oversized original message list.
- A huge recent tool-result message is capped before the next model call.
- The user goal (first message) is never dropped.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import context_manager
from context_manager import (
    _cap_recent_message,
    _format_middle_message,
    _RECENT_HEAD,
    _RECENT_TAIL,
    maybe_compact,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class TestMiddleFormatting:
    def test_shell_error_tail_preserved(self):
        body = "Tool 'shell' failed (exit 1).\n" + ("x" * 5000) + "\nFATAL: disk full"
        rendered = _format_middle_message(_msg("user", body))
        assert "Tool 'shell' failed" in rendered  # header (start)
        assert "FATAL: disk full" in rendered      # error tail (end)
        assert "chars omitted" in rendered

    def test_file_output_head_and_tail_preserved(self):
        body = "HEADER_LINE\n" + ("a" * 6000) + "\nTRAILER_LINE"
        rendered = _format_middle_message(_msg("user", body))
        assert "HEADER_LINE" in rendered
        assert "TRAILER_LINE" in rendered
        assert "chars omitted" in rendered

    def test_prose_is_head_capped(self):
        body = "decision: " + ("y" * 5000)
        rendered = _format_middle_message(_msg("assistant", body))
        assert rendered.startswith("[assistant]: decision:")
        assert "chars omitted" in rendered
        assert len(rendered) < len(body)


class TestRecentCapping:
    def test_huge_recent_message_is_capped(self):
        big = "S" * (_RECENT_HEAD + _RECENT_TAIL + 10_000)
        capped = _cap_recent_message(_msg("user", big))
        assert len(capped["content"]) < len(big)
        assert "chars omitted" in capped["content"]

    def test_small_recent_message_unchanged(self):
        m = _msg("user", "short content")
        assert _cap_recent_message(m) is m


class TestMaybeCompact:
    def _big_messages(self) -> list[dict]:
        msgs = [_msg("user", "GOAL: do the big task")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"Tool result {i}\n" + ("r" * 4000) + "\nERR_TAIL"))
        return msgs

    def test_under_threshold_returns_unchanged(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "{}"), _msg("user", "ok")]
        llm = MagicMock()
        out = maybe_compact(msgs, "system", 100_000, "", llm)
        assert out is msgs
        llm.chat.assert_not_called()

    def test_short_overbudget_history_is_capped(self, monkeypatch):
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [
            _msg("user", "GOAL: inspect logs"),
            _msg("assistant", '{"action": "shell"}'),
            _msg("user", "Tool 'shell' succeeded.\n" + ("z" * 40_000) + "\nIMPORTANT_TAIL"),
        ]
        llm = MagicMock()

        out = maybe_compact(msgs, "system", 4000, "", llm)

        assert len(out) == 3
        assert out[0]["content"] == "GOAL: inspect logs"
        assert "IMPORTANT_TAIL" in out[-1]["content"]
        assert "chars omitted" in out[-1]["content"]
        assert char_estimate(out, "system") <= int(4000 * 0.85)
        llm.chat.assert_not_called()

    def test_compaction_preserves_goal_and_uses_summary(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.return_value = "• summarized steps"
        out = maybe_compact(msgs, "system", 100_000, "", llm)
        # First message (goal) preserved.
        assert out[0]["content"] == "GOAL: do the big task"
        # Summary message injected.
        assert any("Compacted context" in str(m.get("content")) for m in out)
        llm.chat.assert_called_once()

    def test_llm_failure_returns_deterministic_fallback(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("model down")
        out = maybe_compact(msgs, "system", 100_000, "", llm)
        # Not the oversized original; compacted to first + summary + last (4 msgs).
        assert len(out) < len(msgs)
        assert out[0]["content"] == "GOAL: do the big task"
        summary_msg = next(m for m in out if "Compacted context" in str(m.get("content")))
        # Deterministic fallback preserves error tails from the middle.
        assert "ERR_TAIL" in summary_msg["content"]

    def test_empty_summary_falls_back(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.return_value = "   "
        out = maybe_compact(msgs, "system", 100_000, "", llm)
        summary_msg = next(m for m in out if "Compacted context" in str(m.get("content")))
        assert "ERR_TAIL" in summary_msg["content"]

    def test_huge_recent_result_capped_in_output(self, monkeypatch):
        msgs = self._big_messages()
        # Make the most recent message enormous.
        msgs[-1] = _msg("user", "Z" * (_RECENT_HEAD + _RECENT_TAIL + 50_000))
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.return_value = "• summary"
        out = maybe_compact(msgs, "system", 100_000, "", llm)
        assert "chars omitted" in out[-1]["content"]
        assert len(out[-1]["content"]) < (_RECENT_HEAD + _RECENT_TAIL + 50_000)

    def test_still_oversized_triggers_deterministic_tightening(self, monkeypatch):
        # A char-proportional estimator so shrinking actually lowers the count.
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Enormous first (goal) and recent messages that survive normal compaction.
        msgs = [_msg("user", "GOAL " + ("g" * 40_000))]
        for i in range(6):
            msgs.append(_msg("assistant", "act " + ("p" * 3000)))
            msgs.append(_msg("user", f"res {i} " + ("r" * 5000)))
        msgs[-1] = _msg("user", "TAILMSG " + ("z" * 40_000))
        llm = MagicMock()
        llm.chat.return_value = "• " + ("s" * 40_000)  # oversized summary too

        # Tight budget: 0.85 * ctx must be below the post-compaction size so the
        # deterministic tightening loop engages and drives it under budget.
        ctx_max = 4000  # threshold = 3400 tokens
        out = maybe_compact(msgs, "system", ctx_max, "", llm)

        final_tokens = char_estimate(out, "system")
        assert final_tokens <= int(ctx_max * 0.85)
        # Goal is preserved (head) but bounded, not the full 40k chars.
        assert out[0]["content"].startswith("GOAL ")
        assert len(out[0]["content"]) < 40_000
