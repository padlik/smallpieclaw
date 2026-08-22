"""Unit tests for checkpoint_store.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from checkpoint_store import CheckpointStore


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

    assert "Failed to save checkpoint for r-oserror" in caplog.text


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
        store.save(f"r-trace{i}", _prunable_state(f"r-trace{i}", f"2026-08-21T10:0{i}:00Z"))
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


def test_corrupted_files_pruned_during_save(tmp_path: Path) -> None:
    """Corrupted checkpoint files are removed during pruning."""
    store = CheckpointStore(str(tmp_path), max_checkpoints=10)
    store.save("r-valid1", _prunable_state("r-valid1", "2026-08-21T10:00:00Z"))
    store.save("r-valid2", _prunable_state("r-valid2", "2026-08-21T11:00:00Z"))
    checkpoint_dir = tmp_path / "run_checkpoints"
    (checkpoint_dir / "r-corrupt.json").write_text("not valid json", encoding="utf-8")
    store.save("r-valid3", _prunable_state("r-valid3", "2026-08-21T12:00:00Z"))
    assert not (checkpoint_dir / "r-corrupt.json").exists()
    assert len(store.list()) == 3
