"""Tests for LLM error classification and react_loop error recovery."""

from __future__ import annotations

import os
from typing import Callable, Optional
from unittest.mock import patch

import httpx

from checkpoint_store import CheckpointStore
from confirmation import ConfirmationManager
from llm_client import LLMCancelledError, LLMEmptyResponseError, LLMPermanentError
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


class ScriptedFinishLLM:
    """Fake LLM that never raises and returns a finish response."""

    def __init__(self, response: str = '{"action": "finish", "result": "done"}'):
        self._response = response
        self._call_count = 0
        self.calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": "test-model"}

    def set_trace_id(self, trace_id: str) -> None:
        pass

    def chat_with_fallback(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        self.calls.append(list(messages))
        self._call_count += 1
        return self._response

    def chat(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        return "compaction summary"


class ErrorThenSuccessLLM:
    """Fake LLM that raises on the first call and succeeds afterwards."""

    def __init__(self, error: Exception, success_response: str):
        self._error = error
        self._success = success_response
        self._call_count = 0
        self.calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": "test-model"}

    def set_trace_id(self, trace_id: str) -> None:
        pass

    def chat_with_fallback(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        _ = system, progress_cb, json_mode
        self.calls.append(list(messages))
        self._call_count += 1
        if self._call_count == 1:
            raise self._error
        return self._success

    def chat(
        self,
        messages: list,
        system: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        json_mode: bool = False,
    ) -> str:
        _ = system, progress_cb, json_mode
        return "compaction summary"


def _build_context(
    tmp_path,
    llm,
    executor: RecordingExecutor,
    trace_id: str = "r-test1",
    checkpoint_enabled: bool = True,
    confirmation: Optional[ConfirmationManager] = None,
) -> ReactContext:
    """Build a ReactContext wired for error-recovery tests."""
    data_dir = str(tmp_path / "data")
    return ReactContext(
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
    """Verify checkpoint write + retry/cancel/timeout branches."""

    def test_retry_returns_none(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        ctx = _build_context(
            tmp_path,
            ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}'),
            RecordingExecutor(),
            trace_id="r-retry",
        )

        def auto_retry(*_args, **_kwargs):
            return "retry"

        ctx.confirmation.request_retry = auto_retry
        assert _handle_llm_error(ctx, state, error_info, lambda _: None, "goal") is None
        assert os.path.exists(_checkpoint_path(ctx, "r-retry"))

    def test_cancel_deletes_checkpoint(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        llm = ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}')
        ctx = _build_context(tmp_path, llm, RecordingExecutor(), trace_id="r-cancel")
        # Write a pre-existing checkpoint first.
        if ctx.checkpoint_store is not None:
            ctx.checkpoint_store.save("r-cancel", {"created_at": "2024-01-01T00:00:00Z"})

        def auto_cancel(*_args, **_kwargs):
            return "cancel"

        ctx.confirmation.request_retry = auto_cancel
        result = _handle_llm_error(ctx, state, error_info, lambda _: None, "goal")
        assert result == "❌ ⏱️ Request timed out"
        assert not os.path.exists(_checkpoint_path(ctx, "r-cancel"))

    def test_timeout_preserves_checkpoint(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        llm = ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}')
        ctx = _build_context(tmp_path, llm, RecordingExecutor(), trace_id="r-timeout")

        def auto_timeout(*_args, **_kwargs):
            return "timeout"

        ctx.confirmation.request_retry = auto_timeout
        result = _handle_llm_error(ctx, state, error_info, lambda _: None, "goal")
        assert result == "❌ ⏱️ Request timed out"
        assert os.path.exists(_checkpoint_path(ctx, "r-timeout"))

    def test_checkpoint_disabled_does_not_write(self, tmp_path):
        error_info = LLMErrorInfo("timeout", "⏱️ Request timed out", True, "detail")
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8)
        llm = ErrorThenSuccessLLM(RuntimeError("nope"), '{"action": "finish", "result": "done"}')
        ctx = _build_context(
            tmp_path,
            llm,
            RecordingExecutor(),
            trace_id="r-disabled",
            checkpoint_enabled=False,
        )

        def auto_retry(*_args, **_kwargs):
            return "retry"

        ctx.confirmation.request_retry = auto_retry
        assert _handle_llm_error(ctx, state, error_info, lambda _: None, "goal") is None
        assert not os.path.exists(_checkpoint_path(ctx, "r-disabled"))


# ---------------------------------------------------------------------------
# Integration tests: react_loop error recovery
# ---------------------------------------------------------------------------

class TestReactLoopErrorRecovery:
    """End-to-end react_loop recovery via inline retry."""

    def test_retry_then_success(self, tmp_path):
        error = httpx.TimeoutException("timed out")
        success = '{"action": "finish", "result": "recovered"}'
        llm = ErrorThenSuccessLLM(error, success)
        executor = RecordingExecutor()
        confirmation = ConfirmationManager()

        def request_retry(token, error_info_json, progress_cb, timeout_seconds=120):
            # Capture the marker first, then signal retry.
            progress_cb(f"__LLM_ERROR__:{token}:{error_info_json}")
            confirmation.signal_retry(token, "retry")
            return "retry"

        confirmation.request_retry = request_retry  # type: ignore[method-assign]

        result, ctx, progress = _run_with_system(
            llm, executor, tmp_path,
            trace_id="r-recover",
            confirmation=confirmation,
        )

        assert result == "recovered"
        assert llm._call_count == 2
        assert os.path.exists(_checkpoint_path(ctx, "r-recover")) is False
        # Verify the error card marker was emitted.
        assert any("__LLM_ERROR__" in p for p in progress)
        # The user-facing message is in the error_info JSON inside the marker;
        # the marker itself contains the message text.
        assert any("Request timed out" in p for p in progress)

    def test_cancel_returns_error(self, tmp_path):
        error = httpx.TimeoutException("timed out")
        success = '{"action": "finish", "result": "recovered"}'
        llm = ErrorThenSuccessLLM(error, success)
        executor = RecordingExecutor()
        confirmation = ConfirmationManager()

        def request_retry(token, error_info_json, progress_cb, timeout_seconds=120):
            progress_cb(f"__LLM_ERROR__:{token}:{error_info_json}")
            confirmation.signal_retry(token, "cancel")
            return "cancel"

        confirmation.request_retry = request_retry  # type: ignore[method-assign]

        result, ctx, _ = _run_with_system(
            llm, executor, tmp_path,
            trace_id="r-cancel",
            confirmation=confirmation,
        )

        assert result.startswith("❌")
        assert llm._call_count == 1
        assert not os.path.exists(_checkpoint_path(ctx, "r-cancel"))

    def test_retry_timeout_preserves_checkpoint(self, tmp_path):
        error = httpx.TimeoutException("timed out")
        success = '{"action": "finish", "result": "recovered"}'
        llm = ErrorThenSuccessLLM(error, success)
        executor = RecordingExecutor()
        confirmation = ConfirmationManager()

        def request_retry(token, error_info_json, progress_cb, timeout_seconds=120):
            progress_cb(f"__LLM_ERROR__:{token}:{error_info_json}")
            # Do not signal anything — return timeout immediately.
            return "timeout"

        confirmation.request_retry = request_retry  # type: ignore[method-assign]

        result, ctx, _ = _run_with_system(
            llm, executor, tmp_path,
            trace_id="r-preserved",
            confirmation=confirmation,
        )

        assert result.startswith("❌")
        assert llm._call_count == 1
        assert os.path.exists(_checkpoint_path(ctx, "r-preserved"))

    def test_checkpoint_enabled_false_still_retries(self, tmp_path):
        error = httpx.TimeoutException("timed out")
        success = '{"action": "finish", "result": "recovered"}'
        llm = ErrorThenSuccessLLM(error, success)
        executor = RecordingExecutor()
        confirmation = ConfirmationManager()

        def request_retry(token, error_info_json, progress_cb, timeout_seconds=120):
            progress_cb(f"__LLM_ERROR__:{token}:{error_info_json}")
            confirmation.signal_retry(token, "retry")
            return "retry"

        confirmation.request_retry = request_retry  # type: ignore[method-assign]

        result, ctx, _ = _run_with_system(
            llm, executor, tmp_path,
            trace_id="r-nocheckpt",
            confirmation=confirmation,
            checkpoint_enabled=False,
        )

        assert result == "recovered"
        assert llm._call_count == 2
        assert not os.path.exists(_checkpoint_path(ctx, "r-nocheckpt"))


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
