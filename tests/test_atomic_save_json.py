"""Tests for shared atomic JSON persistence in memory_store.py (G-J item H)."""

import json
import os
from unittest.mock import patch

import pytest

from memory_store import MemoryStore, ResultsMemory, _atomic_save_json


def test_atomic_save_json_writes_pretty_utf8(tmp_path):
    path = tmp_path / "store.json"
    data = {"greeting": "héllo", "items": [1, 2, 3]}
    _atomic_save_json(str(path), data)

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "héllo" in text
    assert json.loads(text) == data


def test_atomic_save_json_preserves_existing_file_on_commit_failure(tmp_path):
    path = tmp_path / "store.json"
    original = {"safe": True}
    path.write_text(json.dumps(original), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    with patch("os.replace", side_effect=boom):
        with pytest.raises(OSError, match="disk full"):
            _atomic_save_json(str(path), {"safe": False}, attempts=2)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_atomic_save_json_retries_then_succeeds(tmp_path):
    path = tmp_path / "store.json"
    data = {"ok": True}

    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(src)
        if len(calls) < 2:
            raise OSError("busy")
        return real_replace(src, dst)

    with patch("os.replace", side_effect=flaky_replace):
        _atomic_save_json(str(path), data, attempts=3)

    assert len(calls) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == data
    # temp file should be gone
    assert not any(f.endswith(".tmp") for f in os.listdir(tmp_path))


def test_atomic_save_json_uses_unique_temp_names(tmp_path):
    path = tmp_path / "store.json"
    temps = set()

    real_replace = os.replace

    def capture_replace(src, dst):
        temps.add(src)
        return real_replace(src, dst)

    with patch("os.replace", side_effect=capture_replace):
        for i in range(10):
            _atomic_save_json(str(path), {"i": i})

    assert len(temps) == 10


def test_memory_store_uses_atomic_save(monkeypatch, tmp_path):
    saved_args = {}

    def fake_atomic_save(path, data, *, attempts, base_delay):
        saved_args["path"] = path
        saved_args["data"] = data
        saved_args["attempts"] = attempts
        saved_args["base_delay"] = base_delay

    monkeypatch.setattr("memory_store._atomic_save_json", fake_atomic_save)
    store = MemoryStore(str(tmp_path / "mem.json"))
    store.set("key", "value")

    assert saved_args["path"] == str(tmp_path / "mem.json")
    assert saved_args["data"]["key"] == "value"
    assert saved_args["attempts"] == 3


def test_results_memory_round_trip(tmp_path):
    path = str(tmp_path / "results.json")
    results = ResultsMemory(path)
    results.add_result("goal", "summary", tools_used=["tool"])

    loaded = ResultsMemory(path)
    entries = loaded.search("goal", top_k=1)
    assert entries
    assert entries[0]["summary"] == "summary"
    assert entries[0]["tools_used"] == ["tool"]


@patch("memory_store._atomic_save_json")
def test_results_memory_save_passes_correct_arguments(fake_save, tmp_path):
    path = str(tmp_path / "results.json")
    results = ResultsMemory(path)
    results.add_result("g", "s")

    assert fake_save.call_args.args[0] == path
    assert fake_save.call_args.kwargs["attempts"] == 3
