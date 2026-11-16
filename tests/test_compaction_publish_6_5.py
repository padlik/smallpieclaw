"""Task 6.5: a compaction candidate is built on a copy and published only when valid.

The automatic path publishes the new history and goal index together, and only for a summary
that is non-empty after stripping whitespace and a candidate that passes native group
validation. The summary, goal and retained spans are never capped or truncated. A summary that
leaves the request over the threshold is sent as is (no same-step recursion); a later eligible
step may summarize the earlier summary as ordinary middle history.

Failure warning/disable-for-run (6.6) and strict pre-send validation (6.7) are out of scope here.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import httpx
import pytest

import context_manager
from context_manager import CompactionFailedError, maybe_compact
from model_registry import ModelMetadata
from providers._errors import LLMPermanentError
from react_loop import _LoopState, _prepare_request
from tests.execution_harness import RecordingExecutor, build_context, run_react
from tests.run_model_fixtures import make_run_model_client

PREFIX = "[Compacted context — earlier steps summary]\n"
THRESHOLD = 100
HUGE = "H" * 6000


def _chars(messages, system, model=None):
    return sum(len(json.dumps(m, default=str)) for m in messages) // 4


@pytest.fixture
def _char_estimator(monkeypatch):
    monkeypatch.setattr(context_manager, "estimate_messages_tokens", _chars)


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


def _calls(*ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "t", "arguments": '{"k": 1}'}}
            for i in ids
        ],
    }


def _res(call_id, text="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _history():
    return [
        _u("goal " + HUGE),
        _a("old1 " + HUGE),
        _u("old2 " + HUGE),
        _calls("a", "b"),
        _res("a", "R" * 3000),
        _res("b", "S" * 3000),
    ]


def _compact(messages, summarize, goal_idx=0):
    return maybe_compact(
        messages, "sys", MagicMock(), goal_idx=goal_idx, threshold=THRESHOLD,
        summarize=summarize,
    )


@pytest.mark.usefixtures("_char_estimator")
class TestSummaryPublishedWhole:
    def test_summary_is_stripped_but_never_truncated(self):
        body = "".join(f"- step {i} detail\n" for i in range(900))  # far beyond 6000 chars
        assert len(body) > 12_000
        out, idx = _compact(_history(), lambda _p: f"  \n\t{body}\n  ")
        assert out[1] == {"role": "user", "content": PREFIX + body.rstrip()}
        assert "omitted" not in out[1]["content"] and idx == 0

    def test_goal_and_recent_spans_are_the_same_unchanged_objects(self):
        msgs = _history()
        out, idx = _compact(msgs, lambda _p: "- s")
        assert out[idx] is msgs[0]
        assert [id(m) for m in out[-3:]] == [id(m) for m in msgs[-3:]]
        assert len(out) == 5


@pytest.mark.usefixtures("_char_estimator")
class TestRefusedPublication:
    @pytest.mark.parametrize("blank", ["", " ", "\n\t  \r\n", "\u00a0\u2003"])
    def test_whitespace_summary_publishes_nothing(self, blank):
        msgs = _history()
        snapshot = copy.deepcopy(msgs)
        calls = []
        with pytest.raises(CompactionFailedError) as info:  # 6.6: refusal is signalled
            _compact(msgs, lambda p: calls.append(p) or blank)
        assert len(calls) == 1 and info.value.reason == "empty_summary"
        assert msgs == snapshot

    def test_invalid_candidate_keeps_original_history_and_goal_index(self, monkeypatch):
        real = context_manager.validate_native_history

        def reject_candidate(messages):
            if any(str(m.get("content")).startswith(PREFIX) for m in messages):
                raise LLMPermanentError("malformed")
            return real(messages)

        monkeypatch.setattr(context_manager, "validate_native_history", reject_candidate)
        msgs = _history()
        snapshot = copy.deepcopy(msgs)
        with pytest.raises(CompactionFailedError) as info:  # 6.6: refusal is signalled
            _compact(msgs, lambda _p: "- fine summary")
        assert info.value.reason == "invalid_candidate" and msgs == snapshot

    def test_the_published_list_is_the_candidate_validated_first(self, monkeypatch):
        seen = []
        real = context_manager.validate_native_history

        def spy(messages):
            seen.append(messages)
            return real(messages)

        monkeypatch.setattr(context_manager, "validate_native_history", spy)
        msgs = _history()
        out, _idx = _compact(msgs, lambda _p: "- s")
        assert seen[0] is msgs  # entry validation of the original
        assert seen[-1] is out and out is not msgs and out[1]["content"].startswith(PREFIX)


@pytest.mark.usefixtures("_char_estimator")
class TestCopyAndAtomicity:
    def test_history_is_untouched_while_the_summary_is_requested_and_after(self):
        goal = {"role": "user", "content": [
            {"type": "text", "text": "look " + HUGE},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}
        msgs = [goal, _a("x " + HUGE), _u("y " + HUGE), _calls("a"), _res("a", "r")]
        snapshot = copy.deepcopy(msgs)
        listing = list(msgs)
        during = []

        def summarize(_p):
            during.append((msgs == snapshot, msgs == listing and all(
                a is b for a, b in zip(msgs, listing, strict=True))))
            return "- s"

        out, idx = _compact(msgs, summarize)
        assert during == [(True, True)]
        assert msgs == snapshot and out is not msgs
        assert out[idx] is goal and out[idx]["content"][1]["image_url"]["url"].endswith("AAAA")

    @pytest.mark.parametrize(
        "history, goal",
        [
            # earlier conversation precedes the goal: it is summarized after the kept goal
            ([_u("o1 " + HUGE), _a("o2 " + HUGE), _u("GOAL"), _a("x " + HUGE), _u("y " + HUGE),
              _a("z"), _u("w")], 2),
            # goal inside a parallel batch keeps the whole batch
            ([_u("o " + HUGE), _calls("a", "b"), _res("a", "GOAL"), _res("b"), _a("x " + HUGE),
              _u("y"), _a("z")], 2),
        ],
    )
    def test_goal_index_points_at_the_unchanged_goal(self, history, goal):
        msgs = copy.deepcopy(history)
        out, idx = _compact(msgs, lambda _p: "- s", goal_idx=goal)
        assert out is not msgs and out[idx] is msgs[goal]
        assert [m for m in out if m in msgs[-2:]] == msgs[-2:]


# ---------------------------------------------------------------------------
# Real ReactContext + real pinned LLMClient over a mock transport
# ---------------------------------------------------------------------------

FINISH = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
TOOL = {"choices": [{"message": {"content": '{"action":"tool","tool":"probe","args":{}}'}}]}


def _client(handler):
    return make_run_model_client(
        "openai", httpx.MockTransport(handler),
        metadata=ModelMetadata(context_tokens=9_000, output_tokens=200), upstream_id="pub-model",
    )


def _is_summary(body):
    return "Summarize these intermediate agent steps" in body


class TestOverThresholdRequests:
    def test_still_over_threshold_summary_is_sent_once_per_step_and_later_step_resummarizes(self):
        log: list[tuple[str, str]] = []
        one, two = ("SUM-ONE " * 400).strip(), ("SUM-TWO " * 400).strip()
        summaries = iter([one + " \n", two])
        mains = iter([TOOL, FINISH])

        def handler(request):
            body = request.content.decode()
            if _is_summary(body):
                log.append(("summary", body))
                return httpx.Response(200, json={"choices": [{"message": {"content": next(summaries)}}]})
            log.append(("main", body))
            return httpx.Response(200, json=next(mains))

        client = _client(handler)
        short_term = MagicMock()
        short_term.get_messages.return_value = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"history {i} " + "alpha beta " * 400}
            for i in range(6)
        ]
        try:
            result, _calls_, _progress = run_react(
                client, RecordingExecutor(), "GOAL", short_term=short_term,
                compaction_threshold_tokens=300,
            )
        finally:
            client.close()
        assert result == "done"
        assert [kind for kind, _ in log] == ["summary", "main", "summary", "main"]
        # step 1: the long summary reaches the main request whole and is still over threshold
        assert one in log[1][1] and "GOAL" in log[1][1]
        assert "SUM-ONE" not in log[0][1]
        # step 2: the earlier summary is ordinary middle history of the new summary prompt
        # (the prompt renders each middle message with its usual head/tail excerpt)
        assert "Compacted context" in log[2][1] and "SUM-ONE SUM-ONE" in log[2][1]
        assert two in log[3][1] and "SUM-ONE" not in log[3][1]

    def test_history_and_goal_index_are_unchanged_during_the_summary_call(self):
        seen: list[tuple[bool, int]] = []
        messages = [{"role": "user", "content": "GOAL"}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"old {i} " + "alpha beta " * 400}
            for i in range(6)
        ] + [_a("recent a"), _u("recent b")]
        state = _LoopState(messages=messages, goal_idx=0, max_steps=1)
        original = list(messages)

        def handler(request):
            seen.append((state.messages is messages and state.messages == original, state.goal_idx))
            return httpx.Response(200, json={"choices": [{"message": {"content": "- done"}}]})

        client = _client(handler)
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=500)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert seen == [(True, 0)]
        assert state.messages is not messages and messages == original
        assert state.messages[state.goal_idx] is messages[0]
        assert state.messages[-2:] == original[-2:]
