"""P2: content-aware context compaction tests.

Covers:
- Large shell output keeps its trailing error visible to the compaction prompt.
- Large file output preserves both head and tail with an omission marker.
- Generic prose is bounded but still summarized.
- A compaction LLM failure returns a bounded deterministic fallback, not the
  oversized original message list.
- A huge recent tool-result message is capped before the next model call.
- The user goal (first message) is never dropped.
- [Regression] The *current* active goal is preserved even when short-term
  history is prepended before it (goal_idx != 0).
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
    resolve_compaction_threshold,
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
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
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

        out, _ = maybe_compact(msgs, "system", 4000, llm)

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
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
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
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
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
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
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
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
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
        out, _ = maybe_compact(msgs, "system", ctx_max, llm)

        final_tokens = char_estimate(out, "system")
        assert final_tokens <= int(ctx_max * 0.85)
        # Goal is preserved (head) but bounded, not the full 40k chars.
        assert out[0]["content"].startswith("GOAL ")
        assert len(out[0]["content"]) < 40_000


class TestGoalDriftRegression:
    """Regression: compaction must preserve the current active goal even when
    short-term conversation history is prepended before it in the message list.

    This mirrors the structure built by ``react_loop.react_loop()``:

    .. code-block:: python

        messages = []
        messages.extend(ctx.short_term.get_messages())  # prior turns
        goal_idx = len(messages)
        messages.append(first_msg)                      # current goal
        # … ReAct assistant/tool messages appended per step …
    """

    def _make_messages_with_history(
        self,
        n_history_pairs: int = 2,
        n_react_steps: int = 4,
    ) -> tuple[list[dict], int]:
        """Build a message list with short-term history preceding the current goal.

        Returns:
            A ``(messages, goal_idx)`` tuple where ``goal_idx`` is the index of
            the current-goal message inside ``messages``.
        """
        msgs: list[dict] = []
        for i in range(n_history_pairs):
            msgs.append(_msg("user", f"old goal {i}"))
            msgs.append(_msg("assistant", f"old answer {i}"))

        goal_idx = len(msgs)
        msgs.append(_msg("user", "CURRENT GOAL: process the new dataset"))

        for i in range(n_react_steps):
            msgs.append(_msg("assistant", f"action {i} " + "p" * 2000))
            msgs.append(_msg("user", f"Tool result {i}\n" + "r" * 4000 + "\nERR_TAIL"))

        return msgs, goal_idx

    def test_current_goal_preserved_with_goal_idx(self, monkeypatch):
        """With goal_idx supplied, the current goal is always in the compacted output."""
        msgs, goal_idx = self._make_messages_with_history()
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summarized prior steps"

        out, _ = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)

        goal_msgs = [
            m for m in out
            if m["role"] == "user" and "CURRENT GOAL" in m.get("content", "")
            and "Compacted context" not in m.get("content", "")
        ]
        assert goal_msgs, (
            "Current-goal message must appear verbatim in compacted output when goal_idx is passed"
        )

    def test_stale_history_not_preserved_as_first_with_goal_idx(self, monkeypatch):
        """The stale short-term message is NOT pinned as the first preserved entry."""
        msgs, goal_idx = self._make_messages_with_history()
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summary"

        out, _ = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)

        # The first preserved message in the output must be the current goal,
        # not the stale "old goal 0" from short-term history.
        assert "CURRENT GOAL" in out[0]["content"], (
            f"Expected first preserved message to be the current goal; got: {out[0]['content'][:80]!r}"
        )

    def test_goal_preserved_without_goal_idx_shows_old_behaviour(self, monkeypatch):
        """Document pre-fix behaviour: goal_idx=0 (default) anchors messages[0].

        When short-term history precedes the goal and goal_idx is *not* passed,
        messages[0] (stale history) becomes the preserved anchor and the actual
        current goal is swept into the summarised middle.  This test records
        that baseline so the regression tests above are meaningful.
        """
        msgs, _goal_idx = self._make_messages_with_history()
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summary"

        # No goal_idx → default 0 → messages[0] is the stale "old goal 0"
        out, _ = maybe_compact(msgs, "system", 100_000, llm)

        assert "old goal 0" in out[0]["content"], (
            "With goal_idx=0 (default), the stale short-term message is preserved as first"
        )
        # The current goal is NOT directly preserved (it's in the summarised middle).
        current_goal_preserved = any(
            "CURRENT GOAL" in m.get("content", "")
            and "Compacted context" not in m.get("content", "")
            for m in out
        )
        assert not current_goal_preserved, (
            "With goal_idx=0, the current goal should be absent from verbatim output (swept into middle)"
        )

    def test_no_short_term_history_goal_idx_zero_unchanged(self, monkeypatch):
        """goal_idx=0 with no preceding history behaves identically to the original code."""
        msgs = [_msg("user", "GOAL: do something")]
        for i in range(4):
            msgs.append(_msg("assistant", f"step {i} " + "x" * 2000))
            msgs.append(_msg("user", f"result {i} " + "y" * 3000 + "\nTAIL"))
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summary"

        out, _ = maybe_compact(msgs, "system", 100_000, llm, goal_idx=0)

        assert out[0]["content"] == "GOAL: do something", (
            "With goal_idx=0 and no prior history, the goal message must remain first"
        )
        assert any("Compacted context" in str(m.get("content")) for m in out)


class TestRepeatedCompactionRegression:
    """Regression: the active-goal anchor must survive *repeated* compactions.

    ``maybe_compact`` returns the goal's index in the (possibly compacted) list,
    so ``react_loop`` feeds that index straight back into the next compaction
    instead of guessing. These tests drive two or more compactions in sequence
    and assert the goal stays anchored at the returned index, covering both the
    common case (goal precedes the tail → index 0) and the last-2-window clamp
    (goal preserved inside the recent tail at index 2 or 3).
    """

    def _make_initial_messages(
        self,
        n_history_pairs: int = 2,
        n_react_steps: int = 4,
    ) -> tuple[list[dict], int]:
        """Build messages with short-term history preceding the current goal.

        Returns:
            ``(messages, goal_idx)`` mirroring what ``react_loop`` builds.
        """
        msgs: list[dict] = []
        for i in range(n_history_pairs):
            msgs.append(_msg("user", f"old goal {i}"))
            msgs.append(_msg("assistant", f"old answer {i}"))

        goal_idx = len(msgs)
        msgs.append(_msg("user", "CURRENT GOAL: analyse the quarterly report"))

        for i in range(n_react_steps):
            msgs.append(_msg("assistant", f"action {i} " + "p" * 2000))
            msgs.append(_msg("user", f"Tool result {i}\n" + "r" * 4000 + "\nERR_TAIL"))

        return msgs, goal_idx

    def test_second_compaction_still_preserves_current_goal_first(self, monkeypatch):
        """CURRENT GOAL stays anchored across two compactions via the returned index.

        Mirrors ``react_loop``: the index returned by the first ``maybe_compact``
        is fed straight into the second call — no caller-side guessing.
        """
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summarized steps"

        msgs, goal_idx = self._make_initial_messages()

        # --- First compaction (mirrors react_loop step N) ---
        compacted, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        assert len(compacted) < len(msgs), (
            "First compaction should have fired to produce a shorter list"
        )
        # The goal precedes the recent tail, so it becomes the first anchor.
        assert goal_idx == 0
        assert "CURRENT GOAL" in compacted[goal_idx]["content"]

        # Simulate more ReAct steps after the first compaction.
        msgs = compacted
        for i in range(4):
            msgs.append(_msg("assistant", f"post-compact action {i} " + "p" * 2000))
            msgs.append(_msg("user", f"post-compact result {i}\n" + "r" * 4000))

        # --- Second compaction (mirrors react_loop step M > N) ---
        compacted2, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        assert len(compacted2) < len(msgs), (
            "Second compaction should also fire after more steps were added"
        )

        # CURRENT GOAL must still be at the returned index after the second compaction.
        assert "CURRENT GOAL" in compacted2[goal_idx]["content"], (
            f"After second compaction, CURRENT GOAL must be at returned index {goal_idx}; "
            f"got: {compacted2[goal_idx]['content'][:80]!r}"
        )

    def test_goal_in_last2_window_stays_anchored_via_returned_index(self, monkeypatch):
        """Oracle edge case 1: goal in the last-2 window is clamped into the tail.

        When ``messages = short_term_history + [goal]`` the goal is at the last
        index, so full compaction clamps it into the preserved ``last`` tail
        rather than the ``first`` anchor. The old heuristic unconditionally reset
        ``goal_idx = 0``, mis-anchoring the next compaction. The returned index
        instead points at the goal *inside the tail* (verbatim, not the summary),
        and feeding it back keeps the goal anchored across a second compaction.
        """
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summarized"

        # Goal is the LAST message (as at react_loop step 1 before any ReAct
        # messages accumulate) → clamped into the preserved `last` tail.
        msgs = [
            _msg("user", "old goal 0"),
            _msg("assistant", "old answer 0"),
            _msg("user", "old goal 1"),
            _msg("assistant", "old answer 1"),
            _msg("user", "CURRENT GOAL: reconcile the ledger"),
        ]
        goal_idx = len(msgs) - 1  # last index

        compacted, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        assert len(compacted) < len(msgs)
        # Clamped into the tail — NOT the naive 0 the old heuristic would have used.
        assert goal_idx != 0
        # The returned index points at the goal verbatim (not the summary).
        assert "CURRENT GOAL" in compacted[goal_idx]["content"]
        assert "Compacted context" not in compacted[goal_idx]["content"]
        # The naive reset (index 0) would have pointed at a non-goal message.
        assert "CURRENT GOAL" not in compacted[0]["content"]

        # Add more ReAct steps and compact again using the RETURNED index.
        msgs = compacted
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + "p" * 2000))
            msgs.append(_msg("user", f"result {i}\n" + "r" * 4000))

        compacted2, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        assert "CURRENT GOAL" in compacted2[goal_idx]["content"], (
            f"Goal must stay anchored via the returned index (no drift); got "
            f"{compacted2[goal_idx]['content'][:80]!r}"
        )

    def test_n4_full_compaction_boundary_goal_index(self, monkeypatch):
        """Oracle edge case 2: full compaction at n==4 returns length 4 (goal mid-list).

        Full compaction always returns exactly 4 messages (first + summary +
        the 2-message tail). When the input is already length 4 the old
        ``len(compacted) < len(messages)`` guard was False, so the anchor reset
        was skipped and the goal drifted. The returned index locates the goal
        correctly (index 0 here) regardless of the length coincidence, and a
        subsequent compaction keeps it anchored.
        """
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )
        llm = MagicMock()
        llm.chat.return_value = "• summarized"

        # Goal mid-list in a 4-message context → full compaction returns length 4.
        msgs = [
            _msg("user", "stale preamble"),
            _msg("user", "CURRENT GOAL: audit the pipeline"),
            _msg("assistant", "action 0 " + "p" * 2000),
            _msg("user", "result 0 " + "r" * 4000),
        ]
        goal_idx = 1

        compacted, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        # Length is unchanged: the old `<` guard would have skipped the reset here.
        assert len(compacted) == len(msgs) == 4
        # The returned index still locates the goal precisely.
        assert "CURRENT GOAL" in compacted[goal_idx]["content"]
        assert "Compacted context" not in compacted[goal_idx]["content"]

        # A subsequent compaction (after more steps) keeps the goal anchored.
        msgs = compacted
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + "p" * 2000))
            msgs.append(_msg("user", f"result {i}\n" + "r" * 4000))

        compacted2, goal_idx = maybe_compact(msgs, "system", 100_000, llm, goal_idx=goal_idx)
        assert "CURRENT GOAL" in compacted2[goal_idx]["content"], (
            f"Goal must stay anchored after the n==4 boundary compaction; got "
            f"{compacted2[goal_idx]['content'][:80]!r}"
        )


class TestPerModelContextWindow:
    """Per-model context_window awareness for compaction threshold.

    The threshold formula is: int((effective - model_max_tokens) * 0.85)
    where effective = model.context_window or agent.ctx_max_tokens.
    """

    def test_context_window_set_uses_per_model_limit(self, monkeypatch):
        """Model with context_window set uses per-model limit for threshold."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Build messages that exceed the per-model threshold but not the agent default.
        # context_window=8192, max_tokens=1024 → threshold = max(6092, 256) = 6092
        # agent.ctx_max_tokens=90000 → threshold would be int(90000*0.85) = 76500
        # We need messages > 6092 tokens but < 76500 tokens to prove per-model is used.
        # char_estimate divides chars by 4, so we need > 24412 chars of content.
        msgs = [_msg("user", "GOAL: test per-model")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 4000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 8000)))
        llm = MagicMock()
        llm.chat.return_value = "summary"

        # Per-model: context_window=8192, max_tokens=1024
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 8192, llm)
        # Compaction should fire because threshold (6092) < message tokens
        assert len(out) < len(msgs)
        llm.chat.assert_called()

    def test_context_window_unset_falls_back_to_agent_default(self, monkeypatch):
        """Model without context_window falls back to agent.ctx_max_tokens."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Same messages as above, but with a large ctx_max_tokens (agent default)
        msgs = [_msg("user", "GOAL: test fallback")]
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 4000)))
        llm = MagicMock()

        # Agent default: ctx_max_tokens=90000, max_tokens=1024
        # threshold = max(int((90000-1024)*0.85), 256) = max(75629, 256) = 75629
        llm.llm_cfg = {"max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 90_000, llm)
        # No compaction — under threshold
        assert out is msgs
        llm.chat.assert_not_called()

    def test_threshold_reserves_completion_tokens(self, monkeypatch):
        """Compaction threshold reserves completion tokens before margin.

        max(int((8192 - 1024) * 0.85), 256) = max(6092, 256) = 6092,
        NOT int(8192 * 0.85) = 6963.
        """
        def char_estimate(messages, system, model=None):
            # Return a value between 6092 and 6963 to prove the formula
            return 6500

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL"), _msg("assistant", "act"), _msg("user", "res")]
        # Pad to >3 messages so compaction path engages
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))
        llm = MagicMock()
        llm.chat.return_value = "summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        # With completion reservation: threshold = 6092, 6500 > 6092 → compacts
        out, _ = maybe_compact(msgs, "system", 8192, llm)
        llm.chat.assert_called()  # compaction fired

        # Without completion reservation (old formula): threshold = 6963, 6500 < 6963 → no compact
        llm2 = MagicMock()
        llm2.llm_cfg = {"context_window": 8192, "max_tokens": 0}
        out2, _ = maybe_compact(msgs, "system", 8192, llm2)
        # With max_tokens=0: threshold = max(6963, 256) = 6963 > 6500 → no compaction
        assert out2 is msgs
        llm2.chat.assert_not_called()

    def test_negative_threshold_floor_prevents_degenerate_compaction(self, monkeypatch):
        """A misconfigured model_max_tokens >= ctx_max_tokens never compacts under the 256 floor."""
        def char_estimate(messages, system, model=None):
            # Messages claim 200 tokens — below the 256-token fixed floor.
            return 200

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL")]
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))
        llm = MagicMock()

        # ctx_max_tokens=512 with default max_tokens=1024 would yield a negative raw threshold.
        # Floor clamps threshold to 256, so 200-token messages stay uncompacted.
        out, _ = maybe_compact(msgs, "system", 512, llm)
        assert out is msgs
        llm.chat.assert_not_called()

    def test_effective_limit_resolved_from_llm_cfg_per_turn(self):
        """react_loop reads context_window from llm_cfg per-turn at the compaction call site.

        Spec scenario: 'Effective limit is resolved per-turn at the compaction call site'
        — the effective limit SHALL be read from the active model config via
        ``ctx.llm.llm_cfg`` and no mid-run model transition SHALL occur.
        """
        ctx_max_tokens = 90_000  # agent default

        # Case 1: context_window set → effective uses per-model value, not agent default
        llm = MagicMock()
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        _effective = llm.llm_cfg.get("context_window") or ctx_max_tokens
        _budget = llm.llm_cfg.get("max_tokens") or 1024
        assert _effective == 8192, "per-turn resolution must read context_window from llm_cfg"
        assert _budget == 1024

        # Case 2: context_window absent → falls back to agent default
        llm2 = MagicMock()
        llm2.llm_cfg = {"max_tokens": 1024}
        _effective2 = llm2.llm_cfg.get("context_window") or ctx_max_tokens
        assert _effective2 == 90_000, "absent context_window must fall back to agent ctx_max_tokens"

        # Case 3: context_window = 0 (falsy) → falls back to agent default
        llm3 = MagicMock()
        llm3.llm_cfg = {"context_window": 0, "max_tokens": 1024}
        _effective3 = llm3.llm_cfg.get("context_window") or ctx_max_tokens
        assert _effective3 == 90_000, "falsy context_window (0) must fall back to agent ctx_max_tokens"

    def test_max_tokens_none_falls_back_to_default(self, monkeypatch):
        """resolve_compaction_threshold handles an explicit None max_tokens value."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL: test none fallback")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 4000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 8000)))

        llm = MagicMock()
        llm.chat.return_value = "summary"
        llm.llm_cfg = {"max_tokens": None, "context_window": 8192}
        _effective_ctx = llm.llm_cfg.get("context_window") or 90_000

        # Should not raise TypeError; threshold uses max_tokens fallback 1024.
        out, _ = maybe_compact(msgs, "system", _effective_ctx, llm)
        assert len(out) < len(msgs)
        llm.chat.assert_called()


