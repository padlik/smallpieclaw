"""Task 4.4: request-level recovery contracts on the real pinned client.

The real ``LLMClient`` talks to an ``httpx.MockTransport`` (the external provider
boundary) and the real ``react_loop._request_turn`` owns the logical request. The
real ``_classify_llm_error`` is wrapped by a counting spy, never replaced.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

import httpx
import pytest

import react_loop
from llm_client import LLMCancelledError
from react_loop import ReactContext, _LoopState, _request_turn
from tests.execution_harness import _NullMemory, _NullToolIndex
from tests.test_run_model_client import _client

_TOOLS = [{"type": "function", "function": {"name": "finish", "parameters": {"type": "object"}}}]
_FINISH = '{"action":"finish","result":"ok"}'


class _Harness:
    """Collect requests, waits and classifications around one logical request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler: Any, adapter: str) -> None:
        self.requests: list[httpx.Request] = []
        self.waits: list[float] = []
        self.classified: list[str] = []
        self.cancel_event = threading.Event()
        self._handler = handler
        real = react_loop._classify_llm_error

        def spy(exc: Exception) -> Any:
            info = real(exc)
            self.classified.append(info.type)
            return info

        monkeypatch.setattr(react_loop, "_classify_llm_error", spy)
        self.client = _client(adapter, transport=httpx.MockTransport(self._dispatch))
        from confirmation import ConfirmationManager

        confirmation = ConfirmationManager()
        self.ctx = ReactContext(
            llm=self.client, tool_index=_NullToolIndex(), memory=_NullMemory(),
            builtin_executor=None, mcp_manager=None, skill_registry=None,
            max_retries=4,
            retry_wait=lambda event, delay: self.waits.append(delay) or event.is_set(),
            confirmation=confirmation, cancel_event=self.cancel_event, trace_id="r-4-4",
        )
        self.ctx._tool_defs = list(_TOOLS)
        self.state = _LoopState(
            messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=10
        )

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(self, request)

    @property
    def attempt(self) -> Any:
        """Return the logical request's attempt state (must exist after a run)."""
        assert self.state.attempt_state is not None
        return self.state.attempt_state

    def run(self) -> Any:
        try:
            return _request_turn(self.ctx, self.state, "system", lambda _m: None, True)
        finally:
            self.client.close()


def test_unsupported_native_adapter_converts_locally_to_json_on_the_same_pin(monkeypatch):
    """Local structural conversion: one JSON request, same endpoint/model/key, no count or wait."""
    h = _Harness(
        monkeypatch,
        lambda _h, _r: httpx.Response(200, json={"content": [{"type": "text", "text": _FINISH}]}),
        "anthropic",
    )
    turn = h.run()
    assert turn.raw == _FINISH
    assert len(h.requests) == 1
    request = h.requests[0]
    assert request.url.host == "anthropic.example.test"
    assert request.headers["x-api-key"] == "fake-key"
    body = json.loads(request.content)
    assert body["model"] == "upstream/model"
    assert "tools" not in body
    attempt = h.attempt
    assert attempt.mode == "json"
    assert attempt.outbound_attempts == 1
    assert attempt.automatic_retries_used == 0
    assert h.waits == [] and h.classified == []


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": {"message": "tools are not supported", "code": "unsupported_tools"}}),
        (400, {"error": {"message": "this model does not support tools"}}),
        (404, {"error": {"message": "no such model", "code": "model_not_found"}}),
        (422, {"error": {"message": "unprocessable tools"}}),
    ],
    ids=["400-coded", "400-plain", "404", "422"],
)
def test_provider_rejection_of_native_request_is_not_local_conversion(monkeypatch, status, payload):
    """A provider's unsupported-tools reply is a failure of the owning request, never a JSON fallback."""
    h = _Harness(monkeypatch, lambda _h, _r: httpx.Response(status, json=payload), "openai")
    turn = h.run()
    assert len(h.requests) == 1
    assert {r.url.host for r in h.requests} == {"openai.example.test"}
    assert "tools" in json.loads(h.requests[0].content)
    attempt = h.attempt
    assert attempt.mode == "native"
    assert attempt.outbound_attempts == 1
    assert attempt.automatic_retries_used == 0
    assert h.waits == []
    assert len(h.classified) == 1 and h.classified[0] in {"permanent", "unknown"}
    assert turn.early_return is not None and turn.early_return.startswith("❌")


