"""Context-manager owner suite: automatic compaction history behavior.

Task 2.3 relocated the non-policy regressions of ``context_manager.py`` here
from ``tests/test_p2_compaction.py`` (goal / ``goal_idx`` anchoring, atomic
native call/result spans and partition, malformed native history rejection,
summary-prompt middle rendering) and the summary-cancellation node from
``tests/test_request_preparation.py``. Assertions are unchanged.

``TestMiddleFormatting`` covers the existing summary-prompt *input* rendering
(head/tail of middle messages fed to the summarizer), not truncation of
published history (ledger open item O3).

PHASE2 note: some nodes still call ``maybe_compact`` with the current
positional ``ctx_max_tokens`` and the ``force``/``repair_target``/``input_tokens``
repair arguments; tasks 6.4/6.6 rewrite that entry point and 10.1 retires the
repair path, preserving the goal, span, validation and cancellation assertions.
"""

from __future__ import annotations

import copy
import json
import threading
from unittest.mock import MagicMock

import context_manager
import pytest
from context_manager import (
    _format_middle_message,
    _message_spans,
    _partition_compaction,
    maybe_compact,
)
from providers._errors import LLMCancelledError, LLMPermanentError


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _has_summary(messages: list[dict]) -> bool:
    return any("[Compacted context" in str(m.get("content")) for m in messages)


def _over_until_summarized(messages, system, model=None) -> int:
    """Estimator: oversized until the compacted summary is present (or tiny)."""
    return 10 if len(messages) <= 3 or _has_summary(messages) else 1_000_000


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


class TestMaybeCompact:
    def _big_messages(self) -> list[dict]:
        msgs = [_msg("user", "GOAL: do the big task")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"Tool result {i}\n" + ("r" * 4000) + "\nERR_TAIL"))
        return msgs
    def test_compaction_preserves_goal_and_uses_summary(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.return_value = "• summarized steps"
        out, _ = maybe_compact(msgs, "system", llm, threshold=100_000)
        # First message (goal) preserved.
        assert out[0]["content"] == "GOAL: do the big task"
        # Summary message injected.
        assert any("Compacted context" in str(m.get("content")) for m in out)
        llm.chat.assert_called_once()


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

        out, _ = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)

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

        out, _ = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)

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
        out, _ = maybe_compact(msgs, "system", llm, threshold=100_000)

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

        out, _ = maybe_compact(msgs, "system", llm, goal_idx=0, threshold=100_000)

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
        compacted, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
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
        compacted2, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
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

        compacted, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
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

        compacted2, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
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
            _over_until_summarized,
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

        compacted, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
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

        compacted2, goal_idx = maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=100_000)
        assert "CURRENT GOAL" in compacted2[goal_idx]["content"], (
            f"Goal must stay anchored after the n==4 boundary compaction; got "
            f"{compacted2[goal_idx]['content'][:80]!r}"
        )


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

        out, _ = maybe_compact(msgs, "system", llm, threshold=8192)

        assert out is msgs
        llm.chat.assert_not_called()


# ---------------------------------------------------------------------------
# Atomic native groups and goal preservation
# ---------------------------------------------------------------------------

def _call(call_id: str, name: str = "shell", args: str = '{"cmd": "ls"}') -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _assistant_calls(*calls: dict, content=None) -> dict:
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def _tool(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assert_wire_valid(messages: list[dict]) -> None:
    """Every tool result follows its assistant call and every call has all results."""
    i = 0
    while i < len(messages):
        m = messages[i]
        assert m["role"] != "tool", f"orphan tool result at {i}"
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            got = []
            i += 1
            while i < len(messages) and messages[i]["role"] == "tool":
                got.append(messages[i]["tool_call_id"])
                i += 1
            assert sorted(got) == sorted(ids)
            continue
        i += 1


def _char_estimate(messages, system, model=None):
    total = 0
    for m in messages:
        total += len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or []))
    return total // 4


