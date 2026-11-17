"""Unit tests for checkpoint_store.py."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from checkpoint_store import (
    CheckpointRetireStatus,
    CheckpointStore,
)


def _sample_state() -> dict:
    return {
        "trace_id": "r-a1b2c3d4",
        "user_goal": "Analyze the logs",
        "messages": [{"role": "user", "content": "hello"}],
        "step": 3,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model": "gpt-4o",
        "created_at": "2026-08-21T14:30:00Z",
        "error_info": {
            "type": "timeout",
            "message": "⏱️ Request timed out",
            "retryable": True,
            "detail": "...",
        },
    }


def test_save_load_round_trip(tmp_path: Path) -> None:
    """Saving and loading preserves all checkpoint fields."""
    store = CheckpointStore(str(tmp_path))
    state = _sample_state()
    store.save(state["trace_id"], state)
    loaded = store.load(state["trace_id"])
    assert loaded == state


def test_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    """After save, no .tmp file remains in the checkpoint directory."""
    store = CheckpointStore(str(tmp_path))
    store.save("r-atomic", _sample_state())
    tmp_files = list(tmp_path.glob("run_checkpoints/*.tmp"))
    assert tmp_files == []


def test_deletes_existing_checkpoint(tmp_path: Path) -> None:
    """Delete removes an existing checkpoint file."""
    store = CheckpointStore(str(tmp_path))
    store.save("r-delete", _sample_state())
    assert store.load("r-delete") is not None
    store.delete("r-delete")
    assert store.load("r-delete") is None


def test_delete_missing_checkpoint_is_no_op(tmp_path: Path) -> None:
    """Deleting a non-existent checkpoint does not raise."""
    store = CheckpointStore(str(tmp_path))
    store.delete("r-missing")
    assert store.load("r-missing") is None


def test_list_sorted_by_created_at_descending(tmp_path: Path) -> None:
    """list() returns checkpoints newest first by created_at."""
    store = CheckpointStore(str(tmp_path))
    states = [
        {**_sample_state(), "trace_id": "r-oldest", "created_at": "2026-08-21T10:00:00Z"},
        {**_sample_state(), "trace_id": "r-middle", "created_at": "2026-08-21T14:00:00Z"},
        {**_sample_state(), "trace_id": "r-newest", "created_at": "2026-08-21T18:00:00Z"},
    ]
    for state in states:
        store.save(state["trace_id"], state)
    result = store.list()
    assert [cp["trace_id"] for cp in result] == ["r-newest", "r-middle", "r-oldest"]


def test_load_corrupted_file_returns_none(tmp_path: Path) -> None:
    """Invalid JSON in a checkpoint file yields None from load()."""
    store = CheckpointStore(str(tmp_path))
    checkpoint_dir = tmp_path / "run_checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    (checkpoint_dir / "r-corrupt.json").write_text("not valid json", encoding="utf-8")
    assert store.load("r-corrupt") is None


def test_list_skips_corrupted_file(tmp_path: Path) -> None:
    """list() skips files that cannot be parsed as JSON."""
    store = CheckpointStore(str(tmp_path))
    valid = {**_sample_state(), "trace_id": "r-valid", "created_at": "2026-08-21T12:00:00Z"}
    store.save("r-valid", valid)
    checkpoint_dir = tmp_path / "run_checkpoints"
    (checkpoint_dir / "r-corrupt.json").write_text("not valid json", encoding="utf-8")
    result = store.list()
    assert len(result) == 1
    assert result[0]["trace_id"] == "r-valid"


def test_missing_created_at_key_is_treated_as_corrupted(tmp_path: Path) -> None:
    """A checkpoint missing the created_at key is skipped/returned as None."""
    store = CheckpointStore(str(tmp_path))
    checkpoint_dir = tmp_path / "run_checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    missing = {"trace_id": "r-no-created"}
    (checkpoint_dir / "r-no-created.json").write_text(
        json.dumps(missing), encoding="utf-8"
    )
    assert store.load("r-no-created") is None
    assert store.list() == []


def test_save_oserror_is_non_fatal_and_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An OSError during save is swallowed and a warning is logged."""
    store = CheckpointStore(str(tmp_path))

    def _raise_oserror(*args, **kwargs):
        raise OSError("disk full")

    with patch("os.open", _raise_oserror):
        with caplog.at_level(logging.WARNING, logger="checkpoint_store"):
            store.save("r-oserror", _sample_state())

    assert "Checkpoint save failed (OSError)" in caplog.text


