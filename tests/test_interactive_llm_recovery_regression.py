"""Task 2.3: interactive LLM recovery stays intact for eligible failures.

Real ``react_loop`` with a real ``ConfirmationManager`` (real event wait) and a
real ``CheckpointStore``. The "operator" is a callback answering the error card
exactly as Telegram would; only the model transport is scripted.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from checkpoint_store import CheckpointStore, is_resume_eligible
from confirmation import RETRY_PREFIX, ConfirmationManager
from react_loop import react_loop
from tests.execution_harness import (
    LLMCall,
    RecordingExecutor,
    ScriptedLLM,
    build_context,
    make_outcome,
)

TOOL = '{"action": "tool", "tool": "shell", "args": {"command": "ls"}}'
FINISH = '{"action": "finish", "result": "recovered"}'
TRACE = "r-interactive1"


class _FlakyLLM(ScriptedLLM):
    """Scripted model whose ``Exception`` items are raised after the send is counted."""

    def chat(self, messages, system=None, progress_cb=None, json_mode=False):
        if not json_mode:
            return "summary"
        self.calls.append(LLMCall(messages=[dict(m) for m in messages], system=system,
                                  images_present=False))
        item = self._next()
        self._count_attempt("json")
        if isinstance(item, Exception):
            raise item
        return item


class _Operator:
    """Answers each error card with the next scripted decision, like Telegram."""

    def __init__(self, confirmation: ConfirmationManager, decisions: list[str]) -> None:
        self.confirmation = confirmation
        self.decisions = list(decisions)
        self.cards: list[dict] = []
        self.progress: list[str] = []

    def __call__(self, message: str) -> None:
        self.progress.append(message)
        if not message.startswith(f"{RETRY_PREFIX}:"):
            return
        _, token, payload = message.split(":", 2)
        self.cards.append(json.loads(payload))
        if self.decisions:
            self.confirmation.signal_retry(token, self.decisions.pop(0))


def _run(tmp_path, responses, decisions, *, checkpoint_enabled=True, timeout=30, store=None):
    store = store or CheckpointStore(str(tmp_path / "data"))
    confirmation = ConfirmationManager()
    operator = _Operator(confirmation, decisions)
    executor = RecordingExecutor({"shell": make_outcome(output="file.txt")})
    llm = _FlakyLLM(responses)
    ctx = build_context(
        llm, executor, trace_id=TRACE, checkpoint_store=store,
        checkpoint_enabled=checkpoint_enabled, confirmation=confirmation,
        interactive_llm_recovery=True, retry_timeout_seconds=timeout, max_retries=0,
    )
    with patch("react_loop._build_system_prompt", return_value=("sys", None)):
        result = react_loop(ctx, "list files", progress_callback=operator)
    return result, llm, executor, operator, store


def _checkpoint_file(store: CheckpointStore) -> Path:
    return Path(store._checkpoint_dir) / f"{TRACE}.json"


def test_retry_continues_with_full_context_without_replaying_tools(tmp_path):
    result, llm, executor, operator, store = _run(
        tmp_path, [TOOL, httpx.TimeoutException("t"), FINISH], ["retry"],
    )
    assert result == "recovered"
    assert len(executor.calls) == 1  # the completed tool was not replayed
    failed_call, retried_call = llm.calls[1], llm.calls[2]
    assert retried_call.messages == failed_call.messages  # same full conversation
    assert any("file.txt" in str(m.get("content")) for m in retried_call.messages)
    assert len(operator.cards) == 1 and operator.cards[0]["tool_results_count"] == 1
    assert not _checkpoint_file(store).exists()  # retired after the successful continuation


def test_manual_retry_does_not_refill_the_automatic_budget(tmp_path):
    result, llm, _, operator, _ = _run(
        tmp_path,
        [httpx.TimeoutException("a"), httpx.TimeoutException("b"), FINISH],
        ["retry", "retry"],
    )
    assert result == "recovered"
    assert len(llm.calls) == 3  # one send per operator decision, no hidden resends
    assert len(operator.cards) == 2


def test_cancel_returns_error_and_retires_checkpoint(tmp_path):
    result, llm, _, operator, store = _run(
        tmp_path, [httpx.TimeoutException("t"), FINISH], ["cancel"],
    )
    assert result.startswith("❌")
    assert len(llm.calls) == 1 and len(operator.cards) == 1
    assert not _checkpoint_file(store).exists()


def test_unanswered_card_times_out_and_leaves_a_resumable_checkpoint(tmp_path):
    result, llm, _, operator, store = _run(
        tmp_path, [httpx.TimeoutException("t"), FINISH], [], timeout=0.2,
    )
    assert result.startswith("❌")
    assert len(llm.calls) == 1 and len(operator.cards) == 1
    saved = json.loads(_checkpoint_file(store).read_text(encoding="utf-8"))
    assert saved["error_info"]["retryable"] is True and is_resume_eligible(saved)


def test_checkpoint_disabled_retries_in_memory_and_warns(tmp_path):
    result, llm, _, operator, store = _run(
        tmp_path, [httpx.TimeoutException("t"), FINISH], ["retry"], checkpoint_enabled=False,
    )
    assert result == "recovered" and len(llm.calls) == 2
    assert not list(Path(store._checkpoint_dir).glob("*.json"))
    assert any("Disk recovery is unavailable" in m for m in operator.progress)


def test_failed_checkpoint_save_retries_in_memory_and_warns(tmp_path, monkeypatch):
    store = CheckpointStore(str(tmp_path / "data"))
    monkeypatch.setattr(store, "save", lambda *args, **kwargs: False)
    result, llm, _, operator, _ = _run(
        tmp_path, [httpx.TimeoutException("t"), FINISH], ["retry"], store=store,
    )
    assert result == "recovered" and len(llm.calls) == 2
    assert not _checkpoint_file(store).exists()
    assert any("Disk recovery is unavailable" in m for m in operator.progress)


def _save_eligible_snapshot(store: CheckpointStore):
    owner = store.acquire_owner(TRACE)
    assert owner is not None
    checkpoint = {
        "format_version": 2, "trace_id": TRACE, "user_goal": "list files",
        "messages": [{"role": "user", "content": "list files"}], "step": 1, "goal_idx": 0,
        "max_steps": 8, "json_fail_streak": 0, "model_id": "m",
        "attempt_state": {
            "max_retries": 0, "retry_delay": 2.0, "mode": "json", "outbound_attempts": 1,
            "automatic_retries_used": 0, "manual_retries_used": 0,
            "manual_retry_authorized": False, "partial_output": False, "effect": False,
        },
        "error_info": {"type": "timeout", "message": "m", "retryable": True, "detail": ""},
        "created_at": "2026-08-21T14:30:00Z",
    }
    assert store.save(TRACE, checkpoint, owner=owner)
    return owner


def test_blocked_retirement_prevents_the_send_and_keeps_the_changed_file(tmp_path):
    store = CheckpointStore(str(tmp_path / "data"))
    owner = _save_eligible_snapshot(store)
    path = _checkpoint_file(store)
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["created_at"] = "2026-09-01T00:00:00Z"  # another actor replaced the snapshot
    path.write_text(json.dumps(changed), encoding="utf-8")
    changed_bytes = path.read_bytes()
    llm = ScriptedLLM([FINISH])
    ctx = build_context(
        llm, RecordingExecutor(), trace_id=TRACE, checkpoint_store=store,
        checkpoint_owner=owner, interactive_llm_recovery=True,
    )
    try:
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            result = react_loop(ctx, "list files", progress_callback=lambda _m: None)
    finally:
        owner.release()
    assert "no model call was sent" in result
    assert llm.calls == []  # no send was authorized
    assert path.read_bytes() == changed_bytes


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_permission_failure_on_retirement_blocks_send_and_preserves_snapshot(tmp_path):
    store = CheckpointStore(str(tmp_path / "data"))
    owner = _save_eligible_snapshot(store)
    path = _checkpoint_file(store)
    before = path.read_bytes()
    directory = path.parent
    llm = ScriptedLLM([FINISH])
    ctx = build_context(
        llm, RecordingExecutor(), trace_id=TRACE, checkpoint_store=store,
        checkpoint_owner=owner, interactive_llm_recovery=True,
    )
    directory.chmod(0o500)  # the filesystem refuses removal of the snapshot
    try:
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            result = react_loop(ctx, "list files", progress_callback=lambda _m: None)
    finally:
        directory.chmod(0o700)
        owner.release()
    assert "no model call was sent" in result
    assert llm.calls == []
    assert path.read_bytes() == before


def test_message_arriving_during_an_active_retry_prompt_is_deferred():
    from tests.test_deferred_message import _make_ctx, _make_iface, _make_update

    iface = _make_iface()
    iface._prompt_registry = None
    confirmation = ConfirmationManager()
    tokens: list[str] = []
    prompt_open = threading.Event()
    handler_calls: list[str] = []

    def agent_handler(_user_id, text, _progress, **_kwargs):
        handler_calls.append(text)

        def card(message: str) -> None:
            tokens.append(message.split(":", 2)[1])
            prompt_open.set()

        return confirmation.request_retry("tok", "{}", card, timeout_seconds=10)

    iface.agent_handler = agent_handler

    async def scenario() -> None:
        first = asyncio.create_task(
            iface._run_agent_task(_make_update(42, "first"), _make_ctx(), "first")
        )
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, prompt_open.wait, 5)
        await iface._run_agent_task(_make_update(42, "second"), _make_ctx(), "second")
        deferred = iface._deferred_messages[iface._current_deferred_token[42]]
        assert deferred.task_text == "second"
        assert handler_calls == ["first"]  # the blocked run was not re-entered
        confirmation.signal_retry(tokens[0], "cancel")
        await first

    asyncio.run(scenario())