class TestAtomicSpans:
    def test_spans_group_multicall_results(self):
        msgs = [
            _msg("user", "g"),
            _assistant_calls(_call("a"), _call("b")),
            _tool("a"), _tool("b"),
            _msg("user", "tail"),
        ]
        assert _message_spans(msgs) == [(0, 1), (1, 4), (4, 5)]

    def test_partition_expands_suffix_to_span_boundary(self):
        msgs = [
            _msg("user", "g"),
            _msg("assistant", "x"),
            _assistant_calls(_call("a"), _call("b")),
            _tool("a"), _tool("b"),
        ]
        first, middle, last = _partition_compaction(msgs, 0)
        assert (first, middle, last) == ([0], [1], [2, 3, 4])

    def test_goal_inside_suffix_leaves_first_empty(self):
        msgs = [_msg("user", "h"), _msg("assistant", "x"), _msg("user", "g")]
        first, middle, last = _partition_compaction(msgs, 2)
        assert (first, middle, last) == ([], [0], [1, 2])


class TestGoalPreserved:
    HUGE = "GOAL " + "g" * 60_000

    @pytest.mark.parametrize("n_history", [0, 1, 6])
    @pytest.mark.parametrize("threshold", [500, 4000])
    def test_huge_goal_unchanged_on_every_path(self, monkeypatch, n_history, threshold):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        goal = {"role": "user", "content": self.HUGE, "extra": {"k": [1, 2]}}
        msgs = [goal]
        for i in range(n_history):
            msgs.append(_msg("assistant", f"a{i} " + "p" * 3000))
            msgs.append(_msg("user", f"r{i} " + "r" * 3000))
        before = copy.deepcopy(msgs)
        llm = MagicMock()
        llm.chat.return_value = "- s"
        out, idx = maybe_compact(msgs, "system", llm, threshold=threshold)
        assert out[idx] == goal
        assert out[idx]["content"] == self.HUGE
        assert msgs == before

    def test_multimodal_goal_is_intact(self, monkeypatch):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        goal = {"role": "user", "content": [
            {"type": "text", "text": "look " + "t" * 30_000},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 30_000}},
        ]}
        msgs = [goal] + [_msg("assistant" if i % 2 == 0 else "user", "w" * 3000) for i in range(6)]
        before = copy.deepcopy(msgs)
        out, idx = maybe_compact(msgs, "system", MagicMock(**{"chat.return_value": "- s"}), threshold=4000)
        assert out[idx] == before[0]
        assert msgs == before


class TestAtomicGroupCompaction:
    def _compact(self, msgs, goal_idx=0, summary="- sum"):
        llm = MagicMock()
        llm.chat.return_value = summary
        return maybe_compact(msgs, "system", llm, goal_idx=goal_idx, threshold=4000), llm
    def test_five_message_orphan_reproduction(self, monkeypatch):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        msgs = [
            _msg("user", "GOAL"),
            _msg("assistant", "thinking " + "t" * 5000),
            _assistant_calls(_call("a"), _call("b"), content="plan " + "p" * 5000),
            _tool("a", "ra " + "x" * 5000),
            _tool("b", "rb " + "y" * 5000),
        ]
        (out, idx), llm = self._compact(msgs)
        _assert_wire_valid(out)
        assert out[idx]["content"] == "GOAL"
        ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
        assert ids in ([], ["a", "b"])

    def test_historical_huge_args_keep_whole_native_span_after_one_summary(self, monkeypatch):
        """A huge recent call/result span is not clipped to make the estimate fit."""
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        huge = json.dumps({"blob": "Q" * 120_000})
        msgs = [
            _msg("user", "GOAL"),
            _msg("assistant", "old " + "o" * 2000),
            _msg("user", "older " + "u" * 2000),
            _assistant_calls(_call("big", "write_file", huge), content="writing"),
            _tool("big", "done"),
        ]
        prompts: list[str] = []
        out, idx = maybe_compact(
            msgs, "system", MagicMock(), threshold=4000,
            summarize=lambda prompt: prompts.append(prompt) or "- early",
        )
        assert len(prompts) == 1
        _assert_wire_valid(out)
        assert out[idx] is msgs[0]
        assert out[-2:] == msgs[-2:]

    def test_multicall_last_two_boundary_retains_whole_group(self, monkeypatch):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        msgs = [_msg("user", "GOAL")]
        for i in range(4):
            msgs += [_assistant_calls(_call(f"c{i}")), _tool(f"c{i}", "r" * 4000)]
        msgs += [_assistant_calls(_call("m1"), _call("m2"), _call("m3")),
                 _tool("m1"), _tool("m2"), _tool("m3")]
        (out, _idx), _llm = self._compact(msgs)
        _assert_wire_valid(out)
        assert [m["tool_call_id"] for m in out if m["role"] == "tool"][-3:] == ["m1", "m2", "m3"]
    def test_summary_request_renders_calls_and_result_ids(self, monkeypatch):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        msgs = [
            _msg("user", "GOAL"),
            _assistant_calls(_call("zz", "read_file", '{"path": "/a"}')),
            _tool("zz", "contents"),
            _msg("assistant", "x " * 4000),
            _msg("user", "y " * 4000),
        ]
        seen: list[str] = []
        maybe_compact(msgs, "system", MagicMock(),
                      summarize=lambda p: seen.append(p) or "- s", threshold=1000)
        assert "read_file" in seen[0] and "/a" in seen[0] and "id=zz" in seen[0]


