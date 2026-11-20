"""Bounded request-level retry and protocol-switch coverage."""

from __future__ import annotations

from contextlib import contextmanager
import httpx
import pytest

from call_attempt_state import CallAttemptState
from checkpoint_store import CheckpointRetireStatus, CheckpointStore
from interfaces import ChatResponse
from llm_client import LLMContextOverflowError, LLMPermanentError
from react_loop import ReactContext, _LoopState, _request_turn
from tests.execution_harness import (
    LLMCall,
    RecordingExecutor,
    ScriptedLLM,
    _NullMemory,
    _NullToolIndex,
    run_react,
)
from tests.test_run_model_client import _client


class ScriptedRequestLLM:
    """Minimal public-call fake; invocation accounting is owned by the loop."""

    def __init__(self, json_script=(), native_script=()):
        self.json_script = list(json_script)
        self.native_script = list(native_script)
        self.json_calls = 0
        self.native_calls = 0
        self.llm_cfg = {"model": "fixture-model", "id": "provider/model"}
        self._attempt_binding: tuple[CallAttemptState, bool] | None = None

    @contextmanager
    def bind_attempt(self, state, *, manual=False, definition_revision=0):
        del definition_revision
        previous = self._attempt_binding
        self._attempt_binding = (state, manual)
        try:
            yield
        finally:
            self._attempt_binding = previous

    def _record(self, mode):
        if self._attempt_binding is not None:
            state, manual = self._attempt_binding
            state.begin_outbound_attempt(mode, manual=manual)

    def _take(self, script, mode):
        result = script.pop(0)
        if isinstance(result, BaseException):
            if not isinstance(result, NotImplementedError):
                self._record(mode)
            raise result
        self._record(mode)
        return result

    def chat(self, *args, **kwargs):
        del args, kwargs
        self.json_calls += 1
        return self._take(self.json_script, "json")

    def chat_with_tools(self, *args, **kwargs):
        del args, kwargs
        self.native_calls += 1
        return self._take(self.native_script, "native")


def _request(llm, *, retries=4, wait=None, native=False, retry_response="timeout"):
    import threading
    from confirmation import ConfirmationManager

    confirmation = ConfirmationManager()
    confirmation.request_retry = lambda *args, **kwargs: retry_response
    waits = []

    def fake_wait(event, delay):
        waits.append(delay)
        return event.is_set()

    ctx = ReactContext(
        llm=llm, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        max_retries=retries, retry_wait=wait or fake_wait,
        confirmation=confirmation, cancel_event=threading.Event(),
        trace_id="r-retry-test",
    )
    ctx._tool_defs = [{"type": "function", "function": {"name": "finish"}}] if native else []
    state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=10)
    turn = _request_turn(ctx, state, "system", lambda _message: None, native)
    return turn, state, waits


def test_timeout_has_five_actual_calls_and_exponential_waits():
    llm = ScriptedRequestLLM(json_script=[httpx.TimeoutException("fixture")] * 4 + ['{"action":"finish"}'])
    turn, state, waits = _request(llm)
    assert llm.json_calls == 5
    assert waits == [2.0, 4.0, 8.0, 16.0]
    assert state.attempt_state.outbound_attempts == 5
    assert turn.raw == '{"action":"finish"}'


def test_real_client_transport_counts_each_automatic_retry_once():
    """Exercise retry accounting at the real client's MockTransport boundary."""
    import threading

    responses = [503, 503, 200]
    captured = []

    def handler(request):
        captured.append(request)
        status = responses.pop(0)
        if status == 200:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"action":"finish"}'}}]},
            )
        return httpx.Response(status, text="fixture unavailable")

    client = _client(transport=httpx.MockTransport(handler))
    waits = []
    ctx = ReactContext(
        llm=client, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        max_retries=2, retry_wait=lambda event, delay: waits.append(delay) or event.is_set(),
        cancel_event=threading.Event(), trace_id="r-mock-transport",
    )
    ctx._tool_defs = []
    state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=10)
    try:
        turn = _request_turn(ctx, state, "system", lambda _message: None)
        assert turn.raw == '{"action":"finish"}'
        assert len(captured) == 3
        assert state.attempt_state.outbound_attempts == 3
        assert state.attempt_state.automatic_retries_used == 2
        assert waits == [2.0, 4.0]
    finally:
        client.close()


