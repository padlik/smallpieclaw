"""Tests for LLM error classification and react_loop error recovery."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Callable, Optional
from unittest.mock import patch

import httpx

from checkpoint_store import CheckpointStore
from call_attempt_state import CallAttemptState
from confirmation import ConfirmationManager
from llm_client import LLMCancelledError, LLMEmptyResponseError, LLMPermanentError
from model_registry import ModelMetadata
from react_loop import (
    LLMErrorInfo,
    ReactContext,
    _LoopState,
    _classify_llm_error,
    _get_user_goal,
    _handle_llm_error,
    react_loop,
)
from tests.execution_harness import (
    RecordingExecutor,
    _NullMemory,
    _NullToolIndex,
)
from tests.run_model_fixtures import make_run_model_client


# ---------------------------------------------------------------------------
# Error classification tests
# ---------------------------------------------------------------------------

class TestClassifyLLMError:
    """Verify _classify_llm_error maps exception types to the right bucket."""

    def test_timeout(self):
        info = _classify_llm_error(httpx.TimeoutException("timed out"))
        assert info.type == "timeout"
        assert info.retryable is True
        assert info.message.startswith("⏱️")

    def test_connection(self):
        info = _classify_llm_error(httpx.ConnectError("refused"))
        assert info.type == "connection"
        assert info.retryable is True

    def test_rate_limit(self):
        exc = _make_http_status_error(429)
        info = _classify_llm_error(exc)
        assert info.type == "rate_limit"
        assert info.retryable is True

    def test_other_http_status_is_unknown(self):
        exc = _make_http_status_error(500)
        info = _classify_llm_error(exc)
        assert info.type == "unknown"
        assert info.retryable is True

    def test_empty_response(self):
        info = _classify_llm_error(LLMEmptyResponseError("empty"))
        assert info.type == "empty"
        assert info.retryable is True

    def test_context_overflow_not_retryable(self):
        exc = _make_http_status_error(400, body='{"error": {"message": "context_length_exceeded"}}')
        info = _classify_llm_error(exc)
        assert info.type == "context"
        assert info.retryable is False

    def test_context_overflow_413(self):
        exc = _make_http_status_error(413, body="Request too large: context window exceeded")
        info = _classify_llm_error(exc)
        assert info.type == "context"
        assert info.retryable is False

    def test_context_overflow_gemini_400(self):
        exc = _make_http_status_error(
            400,
            body="The input token count (50000) exceeds the maximum number of tokens allowed (8192).",
        )
        info = _classify_llm_error(exc)
        assert info.type == "context"
        assert info.retryable is False

    def test_empty_response_still_classified(self):
        """Verify LLMEmptyResponseError is caught and classified as empty/retryable.

        This is a regression guard: LLMEmptyResponseError is a sibling of LLMError
        (both extend RuntimeError), not a subclass — it must be listed explicitly
        in the catch tuple.
        """
        info = _classify_llm_error(LLMEmptyResponseError("empty"))
        assert info.type == "empty"
        assert info.retryable is True

    def test_400_without_context_indicators_is_unknown(self):
        exc = _make_http_status_error(400, body='{"error": {"message": "invalid_parameter"}}')
        info = _classify_llm_error(exc)
        assert info.type == "unknown"
        assert info.retryable is True

    def test_permanent_not_retryable(self):
        info = _classify_llm_error(LLMPermanentError("bad request"))
        assert info.type == "permanent"
        assert info.retryable is False

    def test_unknown_runtime_error_is_retryable(self):
        info = _classify_llm_error(RuntimeError("something"))
        assert info.type == "unknown"
        assert info.retryable is True

    def test_cancelled_error_is_not_special_cased(self):
        # LLMCancelledError must propagate BEFORE classification in _request_turn.
        # The classifier itself has no special handling, so it falls through to
        # unknown.
        info = _classify_llm_error(LLMCancelledError("cancelled"))
        assert info.type == "unknown"
        assert info.retryable is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_status_error(status_code: int, body: str = "") -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code for testing."""
    request = httpx.Request("POST", "https://example.com/v1/chat")
    response = httpx.Response(status_code=status_code, content=body.encode(), request=request)
    return httpx.HTTPStatusError(
        "server error",
        request=request,
        response=response,
    )


def _checkpoint_path(ctx: ReactContext, trace_id: str) -> str:
    if ctx.checkpoint_store is None:
        return ""
    return os.path.join(
        ctx.checkpoint_store._checkpoint_dir,  # type: ignore[attr-defined]
        f"{trace_id}.json",
    )