def _supported_checkpoint(trace_id: str = "r-owned") -> dict:
    """Return a strict v2 snapshot suitable for guarded retirement tests."""
    checkpoint = {
        **_sample_state(),
        "trace_id": trace_id,
        "format_version": 2,
        "model_id": "fixture-model",
        "attempt_state": {
            "max_retries": 4,
            "retry_delay": 2.0,
            "mode": "json",
            "outbound_attempts": 1,
            "automatic_retries_used": 0,
            "manual_retries_used": 0,
            "manual_retry_authorized": False,
            "partial_output": False,
            "effect": False,
        },
    }
    checkpoint.pop("model")
    return checkpoint


def test_guarded_retirement_requires_exact_active_snapshot(tmp_path: Path) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint()
    assert store.save("r-owned", checkpoint)
    owner = store.acquire_owner("r-owned")
    assert owner is not None
    try:
        loaded = store.load("r-owned", owner=owner)
        assert loaded == checkpoint
        changed = {**checkpoint, "step": 99}
        path = tmp_path / "run_checkpoints" / "r-owned.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        result = store.retire_pending("r-owned", owner)
        assert result.status is CheckpointRetireStatus.BLOCKED
        assert json.loads(path.read_text(encoding="utf-8")) == changed
    finally:
        owner.release()


def test_guarded_retirement_blocks_missing_loaded_snapshot(tmp_path: Path) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint()
    store.save("r-owned", checkpoint)
    owner = store.acquire_owner("r-owned")
    assert owner is not None
    try:
        assert store.load("r-owned", owner=owner) == checkpoint
        (tmp_path / "run_checkpoints" / "r-owned.json").unlink()
        result = store.retire_pending("r-owned", owner)
        assert result.status is CheckpointRetireStatus.BLOCKED
    finally:
        owner.release()


def test_guarded_retirement_unlinks_only_matching_snapshot(tmp_path: Path) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint()
    owner = store.acquire_owner("r-owned")
    assert owner is not None
    try:
        assert store.save("r-owned", checkpoint, owner=owner)
        result = store.retire_pending("r-owned", owner)
        assert result.status is CheckpointRetireStatus.RETIRED
        assert store.load("r-owned", owner=owner) is None
    finally:
        owner.release()


def test_same_trace_has_only_one_process_local_owner(tmp_path: Path) -> None:
    first = CheckpointStore(str(tmp_path))
    second = CheckpointStore(str(tmp_path))
    owner = first.acquire_owner("r-shared")
    assert owner is not None
    try:
        assert second.acquire_owner("r-shared") is None
    finally:
        owner.release()
    next_owner = second.acquire_owner("r-shared")
    assert next_owner is not None
    next_owner.release()


def test_ownerless_save_holds_temporary_owner_through_replace(tmp_path: Path, monkeypatch) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint("r-save-race")
    entered = threading.Event()
    proceed = threading.Event()
    original_replace = __import__("os").replace
    results: list[bool] = []

    def blocking_replace(source, destination):
        if str(destination).endswith("r-save-race.json"):
            entered.set()
            assert proceed.wait(2)
        return original_replace(source, destination)

    monkeypatch.setattr("checkpoint_store.os.replace", blocking_replace)
    worker = threading.Thread(target=lambda: results.append(store.save("r-save-race", checkpoint)))
    worker.start()
    assert entered.wait(2)
    assert store.acquire_owner("r-save-race") is None
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
    assert results == [True]
    owner = store.acquire_owner("r-save-race")
    assert owner is not None
    owner.release()