def test_zero_retry_budget_is_one_call():
    llm = ScriptedRequestLLM(json_script=[httpx.TimeoutException("fixture"), "unused"])
    turn, state, waits = _request(llm, retries=0)
    assert llm.json_calls == 1
    assert waits == []
    assert turn.early_return.startswith("❌")
    assert state.attempt_state.outbound_attempts == 1


def test_local_native_unsupported_converts_to_json_without_count_or_wait():
    llm = ScriptedRequestLLM(native_script=[NotImplementedError("unsupported")], json_script=['{"action":"finish"}'])
    turn, state, waits = _request(llm, native=True)
    assert llm.native_calls == 1
    assert llm.json_calls == 1
    assert state.attempt_state.mode == "json"
    assert state.attempt_state.outbound_attempts == 1
    assert waits == []
    assert turn.raw == '{"action":"finish"}'


def test_native_timeout_retries_native_without_json_switch():
    llm = ScriptedRequestLLM(native_script=[httpx.TimeoutException("fixture"), ChatResponse(text="prose")])
    turn, state, waits = _request(llm, native=True)
    assert llm.native_calls == 2
    assert llm.json_calls == 0
    assert state.attempt_state.mode == "native"
    assert waits == [2.0]
    assert turn.text_from_native
    assert turn.raw == "prose"


def test_native_empty_response_reasks_same_native_request():
    llm = ScriptedRequestLLM(native_script=[ChatResponse(text=""), ChatResponse(text="ready")])
    turn, state, waits = _request(llm, native=True)
    assert llm.native_calls == 2
    assert llm.json_calls == 0
    assert state.attempt_state.outbound_attempts == 2
    assert waits == [2.0]
    assert turn.raw == "ready"


def test_unknown_permanent_and_context_errors_do_not_auto_retry():
    cases = [RuntimeError("sensitive details"), LLMPermanentError("sensitive details"), LLMContextOverflowError("overflow")]
    for error in cases:
        llm = ScriptedRequestLLM(json_script=[error, "unused"])
        turn, state, waits = _request(llm)
        assert llm.json_calls == 1
        assert waits == []
        assert turn.early_return
        assert "sensitive details" not in turn.early_return
        assert state.attempt_state.outbound_attempts == 1


def test_empty_response_reasks_share_the_attempt_budget():
    llm = ScriptedRequestLLM(json_script=["", "", ""])
    turn, state, waits = _request(llm)
    assert llm.json_calls == 3
    assert state.attempt_state.outbound_attempts == 3
    assert waits == [2.0, 4.0]
    assert turn.early_return.startswith("❌")


def test_checkpoint_retirement_failure_blocks_before_call_or_grant_consumption(tmp_path, monkeypatch):
    import threading

    from confirmation import ConfirmationManager

    trace_id = "r-retire-block"
    checkpoint = {
        "format_version": 2,
        "trace_id": trace_id,
        "user_goal": "goal",
        "messages": [{"role": "user", "content": "goal"}],
        "step": 1,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model_id": "provider/model",
        "attempt_state": CallAttemptState(max_retries=0).to_dict(),
        "error_info": {"type": "timeout", "message": "timeout", "retryable": True, "detail": ""},
        "created_at": "2026-01-01T00:00:00Z",
    }
    store = CheckpointStore(str(tmp_path))
    owner = store.acquire_owner(trace_id)
    assert owner is not None
    assert store.save(trace_id, checkpoint, owner=owner)
    llm = ScriptedRequestLLM(json_script=['{"action":"finish"}'])
    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    attempt.authorize_manual_retry()
    state = _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0,
        max_steps=8, attempt_state=attempt,
    )
    confirmation = ConfirmationManager()
    ctx = ReactContext(
        llm=llm, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=True,
        confirmation=confirmation, cancel_event=threading.Event(), trace_id=trace_id,
    )
    monkeypatch.setattr("checkpoint_store.os.remove", lambda _path: (_ for _ in ()).throw(OSError("denied")))
    try:
        turn = _request_turn(ctx, state, "system", lambda _message: None)
        assert turn.early_return == "The saved checkpoint could not be retired; no model call was sent."
        assert llm.json_calls == 0
        assert attempt.outbound_attempts == 1
        assert attempt.manual_retry_authorized is True
        assert store.recovery_available(trace_id, owner) is True
        assert store.retire_pending(trace_id, owner).status is CheckpointRetireStatus.BLOCKED
    finally:
        owner.release()


