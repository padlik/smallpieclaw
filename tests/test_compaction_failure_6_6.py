"""Task 6.6: automatic compaction failure preserves history and disables the run's attempts.

A failed summary (provider error after its own retries), a whitespace summary and an invalid
candidate are separate failures: the original history and goal index are sent unchanged, one
content-free warning is logged, and further automatic attempts are disabled for that run only.
Cancellation propagates without any failure record. A provider context rejection of the main
request keeps the existing error/checkpoint path: no forced summary and no resend.

Everything runs against the real pinned ``LLMClient`` over ``httpx.MockTransport``.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

import context_manager
from agent_logging import LogEvent
from context_manager import CompactionFailedError, maybe_compact
from model_registry import ModelMetadata
from providers._errors import LLMCancelledError, LLMError, LLMPermanentError
from react_loop import _LoopState, _prepare_request, react_loop
from tests.execution_harness import RecordingExecutor, build_context, run_react
from tests.run_model_fixtures import make_run_model_client

PREFIX = "[Compacted context — earlier steps summary]"
SUMMARY_OK = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}
FINISH = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
TOOL = {"choices": [{"message": {"content": '{"action":"tool","tool":"probe","args":{}}'}}]}
CANARY_HISTORY = "CANARY-HISTORY-7731"
CANARY_BODY = "CANARY-PROVIDER-BODY-5520"
CANARY_KEY = "fake-test-key"
THRESHOLD = 300


def _is_summary(body: str) -> bool:
    return "Summarize these intermediate agent steps" in body


def _client(handler, cancel_event=None):
    return make_run_model_client(
        "openai", httpx.MockTransport(handler),
        metadata=ModelMetadata(context_tokens=9_000, output_tokens=200),
        upstream_id="fail-model", cancel_event=cancel_event,
    )


def _short_term():
    short_term = MagicMock()
    short_term.get_messages.return_value = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"history {i} {CANARY_HISTORY} " + "alpha beta " * 400}
        for i in range(6)
    ]
    return short_term


class _Wire:
    """Records requests by kind and scripts the summary/main replies."""

    def __init__(self, summaries, mains):
        self.summaries = list(summaries)
        self.mains = list(mains)
        self.summary_bodies: list[str] = []
        self.main_bodies: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if _is_summary(body):
            self.summary_bodies.append(body)
            reply = self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]
            return reply(request) if callable(reply) else httpx.Response(200, json=reply)
        self.main_bodies.append(body)
        reply = self.mains.pop(0) if len(self.mains) > 1 else self.mains[0]
        return reply(request) if callable(reply) else httpx.Response(200, json=reply)


def _http_503(_request):
    return httpx.Response(503, json={"error": {"message": CANARY_BODY}})


def _blank(_request):
    return httpx.Response(200, json={"choices": [{"message": {"content": " \n\t "}}]})


def _loop(wire, *, caplog, **ctx):
    client = _client(wire)
    try:
        with caplog.at_level(logging.DEBUG):
            result, calls, _progress = run_react(
                client, RecordingExecutor(), "GOAL", short_term=_short_term(),
                compaction_threshold_tokens=THRESHOLD, max_retries=0, **ctx,
            )
    finally:
        client.close()
    return result, calls


def _failure_records(caplog):
    return [r for r in caplog.records if "Automatic compaction failed" in r.getMessage()]


def _assert_only_client_request_failures(caplog, summary_requests):
    """Under the production pipeline the client records one ERROR ``LLM_FAILED`` per failed
    uncancelled request; compaction itself records none (unbootstrapped logging records none)."""
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert all(
        r.name == "llm_client" and r.levelno == logging.ERROR
        and isinstance(r.msg, dict)
        and r.msg.get("event_type") == LogEvent.LLM_FAILED.value
        for r in errors
    ), errors
    assert len(errors) <= summary_requests


def _assert_content_free(caplog):
    for record in caplog.records:
        blob = repr(record.__dict__) + record.getMessage()
        for canary in (CANARY_HISTORY, CANARY_BODY, CANARY_KEY, "alpha beta", "earlier steps"):
            assert canary not in blob, (canary, record.getMessage())
        assert record.exc_info is None


def _assert_original_history_sent_every_step(wire, steps):
    assert len(wire.main_bodies) == steps
    for body in wire.main_bodies:
        assert "history 0" in body and "history 5" in body and "GOAL" in body
        assert "Compacted context" not in body


# ---------------------------------------------------------------------------
# Failure / whitespace / invalid candidate: each separate, full loop, real client
# ---------------------------------------------------------------------------

class TestFailedSummaryDisablesTheRun:
    def test_terminal_provider_error_sends_original_and_attempts_once(self, caplog):
        wire = _Wire([_http_503], [TOOL, TOOL, FINISH])
        result, calls = _loop(wire, caplog=caplog)
        assert result == "done" and len(calls) == 2
        assert len(wire.summary_bodies) == 1  # later over-threshold steps make no attempt
        _assert_original_history_sent_every_step(wire, 3)
        records = _failure_records(caplog)
        assert len(records) == 1 and records[0].levelno == logging.WARNING
        assert records[0].args[0] == "summary_failed"
        _assert_only_client_request_failures(caplog, len(wire.summary_bodies))
        _assert_content_free(caplog)

    def test_provider_empty_response_error_is_a_failure(self, caplog):
        wire = _Wire([lambda _r: httpx.Response(200, json={"choices": []})], [TOOL, FINISH])
        result, _calls = _loop(wire, caplog=caplog)
        assert result == "done" and len(wire.summary_bodies) == 1
        _assert_original_history_sent_every_step(wire, 2)
        assert [r.args[0] for r in _failure_records(caplog)] == ["summary_failed"]
        _assert_only_client_request_failures(caplog, len(wire.summary_bodies))


class TestWhitespaceSummaryDisablesTheRun:
    def test_whitespace_provider_reply_sends_original_and_attempts_once(self, caplog):
        wire = _Wire([_blank], [TOOL, TOOL, FINISH])
        result, _calls = _loop(wire, caplog=caplog)
        assert result == "done" and len(wire.summary_bodies) == 1
        _assert_original_history_sent_every_step(wire, 3)
        assert len(_failure_records(caplog)) == 1
        _assert_only_client_request_failures(caplog, len(wire.summary_bodies))
        _assert_content_free(caplog)

    @pytest.mark.parametrize("blank", ["", " ", "\n\t  \r\n", "\u00a0\u2003"])
    def test_blank_callback_result_is_a_failure_not_a_silent_return(self, blank):
        msgs = _history()
        snapshot = copy.deepcopy(msgs)
        with pytest.raises(CompactionFailedError) as info:
            maybe_compact(msgs, "sys", MagicMock(), threshold=100, summarize=lambda _p: blank)
        assert info.value.reason == "empty_summary" and msgs == snapshot


class TestInvalidCandidateDisablesTheRun:
    def test_invalid_candidate_sends_original_and_attempts_once(self, caplog, monkeypatch):
        real = context_manager.validate_native_history

        def reject_candidate(messages):
            if any(str(m.get("content")).startswith(PREFIX) for m in messages):
                raise LLMPermanentError("malformed")
            return real(messages)

        monkeypatch.setattr(context_manager, "validate_native_history", reject_candidate)
        wire = _Wire([SUMMARY_OK], [TOOL, TOOL, FINISH])
        result, _calls = _loop(wire, caplog=caplog)
        assert result == "done" and len(wire.summary_bodies) == 1
        _assert_original_history_sent_every_step(wire, 3)
        assert [r.args[0] for r in _failure_records(caplog)] == ["invalid_candidate"]
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_invalid_input_history_is_not_a_compaction_failure(self):
        bad = [{"role": "tool", "tool_call_id": "x", "content": "orphan"}, {"role": "user", "content": "u"}]
        called = []
        with pytest.raises(LLMPermanentError) as info:
            maybe_compact(bad, "sys", MagicMock(), threshold=1,
                          summarize=lambda p: called.append(p) or "s")
        assert not isinstance(info.value, CompactionFailedError) and called == []


def _history():
    huge = "H" * 6000
    return [
        {"role": "user", "content": "goal " + huge},
        {"role": "assistant", "content": "old1 " + huge},
        {"role": "user", "content": "old2 " + huge},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "r2"},
    ]


class TestUnexpectedErrorsStayVisible:
    def test_programmer_error_in_the_summary_callback_propagates(self):
        def broken(_prompt):
            raise TypeError("bug")

        with pytest.raises(TypeError):
            maybe_compact(_history(), "sys", MagicMock(), threshold=100, summarize=broken)

    def test_provider_error_type_is_reported_as_a_label_only(self):
        def failing(_prompt):
            raise LLMError(f"secret {CANARY_BODY}")

        with pytest.raises(CompactionFailedError) as info:
            maybe_compact(_history(), "sys", MagicMock(), threshold=100, summarize=failing)
        assert info.value.reason == "summary_failed" and info.value.cause_type == "LLMError"
        assert CANARY_BODY not in str(info.value)


# ---------------------------------------------------------------------------
# Run scope
# ---------------------------------------------------------------------------

class TestRunScope:
    def test_new_run_on_same_controller_client_and_context_attempts_again(self, caplog):
        wire = _Wire([_http_503, SUMMARY_OK], [FINISH])
        client = _client(wire)
        ctx = build_context(
            client, RecordingExecutor(), short_term=_short_term(),
            compaction_threshold_tokens=THRESHOLD, max_retries=0,
        )
        try:
            with caplog.at_level(logging.DEBUG), \
                    patch("react_loop._build_system_prompt", return_value=("sys", None)):
                assert react_loop(ctx, "GOAL") == "done"
                assert len(wire.summary_bodies) == 1
                assert "Compacted context" not in wire.main_bodies[0]
                assert react_loop(ctx, "GOAL") == "done"
        finally:
            client.close()
        assert len(wire.summary_bodies) == 2  # the second run attempted again
        assert "Compacted context" in wire.main_bodies[1]
        assert len(_failure_records(caplog)) == 1

    def test_flag_is_run_state_not_shared(self):
        assert _LoopState(messages=[], goal_idx=0, max_steps=1).auto_compaction_disabled is False

    def test_no_eligible_middle_is_not_a_failure_and_does_not_disable(self):
        seen: list[str] = []
        client = _client(lambda r: seen.append(r.content.decode()) or httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(
            messages=[{"role": "user", "content": "goal " + "H" * 6000},
                      {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}],
            goal_idx=0, max_steps=1,
        )
        try:
            _prepare_request(ctx, state, "sys", 0)
            assert seen == [] and state.auto_compaction_disabled is False
            state.messages = _history() + [{"role": "assistant", "content": "x " * 3000}]
            state.messages.append({"role": "user", "content": "y"})
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert len(seen) == 1 and state.auto_compaction_disabled is False

    def test_successful_summary_does_not_disable(self):
        client = _client(lambda _r: httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert state.auto_compaction_disabled is False

    def test_disabled_run_skips_the_attempt_and_keeps_history(self):
        seen: list[str] = []
        client = _client(lambda r: seen.append("x") or httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        state.auto_compaction_disabled = True
        before = state.messages
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert seen == [] and state.messages is before


# ---------------------------------------------------------------------------
# State preservation on failure (same objects, main attempt/checkpoint untouched)
# ---------------------------------------------------------------------------

class TestFailurePreservesMainState:
    def test_original_objects_goal_index_and_main_attempt_survive(self):
        from call_attempt_state import CallAttemptState

        client = _client(_http_503)
        spy = MagicMock()
        ctx = build_context(
            client, RecordingExecutor(), compaction_threshold_tokens=10, max_retries=0,
            checkpoint_store=spy,
        )
        messages = _history()
        state = _LoopState(messages=messages, goal_idx=0, max_steps=1)
        state.attempt_state = main_attempt = CallAttemptState(0, 0)
        before_attempt = main_attempt.to_dict()
        listing = list(messages)
        snapshot = copy.deepcopy(messages)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert state.messages is messages and state.goal_idx == 0
        assert all(a is b for a, b in zip(messages, listing, strict=True)) and messages == snapshot
        assert state.auto_compaction_disabled is True
        assert state.attempt_state is main_attempt and main_attempt.to_dict() == before_attempt
        assert spy.method_calls == []


# ---------------------------------------------------------------------------
# Cancellation: propagates, no warning, no disable, no error classification
# ---------------------------------------------------------------------------

class TestCancellation:
    def _prepare(self, reply, caplog, *, client_sees_cancel):
        event = threading.Event()

        def handler(request):
            event.set()  # the provider was entered; the operator cancels meanwhile
            return reply(request)

        # Without the event on the client only the loop's own checks can see the cancel.
        client = _client(handler, cancel_event=event if client_sees_cancel else None)
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10,
                            cancel_event=event, max_retries=2)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        original = state.messages
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert state.messages is original and state.auto_compaction_disabled is False
        assert _failure_records(caplog) == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.parametrize("reply", [
        lambda _r: httpx.Response(200, json=SUMMARY_OK), _blank, _http_503,
    ], ids=["valid-reply", "whitespace-reply", "provider-error"])
    @pytest.mark.parametrize("client_sees_cancel", [True, False])
    def test_cancel_set_while_the_provider_call_is_in_flight(
        self, reply, client_sees_cancel, caplog,
    ):
        self._prepare(reply, caplog, client_sees_cancel=client_sees_cancel)

    def test_cancel_during_candidate_validation_is_a_cancel_not_a_failure(
        self, caplog, monkeypatch,
    ):
        event = threading.Event()
        real = context_manager.validate_native_history

        def cancel_then_reject(messages):
            if any(str(m.get("content")).startswith(PREFIX) for m in messages):
                event.set()
                raise LLMPermanentError("malformed")
            return real(messages)

        monkeypatch.setattr(context_manager, "validate_native_history", cancel_then_reject)
        client = _client(lambda _r: httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10,
                            cancel_event=event)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert state.auto_compaction_disabled is False and _failure_records(caplog) == []

    def test_cancelled_error_from_the_callback_is_not_a_compaction_failure(self):
        def cancelled(_prompt):
            raise LLMCancelledError("cancelled")

        with pytest.raises(LLMCancelledError):
            maybe_compact(_history(), "sys", MagicMock(), threshold=100, summarize=cancelled)

    def test_loop_ends_through_cancellation_without_sending_the_main_request(self, caplog):
        event = threading.Event()

        def handler(request):
            wire.summary_bodies.append("s")
            event.set()
            return httpx.Response(200, json=SUMMARY_OK)

        wire = _Wire([handler], [FINISH])
        client = _client(wire, cancel_event=event)
        ctx = build_context(client, RecordingExecutor(), short_term=_short_term(),
                            compaction_threshold_tokens=THRESHOLD, cancel_event=event,
                            owns_cancel_event=False)
        try:
            with caplog.at_level(logging.DEBUG), \
                    patch("react_loop._build_system_prompt", return_value=("sys", None)):
                result = react_loop(ctx, "GOAL")
        finally:
            client.close()
        assert "Cancelled" in result or "stopped" in result
        assert wire.main_bodies == [] and _failure_records(caplog) == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_cooperative_cancel_while_the_provider_call_blocks(self):
        event, entered, release = threading.Event(), threading.Event(), threading.Event()

        def handler(_request):
            entered.set()
            release.wait(5)
            return httpx.Response(200, json=SUMMARY_OK)

        client = _client(handler, cancel_event=event)
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10,
                            cancel_event=event)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        outcome: list[BaseException] = []

        def work():
            try:
                _prepare_request(ctx, state, "sys", 0)
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        worker = threading.Thread(target=work)
        try:
            worker.start()
            assert entered.wait(5)  # the provider was actually entered
            event.set()
        finally:
            release.set()
            worker.join(10)
            client.close()
        assert not worker.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], LLMCancelledError)
        assert state.auto_compaction_disabled is False


# ---------------------------------------------------------------------------
# Provider context rejection of the MAIN request: existing path, no repair
# ---------------------------------------------------------------------------

class TestProviderContextRejection:
    def _reject(self):
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded",
                                                   "message": "context_length_exceeded"}})

    @pytest.mark.parametrize("threshold, summaries", [(10_000_000, 0), (THRESHOLD, 1)])
    @pytest.mark.parametrize("retries", [0, 2])
    def test_rejection_takes_the_error_path_without_summary_or_resend(
        self, threshold, summaries, retries,
    ):
        wire = _Wire([SUMMARY_OK], [lambda _r: self._reject()])
        store = MagicMock()
        store.save.return_value = True
        confirmation = MagicMock()
        confirmation.request_retry.return_value = "cancel"  # the operator declines a retry
        client = _client(wire)
        ctx = build_context(
            client, RecordingExecutor(), short_term=_short_term(),
            compaction_threshold_tokens=threshold, max_retries=retries,
            checkpoint_store=store, checkpoint_enabled=True, confirmation=confirmation,
        )
        try:
            with patch("react_loop._build_system_prompt", return_value=("sys", None)):
                result = react_loop(ctx, "GOAL")
        finally:
            client.close()
        assert len(wire.main_bodies) == 1  # no automatic resend, whatever the retry setting
        assert len(wire.summary_bodies) == summaries  # nothing was summarized in reaction
        assert store.save.call_count == 1 and confirmation.request_retry.call_count == 1
        payload = store.save.call_args.args[1]
        assert payload["error_info"]["type"] == "context"
        assert payload["model_id"]
        if summaries == 0:  # nothing was compacted, and nothing was repaired after the rejection
            assert PREFIX not in json.dumps(payload["messages"])
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Pass-2: a disabled run still validates the source history before every send
# ---------------------------------------------------------------------------

SECRET_IN_HISTORY = "CANARY-MALFORMED-SECRET-9913"


def _call_entry(call_id):
    return {"id": call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _malformed_histories():
    secret = {"role": "user", "content": SECRET_IN_HISTORY}
    return {
        "orphan-result": [secret, {"role": "tool", "tool_call_id": "x", "content": SECRET_IN_HISTORY}],
        "duplicate-call-id": [secret, {"role": "assistant", "content": None,
                                       "tool_calls": [_call_entry("d"), _call_entry("d")]},
                              {"role": "tool", "tool_call_id": "d", "content": "r"}],
        "empty-call-id": [secret, {"role": "assistant", "content": None,
                                   "tool_calls": [_call_entry("")]},
                          {"role": "tool", "tool_call_id": "", "content": "r"}],
        "non-string-result": [secret, {"role": "assistant", "content": None,
                                       "tool_calls": [_call_entry("n")]},
                              {"role": "tool", "tool_call_id": "n", "content": {"k": SECRET_IN_HISTORY}}],
    }


class TestDisabledRunStillValidatesSource:
    @pytest.mark.parametrize("kind", list(_malformed_histories()))
    def test_malformed_source_fails_sanitized_with_no_provider_io(self, kind, caplog):
        seen: list[str] = []
        client = _client(lambda r: seen.append(r.content.decode()) or httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(messages=_malformed_histories()[kind], goal_idx=0, max_steps=1)
        state.auto_compaction_disabled = True
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMPermanentError) as info:
                _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert seen == []  # neither summary nor main I/O
        assert SECRET_IN_HISTORY not in str(info.value)
        assert SECRET_IN_HISTORY not in repr([r.__dict__ for r in caplog.records])
        assert _failure_records(caplog) == []  # a normal native-history error, not a compaction failure

    def test_loop_entry_with_disabled_state_sends_nothing_for_malformed_source(self):
        wire = _Wire([SUMMARY_OK], [FINISH])
        client = _client(wire)
        store = MagicMock()
        store.save.return_value = True
        confirmation = MagicMock()
        confirmation.request_retry.return_value = "cancel"
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10,
                            checkpoint_store=store, checkpoint_enabled=True,
                            confirmation=confirmation)
        state = _LoopState(messages=_malformed_histories()["orphan-result"], goal_idx=0, max_steps=9)
        state.auto_compaction_disabled = True
        try:
            with patch("react_loop._build_system_prompt", return_value=("sys", None)):
                result = react_loop(ctx, "GOAL", initial_state=state)
        finally:
            client.close()
        assert wire.main_bodies == [] and wire.summary_bodies == []
        assert isinstance(result, str) and SECRET_IN_HISTORY not in result

    def test_good_source_failure_disables_then_later_malformed_source_is_not_sent(self, caplog):
        wire = _Wire([_http_503], [FINISH])
        client = _client(wire)
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10, max_retries=0)
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=1)
        try:
            with caplog.at_level(logging.DEBUG):
                _prepare_request(ctx, state, "sys", 0)
                assert state.auto_compaction_disabled is True and len(wire.summary_bodies) == 1
                state.messages = state.messages + [_malformed_histories()["orphan-result"][1]]
                with pytest.raises(LLMPermanentError):
                    _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert len(wire.summary_bodies) == 1 and wire.main_bodies == []
        assert state.auto_compaction_disabled is True  # the flag is not reset by the integrity error
        assert len(_failure_records(caplog)) == 1  # only the first, genuine failure

    def test_bad_input_before_the_first_attempt_is_not_a_compaction_failure(self):
        seen: list[str] = []
        client = _client(lambda r: seen.append("x") or httpx.Response(200, json=SUMMARY_OK))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=10)
        state = _LoopState(messages=_malformed_histories()["duplicate-call-id"], goal_idx=0, max_steps=1)
        try:
            with pytest.raises(LLMPermanentError) as info:
                _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert not isinstance(info.value, CompactionFailedError)
        assert seen == [] and state.auto_compaction_disabled is False

    def test_valid_disabled_run_still_sends_the_main_request_with_originals(self):
        wire = _Wire([SUMMARY_OK], [FINISH])
        client = _client(wire)
        ctx = build_context(client, RecordingExecutor(), short_term=_short_term(),
                            compaction_threshold_tokens=THRESHOLD, max_retries=0)
        messages = _history()
        state = _LoopState(messages=messages, goal_idx=0, max_steps=1)
        state.auto_compaction_disabled = True
        listing = list(messages)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert wire.summary_bodies == [] and state.messages is messages
        assert all(a is b for a, b in zip(messages, listing, strict=True)) and state.goal_idx == 0