def test_ownerless_delete_holds_temporary_owner_until_unlink(tmp_path: Path, monkeypatch) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint("r-delete-race")
    assert store.save("r-delete-race", checkpoint)
    entered = threading.Event()
    proceed = threading.Event()
    original_remove = __import__("os").remove

    def blocking_remove(path):
        if str(path).endswith("r-delete-race.json"):
            entered.set()
            assert proceed.wait(2)
        return original_remove(path)

    monkeypatch.setattr("checkpoint_store.os.remove", blocking_remove)
    worker = threading.Thread(target=lambda: store.delete("r-delete-race"))
    worker.start()
    assert entered.wait(2)
    assert store.acquire_owner("r-delete-race") is None
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not (tmp_path / "run_checkpoints" / "r-delete-race.json").exists()
    owner = store.acquire_owner("r-delete-race")
    assert owner is not None
    owner.release()


def test_ownerless_retirement_holds_temporary_owner_until_unlink(tmp_path: Path, monkeypatch) -> None:
    store = CheckpointStore(str(tmp_path))
    checkpoint = _supported_checkpoint("r-retire-race")
    assert store.save("r-retire-race", checkpoint)
    entered = threading.Event()
    proceed = threading.Event()
    original_remove = __import__("os").remove
    statuses = []

    def blocking_remove(path):
        if str(path).endswith("r-retire-race.json"):
            entered.set()
            assert proceed.wait(2)
        return original_remove(path)

    monkeypatch.setattr("checkpoint_store.os.remove", blocking_remove)
    worker = threading.Thread(
        target=lambda: statuses.append(
            store.retire_pending("r-retire-race", expected_snapshot=checkpoint).status
        )
    )
    worker.start()
    assert entered.wait(2)
    assert store.acquire_owner("r-retire-race") is None
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
    assert statuses == [CheckpointRetireStatus.RETIRED]


def test_prune_reserves_each_trace_and_skips_a_trace_owned_after_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    store = CheckpointStore(str(tmp_path), max_checkpoints=1)
    old = _supported_checkpoint("r-prune-race-old")
    old["created_at"] = "2026-01-01T00:00:00Z"
    new = _supported_checkpoint("r-prune-race-new")
    new["created_at"] = "2026-01-02T00:00:00Z"
    assert store.save(old["trace_id"], old)
    entered = threading.Event()
    proceed = threading.Event()
    original_remove = __import__("os").remove

    def blocking_remove(path):
        if str(path).endswith("r-prune-race-old.json"):
            entered.set()
            assert proceed.wait(2)
        return original_remove(path)

    monkeypatch.setattr("checkpoint_store.os.remove", blocking_remove)
    worker = threading.Thread(target=lambda: store.save(new["trace_id"], new))
    worker.start()
    assert entered.wait(2)
    assert store.acquire_owner(old["trace_id"]) is None
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not (tmp_path / "run_checkpoints" / "r-prune-race-old.json").exists()


def test_prune_leaves_checkpoint_owned_before_inventory(tmp_path: Path) -> None:
    store = CheckpointStore(str(tmp_path), max_checkpoints=1)
    old = _supported_checkpoint("r-prune-owned-old")
    old["created_at"] = "2026-01-01T00:00:00Z"
    new = _supported_checkpoint("r-prune-owned-new")
    new["created_at"] = "2026-01-02T00:00:00Z"
    assert store.save(old["trace_id"], old)
    owner = store.acquire_owner(old["trace_id"])
    assert owner is not None
    try:
        assert store.save(new["trace_id"], new)
        assert store.load(old["trace_id"], owner=owner) == old
    finally:
        owner.release()


@pytest.mark.parametrize("failure", ["rename", "encode"])
def test_ownerless_save_releases_temporary_owner_after_failure(tmp_path: Path, monkeypatch, failure: str) -> None:
    store = CheckpointStore(str(tmp_path))
    if failure == "rename":
        monkeypatch.setattr("checkpoint_store.os.replace", lambda *args: (_ for _ in ()).throw(OSError("fixture")))
    else:
        monkeypatch.setattr("checkpoint_store.json.dump", lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("fixture")))
    assert store.save("r-failed-temp-owner", _supported_checkpoint("r-failed-temp-owner")) is False
    owner = store.acquire_owner("r-failed-temp-owner")
    assert owner is not None
    owner.release()