class _AttemptBoundFake:
    """Test-only binder counts each scripted provider call exactly once."""

    def __init__(self):
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

    def _record_send(self):
        if self._attempt_binding is not None:
            state, manual = self._attempt_binding
            state.begin_outbound_attempt("json", manual=manual)


class ScriptedFinishLLM(_AttemptBoundFake):
    """Fake LLM that never raises and returns a finish response."""

    def __init__(self, response: str = '{"action": "finish", "result": "done"}'):
        super().__init__()
        self._response = response
        self._call_count = 0
        self.calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": "test-model"}

    def set_trace_id(self, trace_id: str) -> None:
        pass

    def chat(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        if not json_mode:
            return "compaction summary"
        self._record_send()
        self.calls.append(list(messages))
        self._call_count += 1
        return self._response


class ErrorThenSuccessLLM(_AttemptBoundFake):
    """Fake LLM that raises on the first call and succeeds afterwards."""

    def __init__(self, error: Exception, success_response: str):
        super().__init__()
        self._error = error
        self._success = success_response
        self._call_count = 0
        self.calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": "test-model"}

    def set_trace_id(self, trace_id: str) -> None:
        pass

    def chat(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        _ = system, progress_cb
        if not json_mode:
            return "compaction summary"
        self._record_send()
        self.calls.append(list(messages))
        self._call_count += 1
        if self._call_count == 1:
            raise self._error
        return self._success


def _build_context(
    tmp_path,
    llm,
    executor: RecordingExecutor,
    trace_id: str = "r-test1",
    checkpoint_enabled: bool = True,
    confirmation: Optional[ConfirmationManager] = None,
    checkpoint_recovery_eligible: bool = False,
) -> ReactContext:
    """Build a ReactContext wired for error-recovery tests (headless by default)."""
    data_dir = str(tmp_path / "data")
    return ReactContext(
        checkpoint_recovery_eligible=checkpoint_recovery_eligible,
        llm=llm,
        tool_index=_NullToolIndex(),
        memory=_NullMemory(),
        builtin_executor=_StubBuiltinExecutor(executor),
        mcp_manager=None,
        skill_registry=None,
        max_iterations=8,
        trace_id=trace_id,
        checkpoint_store=CheckpointStore(data_dir),
        checkpoint_enabled=checkpoint_enabled,
        confirmation=confirmation or ConfirmationManager(),
        max_retries=0,
        retry_wait=lambda event, seconds: event.is_set(),
    )


def _run_with_system(llm, executor, tmp_path, **ctx_overrides):
    """Run react_loop with a stubbed system prompt builder."""
    ctx = _build_context(tmp_path, llm, executor, **ctx_overrides)
    progress: list[str] = []
    with patch("react_loop._build_system_prompt", return_value=("system", None)):
        result = react_loop(
            ctx,
            user_goal="do the thing",
            progress_callback=progress.append,
        )
    return result, ctx, progress


class _StubBuiltinExecutor:
    """Minimal builtin-executor stand-in for the test harness."""

    def __init__(self, executor: RecordingExecutor):
        self._executor = executor

    def is_builtin(self, tool_name: str) -> bool:
        return tool_name != "create_tool"

    def execute(self, tool_name: str, args: dict, **_kwargs) -> dict:
        return self._executor.execute(tool_name, args)


# ---------------------------------------------------------------------------
# _handle_llm_error unit tests
# ---------------------------------------------------------------------------

class TestHandleLLMError:
    """Verify eligible failures save-and-return with truthful guidance."""

    def test_eligible_retryable_saves_and_returns_resume_guidance(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        ctx = _build_context(
            tmp_path,
            ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}'),
            RecordingExecutor(),
            trace_id="r-retry",
            checkpoint_recovery_eligible=True,
        )
        result = _handle_llm_error(ctx, state, error_info, lambda _: None, "goal")
        assert result == (
            "❌ ⏱️ Request timed out. Saved progress is available. Use /resume to continue."
        )
        assert os.path.exists(_checkpoint_path(ctx, "r-retry"))

    def test_exhaustion_guidance_precedes_resume_offer(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        ctx = _build_context(
            tmp_path, ScriptedFinishLLM(), RecordingExecutor(), trace_id="r-exh",
            checkpoint_recovery_eligible=True,
        )
        result = _handle_llm_error(
            ctx, state, error_info, lambda _: None, "goal", automatic_exhausted=True,
        )
        assert "Automatic retries were exhausted." in result
        assert result.endswith("Use /resume to continue.")

    def test_existing_checkpoint_is_preserved_not_deleted(self, tmp_path):
        """Failure handling never deletes (the old Cancel path is retired)."""
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        llm = ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}')
        ctx = _build_context(
            tmp_path, llm, RecordingExecutor(), trace_id="r-keep",
            checkpoint_recovery_eligible=True,
        )
        ctx.checkpoint_store.save("r-keep", {"created_at": "2024-01-01T00:00:00Z"})
        _handle_llm_error(ctx, state, error_info, lambda _: None, "goal")
        assert os.path.exists(_checkpoint_path(ctx, "r-keep"))

    def test_checkpoint_disabled_does_not_write_and_reports_unavailable(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        llm = ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}')
        ctx = _build_context(
            tmp_path,
            llm,
            RecordingExecutor(),
            trace_id="r-disabled",
            checkpoint_enabled=False,
            checkpoint_recovery_eligible=True,
        )
        result = _handle_llm_error(ctx, state, error_info, lambda _: None, "goal")
        assert "cannot be resumed" in result
        assert not os.path.exists(_checkpoint_path(ctx, "r-disabled"))


# ---------------------------------------------------------------------------
# Integration tests: react_loop failure handling
# ---------------------------------------------------------------------------

class TestReactLoopErrorRecovery:
    """End-to-end react_loop immediate durable-only outcomes."""

    def test_eligible_failure_returns_immediately_with_checkpoint(self, tmp_path):
        llm = ErrorThenSuccessLLM(
            httpx.TimeoutException("timed out"), '{"action": "finish", "result": "recovered"}',
        )
        result, ctx, progress = _run_with_system(
            llm, RecordingExecutor(), tmp_path,
            trace_id="r-preserved", checkpoint_recovery_eligible=True,
        )
        assert result.startswith("❌") and "/resume" in result
        assert llm._call_count == 1  # no in-memory continuation
        assert os.path.exists(_checkpoint_path(ctx, "r-preserved"))
        assert not any("__LLM_ERROR__" in p for p in progress)

    def test_checkpoint_disabled_ends_without_retry(self, tmp_path):
        llm = ErrorThenSuccessLLM(
            httpx.TimeoutException("timed out"), '{"action": "finish", "result": "recovered"}',
        )
        result, ctx, _ = _run_with_system(
            llm, RecordingExecutor(), tmp_path,
            trace_id="r-nocheckpt", checkpoint_recovery_eligible=True,
            checkpoint_enabled=False,
        )
        assert result.startswith("❌") and "cannot be resumed" in result
        assert llm._call_count == 1
        assert not os.path.exists(_checkpoint_path(ctx, "r-nocheckpt"))

    def test_headless_failure_has_no_checkpoint(self, tmp_path):
        llm = ErrorThenSuccessLLM(
            httpx.TimeoutException("timed out"), '{"action": "finish", "result": "recovered"}',
        )
        result, ctx, _ = _run_with_system(
            llm, RecordingExecutor(), tmp_path, trace_id="r-headless-x",
        )
        assert result.startswith("❌") and "/resume" not in result
        assert not os.path.exists(_checkpoint_path(ctx, "r-headless-x"))


# ---------------------------------------------------------------------------
# initial_state / resume tests
# ---------------------------------------------------------------------------

class TestInitialStateResume:
    """Verify react_loop honours an optional initial _LoopState."""

    def test_resumes_from_initial_state(self, tmp_path):
        resumed_state = _LoopState(
            messages=[
                {"role": "user", "content": "resumed goal"},
            ],
            goal_idx=0,
            max_steps=8,
            step=0,
        )
        llm = ScriptedFinishLLM()
        executor = RecordingExecutor()
        ctx = _build_context(tmp_path, llm, executor, trace_id="r-resume")

        progress: list[str] = []
        with patch("react_loop._build_system_prompt", return_value=("system", None)):
            result = react_loop(
                ctx,
                user_goal="do the thing",
                progress_callback=progress.append,
                initial_state=resumed_state,
            )

        assert result == "done"
        assert llm._call_count == 1

    def test_user_goal_helper(self):
        state = _LoopState(messages=[{"role": "user", "content": "hello"}], goal_idx=0, max_steps=8)
        assert _get_user_goal(state) == "hello"
        assert _get_user_goal(_LoopState(messages=[], goal_idx=0, max_steps=8)) == ""


# ---------------------------------------------------------------------------
# tool_results_count regression tests
# ---------------------------------------------------------------------------

class TestToolResultsCount:
    """Verify format_tool_result output stays recognisable as a tool result."""

    def test_tool_results_count_matches_format_tool_result(self):
        """format_tool_result output must trigger the count condition."""
        from react_loop import format_tool_result

        success_msg = format_tool_result("test_tool", {"success": True, "output": "ok"})
        failed_msg = format_tool_result(
            "test_tool",
            {"success": False, "exit_code": 1, "error": "boom"},
        )
        assert "tool '" in success_msg.lower()
        assert "tool '" in failed_msg.lower()

        messages = [
            {"role": "user", "content": "goal"},
            {"role": "user", "content": success_msg},
            {"role": "user", "content": failed_msg},
        ]
        count = sum(
            1
            for m in messages
            if m.get("role") == "tool"
            or m.get("role") == "user"
            and "tool '" in m.get("content", "").lower()
        )
        assert count == 2


# ---------------------------------------------------------------------------
# Preparation-failure checkpoint on a real httpx.MockTransport client
# (relocated from tests/test_request_preparation.py::TestPreparationFailureState
# in simplify-model-runtime task 2.2). Task 6.2 retired the oversized-system
# trigger (no local size rejection); the failure is now a malformed native
# tool-call group rejected by ``_prepare_request`` before any provider I/O, with
# the checkpoint-validity and zero-outbound assertions unchanged.
# ---------------------------------------------------------------------------


def run_failing_preparation(tmp_path):
    """Run one real-client turn whose preparation fails, then time out the retry."""
    from checkpoint_store import CheckpointStore
    from confirmation import ConfirmationManager

    store = CheckpointStore(str(tmp_path))
    owner = store.acquire_owner("r-harness1")
    assert owner is not None
    confirmation = ConfirmationManager()
    requests = []

    finish = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
    summary = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        return httpx.Response(200, json=summary if "Summarize these" in body else finish)

    client = make_run_model_client(
        "openai", httpx.MockTransport(handler),
        metadata=ModelMetadata(context_tokens=2_000, output_tokens=200), upstream_id="prep-model",
    )
    try:
        from unittest.mock import patch

        from react_loop import _LoopState, react_loop
        from tests.execution_harness import build_context

        malformed = _LoopState(
            messages=[
                {"role": "user", "content": "goal"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "a", "type": "function",
                     "function": {"name": "probe", "arguments": "{}"}},
                ]},
            ],
            goal_idx=0, max_steps=3,
        )
        ctx = build_context(
            client, RecordingExecutor(),
            checkpoint_store=store, checkpoint_owner=owner, checkpoint_enabled=True,
            confirmation=confirmation, checkpoint_recovery_eligible=True,
        )
        with patch("react_loop._build_system_prompt", return_value=("sys-prompt", None)):
            result = react_loop(ctx, "goal", progress_callback=lambda _m: None, initial_state=malformed)
        return result, store, owner, requests
    finally:
        client.close()