def test_pre_authorized_grant_is_not_stacked_after_pre_send_error(tmp_path):
    import threading

    from confirmation import ConfirmationManager
    from react_loop import LLMErrorInfo, _handle_llm_error

    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    attempt.authorize_manual_retry()
    state = _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0,
        max_steps=8, attempt_state=attempt,
    )
    confirmation = ConfirmationManager()
    confirmation.request_retry = lambda *args, **kwargs: "retry"
    llm = ScriptedRequestLLM()
    ctx = ReactContext(
        llm=llm, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        confirmation=confirmation, cancel_event=threading.Event(), trace_id="r-grant-reuse",
        interactive_llm_recovery=True,
    )

    assert _handle_llm_error(
        ctx, state, LLMErrorInfo("unknown", "failed", True, ""), lambda _message: None, "goal"
    ) is None
    assert attempt.manual_retry_authorized is True
    assert attempt.manual_retries_used == 0
    assert attempt.automatic_retries_used == 0


def test_failed_retry_checkpoint_save_preserves_old_file_and_authorization(tmp_path, monkeypatch):
    import json
    import threading

    from confirmation import ConfirmationManager
    from react_loop import LLMErrorInfo, _handle_llm_error

    trace_id = "r-save-failure"
    store = CheckpointStore(str(tmp_path))
    owner = store.acquire_owner(trace_id)
    assert owner is not None
    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    old_checkpoint = {
        "format_version": 2, "trace_id": trace_id, "user_goal": "goal",
        "messages": [{"role": "user", "content": "goal"}], "step": 1,
        "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
        "model_id": "provider/model", "attempt_state": attempt.to_dict(),
        "error_info": {"type": "timeout", "message": "timeout", "retryable": True, "detail": ""},
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert store.save(trace_id, old_checkpoint, owner=owner)
    attempt.authorize_manual_retry()
    checkpoint_path = tmp_path / "run_checkpoints" / f"{trace_id}.json"
    original_file = checkpoint_path.read_bytes()
    monkeypatch.setattr(store, "save", lambda *args, **kwargs: False)
    confirmation = ConfirmationManager()
    confirmation.request_retry = lambda *args, **kwargs: "retry"
    ctx = ReactContext(
        llm=ScriptedRequestLLM(), tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, confirmation=confirmation,
        cancel_event=threading.Event(), trace_id=trace_id,
        interactive_llm_recovery=True,
    )
    state = _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0,
        max_steps=8, attempt_state=attempt,
    )
    try:
        assert _handle_llm_error(
            ctx, state, LLMErrorInfo("unknown", "failed", True, ""), lambda _message: None, "goal"
        ) is None
        assert checkpoint_path.read_bytes() == original_file
        assert json.loads(original_file)["attempt_state"]["manual_retry_authorized"] is False
        assert attempt.manual_retry_authorized is True
        assert attempt.manual_retries_used == 0
        assert attempt.remaining_retries == 0
        assert ctx.disk_recovery_available is False
    finally:
        owner.release()


def test_retired_grant_is_consumed_once_when_post_send_checkpoint_save_fails(
    tmp_path, monkeypatch
):
    import threading

    from confirmation import ConfirmationManager

    trace_id = "r-post-send-save-failure"
    store = CheckpointStore(str(tmp_path))
    owner = store.acquire_owner(trace_id)
    assert owner is not None
    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    attempt.authorize_manual_retry()
    checkpoint = {
        "format_version": 2, "trace_id": trace_id, "user_goal": "goal",
        "messages": [{"role": "user", "content": "goal"}], "step": 1,
        "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
        "model_id": "provider/model", "attempt_state": attempt.to_dict(),
        "error_info": {"type": "timeout", "message": "timeout", "retryable": True, "detail": ""},
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert store.save(trace_id, checkpoint, owner=owner)
    llm = ScriptedRequestLLM(json_script=[httpx.TimeoutException("fixture")])
    confirmation = ConfirmationManager()
    confirmation.request_retry = lambda *args, **kwargs: "timeout"
    ctx = ReactContext(
        llm=llm, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=True,
        confirmation=confirmation, cancel_event=threading.Event(), trace_id=trace_id,
        max_retries=0, interactive_llm_recovery=True,
    )
    state = _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0,
        max_steps=8, attempt_state=attempt,
    )
    save_attempts: list[str] = []

    def failing_save(*args, **kwargs):
        save_attempts.append(args[0])
        return False

    monkeypatch.setattr(store, "save", failing_save)
    try:
        turn = _request_turn(ctx, state, "system", lambda _message: None)
        assert turn.early_return.startswith("❌")
        assert save_attempts == [trace_id]  # the post-send save was really attempted
        assert llm.json_calls == 1
        assert attempt.outbound_attempts == 2
        assert attempt.manual_retries_used == 1
        assert attempt.manual_retry_authorized is False
        assert store.load(trace_id, owner=owner) is None
        assert ctx.disk_recovery_available is False
    finally:
        owner.release()