def test_cancel_before_any_io_sends_nothing_and_is_not_classified(monkeypatch):
    h = _Harness(monkeypatch, lambda _h, _r: httpx.Response(200, json={}), "openai")
    h.cancel_event.set()
    turn = h.run()
    assert turn.early_return == "[Cancelled]"
    assert h.requests == [] and h.classified == [] and h.waits == []


def test_cancel_inside_provider_call_is_not_classified_or_retried(monkeypatch):
    """The request entered the provider once; the cancellation reached the loop unclassified."""

    def barrier(h: _Harness, _request: httpx.Request) -> httpx.Response:
        h.cancel_event.set()
        raise LLMCancelledError("LLM request cancelled")

    h = _Harness(monkeypatch, barrier, "openai")
    turn = h.run()
    assert len(h.requests) == 1
    assert turn.early_return == "[Cancelled]"
    assert h.classified == [] and h.waits == []
    assert h.attempt.outbound_attempts == 1
    assert h.attempt.automatic_retries_used == 0


@pytest.mark.parametrize(
    "exc_type", [asyncio.CancelledError, KeyboardInterrupt], ids=["asyncio-cancelled", "keyboard"]
)
def test_base_exception_cancellation_propagates_unclassified(monkeypatch, exc_type):
    def barrier(h: _Harness, _request: httpx.Request) -> httpx.Response:
        h.cancel_event.set()
        raise exc_type()

    h = _Harness(monkeypatch, barrier, "openai")
    with pytest.raises(exc_type):
        h.run()
    assert len(h.requests) == 1
    assert h.classified == [] and h.waits == []


def test_native_text_reply_is_unchanged_chat_response(monkeypatch):
    """Control: native success on a native adapter stays native (no conversion)."""
    h = _Harness(
        monkeypatch,
        lambda _h, _r: httpx.Response(
            200, json={"choices": [{"message": {"content": "prose"}}], "usage": {}}
        ),
        "openai",
    )
    turn = h.run()
    assert turn.raw == "prose" and turn.text_from_native
    assert h.attempt.mode == "native"
    assert len(h.requests) == 1


_TORN_DOWN = [
    lambda: httpx.ReadError("transport closed"),
    lambda: httpx.ConnectError("transport closed"),
    lambda: RuntimeError("client closed"),
]


@pytest.mark.parametrize("make_exc", _TORN_DOWN, ids=["read-error", "connect-error", "runtime"])
def test_cancel_with_torn_down_transport_is_not_a_model_failure(monkeypatch, make_exc):
    """``close_http`` interrupts an in-flight call; the resulting transport error is cancellation."""

    def barrier(h: _Harness, _request: httpx.Request) -> httpx.Response:
        h.cancel_event.set()
        raise make_exc()

    h = _Harness(monkeypatch, barrier, "openai")
    turn = h.run()
    assert len(h.requests) == 1
    assert turn.early_return == "[Cancelled]"
    assert h.classified == [] and h.waits == []
    assert h.attempt.automatic_retries_used == 0


@pytest.mark.parametrize("make_exc", _TORN_DOWN, ids=["read-error", "connect-error", "runtime"])
def test_same_transport_error_without_cancel_is_still_classified(monkeypatch, make_exc):
    """Control: only a set cancel event suppresses classification."""

    def failing(_h: _Harness, _request: httpx.Request) -> httpx.Response:
        raise make_exc()

    h = _Harness(monkeypatch, failing, "openai")
    h.run()
    assert h.classified, "uncancelled transport failure must still be classified"


# ---------------------------------------------------------------------------
# Checkpoint resume on the pinned model: saved attempts, no completed-tool replay
# ---------------------------------------------------------------------------

_HISTORY = [
    {"role": "user", "content": "current task"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-done", "type": "function",
        "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
    }]},
    {"role": "tool", "tool_call_id": "call-done", "content": "COMPLETED-TOOL-RESULT"},
]


