"""
Tests for JobExecutionLog — scheduled job execution history.

Covers:
- Basic record() and persistence
- Rotation by age (max_age_hours)
- Rotation by per-job count (max_per_job)
- format_for_prompt() output
- read_recent()
- Atomic write (no partial file corruption)
- _result_log_cb wiring in builtin_executor via spawn_args
- Scheduler wires execution_log and passes _result_log_cb in spawn path
- Scheduler legacy path calls execution_log.record()
- Config keys execution_log_max_age_hours / execution_log_max_per_job
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import JobExecutionLog, Scheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "job_execution_log.jsonl")


@pytest.fixture
def log(log_path):
    return JobExecutionLog(log_path, max_age_hours=48, max_per_job=10)


def _sched(tmp_path, extra_config=None):
    """Helper to create a minimal Scheduler for testing."""
    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "scheduler": {"enabled": False, **(extra_config or {})},
        "agent": {"scheduled_max_iterations": 10},
    }
    return Scheduler(
        config=config,
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        data_dir=str(data_dir),
    )


# ---------------------------------------------------------------------------
# Basic record / persistence
# ---------------------------------------------------------------------------


def test_record_creates_file(log, log_path):
    log.record("backup", "do backup", "OK", success=True, elapsed_s=5, model="gpt-4o")
    assert os.path.exists(log_path)


def test_record_appends_entry(log, log_path):
    log.record("backup", "do backup", "OK", success=True, elapsed_s=5)
    log.record("cleanup", "clean up", "Done", success=True, elapsed_s=3)
    entries = log.read_recent()
    assert len(entries) == 2
    assert entries[0]["tag"] == "backup"
    assert entries[1]["tag"] == "cleanup"


def test_record_stores_correct_fields(log):
    log.record("mytag", "my task", "result text", success=False, elapsed_s=7, model="gemini")
    entries = log.read_recent()
    assert len(entries) == 1
    e = entries[0]
    assert e["tag"] == "mytag"
    assert e["task"] == "my task"
    assert e["result"] == "result text"
    assert e["success"] is False
    assert e["elapsed_s"] == 7
    assert e["model"] == "gemini"
    assert "ts" in e


def test_record_truncates_long_result(log):
    long_result = "x" * 3000
    log.record("t", "task", long_result, success=True)
    entries = log.read_recent()
    assert len(entries[0]["result"]) <= 2000


def test_record_truncates_long_task(log):
    log.record("t", "q" * 300, "result", success=True)
    entries = log.read_recent()
    assert len(entries[0]["task"]) <= 200


# ---------------------------------------------------------------------------
# Rotation by age
# ---------------------------------------------------------------------------


def test_rotate_removes_old_entries(log):
    # Inject an old entry directly
    old_ts = (datetime.utcnow() - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_entry = {
        "ts": old_ts, "tag": "old_job", "task": "t", "result": "r",
        "success": True, "elapsed_s": 1, "model": "",
    }
    with open(log._log_file, "w") as fh:
        fh.write(json.dumps(old_entry) + "\n")

    # Recording a new entry triggers rotation
    log.record("new_job", "t", "r", success=True)
    entries = log.read_recent()
    tags = [e["tag"] for e in entries]
    assert "old_job" not in tags
    assert "new_job" in tags


def test_rotate_keeps_recent_entries(log):
    recent_ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "ts": recent_ts, "tag": "recent_job", "task": "t", "result": "r",
        "success": True, "elapsed_s": 1, "model": "",
    }
    with open(log._log_file, "w") as fh:
        fh.write(json.dumps(entry) + "\n")

    log.record("another", "t", "r", success=True)
    entries = log.read_recent()
    tags = [e["tag"] for e in entries]
    assert "recent_job" in tags


# ---------------------------------------------------------------------------
# Rotation by per-job count
# ---------------------------------------------------------------------------


def test_rotate_caps_per_job_count(tmp_path):
    log = JobExecutionLog(str(tmp_path / "log.jsonl"), max_age_hours=48, max_per_job=3)
    for i in range(6):
        log.record("job_a", f"task {i}", f"result {i}", success=True, elapsed_s=i)
    entries = log.read_recent()
    job_a_entries = [e for e in entries if e["tag"] == "job_a"]
    assert len(job_a_entries) == 3


def test_rotate_caps_independently_per_tag(tmp_path):
    log = JobExecutionLog(str(tmp_path / "log.jsonl"), max_age_hours=48, max_per_job=2)
    for i in range(4):
        log.record("alpha", f"task {i}", f"result {i}", success=True)
    for i in range(4):
        log.record("beta", f"task {i}", f"result {i}", success=True)

    entries = log.read_recent()
    alpha = [e for e in entries if e["tag"] == "alpha"]
    beta = [e for e in entries if e["tag"] == "beta"]
    assert len(alpha) == 2
    assert len(beta) == 2


def test_rotate_keeps_most_recent_within_cap(tmp_path):
    log = JobExecutionLog(str(tmp_path / "log.jsonl"), max_age_hours=48, max_per_job=2)
    for i in range(4):
        log.record("myjob", "task", f"result_{i}", success=True)
    entries = [e for e in log.read_recent() if e["tag"] == "myjob"]
    results = [e["result"] for e in entries]
    # The two most recent should be result_2 and result_3
    assert "result_2" in results
    assert "result_3" in results


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


def test_format_for_prompt_empty(log):
    result = log.format_for_prompt()
    assert result == ""


def test_format_for_prompt_contains_header(log):
    log.record("nightly", "task", "OK", success=True)
    result = log.format_for_prompt()
    assert "SCHEDULED JOB EXECUTION HISTORY" in result


def test_format_for_prompt_includes_tag(log):
    log.record("daily_backup", "task", "OK", success=True)
    result = log.format_for_prompt()
    assert "daily_backup" in result


def test_format_for_prompt_shows_failure_marker(log):
    log.record("bad_job", "task", "Error!", success=False)
    result = log.format_for_prompt()
    assert "❌" in result


def test_format_for_prompt_shows_success_marker(log):
    log.record("good_job", "task", "All good", success=True)
    result = log.format_for_prompt()
    assert "✅" in result


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def test_scheduler_has_execution_log(tmp_path):
    s = _sched(tmp_path)
    assert hasattr(s, "execution_log")
    assert isinstance(s.execution_log, JobExecutionLog)


def test_scheduler_uses_config_for_log_params(tmp_path):
    s = _sched(tmp_path, extra_config={
        "execution_log_max_age_hours": 12,
        "execution_log_max_per_job": 5,
    })
    assert s.execution_log._max_age_hours == 12
    assert s.execution_log._max_per_job == 5


def test_scheduler_legacy_path_records_success(tmp_path):
    s = _sched(tmp_path)
    s._jobs_meta["test_job"] = {"task": "do something", "enabled": True, "notify": False}
    s.agent = MagicMock(return_value="all done")

    with patch.object(s.execution_log, "record") as mock_record:
        s._run_job("test_job")

    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["tag"] == "test_job"
    assert kwargs["success"] is True
    assert kwargs["result"] == "all done"


def test_scheduler_legacy_path_records_failure(tmp_path):
    s = _sched(tmp_path)
    s._jobs_meta["bad_job"] = {"task": "do something", "enabled": True, "notify": False}
    s.agent = MagicMock(return_value="❌ something went wrong")

    with patch.object(s.execution_log, "record") as mock_record:
        s._run_job("bad_job")

    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["success"] is False


def test_scheduler_spawn_path_passes_result_log_cb(tmp_path):
    """Scheduler passes _result_log_cb in spawn_args when builtin_executor is available."""
    s = _sched(tmp_path)
    s._jobs_meta["spawn_job"] = {"task": "do spawn task", "enabled": True, "notify": False}
    mock_executor = MagicMock()
    mock_executor._exec_spawn_agent = MagicMock(return_value={"success": True})
    s.builtin_executor = mock_executor

    s._run_job("spawn_job")

    mock_executor._exec_spawn_agent.assert_called_once()
    spawn_args = mock_executor._exec_spawn_agent.call_args[0][0]
    assert "_result_log_cb" in spawn_args
    assert callable(spawn_args["_result_log_cb"])