class TestMiddleRendering:
    def test_assistant_with_none_content_renders_calls(self):
        rendered = _format_middle_message(_assistant_calls(_call("k1", "grep", '{"q": "x"}')))
        assert "grep" in rendered and '{"q": "x"}' in rendered and "id=k1" in rendered

    def test_tool_result_is_labelled_with_id(self):
        assert _format_middle_message(_tool("k1", "body")).startswith("[tool result id=k1]")


# ---------------------------------------------------------------------------
# Malformed native history is rejected before any summary I/O
# ---------------------------------------------------------------------------

_SECRET = "SECRET-MARK"


def _malformed_cases() -> dict[str, list[dict]]:
    a, b = f"a-{_SECRET}", f"b-{_SECRET}"
    two = _assistant_calls(_call(a, args=f'{{"k": "{_SECRET}"}}'), _call(b))
    return {
        "incomplete_ab_with_a": [two, _tool(a, _SECRET)],
        "incomplete_single": [_assistant_calls(_call(a)), _msg("user", _SECRET)],
        "orphan_result": [_tool(a, _SECRET)],
        "mismatched_result": [_assistant_calls(_call(a)), _tool(b, _SECRET)],
        "duplicate_result": [_assistant_calls(_call(a)), _tool(a), _tool(a)],
        "duplicate_call_id": [_assistant_calls(_call(a), _call(a)), _tool(a), _tool(a)],
        "call_wrong_type": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": a, "type": "not-function", "function": {"name": "n", "arguments": "{}"}}]},
            _tool(a),
        ],
        "call_args_invalid_json": [_assistant_calls(_call(a, args="not-json")), _tool(a)],
        "call_args_missing": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": a, "type": "function", "function": {"name": "n"}}]},
            _tool(a),
        ],
        "call_args_empty_string": [_assistant_calls(_call(a, args="")), _tool(a)],
        "call_args_scalar_json": [_assistant_calls(_call(a, args="5")), _tool(a)],
        "call_args_wrong_kind": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": a, "function": {"name": "n", "arguments": 7}}]},
            _tool(a),
        ],
        "result_id_list": [_assistant_calls(_call(a)), {"role": "tool", "tool_call_id": [a], "content": "x"}],
        "result_id_dict": [_assistant_calls(_call(a)), {"role": "tool", "tool_call_id": {"k": a}, "content": "x"}],
        "result_id_empty": [_assistant_calls(_call(a)), {"role": "tool", "tool_call_id": "", "content": "x"}],
        "call_not_dict": [{"role": "assistant", "content": None, "tool_calls": [_SECRET]}, _tool(a)],
        "call_missing_name": [
            {"role": "assistant", "content": None, "tool_calls": [{"id": a, "function": {}}]}, _tool(a),
        ],
        "call_empty_id": [_assistant_calls(_call("")), _tool("")],
        "non_string_result": [_assistant_calls(_call(a)), {"role": "tool", "tool_call_id": a, "content": None}],
        "tool_calls_not_list": [{"role": "assistant", "content": None, "tool_calls": {"id": a}}],
    }