def test_permanent_preparation_failure_is_terminal_without_checkpoint(tmp_path):
    """A malformed request is non-retryable: no provider I/O and no new checkpoint."""
    result, store, owner, requests = run_failing_preparation(tmp_path)
    try:
        assert result.startswith("❌")
        assert "model/provider configuration" in result
        assert requests == []
        assert store.load("r-harness1", owner=owner) is None
    finally:
        owner.release()


def save_zero_outbound_checkpoint(tmp_path, trace_id="r-harness1"):
    """Persist an interactive retryable pre-send failure; return (store, owner)."""
    ctx = _build_context(
        tmp_path, ScriptedFinishLLM(), RecordingExecutor(), trace_id=trace_id,
        checkpoint_recovery_eligible=True,
    )
    owner = ctx.checkpoint_store.acquire_owner(trace_id)
    ctx.checkpoint_owner = owner
    state = _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8,
        attempt_state=CallAttemptState(max_retries=0),
    )
    _handle_llm_error(
        ctx, state, LLMErrorInfo("timeout", "⏱️ Request timed out", True, "d"),
        lambda _m: None, "goal",
    )
    return ctx.checkpoint_store, owner


def test_retryable_pre_send_failure_saves_valid_zero_outbound_state(tmp_path):
    store, owner = save_zero_outbound_checkpoint(tmp_path, "r-zero-out")
    try:
        saved = store.load("r-zero-out", owner=owner)
        restored = CallAttemptState.from_dict(saved["attempt_state"])
        assert restored.outbound_attempts == 0
        assert restored.manual_retry_authorized is False
    finally:
        owner.release()
