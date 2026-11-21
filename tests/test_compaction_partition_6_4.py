"""Task 6.4: automatic compaction partition and summary isolation.

Over-threshold requests make one attempt: the goal and the last two messages (expanded to
whole native call/result spans) stay unchanged, only a nonempty eligible middle is
summarized, an empty middle changes nothing, and the summary goes through the run's pinned
client with its own attempt state, never touching the main request's state or checkpoint.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import httpx
import pytest

import context_manager
from call_attempt_state import CallAttemptState
from context_manager import _message_spans, _partition_compaction, maybe_compact
from model_registry import ModelMetadata
from react_loop import _LoopState, _prepare_request
from tests.execution_harness import RecordingExecutor, build_context
from tests.run_model_fixtures import make_run_model_client

SUMMARY_BODY = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}
THRESHOLD = 100


def _chars(messages, system, model=None):
    return sum(len(json.dumps(m, default=str)) for m in messages) // 4


@pytest.fixture(autouse=True)
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
            {"id": i, "type": "function", "function": {"name": "t", "arguments": "{}"}} for i in ids
        ],
    }


def _res(call_id, text="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _run(messages, goal_idx=0, **kw):
    prompts: list[str] = []

    def summarize(prompt):
        prompts.append(prompt)
        return "- summary"

    snapshot = copy.deepcopy(messages)
    out, idx = maybe_compact(
        messages, "sys", MagicMock(), goal_idx=goal_idx, threshold=THRESHOLD,
        summarize=summarize, **kw,
    )
    assert messages == snapshot, "input history must never be mutated"
    return out, idx, prompts


HUGE = "H" * 6000


class TestPartitionParameters:
    """Characterization: `_partition_compaction` keeps whole spans around goal and tail."""

    @pytest.mark.parametrize(
        "messages, goal, expected",
        [
            ([_u("g")], 0, ([], [], [0])),
            ([_u("g"), _a("x")], 0, ([], [], [0, 1])),
            ([_u("g"), _a("1"), _u("2"), _a("3"), _u("4")], 0, ([0], [1, 2], [3, 4])),
            # goal near the end sits inside the protected tail
            ([_u("o"), _a("1"), _u("2"), _u("g"), _a("4")], 3, ([], [0, 1, 2], [3, 4])),
            # goal inside a parallel batch protects the whole batch
            (
                [_u("o"), _calls("a", "b"), _res("a"), _res("b"), _a("x"), _u("y"), _a("z")],
                2, ([1, 2, 3], [0, 4], [5, 6]),
            ),
            # last two start at a result: expand to the assistant call and all results
            (
                [_u("g"), _a("m"), _calls("a", "b"), _res("a"), _res("b")],
                0, ([0], [1], [2, 3, 4]),
            ),
            # adjacent native batches: only the tail batch is kept
            (
                [_u("g"), _calls("a"), _res("a"), _calls("b", "c"), _res("b"), _res("c")],
                0, ([0], [1, 2], [3, 4, 5]),
            ),
            # earlier summary is ordinary middle history
            (
                [_u("g"), _u("[Compacted context — earlier steps summary]\nold"), _a("1"), _u("2"), _a("3")],
                0, ([0], [1, 2], [3, 4]),
            ),
        ],
    )
    def test_partition(self, messages, goal, expected):
        assert _partition_compaction(messages, goal) == expected

    def test_spans_never_split_a_batch(self):
        msgs = [_calls("a", "b"), _res("a"), _res("b"), _u("x")]
        assert _message_spans(msgs) == [(0, 3), (3, 4)]


class TestEmptyEligibleMiddle:
    def test_goal_plus_two_recent_is_returned_untouched(self):
        msgs = [_u("goal " + HUGE), _a("a " + HUGE), _u("b " + HUGE)]
        out, idx, prompts = _run(msgs)
        assert out is msgs and idx == 0 and prompts == []

    def test_parallel_batch_results_are_not_clipped(self):
        msgs = [_u("goal"), _calls("a", "b"), _res("a", HUGE), _res("b", HUGE)]
        out, idx, prompts = _run(msgs)
        assert out is msgs and idx == 0 and prompts == []

    def test_single_message_history(self):
        msgs = [_u("goal " + HUGE)]
        out, idx, prompts = _run(msgs)
        assert out is msgs and idx == 0 and prompts == []

    def test_huge_tool_defs_alone_never_clip_or_summarize(self):
        msgs = [_u("goal"), _a("ok"), _u("next")]
        out, idx, prompts = _run(msgs, input_tokens=10_000_000)
        assert out is msgs and idx == 0 and prompts == []


class TestNonemptyMiddle:
    def _history(self):
        return [
            _u("goal " + HUGE),
            _a("old1 " + HUGE),
            _u("old2 " + HUGE),
            _calls("a", "b"),
            _res("a", "R" * 6000),
            _res("b", "S" * 6000),
        ]

    def test_goal_and_recent_complete_spans_survive_byte_identical(self):
        msgs = self._history()
        out, idx, prompts = _run(msgs)
        assert len(prompts) == 1
        assert out[0] is msgs[0] and idx == 0
        assert out[-3:] == msgs[-3:]
        assert out[1]["content"].startswith("[Compacted context")
        assert len(out) == 5

    def test_still_over_threshold_is_sent_as_is_without_tightening(self):
        msgs = self._history()
        out, _idx, prompts = _run(msgs)
        assert _chars(out, "") > THRESHOLD
        assert len(prompts) == 1
        assert out[-3:] == msgs[-3:]
        assert "chars omitted" not in json.dumps(out[0]) + json.dumps(out[-3:])

    def test_goal_in_recent_tail_with_multimodal_content_is_intact(self):
        goal = {"role": "user", "content": [
            {"type": "text", "text": "look " + HUGE},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}
        msgs = [_u("m1 " + HUGE), _a("m2 " + HUGE), goal, _a("tail " + HUGE)]
        out, idx, prompts = _run(msgs, goal_idx=2)
        assert len(prompts) == 1
        assert out[idx] is goal and out[-1] == msgs[-1]
        assert out[idx]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"

    def test_previous_summary_is_ordinary_middle_history(self):
        old = _u("[Compacted context — earlier steps summary]\nold " + HUGE)
        msgs = [_u("goal"), old, _a("x " + HUGE), _u("y " + HUGE), _a("z " + HUGE)]
        out, _idx, prompts = _run(msgs)
        assert len(prompts) == 1 and "old " in prompts[0]
        assert sum("Compacted context" in str(m["content"]) for m in out) == 1


# ---------------------------------------------------------------------------
# Real pinned client: summary retry isolation (no stand-in for the summarizer)
# ---------------------------------------------------------------------------

class _CheckpointSpy:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append(name)
        return record


def _observe_bindings(client):
    """Record every CallAttemptState the real pinned client is bound to."""
    bound: list[CallAttemptState] = []
    real = client.bind_attempt

    def spy(state, **kw):
        bound.append(state)
        return real(state, **kw)

    client.bind_attempt = spy
    return bound


def _history_state():
    msgs = [_u("goal")] + [
        _u(f"old {i} " + "alpha beta " * 400) if i % 2 == 0 else _a(f"old {i} " + "alpha beta " * 400)
        for i in range(6)
    ] + [_a("recent a"), _u("recent b")]
    return _LoopState(messages=msgs, goal_idx=0, max_steps=1)


class TestSummaryUsesPinnedClientWithOwnAttemptState:
    def _client(self, handler):
        return make_run_model_client(
            "openai", httpx.MockTransport(handler),
            metadata=ModelMetadata(context_tokens=8_192, output_tokens=1_024),
            upstream_id="pinned-model",
        )

    def test_one_shot_success_is_isolated_from_main_request_state(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={**SUMMARY_BODY, "usage": {
                    "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
            )

        client = self._client(handler)
        spy = _CheckpointSpy()
        ctx = build_context(
            client, RecordingExecutor(), max_retries=2, compaction_threshold_tokens=500,
            checkpoint_store=spy,
        )
        state = _history_state()
        main_attempt = state.attempt_state = CallAttemptState(2, 0)
        main_before = main_attempt.to_dict()
        bound = _observe_bindings(client)
        recent = state.messages[-2:]
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert len(seen) == 1
        usage = client.get_today_usage()  # the one summary send is counted exactly once
        assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (7, 3, 10)
        assert len(bound) == 1 and bound[0] is not main_attempt
        assert bound[0].outbound_attempts == 1 and bound[0].remaining_retries == 0
        assert bound[0].manual_retries_used == 0
        assert main_attempt.to_dict() == main_before and main_attempt.outbound_attempts == 0
        for request in seen:  # route/credential/model come from the pinned run model only
            assert str(request.url) == "https://openai.example.test/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer fake-test-key"
            assert json.loads(request.content)["model"] == "pinned-model"
        assert state.attempt_state is main_attempt
        assert state.messages[-2:] == recent and state.messages[0]["content"] == "goal"
        assert any("Compacted context" in str(m["content"]) for m in state.messages)
        assert spy.calls == []

    def test_transient_summary_error_is_one_send_and_leaves_main_state_alone(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503, json={})

        client = self._client(handler)
        spy = _CheckpointSpy()
        ctx = build_context(
            client, RecordingExecutor(), max_retries=2, compaction_threshold_tokens=500,
            checkpoint_store=spy,
        )
        state = _history_state()
        main_attempt = state.attempt_state = CallAttemptState(2, 0)
        main_before = main_attempt.to_dict()
        bound = _observe_bindings(client)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert len(seen) == 1  # one-shot: no summary retry despite max_retries=2
        assert len(bound) == 1 and bound[0] is not main_attempt
        assert bound[0].outbound_attempts == 1 and bound[0].automatic_retries_used == 0
        # 6.6: the terminal failure is absorbed as a compaction failure (history kept, run
        # disabled); the main attempt state and checkpoint are untouched either way.
        assert state.attempt_state is main_attempt and main_attempt.to_dict() == main_before
        assert state.auto_compaction_disabled is True
        assert not any("Compacted context" in str(m["content"]) for m in state.messages)
        assert spy.calls == []

    def test_empty_middle_makes_no_summary_request(self):
        seen: list[httpx.Request] = []
        client = self._client(lambda r: seen.append(r) or httpx.Response(200, json=SUMMARY_BODY))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(messages=[_u("goal " + HUGE), _a("a"), _u("b")], goal_idx=0, max_steps=1)
        before = state.messages
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert seen == [] and state.messages is before