def _place(group: list[dict], where: str) -> tuple[list[dict], int]:
    filler = [_msg("user" if i % 2 == 0 else "assistant", "f " * 3000) for i in range(4)]
    goal = _msg("user", "GOAL")
    if where == "short":
        return [goal] + group, 0
    if where == "middle":
        return [goal] + group + filler + [_msg("assistant", "x"), _msg("user", "y")], 0
    return filler + [goal] + [_msg("assistant", "x")] + group, 4


class TestMalformedNativeHistory:
    @pytest.mark.parametrize("where", ["short", "middle", "suffix"])
    @pytest.mark.parametrize("case", sorted(_malformed_cases()))
    @pytest.mark.parametrize("threshold", [5, 100_000])
    def test_rejected_before_summary_or_early_return(self, monkeypatch, case, where, threshold):
        group = _malformed_cases()[case]
        msgs, goal_idx = _place(group, where)
        # The estimator says everything fits the larger threshold; malformed
        # history must still fail before either path makes summary/provider I/O.
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", lambda *a, **k: 10)
        before = copy.deepcopy(msgs)
        llm = MagicMock()
        summarize = MagicMock(return_value="- s")
        with pytest.raises(LLMPermanentError) as info:
            maybe_compact(msgs, "system", llm, goal_idx=goal_idx,
                          summarize=summarize, threshold=threshold)
        assert _SECRET not in str(info.value)
        summarize.assert_not_called()
        llm.chat.assert_not_called()
        assert msgs == before

    @pytest.mark.parametrize("where", ["short", "middle", "suffix"])
    def test_valid_single_and_multicall_groups_pass(self, monkeypatch, where):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", lambda *a, **k: 10)
        group = [
            _assistant_calls(_call("s1")), _tool("s1"),
            _assistant_calls(_call("m1"), _call("m2"), content="prose"), _tool("m2"), _tool("m1"),
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
        msgs, goal_idx = _place(group, where)
        out, idx = maybe_compact(msgs, "system", MagicMock(), goal_idx=goal_idx, threshold=100_000)
        assert out is msgs and idx == goal_idx


class TestValidReusedAndShapedCalls:
    @pytest.mark.parametrize("where", ["short", "middle", "suffix"])
    @pytest.mark.parametrize("threshold", [5, 100_000])
    def test_production_groups_reusing_supplied_id_are_valid(self, monkeypatch, where, threshold):
        from interfaces import ToolCall
        from native_turns import _append_native_tool_result

        group: list[dict] = []
        _append_native_tool_result(group, ToolCall(id="call_0", name="shell", arguments={"cmd": "ls"}), "r1")
        _append_native_tool_result(group, ToolCall(id="call_0", name="shell", arguments={"cmd": "pwd"}), "r2")
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", lambda *a, **k: 10)
        msgs, goal_idx = _place(group, where)
        before = copy.deepcopy(msgs)
        summarize = MagicMock(return_value="- s")
        context_manager.validate_native_history(msgs)
        try:
            maybe_compact(msgs, "system", MagicMock(), goal_idx=goal_idx,
                          summarize=summarize, threshold=threshold)
        except LLMPermanentError:
            pytest.fail("valid reused IDs rejected")
        if threshold == 100_000:
            assert msgs == before

    def test_args_whitespace_dict_args_prose_and_missing_type_pass_unchanged(self):
        msgs = [
            _msg("user", "GOAL"),
            _assistant_calls(_call("w", args='{ "a" :  "\\u00e9" }'), content="prose"),
            _tool("w"),
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "d", "function": {"name": "n", "arguments": {"k": 1}}}]},
            _tool("d"),
            {"role": "user", "content": [{"type": "text", "text": "multi"}]},
        ]
        before = copy.deepcopy(msgs)
        context_manager.validate_native_history(msgs)
        assert msgs == before


# ---------------------------------------------------------------------------
# Summary cancellation propagates through compaction
# ---------------------------------------------------------------------------

def test_cancellation_propagates_through_compaction():
    ctx_cancel = threading.Event()
    ctx_cancel.set()
    client = MagicMock()
    client.llm_cfg = {}
    messages = [{"role": "user", "content": "x " * 3000} for _ in range(6)]

    def summarize(_prompt):
        raise LLMCancelledError("cancelled")

    with pytest.raises(LLMCancelledError):
        maybe_compact(
            messages, "s", client, threshold=200,
            input_tokens=10_000, summarize=summarize,
        )
