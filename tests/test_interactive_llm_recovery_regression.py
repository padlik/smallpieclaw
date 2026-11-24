"""Durable-only recovery for eligible Telegram LLM failures.

Real ``react_loop`` with a real ``CheckpointStore``. An eligible retryable
failure saves state and returns immediately: no card, token or operator wait, and
no memory-only continuation. Only the model transport is scripted.
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
from confirmation import ConfirmationManager
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
CARD_MARKER = "__LLM_ERROR__"
SAVED = "Saved progress is available. Use /resume to continue."
UNAVAILABLE = "Progress could not be saved for recovery. This request cannot be resumed."


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


def _run(tmp_path, responses, *, checkpoint_enabled=True, store=None, owner=None):
    store = store or CheckpointStore(str(tmp_path / "data"))
    progress: list[str] = []
    executor = RecordingExecutor({"shell": make_outcome(output="file.txt")})
    llm = _FlakyLLM(responses)
    ctx = build_context(
        llm, executor, trace_id=TRACE, checkpoint_store=store,
        checkpoint_enabled=checkpoint_enabled, confirmation=ConfirmationManager(),
        checkpoint_recovery_eligible=True, checkpoint_owner=owner, max_retries=0,
    )
    with patch("react_loop._build_system_prompt", return_value=("sys", None)):
        result = react_loop(ctx, "list files", progress_callback=progress.append)
    return result, llm, executor, progress, store


def _checkpoint_file(store: CheckpointStore) -> Path:
    return Path(store._checkpoint_dir) / f"{TRACE}.json"


def test_failure_saves_and_returns_without_card_wait_or_in_memory_continuation(tmp_path):
    result, llm, executor, progress, store = _run(
        tmp_path, [TOOL, httpx.TimeoutException("t"), FINISH],
    )
    assert result.startswith("❌") and SAVED in result
    assert len(llm.calls) == 2  # the queued FINISH is never sent: no memory-only retry
    assert len(executor.calls) == 1
    assert not [m for m in progress if m.startswith(CARD_MARKER)]
    saved = json.loads(_checkpoint_file(store).read_text(encoding="utf-8"))
    assert saved["error_info"]["retryable"] is True and is_resume_eligible(saved)
    assert any("file.txt" in str(m.get("content")) for m in saved["messages"])


def test_checkpoint_disabled_reports_unavailable_without_memory_retry(tmp_path):
    result, llm, _, progress, store = _run(
        tmp_path, [httpx.TimeoutException("t"), FINISH], checkpoint_enabled=False,
    )
    assert result.startswith("❌") and UNAVAILABLE in result and SAVED not in result
    assert len(llm.calls) == 1
    assert not list(Path(store._checkpoint_dir).glob("*.json"))
    assert not [m for m in progress if m.startswith(CARD_MARKER)]


def test_save_returning_false_reports_unavailable(tmp_path, monkeypatch):
    store = CheckpointStore(str(tmp_path / "data"))
    monkeypatch.setattr(store, "save", lambda *args, **kwargs: False)
    result, llm, _, _, _ = _run(tmp_path, [httpx.TimeoutException("t"), FINISH], store=store)
    assert UNAVAILABLE in result and SAVED not in result
    assert len(llm.calls) == 1 and not _checkpoint_file(store).exists()


def test_save_oserror_is_non_fatal_and_reports_unavailable(tmp_path, monkeypatch):
    store = CheckpointStore(str(tmp_path / "data"))

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", boom)
    result, llm, _, _, _ = _run(tmp_path, [httpx.TimeoutException("t"), FINISH], store=store)
    assert result.startswith("❌") and UNAVAILABLE in result and SAVED not in result
    assert len(llm.calls) == 1


def test_failed_owner_verification_withholds_resume_offer(tmp_path, monkeypatch):
    store = CheckpointStore(str(tmp_path / "data"))
    owner = store.acquire_owner(TRACE)
    assert owner is not None
    monkeypatch.setattr(store, "recovery_available", lambda *args, **kwargs: False)
    try:
        result, _, _, _, _ = _run(
            tmp_path, [httpx.TimeoutException("t"), FINISH], store=store, owner=owner,
        )
    finally:
        owner.release()
    assert UNAVAILABLE in result and SAVED not in result  # file exists, verification failed


def test_owner_backed_success_offers_resume_only_after_exact_verification(tmp_path):
    store = CheckpointStore(str(tmp_path / "data"))
    owner = store.acquire_owner(TRACE)
    assert owner is not None
    try:
        result, _, _, _, _ = _run(
            tmp_path, [httpx.TimeoutException("t"), FINISH], store=store, owner=owner,
        )
        assert SAVED in result
        assert store.recovery_available(TRACE, owner)
    finally:
        owner.release()


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
        checkpoint_owner=owner, checkpoint_recovery_eligible=True,
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
        checkpoint_owner=owner, checkpoint_recovery_eligible=True,
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


def test_message_arriving_during_a_running_agent_task_is_deferred():
    """General deferred-message handling is unchanged (no LLM card involved)."""
    from tests.test_deferred_message import _make_ctx, _make_iface, _make_update

    iface = _make_iface()
    iface._prompt_registry = None
    running = threading.Event()
    release = threading.Event()
    handler_calls: list[str] = []

    def agent_handler(_user_id, text, _progress, **_kwargs):
        handler_calls.append(text)
        running.set()
        assert release.wait(5)
        return "done"

    iface.agent_handler = agent_handler

    async def scenario() -> None:
        first = asyncio.create_task(
            iface._run_agent_task(_make_update(42, "first"), _make_ctx(), "first")
        )
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, running.wait, 5)
        await iface._run_agent_task(_make_update(42, "second"), _make_ctx(), "second")
        deferred = iface._deferred_messages[iface._current_deferred_token[42]]
        assert deferred.task_text == "second"
        assert handler_calls == ["first"]  # the running task was not re-entered
        release.set()
        await first

    asyncio.run(scenario())