def _saved_manual_resume(tmp_path):
    """Create an admitted, disk-backed manual retry with its active owner."""
    trace_id = "r-disabled-write-resume"
    store = CheckpointStore(str(tmp_path))
    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    attempt.authorize_manual_retry()
    checkpoint = {
        "format_version": 2, "trace_id": trace_id, "user_goal": "goal",
        "messages": [{"role": "user", "content": "goal"}], "step": 1,
        "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
        "model_id": "provider/model", "attempt_state": attempt.to_dict(),
        "error_info": {"type": "timeout", "message": "timeout", "retryable": True, "detail": ""},
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert store.save(trace_id, checkpoint)
    owner = store.acquire_owner(trace_id)
    assert owner is not None
    assert store.load(trace_id, owner=owner) == checkpoint
    return trace_id, store, owner, attempt, checkpoint


def test_disabled_checkpoint_writes_still_retire_resumed_snapshot_before_mocktransport_send(tmp_path):
    import threading

    trace_id, store, owner, attempt, checkpoint = _saved_manual_resume(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"action":"finish"}'}}]},
        )

    client = _client(transport=httpx.MockTransport(handler))
    ctx = ReactContext(
        llm=client, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=False,
        cancel_event=threading.Event(), trace_id=trace_id, max_retries=0,
    )
    state = _LoopState(
        messages=checkpoint["messages"], goal_idx=0, max_steps=8,
        attempt_state=attempt,
    )
    checkpoint_path = tmp_path / "run_checkpoints" / f"{trace_id}.json"
    try:
        turn = _request_turn(ctx, state, "system", lambda _message: None)
        assert turn.raw == '{"action":"finish"}'
        assert len(captured) == 1
        assert not checkpoint_path.exists()
        assert attempt.outbound_attempts == 2
        assert attempt.manual_retries_used == 1
        assert attempt.manual_retry_authorized is False
        assert store.recovery_available(trace_id, owner) is False
    finally:
        client.close()
        owner.release()


def test_disabled_checkpoint_writes_blocked_retirement_sends_nothing_and_preserves_grant(
    tmp_path, monkeypatch
):
    import threading

    trace_id, store, owner, attempt, checkpoint = _saved_manual_resume(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "unused"}}]})

    client = _client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "checkpoint_store.os.remove",
        lambda _path: (_ for _ in ()).throw(OSError("denied")),
    )
    ctx = ReactContext(
        llm=client, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=False,
        cancel_event=threading.Event(), trace_id=trace_id, max_retries=0,
    )
    state = _LoopState(
        messages=checkpoint["messages"], goal_idx=0, max_steps=8,
        attempt_state=attempt,
    )
    try:
        turn = _request_turn(ctx, state, "system", lambda _message: None)
        assert "no model call was sent" in turn.early_return
        assert captured == []
        assert attempt.outbound_attempts == 1
        assert attempt.manual_retries_used == 0
        assert attempt.manual_retry_authorized is True
        assert store.load(trace_id, owner=owner) == checkpoint
    finally:
        client.close()
        owner.release()


def _cancelled_resume(tmp_path, *, enabled, mismatch=False, blocked=False, monkeypatch=None):
    import threading

    from react_loop import react_loop

    trace_id, store, owner, attempt, checkpoint = _saved_manual_resume(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "unused"}}]})

    client = _client(transport=httpx.MockTransport(handler))
    path = tmp_path / "run_checkpoints" / f"{trace_id}.json"
    if mismatch:
        import json

        changed = dict(checkpoint, step=2)
        path.write_text(json.dumps(changed))
    if blocked:
        monkeypatch.setattr(
            "checkpoint_store.os.remove",
            lambda _path: (_ for _ in ()).throw(OSError("denied")),
        )
    cancel = threading.Event()
    cancel.set()
    ctx = ReactContext(
        llm=client, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=enabled,
        cancel_event=cancel, owns_cancel_event=False, trace_id=trace_id, max_retries=0,
    )
    state = _LoopState(
        messages=checkpoint["messages"], goal_idx=0, max_steps=8,
        attempt_state=attempt,
    )
    try:
        result = react_loop(ctx, "goal", initial_state=state)
    finally:
        client.close()
    return result, captured, attempt, path, owner, store, checkpoint