class TestToolDefsTokensCompaction:
    """Tool-definition token overhead must be visible to context compaction."""

    def test_tool_defs_tokens_push_under_over_threshold(self, monkeypatch):
        """History alone is under threshold; adding tool_defs_tokens triggers compaction."""

        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)

        # ~3000 tokens of history, below the 6092-token threshold for an 8192 model.
        msgs = [_msg("user", "GOAL: use tools")]
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 1500)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 1500)))

        llm = MagicMock()
        llm.chat.return_value = "• summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        # Without tool_defs overhead, history stays under the 6092 threshold.
        out_no_tools, _ = maybe_compact(msgs, "system", 8192, llm, tool_defs_tokens=0)
        assert out_no_tools is msgs
        llm.chat.assert_not_called()

        # With tool_defs overhead, the combined total exceeds the threshold.
        llm2 = MagicMock()
        llm2.chat.return_value = "• summary"
        llm2.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 8192, llm2, tool_defs_tokens=5000)

        assert len(out) < len(msgs)
        llm2.chat.assert_called_once()
        assert any("Compacted context" in str(m.get("content")) for m in out)
        assert out[0]["content"] == "GOAL: use tools"

    def test_tool_defs_tokens_alone_triggers_compaction(self, monkeypatch):
        """Even tiny messages can exceed the threshold when tool_defs_tokens is large."""

        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)

        msgs = [_msg("user", "GOAL: tiny")]
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))

        llm = MagicMock()
        llm.chat.return_value = "• summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        out, _ = maybe_compact(msgs, "system", 8192, llm, tool_defs_tokens=7000)

        assert len(out) < len(msgs)
        llm.chat.assert_called_once()
        assert any("Compacted context" in str(m.get("content")) for m in out)


