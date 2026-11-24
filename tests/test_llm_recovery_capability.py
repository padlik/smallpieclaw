"""Per-run interactive LLM recovery capability and terminal LLM failures.

Covers eliminate-unusable-llm-recovery tasks 1.1-1.3: the capability defaults to
false, flows through ``AgentController.run`` into ``ReactContext`` per run, and
``_handle_llm_error`` decides before any checkpoint save, token or wait.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from confirmation import ConfirmationManager
from react_loop import (
    LLMErrorInfo,
    _LoopState,
    _handle_llm_error,
    react_loop,
)
from tests.execution_harness import RecordingExecutor, build_context
from tests.test_react_loop_error_recovery import (
    ErrorThenSuccessLLM,
    _build_context,
    _checkpoint_path,
)

PROVIDER_SECRET = "provider-secret-text-12345"


def _state() -> _LoopState:
    return _LoopState(
        messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=8,
    )


def _info(kind: str, message: str, retryable: bool) -> LLMErrorInfo:
    return LLMErrorInfo(kind, message, retryable, PROVIDER_SECRET)


# ---------------------------------------------------------------------------
# 1.1 capability default and propagation
# ---------------------------------------------------------------------------

def test_react_context_defaults_to_headless_recovery():
    ctx = build_context(MagicMock(), RecordingExecutor())
    assert ctx.checkpoint_recovery_eligible is False


def _controller_with_ctx_probe(make_agent_controller, **run_kwargs):
    llm = MagicMock()
    llm.llm_cfg = {"model": "m"}
    controller = make_agent_controller(llm=llm)
    observed: list[bool] = []

    def probe(ctx, *_a, **_k):
        observed.append(ctx.checkpoint_recovery_eligible)
        return "ok"

    with patch("agent_controller.react_loop", side_effect=probe):
        controller.run("goal", **run_kwargs)
    return observed


def test_controller_run_defaults_to_headless_recovery(make_agent_controller):
    assert _controller_with_ctx_probe(make_agent_controller) == [False]


def test_controller_run_enables_recovery_only_when_requested(make_agent_controller):
    observed = _controller_with_ctx_probe(
        make_agent_controller, checkpoint_recovery_eligible=True,
    )
    assert observed == [True]


def test_progress_and_notification_callbacks_do_not_enable_recovery(make_agent_controller):
    observed = _controller_with_ctx_probe(
        make_agent_controller,
        progress_callback=lambda _m: None,
        milestone_notify_fn=lambda _n: None,
    )
    assert observed == [False]


def test_concurrent_runs_do_not_share_recovery_capability(make_agent_controller):
    llm = MagicMock()
    llm.llm_cfg = {"model": "m"}
    controller = make_agent_controller(llm=llm)
    barrier = threading.Barrier(2, timeout=5)
    observed: dict[str, bool] = {}

    def probe(ctx, user_goal, *_a, **_k):
        barrier.wait()  # both runs are in flight simultaneously
        observed[user_goal] = ctx.checkpoint_recovery_eligible
        return "ok"

    with patch("agent_controller.react_loop", side_effect=probe):
        threads = [
            threading.Thread(
                target=controller.run, args=("interactive",),
                kwargs={"checkpoint_recovery_eligible": True},
            ),
            threading.Thread(target=controller.run, args=("headless",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

    assert observed == {"interactive": True, "headless": False}


# ---------------------------------------------------------------------------
# 1.3 decision before save/token/wait
# ---------------------------------------------------------------------------

RETRYABLE_KINDS = [
    ("timeout", "⏱️ Request timed out"),
    ("connection", "🔌 Connection failed"),
    ("rate_limit", "🚫 Rate limit reached"),
    ("empty", "📭 Model returned no content"),
    ("unknown", "❌ LLM error: unexpected provider failure"),
]


@pytest.mark.parametrize(("kind", "message"), RETRYABLE_KINDS)
def test_headless_retryable_failure_is_terminal_without_wait_token_or_checkpoint(
    tmp_path, kind, message,
):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-headless", confirmation=confirmation,
    )
    result = _handle_llm_error(
        ctx, _state(), _info(kind, message, True), lambda _m: None, "goal",
    )
    assert isinstance(result, str) and result.startswith("❌")
    assert not os.path.exists(_checkpoint_path(ctx, "r-headless"))


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize(
    ("kind", "message", "guidance"),
    [
        ("context", "📏 Context too long", "Reduce the request or context"),
        ("permanent", "❌ Provider rejected the request", "model/provider configuration"),
    ],
)
def test_nonretryable_failure_is_terminal_with_actionable_guidance(
    tmp_path, interactive, kind, message, guidance,
):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-nonretry", confirmation=confirmation,
    )
    ctx.checkpoint_recovery_eligible = interactive
    progress: list[str] = []
    result = _handle_llm_error(
        ctx, _state(), _info(kind, message, False), progress.append, "goal",
    )
    assert result is not None and result.startswith("❌")
    assert guidance in result
    assert progress == []
    assert not os.path.exists(_checkpoint_path(ctx, "r-nonretry"))


def test_terminal_guidance_never_interpolates_provider_text(tmp_path):
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        confirmation=ConfirmationManager(),
    )
    for kind, message, retryable in [
        ("context", "📏 Context too long", False),
        ("permanent", "❌ Provider rejected the request", False),
        ("timeout", "⏱️ Request timed out", True),
    ]:
        result = _handle_llm_error(
            ctx, _state(), _info(kind, message, retryable), lambda _m: None, "goal",
        )
        assert PROVIDER_SECRET not in result


def test_cancellation_wins_over_headless_failure_and_signal_is_kept(tmp_path):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-cancelled", confirmation=confirmation,
    )
    ctx.cancel_event.set()
    result = _handle_llm_error(
        ctx, _state(), _info("timeout", "⏱️ Request timed out", True),
        lambda _m: None, "goal",
    )
    assert result == "[Cancelled]"
    assert ctx.cancel_event.is_set()
    assert not os.path.exists(_checkpoint_path(ctx, "r-cancelled"))


def test_cancellation_wins_over_interactive_failure_before_checkpoint(tmp_path):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-cancelled2", confirmation=confirmation,
    )
    ctx.checkpoint_recovery_eligible = True
    ctx.cancel_event.set()
    result = _handle_llm_error(
        ctx, _state(), _info("timeout", "⏱️ Request timed out", True),
        lambda _m: None, "goal",
    )
    assert result == "[Cancelled]"
    assert not os.path.exists(_checkpoint_path(ctx, "r-cancelled2"))


def test_exhaustion_is_claimed_only_when_automatic_budget_was_exhausted(tmp_path):
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        confirmation=ConfirmationManager(),
    )
    info = _info("timeout", "⏱️ Request timed out", True)
    exhausted = _handle_llm_error(
        ctx, _state(), info, lambda _m: None, "goal", automatic_exhausted=True,
    )
    not_exhausted = _handle_llm_error(
        ctx, _state(), info, lambda _m: None, "goal",
    )
    assert "exhausted" in exhausted
    assert "exhausted" not in not_exhausted


def test_headless_unknown_failure_does_not_claim_exhaustion(tmp_path):
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        confirmation=ConfirmationManager(),
    )
    result = _handle_llm_error(
        ctx, _state(), _info("unknown", "❌ LLM error: unexpected provider failure", True),
        lambda _m: None, "goal",
    )
    assert "exhausted" not in result


def test_eligible_retryable_failure_saves_and_returns_without_card_token_or_wait(tmp_path):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-interactive", confirmation=confirmation,
    )
    ctx.checkpoint_recovery_eligible = True
    progress: list[str] = []
    result = _handle_llm_error(
        ctx, _state(), _info("timeout", "⏱️ Request timed out", True),
        progress.append, "goal",
    )
    assert result is not None and result.startswith("❌ ⏱️ Request timed out")
    assert "Saved progress is available. Use /resume to continue." in result
    assert PROVIDER_SECRET not in result
    assert not [m for m in progress if m.startswith("__LLM_ERROR__")]  # no card
    assert os.path.exists(_checkpoint_path(ctx, "r-interactive"))


def test_eligible_failure_with_writing_disabled_reports_unavailable_and_never_waits(tmp_path):
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-disabled-eligible", confirmation=confirmation, checkpoint_enabled=False,
    )
    ctx.checkpoint_recovery_eligible = True
    result = _handle_llm_error(
        ctx, _state(), _info("timeout", "⏱️ Request timed out", True),
        lambda _m: None, "goal",
    )
    assert result is not None and result.startswith("❌")
    assert "Progress could not be saved for recovery. This request cannot be resumed." in result
    assert "/resume" not in result.replace("cannot be resumed", "")
    assert not os.path.exists(_checkpoint_path(ctx, "r-disabled-eligible"))


def test_eligible_failure_never_continues_in_memory(tmp_path):
    """The old 'retry' answer path is gone: the handler always returns a terminal string."""
    ctx = _build_context(
        tmp_path, ErrorThenSuccessLLM(RuntimeError("x"), "{}"), RecordingExecutor(),
        trace_id="r-noretry", confirmation=ConfirmationManager(),
    )
    ctx.checkpoint_recovery_eligible = True
    result = _handle_llm_error(
        ctx, _state(), _info("unknown", "❌ LLM error: unexpected provider failure", True),
        lambda _m: None, "goal",
    )
    assert isinstance(result, str)


def test_headless_loop_failure_sends_exactly_once_and_never_waits(tmp_path):
    """A real react_loop run: one outbound send, terminal ❌, no recovery data."""
    llm = ErrorThenSuccessLLM(httpx.TimeoutException("t"), '{"action":"finish","result":"x"}')
    confirmation = ConfirmationManager()
    ctx = _build_context(
        tmp_path, llm, RecordingExecutor(), trace_id="r-loopheadless",
        confirmation=confirmation,
    )
    with patch("react_loop._build_system_prompt", return_value=("system", None)):
        result = react_loop(ctx, "goal", progress_callback=lambda _m: None)
    assert result.startswith("❌")
    assert llm._call_count == 1
    assert not os.path.exists(_checkpoint_path(ctx, "r-loopheadless"))


# ---------------------------------------------------------------------------
# Exhaustion wording follows the actual automatic budget (review finding 2)
# ---------------------------------------------------------------------------

def _run_headless_loop(llm, *, max_retries: int):
    from tests.execution_harness import RecordingExecutor, build_context

    ctx = build_context(llm, RecordingExecutor(), max_retries=max_retries)
    with patch("react_loop._build_system_prompt", return_value=("system", None)):
        return react_loop(ctx, "goal", progress_callback=lambda _m: None)


def test_empty_response_cap_with_budget_left_does_not_claim_exhaustion():
    from llm_client import LLMEmptyResponseError
    from tests.test_interactive_llm_recovery_regression import _FlakyLLM

    llm = _FlakyLLM([LLMEmptyResponseError("e")] * 3)
    result = _run_headless_loop(llm, max_retries=4)
    assert len(llm.calls) == 3  # the existing two-empty-retry cap, budget not spent
    assert result.startswith("❌") and "exhausted" not in result


def test_effect_veto_with_budget_left_does_not_claim_exhaustion():
    from tests.test_interactive_llm_recovery_regression import _FlakyLLM

    class _EffectLLM(_FlakyLLM):
        def chat(self, messages, system=None, progress_cb=None, json_mode=False):
            try:
                return super().chat(messages, system, progress_cb, json_mode)
            except httpx.TimeoutException:
                self._attempt_binding[0].mark_effect()  # the failed send had an effect
                raise

    llm = _EffectLLM([httpx.TimeoutException("t")] * 3)
    result = _run_headless_loop(llm, max_retries=4)
    assert len(llm.calls) == 1
    assert result.startswith("❌") and "exhausted" not in result


def test_spent_automatic_budget_claims_exhaustion():
    from tests.test_interactive_llm_recovery_regression import _FlakyLLM

    llm = _FlakyLLM([httpx.TimeoutException("t")] * 5)
    result = _run_headless_loop(llm, max_retries=2)
    assert len(llm.calls) == 3
    assert "exhausted" in result
