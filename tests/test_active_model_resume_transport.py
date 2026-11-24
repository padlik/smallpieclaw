"""Transport-backed active-model resume across supported adapters (tasks 2.4, 2.5).

A real ``AgentController`` resumes a saved native-history checkpoint (created with a
different, no-longer-configured model) on a real ``LLMClient`` bound to an
``httpx.MockTransport``. The real ``react_loop`` runs; only HTTP is scripted. The
selected target receives the preserved tool results, no completed tool runs again,
local native-to-JSON conversion consumes no outbound attempt, and the saved
automatic budget is never refilled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import react_loop
from call_attempt_state import CallAttemptState
from checkpoint_store import CheckpointStore
from model_registry import ModelMetadata
from tests.execution_harness import RecordingExecutor, build_context
from tests.run_model_fixtures import make_run_model_client

TRACE = "r-resume-am1"
OLD_MODEL = "old-connection/old-model"
ACTIVE_MODEL = "test-connection/active-model"
TOOL_RESULT = "COMPLETED-TOOL-RESULT"
FINISH = '{"action":"finish","result":"ok"}'
PROVIDER_BODY_SECRET = "PROVIDER-BODY-SECRET-123"

HISTORY = [
    {"role": "user", "content": "current task"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-done", "type": "function",
        "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
    }]},
    {"role": "tool", "tool_call_id": "call-done", "content": TOOL_RESULT},
]

SUCCESS = {
    "openai": lambda: {"choices": [{"message": {"content": FINISH}}], "usage": {}},
    "openrouter": lambda: {"choices": [{"message": {"content": FINISH}}], "usage": {}},
    "anthropic": lambda: {"content": [{"type": "text", "text": FINISH}], "usage": {}},
    # Google's native-tool wire is OpenAI-compatible (``messages``/``choices``).
    "google": lambda: {"choices": [{"message": {"content": FINISH}}], "usage": {}},
    "ollama": lambda: {
        "model": "active-model", "created_at": "2026-10-14T00:00:00Z",
        "message": {"role": "assistant", "content": FINISH}, "done": True,
        "prompt_eval_count": 4, "eval_count": 2,
    },
}
NATIVE_ADAPTERS = ["openai", "openrouter", "google", "ollama"]


def _saved_checkpoint(history: list | None = None) -> dict:
    attempt = CallAttemptState(max_retries=4, retry_delay=2.0)
    attempt.begin_outbound_attempt("native")
    attempt.begin_outbound_attempt("native")  # one automatic retry already spent
    return {
        "format_version": 2, "trace_id": TRACE, "user_goal": "current task",
        "messages": json.loads(json.dumps(history if history is not None else HISTORY)),
        "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
        "model_id": OLD_MODEL, "attempt_state": attempt.to_dict(),
        "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
        "created_at": "2026-08-21T14:30:00Z",
    }


class _World:
    """One controller + real client on a scripted transport; the real loop runs."""

    def __init__(self, tmp_path: Path, make_agent_controller: Any, adapter: str,
                 respond: Any, history: list | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.executor = RecordingExecutor()
        self.progress: list[str] = []
        self.restored: dict[str, Any] = {}
        self.attempt: CallAttemptState | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            self.requests.append(request)
            return respond(request)

        self.client = make_run_model_client(
            adapter, httpx.MockTransport(handler),
            metadata=ModelMetadata(context_tokens=8192, output_tokens=1024, tool_calling=True),
            upstream_id="active-model",
        )
        assert self.client.llm_cfg["id"] == ACTIVE_MODEL
        self.store = CheckpointStore(str(tmp_path))
        self.checkpoint = _saved_checkpoint(history)
        owner = self.store.acquire_owner(TRACE)
        assert owner is not None
        try:
            assert self.store.save(TRACE, self.checkpoint, owner=owner)
        finally:
            owner.release()
        self.path = Path(self.store._checkpoint_dir) / f"{TRACE}.json"
        self.saved_bytes = self.path.read_bytes()
        self.controller = make_agent_controller(llm=self.client, checkpoint_store=self.store)

    def _real_loop(self, ctx, goal, _progress=None, _images=None, *, initial_state=None, **_kw):
        self.restored = initial_state.attempt_state.to_dict()
        self.attempt = initial_state.attempt_state
        real = build_context(
            ctx.llm, self.executor, trace_id=ctx.trace_id,
            checkpoint_recovery_eligible=ctx.checkpoint_recovery_eligible,
            max_retries=ctx.max_retries,
        )
        real.checkpoint_store, real.checkpoint_owner = ctx.checkpoint_store, ctx.checkpoint_owner
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            return react_loop.react_loop(real, goal, initial_state=initial_state)

    def resume(self) -> str:
        try:
            with patch("agent_controller.react_loop", side_effect=self._real_loop):
                return self.controller.run(
                    "ignored", self.progress.append, resume_from=TRACE,
                    checkpoint_recovery_eligible=True,
                )
        finally:
            self.client.close()

    def bodies(self) -> list[str]:
        return [request.content.decode() for request in self.requests]


def _ok(adapter: str):
    return lambda _request: httpx.Response(200, json=SUCCESS[adapter]())


@pytest.mark.parametrize("adapter", NATIVE_ADAPTERS)
def test_native_adapter_resumes_saved_native_history_without_replaying_tools(
    tmp_path, make_agent_controller, adapter,
):
    world = _World(tmp_path, make_agent_controller, adapter, _ok(adapter))
    result = world.resume()
    assert result == "ok"
    assert len(world.requests) == 1  # one actual send to the selected target
    assert TOOL_RESULT in world.bodies()[0]
    if adapter != "ollama":  # Ollama's wire carries no tool-call ids
        assert "call-done" in world.bodies()[0]
    assert world.executor.calls == []  # no completed tool executes again
    assert world.restored["outbound_attempts"] == 2 and world.restored["max_retries"] == 4
    assert world.restored["manual_retry_authorized"] is True
    attempt = world.attempt
    assert attempt is not None
    assert (attempt.outbound_attempts, attempt.manual_retries_used) == (3, 1)
    assert attempt.automatic_retries_used == 1 and attempt.remaining_retries == 3  # not refilled
    assert attempt.mode == "native"
    assert not world.path.exists()  # retired by the actual send, then the run succeeded
    handoff = [m for m in world.progress if m.startswith("Resuming with ")]
    assert handoff == [
        f"Resuming with {ACTIVE_MODEL}; saved progress was created with {OLD_MODEL}. "
        "Saved conversation and tool results will be sent to the selected model's provider."
    ]


def test_unsupported_native_adapter_converts_saved_history_locally_to_json(
    tmp_path, make_agent_controller,
):
    world = _World(tmp_path, make_agent_controller, "anthropic", _ok("anthropic"))
    assert world.resume() == "ok"
    assert len(world.requests) == 1
    body = json.loads(world.requests[0].content)
    assert "tools" not in body  # linearized to JSON conversation
    assert TOOL_RESULT in json.dumps(body["messages"])  # completed result preserved as text
    assert world.executor.calls == []
    attempt = world.attempt
    assert attempt is not None and attempt.mode == "json"
    # The local conversion consumed no outbound attempt: only the one real send counts.
    assert (attempt.outbound_attempts, attempt.manual_retries_used) == (3, 1)
    assert attempt.automatic_retries_used == 1 and attempt.remaining_retries == 3


def test_local_preparation_failure_preserves_exact_snapshot_with_zero_sends(
    tmp_path, make_agent_controller,
):
    malformed = [
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"},
        }]},
        {"role": "user", "content": "interleaved before the tool result"},
        {"role": "tool", "tool_call_id": "c1", "content": "late result"},
    ]
    world = _World(tmp_path, make_agent_controller, "openai", _ok("openai"), history=malformed)
    result = world.resume()
    assert result.startswith("❌") and "late result" not in result
    assert world.requests == []
    assert world.path.read_bytes() == world.saved_bytes  # exact original bytes
    owner = world.store.acquire_owner(TRACE)
    assert owner is not None  # lease released through normal cleanup
    owner.release()


def test_provider_rejection_after_send_saves_new_model_and_counters_without_fallback(
    tmp_path, make_agent_controller,
):
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": {"message": PROVIDER_BODY_SECRET, "type": "conflict"}},
        )

    world = _World(tmp_path, make_agent_controller, "openai", reject)
    result = world.resume()
    assert result.startswith("❌") and "Use /resume to continue." in result
    assert PROVIDER_BODY_SECRET not in result  # sanitized failure
    assert len(world.requests) == 1  # no fallback model, no whole-task restart
    assert {r.url.host for r in world.requests} == {"openai.example.test"}
    assert world.executor.calls == []
    saved = json.loads(world.path.read_text(encoding="utf-8"))
    assert saved["model_id"] == ACTIVE_MODEL  # the new post-send checkpoint records the new model
    assert saved["messages"] == world.checkpoint["messages"]
    state = CallAttemptState.from_dict(saved["attempt_state"])
    assert (state.outbound_attempts, state.automatic_retries_used, state.manual_retries_used) == (3, 1, 1)
    assert PROVIDER_BODY_SECRET not in json.dumps(saved)
    owner = world.store.acquire_owner(TRACE)
    assert owner is not None
    owner.release()


def test_context_failure_after_send_never_resurrects_the_retired_snapshot(
    tmp_path, make_agent_controller,
):
    def too_long(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "context_length_exceeded"}})

    world = _World(tmp_path, make_agent_controller, "openai", too_long)
    result = world.resume()
    assert result.startswith("❌") and "Reduce the request or context" in result
    assert "/resume" not in result
    assert len(world.requests) == 1
    assert not world.path.exists()  # retired at the send gate; no new checkpoint