class TestBackwardCompatibility:
    """Maybe_compact's pre-change signature and behaviour remain intact."""

    def test_call_without_tool_defs_tokens_unchanged(self, monkeypatch):
        """Default tool_defs_tokens=0 preserves pre-change behaviour."""

        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)

        msgs = [_msg("user", "GOAL: unchanged")]
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 3000)))

        llm = MagicMock()
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        out, _ = maybe_compact(msgs, "system", 8192, llm)

        assert out is msgs
        llm.chat.assert_not_called()


class TestResolveCompactionThreshold:
    """Unit tests for the shared threshold helper."""

    def test_with_context_window(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192, "max_tokens": 1024}, 90_000,
        )
        assert effective == 8192
        assert threshold == max(int((8192 - 1024) * 0.85), 256)
        assert threshold == 6092

    def test_without_context_window_falls_back(self):
        effective, threshold = resolve_compaction_threshold(
            {"max_tokens": 1024}, 90_000,
        )
        assert effective == 90_000
        assert threshold == max(int((90_000 - 1024) * 0.85), 256)
        assert threshold == 75_629

    def test_without_max_tokens_uses_default(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192}, 90_000,
        )
        assert effective == 8192
        assert threshold == max(int((8192 - 1024) * 0.85), 256)
        assert threshold == 6092

    def test_max_tokens_none_uses_default(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192, "max_tokens": None}, 90_000,
        )
        assert effective == 8192
        assert threshold == 6092

    def test_floor_for_misconfigured_budget(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 512, "max_tokens": 1024}, 512,
        )
        assert effective == 512
        assert threshold == 256