@pytest.mark.parametrize("enabled", [True, False])
def test_pre_send_cancel_preserves_saved_checkpoint_regardless_of_write_permission(tmp_path, enabled):
    try:
        result, captured, attempt, path, owner, store, checkpoint = _cancelled_resume(
            tmp_path, enabled=enabled
        )
        assert "[Cancelled]" in result or "stopped by operator" in result
        assert captured == []
        assert attempt.outbound_attempts == 1
        assert attempt.manual_retries_used == 0
        assert attempt.manual_retry_authorized is True
        assert path.exists()
        assert store.load(owner.trace_id, owner=owner) == checkpoint
        assert store.recovery_available(owner.trace_id, owner) is True
        assert owner._released is False
    finally:
        owner.release()


class _StateRecordingLLM(ScriptedLLM):
    """Scripted JSON client that may fail a call and records its bound state."""

    def __init__(self, responses, executor=None):
        super().__init__(responses)
        self.executor = executor
        self.states: list[CallAttemptState] = []
        self.tools_run_before_call: list[int] = []

    def chat(self, messages, system=None, progress_cb=None, json_mode=False):
        del progress_cb, json_mode
        self.calls.append(LLMCall(messages=list(messages), system=system, images_present=False))
        response = self._next()
        self.states.append(self._attempt_binding[0])
        self.tools_run_before_call.append(len(self.executor.calls) if self.executor else 0)
        self._count_attempt("json")
        if isinstance(response, BaseException):
            raise response
        return response


def _no_operator_retry():
    """Confirmation whose retry card times out at once instead of waiting."""
    from confirmation import ConfirmationManager

    confirmation = ConfirmationManager()
    confirmation.request_retry = lambda *args, **kwargs: "timeout"
    return confirmation


_SHELL_ACTION = '{"action": "tool", "tool": "shell", "args": {"command": "touch marker"}}'
_FINISH_ACTION = '{"action": "finish", "result": "done"}'


@pytest.mark.parametrize("adapter", ["openai", "openrouter", "anthropic", "google"])
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text='{"choices":[{"message":{"content":"partial answer"}}]}'),
        httpx.Response(200, text='{"choices":[{"message":{"content":"partial answer'),
    ],
    ids=["transient-status", "truncated-body"],
)
def test_failing_real_client_call_emits_no_partial_output_or_effect(adapter, response):
    """A non-streaming call that fails surfaces none of its received body text.

    Every adapter reads the complete response before parsing and discards
    ``progress_cb``, so a failing call cannot emit partial output; the provider
    holds no executor, so it cannot cause a tool effect either.
    """
    client = _client(adapter, transport=httpx.MockTransport(lambda _request: response))
    emitted: list[str] = []
    state = CallAttemptState(max_retries=4, retry_delay=2.0)
    try:
        with client.bind_attempt(state):
            with pytest.raises(Exception) as raised:
                client.chat([{"role": "user", "content": "goal"}], progress_cb=emitted.append)
    finally:
        client.close()
    assert emitted == []
    assert "partial answer" not in str(raised.value)
    assert state.outbound_attempts == 1
    assert state.partial_output is False
    assert state.effect is False


def test_real_client_failed_call_body_never_reaches_progress_or_history():
    import threading

    responses = [
        httpx.Response(503, text='{"choices":[{"message":{"content":"partial answer"}}]}'),
        httpx.Response(200, json={"choices": [{"message": {"content": _FINISH_ACTION}}]}),
    ]
    client = _client(transport=httpx.MockTransport(lambda _request: responses.pop(0)))
    progress: list[str] = []
    waits: list[float] = []
    ctx = ReactContext(
        llm=client, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
        retry_wait=lambda event, delay: waits.append(delay) or event.is_set(),
        cancel_event=threading.Event(), trace_id="r-no-partial",
    )
    ctx._tool_defs = []
    state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=10)
    try:
        turn = _request_turn(ctx, state, "system", progress.append)
    finally:
        client.close()
    assert turn.raw == _FINISH_ACTION
    assert waits == [2.0]
    assert state.attempt_state.automatic_retries_used == 1
    assert not any("partial answer" in message for message in progress)
    assert not any("partial answer" in str(message) for message in state.messages)