def test_missing_directory_is_created(tmp_path: Path) -> None:
    """CheckpointStore creates the data/run_checkpoints directory if absent."""
    data_dir = tmp_path / "fresh" / "data"
    assert not data_dir.exists()
    store = CheckpointStore(str(data_dir))
    assert (data_dir / "run_checkpoints").is_dir()
    store.save("r-fresh", _sample_state())
    assert store.load("r-fresh") is not None


def _prunable_state(trace_id: str, created_at: str) -> dict:
    return {
        "trace_id": trace_id,
        "user_goal": f"goal {trace_id}",
        "messages": [],
        "step": 1,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model": "test",
        "created_at": created_at,
        "error_info": {
            "type": "timeout",
            "message": "timed out",
            "retryable": True,
            "detail": "...",
        },
    }


def test_retention_cap_prunes_old_checkpoints(tmp_path: Path) -> None:
    """Checkpoints beyond max_checkpoints are pruned on save."""
    store = CheckpointStore(str(tmp_path), max_checkpoints=3)
    for i in range(5):
        state = _supported_checkpoint(f"r-trace{i}")
        state["created_at"] = f"2026-08-21T10:0{i}:00Z"
        store.save(f"r-trace{i}", state)
    checkpoints = store.list()
    assert len(checkpoints) == 3
    trace_ids = [cp["trace_id"] for cp in checkpoints]
    assert "r-trace4" in trace_ids
    assert "r-trace3" in trace_ids
    assert "r-trace2" in trace_ids
    assert "r-trace0" not in trace_ids
    assert "r-trace1" not in trace_ids


def test_default_max_checkpoints_is_20(tmp_path: Path) -> None:
    """Default retention cap is 20."""
    store = CheckpointStore(str(tmp_path))
    assert store._max_checkpoints == 20


def test_max_checkpoints_zero_means_unlimited(tmp_path: Path) -> None:
    """max_checkpoints=0 disables pruning."""
    store = CheckpointStore(str(tmp_path), max_checkpoints=0)
    for i in range(5):
        store.save(f"r-trace{i}", _prunable_state(f"r-trace{i}", f"2026-08-21T10:0{i}:00Z"))
    assert len(store.list()) == 5


def test_corrupted_files_are_preserved_during_save(tmp_path: Path) -> None:
    """Malformed files are not deletion targets during format-scoped pruning."""
    store = CheckpointStore(str(tmp_path), max_checkpoints=10)
    state1 = _supported_checkpoint("r-valid1")
    state1["created_at"] = "2026-08-21T10:00:00Z"
    state2 = _supported_checkpoint("r-valid2")
    state2["created_at"] = "2026-08-21T11:00:00Z"
    store.save("r-valid1", state1)
    store.save("r-valid2", state2)
    checkpoint_dir = tmp_path / "run_checkpoints"
    (checkpoint_dir / "r-corrupt.json").write_text("not valid json", encoding="utf-8")
    state3 = _supported_checkpoint("r-valid3")
    state3["created_at"] = "2026-08-21T12:00:00Z"
    store.save("r-valid3", state3)
    assert (checkpoint_dir / "r-corrupt.json").exists()
    assert len(store.list()) == 3


def test_new_format_pruning_never_removes_legacy_checkpoint(tmp_path: Path) -> None:
    """A new-format checkpoint save does not prune old-format files."""
    store = CheckpointStore(str(tmp_path), max_checkpoints=1)
    legacy = _prunable_state("r-legacy", "2026-08-21T10:00:00Z")
    store.save("r-legacy", legacy)
    current = _supported_checkpoint("r-current")
    current["created_at"] = "2026-08-21T11:00:00Z"
    store.save("r-current", current)
    assert store.load("r-legacy") == legacy
    assert store.load("r-current") == current


def test_save_reports_nonfatal_disk_failure(tmp_path: Path) -> None:
    """Callers can observe a swallowed checkpoint write failure."""
    store = CheckpointStore(str(tmp_path))
    with patch("os.open", side_effect=OSError("disk full")):
        assert store.save("r-oserror-bool", _sample_state()) is False
