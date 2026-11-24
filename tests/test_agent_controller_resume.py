"""Tests for AgentController resume-from-checkpoint wiring."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from checkpoint_store import CheckpointStore
from call_attempt_state import CallAttemptState


def _resume_checkpoint() -> dict:
    """Return a complete supported checkpoint for strict-resume tests."""
    return {
        "format_version": 2,
        "trace_id": "r-strict-test",
        "user_goal": "goal",
        "messages": [{"role": "user", "content": "goal"}],
        "step": 1,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model_id": "canonical-test-id",
        "attempt_state": CallAttemptState().to_dict(),
        "error_info": {
            "type": "timeout", "message": "timeout", "retryable": True, "detail": ""
        },
        "created_at": "2026-08-21T14:30:00Z",
    }


def test_run_with_resume_from_loads_checkpoint(tmp_path, make_agent_controller):
    """Verify that run(resume_from=...) loads checkpoint and passes initial_state."""
    store = CheckpointStore(str(tmp_path))

    checkpoint = {
        "format_version": 2,
        "trace_id": "r-test1234",
        "user_goal": "Analyze the logs",
        "messages": [{"role": "user", "content": "Analyze the logs"}],
        "step": 3,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model_id": "canonical-test-id",
        "attempt_state": CallAttemptState().to_dict(),
        "created_at": "2026-08-21T14:30:00Z",
        "error_info": {
            "type": "timeout",
            "message": "⏱️ Request timed out",
            "retryable": True,
            "detail": "...",
        },
    }
    store.save("r-test1234", checkpoint)

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"id": "canonical-test-id", "model": "upstream-test-model"}
    llm._trace_id = "r-test1234"
    llm.set_trace_id = MagicMock()

    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        result = controller.run("Analyze the logs", resume_from="r-test1234")

        assert result == "success"
        call_kwargs = mock_loop.call_args
        initial_state = call_kwargs.kwargs.get("initial_state")
        assert initial_state is not None
        assert initial_state.step == 3
        assert initial_state.max_steps == 8
        assert initial_state.messages == checkpoint["messages"]

        # The ReactContext must be built with the checkpoint's trace_id, not a
        # fresh trace. This ensures successful completion deletes the original
        # checkpoint and resumed error checkpoints correlate to the same trace.
        ctx = call_kwargs.args[0]
        assert ctx.trace_id == "r-test1234"
        llm.set_trace_id.assert_called_with("r-test1234")


def test_run_without_resume_from_has_no_initial_state(tmp_path, make_agent_controller):
    """Verify that run() without resume_from passes no initial_state."""
    store = CheckpointStore(str(tmp_path))

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"model": "test-model"}
    llm.set_trace_id = MagicMock()

    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        controller.run("test goal")

        call_kwargs = mock_loop.call_args
        initial_state = call_kwargs.kwargs.get("initial_state")
        assert initial_state is None


def test_run_with_resume_from_nonexistent_checkpoint(tmp_path, make_agent_controller):
    """A missing resume checkpoint is refused rather than starting a fresh run."""
    store = CheckpointStore(str(tmp_path))

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"model": "test-model"}
    llm.set_trace_id = MagicMock()

    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        result = controller.run("test goal", resume_from="r-nonexistent")
        assert "Cannot resume checkpoint" in result
        mock_loop.assert_not_called()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cp: cp.update(format_version=2.0),
        lambda cp: cp.update(format_version=True),
        lambda cp: cp.update(messages=[42]),
        lambda cp: cp.update(messages=[{"role": "unknown", "content": "goal"}]),
        lambda cp: cp.update(messages=[{"role": "user", "content": 42}]),
        lambda cp: cp.update(messages=[{"role": "assistant", "content": None}]),
        lambda cp: cp.update(messages=[{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "x", "arguments": "not-json"}}],
        }]),
        lambda cp: cp.update(messages=[{"role": "tool", "tool_call_id": "missing", "content": "result"}]),
        lambda cp: cp.update(goal_idx=-1),
        lambda cp: cp.update(goal_idx=1),
        lambda cp: cp.update(goal_idx=True),
        lambda cp: cp.update(step=True),
    ],
)
def test_strict_resume_rejects_malformed_history_before_loop(
    tmp_path, make_agent_controller, mutate
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    mutate(checkpoint)
    assert store.save("r-strict-test", checkpoint)
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    llm.set_trace_id = MagicMock()
    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop") as mock_loop:
        result = controller.run("goal", resume_from="r-strict-test")
    assert "Cannot resume checkpoint" in result
    mock_loop.assert_not_called()


def test_strict_resume_preserves_legitimate_native_multimodal_history(
    tmp_path, make_agent_controller
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["messages"] = [
        {"role": "user", "content": "goal", "images": ["/fixture/image.png"]},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "found"},
    ]
    assert store.save("r-strict-test", checkpoint)
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    llm.set_trace_id = MagicMock()
    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop", return_value="ok") as mock_loop:
        assert controller.run("goal", resume_from="r-strict-test") == "ok"
    restored = mock_loop.call_args.kwargs["initial_state"]
    assert restored.messages == checkpoint["messages"]


@pytest.mark.parametrize(
    ("outbound", "authorized", "max_retries", "expected_authorized", "manual_used"),
    [
        (1, False, 0, True, 0),
        (1, True, 0, True, 0),
        (0, False, 4, False, 0),
    ],
)
def test_resume_authorizes_exactly_one_manual_continuation_without_refill(
    tmp_path, make_agent_controller, outbound, authorized, max_retries,
    expected_authorized, manual_used,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["attempt_state"] = {
        "max_retries": max_retries,
        "retry_delay": 2.0,
        "mode": "json",
        "outbound_attempts": outbound,
        "automatic_retries_used": max(0, outbound - 1),
        "manual_retries_used": 0,
        "manual_retry_authorized": authorized,
        "partial_output": False,
        "effect": False,
    }
    assert store.save("r-strict-test", checkpoint)
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    llm.set_trace_id = MagicMock()
    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop", return_value="ok") as mock_loop:
        assert controller.run("goal", resume_from="r-strict-test") == "ok"
    state = mock_loop.call_args.kwargs["initial_state"].attempt_state
    assert state.manual_retry_authorized is expected_authorized
    assert state.manual_retries_used == manual_used
    assert state.remaining_retries == max_retries - max(0, outbound - 1)
    if expected_authorized:
        state.begin_outbound_attempt("json", manual=True)
        assert state.manual_retries_used == manual_used + 1
        assert state.manual_retry_authorized is False
        assert state.remaining_retries == max_retries - max(0, outbound - 1)


def test_concurrent_resumes_admit_only_one_request_send(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    assert store.save("r-strict-test", checkpoint)
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    llm.set_trace_id = MagicMock()
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    entered = threading.Event()
    release = threading.Event()
    sent: list[str] = []

    def fake_loop(ctx, *_args, **_kwargs):
        result = store.retire_pending("r-strict-test", ctx.checkpoint_owner)
        assert result.status.value == "retired"
        sent.append(ctx.trace_id)
        entered.set()
        assert release.wait(2)
        return "ok"

    first_result: list[str] = []
    with patch("agent_controller.react_loop", side_effect=fake_loop):
        thread = threading.Thread(
            target=lambda: first_result.append(
                controller.run("goal", resume_from="r-strict-test")
            )
        )
        thread.start()
        assert entered.wait(2)
        second = controller.run("goal", resume_from="r-strict-test")
        release.set()
        thread.join(2)

    assert not thread.is_alive()
    assert first_result == ["ok"]
    assert "already active" in second
    assert sent == ["r-strict-test"]


def test_invalid_utf8_resume_is_refused_and_releases_trace_owner(
    tmp_path, make_agent_controller
):
    store = CheckpointStore(str(tmp_path))
    checkpoint_path = tmp_path / "run_checkpoints" / "r-strict-test.json"
    checkpoint_path.write_bytes(b"\xff\xfe malformed checkpoint")
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    result = controller.run("goal", resume_from="r-strict-test")
    assert "Cannot resume checkpoint" in result
    assert checkpoint_path.read_bytes() == b"\xff\xfe malformed checkpoint"
    owner = store.acquire_owner("r-strict-test")
    assert owner is not None
    owner.release()


def test_controller_releases_owner_when_load_raises(tmp_path, make_agent_controller, monkeypatch):
    store = CheckpointStore(str(tmp_path))
    llm = MagicMock()
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    monkeypatch.setattr(store, "load", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture")))

    with pytest.raises(OSError, match="fixture"):
        controller.run("goal", resume_from="r-strict-test")
    owner = store.acquire_owner("r-strict-test")
    assert owner is not None
    owner.release()


@pytest.mark.parametrize("failure", ["context_setup", "trace_restore", "cancel_cleanup"])
def test_controller_releases_owner_after_setup_or_cleanup_failure(
    tmp_path, make_agent_controller, failure
):
    store = CheckpointStore(str(tmp_path))
    assert store.save("r-strict-test", _resume_checkpoint())
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    if failure == "context_setup":
        with patch("agent_controller.AgentRuntime.build_react_context", side_effect=RuntimeError("fixture")):
            with pytest.raises(RuntimeError, match="fixture"):
                controller.run("goal", resume_from="r-strict-test")
    else:
        if failure == "trace_restore":
            llm.set_trace_id = MagicMock(side_effect=[None, RuntimeError("trace restore")])
            expected_error = "trace restore"
        else:
            llm.set_trace_id = MagicMock()
            controller._cancel_registry.release = MagicMock(side_effect=RuntimeError("cancel cleanup"))
            expected_error = "cancel cleanup"
        with patch("agent_controller.react_loop", return_value="ok"):
            with pytest.raises(RuntimeError, match=expected_error):
                controller.run("goal", resume_from="r-strict-test")

    owner = store.acquire_owner("r-strict-test")
    assert owner is not None
    owner.release()


def test_checkpoint_uses_current_goal_anchor_and_resumes_native_history_unchanged(
    tmp_path, make_agent_controller
):
    from confirmation import ConfirmationManager
    from react_loop import LLMErrorInfo, _LoopState, _get_user_goal, _handle_llm_error
    from tests.execution_harness import RecordingExecutor, build_context

    trace_id = "r-current-goal"
    store = CheckpointStore(str(tmp_path))
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    history = [
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-old", "type": "function",
            "function": {"name": "lookup", "arguments": "{\"q\":\"old\"}"},
        }]},
        {"role": "tool", "tool_call_id": "call-old", "content": "old result"},
        {"role": "user", "content": "current task"},
    ]
    attempt = CallAttemptState(max_retries=0)
    attempt.begin_outbound_attempt("json")
    state = _LoopState(
        messages=history, goal_idx=3, max_steps=8, step=2,
        attempt_state=attempt,
    )
    owner = store.acquire_owner(trace_id)
    assert owner is not None
    ctx = build_context(llm, RecordingExecutor(), trace_id=trace_id)
    ctx.checkpoint_store = store
    ctx.checkpoint_owner = owner
    ctx.checkpoint_enabled = True
    ctx.checkpoint_recovery_eligible = True
    ctx.confirmation = ConfirmationManager()
    try:
        assert _get_user_goal(state) == "current task"
        _handle_llm_error(
            ctx, state, LLMErrorInfo("timeout", "timed out", True, ""),
            lambda _message: None, _get_user_goal(state),
        )
    finally:
        owner.release()

    saved = store.load(trace_id)
    assert saved is not None
    assert saved["user_goal"] == "current task"
    assert saved["goal_idx"] == 3
    assert saved["messages"] == history

    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    with patch("agent_controller.react_loop", return_value="ok") as mock_loop:
        assert controller.run("ignored", resume_from=trace_id) == "ok"
    restored = mock_loop.call_args.kwargs["initial_state"]
    assert restored.goal_idx == 3
    assert restored.messages == history
    assert mock_loop.call_args.args[1] == "current task"


def test_disabled_checkpoint_writes_resume_keeps_owner_for_retirement(
    tmp_path, make_agent_controller
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    assert store.save(checkpoint["trace_id"], checkpoint)
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    controller._checkpoint_enabled = False
    observed = []

    def inspect_context(ctx, *_args, **_kwargs):
        observed.append((ctx.checkpoint_enabled, ctx.checkpoint_owner))
        return "ok"

    with patch("agent_controller.react_loop", side_effect=inspect_context):
        assert controller.run("goal", resume_from=checkpoint["trace_id"]) == "ok"
    assert observed[0][0] is False
    assert observed[0][1] is not None
    assert observed[0][1].released is True


def test_startup_scan_with_checkpoints_sends_notification(tmp_path):
    """Verify that startup scan sends notification when checkpoints exist."""
    store = CheckpointStore(str(tmp_path))
    checkpoint = {
        "trace_id": "r-test1234",
        "user_goal": "Analyze the logs",
        "messages": [],
        "step": 3,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model": "test-model",
        "created_at": "2026-08-21T14:30:00Z",
        "error_info": {
            "type": "timeout",
            "message": "⏱️ Request timed out",
            "retryable": True,
            "detail": "...",
        },
    }
    store.save("r-test1234", checkpoint)

    notifications = []

    def notify(msg):
        notifications.append(msg)

    from checkpoint_store import startup_checkpoint_notice

    startup_notice = startup_checkpoint_notice(store)
    if startup_notice is not None:
        notify(startup_notice)

    assert len(notifications) == 1
    assert "Analyze the logs" in notifications[0]
    assert "/resume" in notifications[0]


def test_startup_scan_no_checkpoints_sends_no_notification(tmp_path):
    """Verify that startup scan sends no notification when no checkpoints exist."""
    store = CheckpointStore(str(tmp_path))

    notifications = []

    def notify(msg):
        notifications.append(msg)

    from checkpoint_store import startup_checkpoint_notice

    startup_notice = startup_checkpoint_notice(store)
    if startup_notice is not None:
        notify(startup_notice)

    assert len(notifications) == 0


def test_strict_resume_first_send_is_initial_attempt(tmp_path):
    """Relocated from tests/test_request_preparation.py (task 2.2).

    PHASE2 (task 10.1): the saved state comes from the preview-fit preparation
    failure; rewrite that trigger while preserving the initial-attempt assertions.
    """
    from call_attempt_state import CallAttemptState
    from react_loop import _LoopState, _request_turn
    from tests.execution_harness import RecordingExecutor, build_context
    from tests.test_react_loop_bounded_retry import ScriptedRequestLLM
    from tests.test_react_loop_error_recovery import save_zero_outbound_checkpoint

    store, owner = save_zero_outbound_checkpoint(tmp_path)
    try:
        saved = store.load("r-harness1", owner=owner)
        attempt = CallAttemptState.from_dict(saved["attempt_state"])
        before = attempt.to_dict()
        llm = ScriptedRequestLLM(json_script=['{"action":"finish","result":"ok"}'])
        ctx = build_context(llm, RecordingExecutor())
        state = _LoopState(
            messages=saved["messages"], goal_idx=saved["goal_idx"],
            max_steps=saved["max_steps"], attempt_state=attempt,
        )
        turn = _request_turn(ctx, state, "system", lambda _m: None)
        assert turn.early_return is None
        assert llm.json_calls == 1
        assert attempt.outbound_attempts == 1
        assert attempt.automatic_retries_used == 0 and attempt.manual_retries_used == 0
        assert attempt.to_dict()["max_retries"] == before["max_retries"]
    finally:
        owner.release()


# ---------------------------------------------------------------------------
# Active-model resume: no historical-model equality, handoff notice (tasks 2.1-2.3)
# ---------------------------------------------------------------------------

HANDOFF = (
    "Resuming with {new}; saved progress was created with {old}. Saved conversation "
    "and tool results will be sent to the selected model's provider."
)


def _active_llm(model_id: str = "b/model") -> MagicMock:
    llm = MagicMock()
    llm.llm_cfg = {"id": model_id, "model": "fixture"}
    llm.set_trace_id = MagicMock()
    return llm


def _controller_with_counting_factory(make_agent_controller, store, model_id="b/model"):
    admissions: list[MagicMock] = []

    def factory(_event):
        client = _active_llm(model_id)
        admissions.append(client)
        return client

    controller = make_agent_controller(
        llm=None, checkpoint_store=store, run_client_factory=factory,
    )
    return controller, admissions


def test_resume_on_a_different_model_no_longer_requires_historical_equality(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    controller, admissions = _controller_with_counting_factory(make_agent_controller, store)

    with patch("agent_controller.react_loop", return_value="ok") as mock_loop:
        assert controller.run("goal", resume_from="r-strict-test") == "ok"
    assert len(admissions) == 1  # one admission of the active model
    assert mock_loop.call_args.args[0].llm is admissions[0]
    assert mock_loop.call_args.kwargs["initial_state"].messages == checkpoint["messages"]


def test_different_model_announces_handoff_after_validation_and_before_model_io(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    controller, _ = _controller_with_counting_factory(make_agent_controller, store)
    events: list[str] = []

    def loop(*_args, **_kwargs):
        events.append("model-io")
        return "ok"

    with patch("agent_controller.react_loop", side_effect=loop):
        controller.run("goal", events.append, resume_from="r-strict-test")
    assert events == [HANDOFF.format(new="b/model", old="a/model"), "model-io"]


def test_same_model_resume_has_no_handoff_notice(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "b/model"
    assert store.save("r-strict-test", checkpoint)
    controller, _ = _controller_with_counting_factory(make_agent_controller, store)
    events: list[str] = []
    with patch("agent_controller.react_loop", return_value="ok"):
        controller.run("goal", events.append, resume_from="r-strict-test")
    assert events == []


def test_ordinary_non_resume_run_never_announces_a_handoff(tmp_path, make_agent_controller):
    controller, _ = _controller_with_counting_factory(
        make_agent_controller, CheckpointStore(str(tmp_path)),
    )
    events: list[str] = []
    with patch("agent_controller.react_loop", return_value="ok"):
        controller.run("goal", events.append)
    assert events == []


def test_handoff_notice_delivery_failure_does_not_readmit_replay_or_switch(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    controller, admissions = _controller_with_counting_factory(make_agent_controller, store)

    def broken_progress(_message: str) -> None:
        raise RuntimeError("telegram down")

    with patch("agent_controller.react_loop", return_value="ok") as mock_loop:
        assert controller.run("goal", broken_progress, resume_from="r-strict-test") == "ok"
    assert len(admissions) == 1  # no second admission
    assert mock_loop.call_count == 1  # no replay
    assert mock_loop.call_args.args[0].llm is admissions[0]  # no model switch


def test_handoff_notice_renders_only_safe_model_identities(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model\n<b>https://user:pass@host/?key=secret-token</b>"
    assert store.save("r-strict-test", checkpoint)
    controller, _ = _controller_with_counting_factory(make_agent_controller, store)
    events: list[str] = []
    with patch("agent_controller.react_loop", return_value="ok"):
        controller.run("goal", events.append, resume_from="r-strict-test")
    assert len(events) == 1
    notice = events[0]
    assert "secret-token" not in notice and "user:pass" not in notice
    assert "\n" not in notice and "<b>" not in notice
    assert notice.startswith("Resuming with b/model; saved progress was created with ")
    assert notice.endswith("provider.")


def test_resume_requires_a_valid_historical_model_string(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = 42  # not a string: malformed saved identity
    assert store.save("r-strict-test", checkpoint)
    controller, admissions = _controller_with_counting_factory(make_agent_controller, store)
    with patch("agent_controller.react_loop") as mock_loop:
        result = controller.run("goal", resume_from="r-strict-test")
    assert "Cannot resume checkpoint" in result
    mock_loop.assert_not_called()


def test_resume_requires_an_admitted_target_identity(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    assert store.save("r-strict-test", _resume_checkpoint())
    llm = MagicMock()
    llm.llm_cfg = {"model": "no-canonical-id"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    with patch("agent_controller.react_loop") as mock_loop:
        result = controller.run("goal", resume_from="r-strict-test")
    assert "Cannot resume checkpoint" in result
    mock_loop.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-model resume keeps ownership, exact-snapshot and cleanup guarantees (3.2)
# ---------------------------------------------------------------------------

def _cross_model_world(tmp_path, make_agent_controller, respond=None):
    from tests.test_active_model_resume_transport import _World, _ok

    return _World(tmp_path, make_agent_controller, "openai", respond or _ok("openai"))


def test_changed_snapshot_blocks_cross_model_send_and_is_never_overwritten(
    tmp_path, make_agent_controller,
):
    import json as _json

    world = _cross_model_world(tmp_path, make_agent_controller)
    real_wrapper = world._real_loop

    def tamper_then_run(ctx, goal, *args, **kwargs):
        changed = _json.loads(world.path.read_text(encoding="utf-8"))
        changed["created_at"] = "2027-01-01T00:00:00Z"  # another actor replaced the snapshot
        world.path.write_text(_json.dumps(changed), encoding="utf-8")
        world.changed_bytes = world.path.read_bytes()
        return real_wrapper(ctx, goal, *args, **kwargs)

    world._real_loop = tamper_then_run
    result = world.resume()
    assert "no model call was sent" in result
    assert world.requests == []  # no transport send
    assert world.path.read_bytes() == world.changed_bytes  # never overwritten
    owner = world.store.acquire_owner("r-resume-am1")
    assert owner is not None
    owner.release()


def test_missing_snapshot_blocks_cross_model_send(tmp_path, make_agent_controller):
    world = _cross_model_world(tmp_path, make_agent_controller)
    real_wrapper = world._real_loop

    def delete_then_run(ctx, goal, *args, **kwargs):
        world.path.unlink()
        return real_wrapper(ctx, goal, *args, **kwargs)

    world._real_loop = delete_then_run
    result = world.resume()
    assert "no model call was sent" in result
    assert world.requests == [] and not world.path.exists()


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="permission bits do not bind root")
def test_retirement_permission_failure_blocks_cross_model_send_and_keeps_bytes(
    tmp_path, make_agent_controller,
):
    world = _cross_model_world(tmp_path, make_agent_controller)
    directory = world.path.parent
    directory.chmod(0o500)  # the filesystem refuses removal of the snapshot
    try:
        result = world.resume()
    finally:
        directory.chmod(0o700)
    assert "no model call was sent" in result
    assert world.requests == []
    assert world.path.read_bytes() == world.saved_bytes
    owner = world.store.acquire_owner("r-resume-am1")
    assert owner is not None
    owner.release()


def test_concurrent_cross_model_resume_is_refused_before_a_second_admission(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    controller, admissions = _controller_with_counting_factory(make_agent_controller, store)
    entered, release = threading.Event(), threading.Event()

    def fake_loop(ctx, *_a, **_k):
        entered.set()
        assert release.wait(2)
        return "ok"

    first: list[str] = []
    with patch("agent_controller.react_loop", side_effect=fake_loop):
        thread = threading.Thread(
            target=lambda: first.append(controller.run("goal", resume_from="r-strict-test"))
        )
        thread.start()
        assert entered.wait(2)
        second = controller.run("goal", resume_from="r-strict-test")
        release.set()
        thread.join(2)
    assert "already active" in second and first == ["ok"]
    assert len(admissions) == 1  # the refused attempt never admitted a model


def test_cancel_during_admission_preserves_snapshot_and_closes_owned_client(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    path = tmp_path / "run_checkpoints" / "r-strict-test.json"
    before = path.read_bytes()
    clients: list[MagicMock] = []
    holder: dict = {}

    def factory(_event):
        holder["controller"].cancel()  # operator stops the run while it is being admitted
        client = _active_llm()
        clients.append(client)
        return client

    controller = make_agent_controller(llm=None, checkpoint_store=store, run_client_factory=factory)
    holder["controller"] = controller
    with patch("agent_controller.react_loop") as mock_loop:
        result = controller.run("goal", resume_from="r-strict-test")
    assert result == "⚠️ Task stopped by operator."
    mock_loop.assert_not_called()
    assert path.read_bytes() == before
    clients[0].close.assert_called_once()
    owner = store.acquire_owner("r-strict-test")
    assert owner is not None
    owner.release()


def test_setup_failure_preserves_snapshot_and_releases_owned_resources(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model"
    assert store.save("r-strict-test", checkpoint)
    path = tmp_path / "run_checkpoints" / "r-strict-test.json"
    before = path.read_bytes()
    controller, admissions = _controller_with_counting_factory(make_agent_controller, store)
    with patch(
        "agent_controller.AgentRuntime.build_react_context", side_effect=RuntimeError("fixture"),
    ):
        with pytest.raises(RuntimeError, match="fixture"):
            controller.run("goal", resume_from="r-strict-test")
    assert path.read_bytes() == before
    admissions[0].close.assert_called_once()
    owner = store.acquire_owner("r-strict-test")
    assert owner is not None
    owner.release()


# ---------------------------------------------------------------------------
# Handoff identity rendering: canonical-ID validation + vault redaction + bound
# ---------------------------------------------------------------------------

def _handoff(saved: str, active: str, redact=None) -> str:
    from agent_controller import _announce_model_handoff

    out: list[str] = []
    _announce_model_handoff(out.append, saved, active, redact)
    assert len(out) == 1
    return out[0]


@pytest.mark.parametrize(
    "unsafe",
    [
        "https://user:pass@host",
        "https://host.example/v1",
        "provider/https://user:pass@host/v1",
        "www.example.com/model",
        "env:API_KEY",
        "sec:vault_key",
        "a/model api_key=hunter2",
        "a/model\nsecond-line",
    ],
)
def test_handoff_never_renders_endpoints_or_credential_shaped_identities(unsafe):
    notice = _handoff(unsafe, "b/model")
    assert "an unrecognized model" in notice
    for leaked in ("user:pass", "://", "www.", "env:", "sec:", "hunter2", "second-line"):
        assert leaked not in notice
    assert notice.startswith("Resuming with b/model; saved progress was created with ")


def test_handoff_redacts_a_vault_secret_inside_an_allowed_charset_identity():
    from builtin_tools.text_utils import redact_vault_secrets

    secret = "S3cretValue99"
    redact = lambda text: redact_vault_secrets(text, [secret], "***REDACTED***")  # noqa: E731
    notice = _handoff(f"a/model-{secret}", "b/model", redact)
    assert secret not in notice and "***REDACTED***" in notice
    assert _handoff("a/model", "b/model", redact).count("***REDACTED***") == 0


def test_handoff_preserves_legitimate_punctuated_and_long_canonical_ids():
    legit = "a/model(2026)"
    assert f"created with {legit}." in _handoff(legit, "b/model")
    unusual = "conn/vendor/Model v2 [beta] (preview)+x=1,y~z"
    assert f"created with {unusual}." in _handoff(unusual, "b/model")
    long_id = "conn/" + "m" * 200
    assert f"created with {long_id}." in _handoff(long_id, "b/model")


def test_handoff_bounds_an_oversized_identity_with_the_existing_indicator():
    notice = _handoff("conn/" + "m" * 400, "b/model")
    assert "..." in notice and "m" * 300 not in notice


def test_controller_uses_its_selector_redactor_for_the_handoff(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _resume_checkpoint()
    checkpoint["model_id"] = "a/model-TOPSECRET99"
    assert store.save("r-strict-test", checkpoint)
    controller, _ = _controller_with_counting_factory(make_agent_controller, store)
    controller._selector_redactor = lambda text: text.replace("TOPSECRET99", "***REDACTED***")
    events: list[str] = []
    with patch("agent_controller.react_loop", return_value="ok"):
        controller.run("goal", events.append, resume_from="r-strict-test")
    assert "TOPSECRET99" not in events[0] and "***REDACTED***" in events[0]