def test_transient_failure_after_completed_tool_turn_retries_without_replay():
    """An earlier completed tool turn neither blocks nor is replayed by a later retry.

    ``max_retries=1``: the first request spends its single retry, so the later
    request can only retry because acceptance of the tool action started a
    fresh attempt state.
    """
    executor = RecordingExecutor()
    llm = _StateRecordingLLM(
        [
            httpx.TimeoutException("fixture"),
            _SHELL_ACTION,
            httpx.TimeoutException("fixture"),
            _FINISH_ACTION,
        ],
        executor,
    )
    waits: list[float] = []
    result, calls, _ = run_react(
        llm, executor, "goal", max_retries=1, confirmation=_no_operator_retry(),
        retry_wait=lambda event, delay: waits.append(delay) or event.is_set(),
    )
    assert result == "done"
    assert [call.tool for call in calls] == ["shell"]
    assert llm.tools_run_before_call == [0, 0, 1, 1]
    first, later = llm.states[0], llm.states[2]
    assert llm.states[1] is first and llm.states[3] is later
    assert later is not first
    assert first.automatic_retries_used == 1
    assert later.outbound_attempts == 2 and later.automatic_retries_used == 1
    assert waits == [2.0, 2.0]
    assert any("shell ok" in str(message.get("content")) for message in llm.calls[2].messages)


def test_parse_reprompt_retains_request_state_and_budget():
    llm = _StateRecordingLLM(["not json at all", httpx.TimeoutException("fixture"), _FINISH_ACTION])
    waits: list[float] = []
    result, calls, _ = run_react(
        llm, RecordingExecutor(), "goal", confirmation=_no_operator_retry(),
        retry_wait=lambda event, delay: waits.append(delay) or event.is_set(),
    )
    assert result == "done"
    assert calls == []
    state = llm.states[0]
    assert all(seen is state for seen in llm.states)
    assert state.outbound_attempts == 3
    assert state.automatic_retries_used == 2
    # The timeout retry is the second automatic retry of the same request.
    assert waits == [4.0]


def test_confirmation_wait_keeps_request_state_until_action_is_accepted():
    import threading
    import time

    from confirmation import ConfirmationManager
    from react_loop import _run_single_step

    class _ConfirmingExecutor:
        def __init__(self):
            self.confirmed: list[str] = []

        def is_builtin(self, _tool_name):
            return True

        def execute(self, _tool_name, _args, **_kwargs):
            return {"requires_confirmation": True, "token": "tok", "description": "shell"}

        def confirm(self, token, **_kwargs):
            self.confirmed.append(token)
            return {"success": True, "output": "ran", "error": "", "exit_code": 0}

    llm = _StateRecordingLLM([_SHELL_ACTION])
    executor = _ConfirmingExecutor()
    confirmation = ConfirmationManager()
    state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=10)
    during_wait = []

    def request_confirmation(token, tool_name, description, progress_cb, **_kwargs):
        del token, tool_name, description, progress_cb
        during_wait.append((state.attempt_state, state.attempt_state.to_dict()))
        return True

    confirmation.request_confirmation = request_confirmation
    ctx = ReactContext(
        llm=llm, tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=executor, mcp_manager=None, skill_registry=None,
        confirmation=confirmation, cancel_event=threading.Event(), trace_id="r-confirm-wait",
    )
    ctx._tool_defs = []
    result = _run_single_step(ctx, state, "system", "goal", time.time(), lambda _m: None, False)
    assert result.early_return is None
    assert executor.confirmed == ["tok"]
    [(waiting_state, snapshot)] = during_wait
    assert waiting_state is llm.states[0]
    assert snapshot["outbound_attempts"] == 1 and snapshot["automatic_retries_used"] == 0
    assert state.attempt_state is None


@pytest.mark.parametrize("case", ["mismatch", "blocked"])
def test_pre_send_cancel_preserves_mismatched_or_blocked_checkpoint(tmp_path, monkeypatch, case):
    try:
        result, captured, attempt, path, owner, store, checkpoint = _cancelled_resume(
            tmp_path, enabled=False, mismatch=case == "mismatch",
            blocked=case == "blocked", monkeypatch=monkeypatch,
        )
        assert captured == []
        assert attempt.manual_retry_authorized is True
        assert path.exists()
        if case == "blocked":
            import json

            assert json.loads(path.read_text()) == checkpoint
    finally:
        owner.release()
