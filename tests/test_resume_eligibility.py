"""Shared resume eligibility (tasks 2.1-2.2).

One recovery-layer policy: an explicit ``error_info.retryable=false`` makes a
checkpoint ineligible; omitted legacy metadata stays eligible. The raw store
enumeration is unchanged, controller resume enforces the rule before manual
authorization or any send, and Telegram ``/resume`` plus startup notices use
the same filtered view. Real ``CheckpointStore`` files throughout; only the
Telegram objects are substituted.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from call_attempt_state import CallAttemptState
from checkpoint_store import (
    CheckpointStore,
    is_resume_eligible,
    startup_checkpoint_notice,
)
from telegram_commands import cmd_resume


def _checkpoint(trace: str, goal: str, created: str, *, retryable: bool | None = True,
                kind: str = "timeout") -> dict:
    error_info = {"type": kind, "message": "m", "detail": ""}
    if retryable is not None:
        error_info["retryable"] = retryable
    return {
        "format_version": 2, "trace_id": trace, "user_goal": goal,
        "messages": [{"role": "user", "content": goal}], "step": 2, "goal_idx": 0,
        "max_steps": 8, "json_fail_streak": 0, "model_id": "canonical-test-id",
        "attempt_state": CallAttemptState().to_dict(), "error_info": error_info,
        "created_at": created,
    }


def _write(store: CheckpointStore, checkpoint: dict) -> Path:
    assert store.save(checkpoint["trace_id"], checkpoint)
    return Path(store._checkpoint_dir) / f"{checkpoint['trace_id']}.json"


# ---------------------------------------------------------------------------
# 2.1 predicate, view, controller enforcement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("retryable", "expected"), [(True, True), (False, False), (None, True)],
    ids=["retryable", "explicit-non-retryable", "legacy-omitted"],
)
def test_resume_eligibility_follows_explicit_retryability(retryable, expected):
    assert is_resume_eligible(_checkpoint("r-aaaa0001", "g", "t", retryable=retryable)) is expected


def test_checkpoint_without_error_info_stays_eligible():
    assert is_resume_eligible({"created_at": "t"}) is True


def test_eligible_view_filters_without_changing_raw_enumeration(tmp_path):
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-aaaa0001", "A", "2026-08-03T00:00:00Z"))
    _write(store, _checkpoint("r-bbbb0002", "B", "2026-08-02T00:00:00Z", retryable=False, kind="context"))
    _write(store, _checkpoint("r-cccc0003", "C", "2026-08-01T00:00:00Z", retryable=None))

    assert [c["user_goal"] for c in store.list()] == ["A", "B", "C"]
    assert [c["user_goal"] for c in store.list_eligible()] == ["A", "C"]


def test_controller_rejects_non_retryable_checkpoint_before_send_or_authorization(
    tmp_path, make_agent_controller,
):
    store = CheckpointStore(str(tmp_path))
    checkpoint = _checkpoint("r-bbbb0002", "B", "2026-08-02T00:00:00Z", retryable=False, kind="context")
    checkpoint["attempt_state"] = {
        "max_retries": 0, "retry_delay": 2.0, "mode": "json", "outbound_attempts": 1,
        "automatic_retries_used": 0, "manual_retries_used": 0,
        "manual_retry_authorized": False, "partial_output": False, "effect": False,
    }
    path = _write(store, checkpoint)
    before = path.read_bytes()
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)

    with patch("agent_controller.react_loop") as loop:
        result = controller.run("ignored", resume_from="r-bbbb0002", interactive_llm_recovery=True)

    assert result.startswith("❌") and "non-retryable" in result and "(context)" in result
    loop.assert_not_called()
    llm.chat.assert_not_called()
    assert path.read_bytes() == before  # file and its (unauthorized) attempt state untouched
    owner = store.acquire_owner("r-bbbb0002")
    assert owner is not None, "checkpoint lease leaked"
    owner.release()


def test_controller_still_resumes_legacy_checkpoint_with_retryable_true(tmp_path, make_agent_controller):
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-aaaa0001", "A", "2026-08-03T00:00:00Z"))
    llm = MagicMock()
    llm.llm_cfg = {"id": "canonical-test-id", "model": "fixture"}
    controller = make_agent_controller(llm=llm, checkpoint_store=store)
    with patch("agent_controller.react_loop", return_value="ok"):
        assert controller.run("ignored", resume_from="r-aaaa0001") == "ok"


# ---------------------------------------------------------------------------
# 2.2 Telegram /resume and startup notices use the same view
# ---------------------------------------------------------------------------

def _iface(store: CheckpointStore, *, busy: bool = False) -> MagicMock:
    iface = MagicMock()
    iface._is_authorized.return_value = True
    iface._get_agent_lock.return_value.locked.return_value = busy
    iface.agent.checkpoint_store = store
    iface._pending_resume = {}
    iface._run_agent_task = AsyncMock()
    return iface


def _update(args: list[str] | None = None):
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


def _texts(update) -> list[str]:
    return [call.args[0] for call in update.effective_message.reply_text.call_args_list]


@pytest.fixture
def mixed_store(tmp_path):
    """Newest-to-oldest: retryable A, non-retryable B, retryable C."""
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-aaaa0001", "goal A", "2026-08-03T00:00:00Z"))
    path_b = _write(
        store, _checkpoint("r-bbbb0002", "goal B", "2026-08-02T00:00:00Z", retryable=False, kind="context"),
    )
    _write(store, _checkpoint("r-cccc0003", "goal C", "2026-08-01T00:00:00Z"))
    return store, path_b


@pytest.mark.asyncio
async def test_resume_list_numbers_only_eligible_checkpoints_contiguously(mixed_store):
    store, _ = mixed_store
    update, ctx = _update()
    await cmd_resume(_iface(store), update, ctx)
    (text,) = _texts(update)
    assert "1." in text and "2." in text and "3." not in text
    assert "goal A" in text and "goal C" in text and "goal B" not in text
    assert text.count("Step 2/8") == 2  # step over max_steps in every entry


@pytest.mark.asyncio
async def test_resume_n_selects_from_the_filtered_list_and_leaves_ineligible_file(mixed_store):
    store, path_b = mixed_store
    before = path_b.read_bytes()
    iface = _iface(store)
    update, ctx = _update(["2"])
    await cmd_resume(iface, update, ctx)
    assert iface._pending_resume == {7: "r-cccc0003"}
    assert path_b.read_bytes() == before


@pytest.mark.asyncio
async def test_resume_announces_goal_with_step_over_max(tmp_path):
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-aaaa0001", "goal A", "2026-08-03T00:00:00Z"))
    update, ctx = _update()
    await cmd_resume(_iface(store), update, ctx)
    assert _texts(update) == ["💾 Resuming: goal A (step 2/8)"]


@pytest.mark.asyncio
async def test_resume_with_only_ineligible_checkpoints_reports_nothing_to_resume(tmp_path):
    store = CheckpointStore(str(tmp_path))
    path = _write(store, _checkpoint("r-bbbb0002", "B", "2026-08-02T00:00:00Z", retryable=False, kind="context"))
    before = path.read_bytes()
    iface = _iface(store)
    update, ctx = _update()
    await cmd_resume(iface, update, ctx)
    assert _texts(update) == ["No unfinished runs to resume."]
    iface._run_agent_task.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_resume_with_no_checkpoint_files_reports_nothing_to_resume(tmp_path):
    update, ctx = _update()
    await cmd_resume(_iface(CheckpointStore(str(tmp_path))), update, ctx)
    assert _texts(update) == ["No unfinished runs to resume."]


@pytest.mark.asyncio
async def test_resume_while_busy_is_rejected(mixed_store):
    store, _ = mixed_store
    update, ctx = _update()
    await cmd_resume(_iface(store, busy=True), update, ctx)
    assert _texts(update) == [
        "⚠️ Agent is currently running. Wait for it to finish or /stop it first."
    ]


@pytest.mark.asyncio
async def test_resume_listing_leaves_malformed_and_ineligible_files_untouched(mixed_store):
    store, path_b = mixed_store
    malformed = Path(store._checkpoint_dir) / "r-dddd0004.json"
    malformed.write_bytes(b"{not json")
    legacy_bytes = {p: p.read_bytes() for p in Path(store._checkpoint_dir).glob("*.json")}
    update, ctx = _update()
    await cmd_resume(_iface(store), update, ctx)
    assert {p: p.read_bytes() for p in Path(store._checkpoint_dir).glob("*.json")} == legacy_bytes
    assert path_b.read_bytes() == legacy_bytes[path_b]


def test_startup_announces_single_eligible_checkpoint(tmp_path):
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-aaaa0001", "goal A", "2026-08-03T00:00:00Z"))
    assert startup_checkpoint_notice(store) == (
        "💾 Found unfinished run: 'goal A'. Send /resume to continue."
    )


def test_startup_sends_nothing_when_only_ineligible_files_exist(tmp_path):
    store = CheckpointStore(str(tmp_path))
    path = _write(store, _checkpoint("r-bbbb0002", "B", "2026-08-02T00:00:00Z", retryable=False, kind="context"))
    before = path.read_bytes()
    assert startup_checkpoint_notice(store) is None
    assert path.read_bytes() == before


def test_startup_names_newest_eligible_goal_when_newest_stored_is_ineligible(tmp_path):
    store = CheckpointStore(str(tmp_path))
    _write(store, _checkpoint("r-bbbb0002", "newest but ineligible", "2026-08-04T00:00:00Z", retryable=False, kind="context"))
    _write(store, _checkpoint("r-aaaa0001", "newest eligible", "2026-08-03T00:00:00Z"))
    _write(store, _checkpoint("r-cccc0003", "older eligible", "2026-08-01T00:00:00Z"))
    notice = startup_checkpoint_notice(store)
    assert notice == (
        "💾 Found 2 unfinished runs. Most recent: 'newest eligible'. Send /resume to see all."
    )


def test_startup_notice_leaves_legacy_file_bytes_unchanged(tmp_path):
    store = CheckpointStore(str(tmp_path))
    path = _write(store, _checkpoint("r-aaaa0001", "legacy", "2026-08-03T00:00:00Z", retryable=None))
    before = path.read_bytes()
    assert json.loads(before)["error_info"].get("retryable") is None
    assert startup_checkpoint_notice(store) is not None
    assert path.read_bytes() == before