def _checkpoint(model_id: str) -> dict:
    from call_attempt_state import CallAttemptState

    attempt = CallAttemptState(max_retries=4, retry_delay=2.0)
    attempt.begin_outbound_attempt("native")
    attempt.begin_outbound_attempt("native")
    return {
        "format_version": 2, "trace_id": "r-resume-44", "user_goal": "current task",
        "messages": json.loads(json.dumps(_HISTORY)), "step": 1, "goal_idx": 0,
        "max_steps": 8, "json_fail_streak": 0, "model_id": model_id,
        "attempt_state": attempt.to_dict(),
        "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
        "created_at": "2026-08-21T14:30:00Z",
    }


def test_resume_on_matching_pin_sends_saved_attempts_once_and_never_replays_tools(
    tmp_path, make_agent_controller
):
    from unittest.mock import patch

    from checkpoint_store import CheckpointStore
    from tests.execution_harness import RecordingExecutor, build_context

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _FINISH}}], "usage": {}}
        )

    client = _client("openai", transport=httpx.MockTransport(handler))
    store = CheckpointStore(str(tmp_path))
    assert store.save("r-resume-44", _checkpoint(client.llm_cfg["id"]))
    executor = RecordingExecutor()
    controller = make_agent_controller(llm=client, checkpoint_store=store)
    seen: dict[str, Any] = {}

    def run_real_loop(ctx, goal, _progress=None, _images=None, *, initial_state=None, **_kwargs):
        seen["restored"] = initial_state.attempt_state.to_dict()
        real = build_context(ctx.llm, executor, trace_id=ctx.trace_id)
        real.checkpoint_store, real.checkpoint_owner = ctx.checkpoint_store, ctx.checkpoint_owner
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            return react_loop.react_loop(real, goal, initial_state=initial_state)

    try:
        with patch("agent_controller.react_loop", side_effect=run_real_loop):
            controller.run("ignored", resume_from="r-resume-44")
    finally:
        client.close()
    assert len(bodies) == 1
    assert executor.calls == []
    wire = json.dumps(bodies[0]["messages"])
    assert "COMPLETED-TOOL-RESULT" in wire and "call-done" in wire
    assert bodies[0]["model"] == "upstream/model"
    restored = seen["restored"]
    assert restored["max_retries"] == 4
    assert restored["outbound_attempts"] == 2
    assert restored["manual_retry_authorized"] is True
    assert restored["mode"] == "native"


def test_resume_on_a_different_model_continues_on_the_admitted_pin(tmp_path, make_agent_controller):
    """The saved model no longer gates resume: the active pin receives the saved history."""
    from unittest.mock import patch

    from checkpoint_store import CheckpointStore

    bodies: list[dict] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        seen["on_disk_at_send"] = os.path.exists(snapshot_path)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _FINISH}}], "usage": {}}
        )

    client = _client("openai", transport=httpx.MockTransport(handler))
    store = CheckpointStore(str(tmp_path))
    assert store.save("r-resume-44", _checkpoint("other-connection/other-model"))
    snapshot_path = os.path.join(store._checkpoint_dir, "r-resume-44.json")
    controller = make_agent_controller(llm=client, checkpoint_store=store)

    def run_real_loop(ctx, goal, _progress=None, _images=None, *, initial_state=None, **_kwargs):
        from tests.execution_harness import RecordingExecutor, build_context

        seen["on_disk_at_admission"] = os.path.exists(snapshot_path)
        seen["attempt"] = initial_state.attempt_state
        real = build_context(ctx.llm, RecordingExecutor(), trace_id=ctx.trace_id)
        real.checkpoint_store, real.checkpoint_owner = ctx.checkpoint_store, ctx.checkpoint_owner
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            return react_loop.react_loop(real, goal, initial_state=initial_state)

    try:
        with patch("agent_controller.react_loop", side_effect=run_real_loop):
            controller.run("ignored", resume_from="r-resume-44")
    finally:
        client.close()
    assert len(bodies) == 1 and bodies[0]["model"] == "upstream/model"
    # Retirement happens at the actual send: still present after admission/restoration.
    assert seen["on_disk_at_admission"] is True and seen["on_disk_at_send"] is False
    attempt = seen["attempt"]
    # Saved counters continue: 2 attempts, 1 automatic retry used, +1 manual continuation.
    assert (attempt.outbound_attempts, attempt.automatic_retries_used) == (3, 1)
    assert attempt.manual_retries_used == 1 and attempt.remaining_retries == 3
    assert store.load("r-resume-44") is None  # retired by the actual send, run succeeded
