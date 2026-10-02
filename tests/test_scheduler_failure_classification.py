"""
Tests for scheduled job failure classification and error-log enrichment.

Part of the "llm-timeout-hardening" OpenSpec change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import JobExecutionLog, _classify_job_failure


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("❌ LLM error: TimeoutException: timed out", "timeout"),
        ("❌ LLM error: HTTPStatusError: 429 Too Many Requests", "rate_limit"),
        ("❌ LLM error: ConnectError: connection refused", "connection"),
        ("❌ LLM error: quota exceeded", "quota"),
        ("❌ LLM error: context too long", "context"),
        ("❌ LLM error: SomethingElse: bad", "unknown"),
        ("Task completed successfully", ""),
        ("", ""),
    ],
)
def test_classify_job_failure(result: str, expected: str) -> None:
    """_classify_job_failure maps sentinel-prefixed results to error types."""
    assert _classify_job_failure(result) == expected


def test_job_execution_log_record_error_type(tmp_path: Path) -> None:
    """JobExecutionLog.record persists a non-empty error_type in the JSONL entry."""
    log_file = tmp_path / "job_execution_log.jsonl"
    execution_log = JobExecutionLog(log_file=str(log_file))

    execution_log.record(
        tag="test_job",
        task="do work",
        result="❌ LLM error: TimeoutException: timed out",
        success=False,
        elapsed_s=1,
        model="gpt-4o-mini",
        error_type="timeout",
    )

    assert log_file.exists()
    with open(log_file, encoding="utf-8") as fh:
        lines = [line.strip() for line in fh if line.strip()]

    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["error_type"] == "timeout"
    assert entry["success"] is False


def test_job_execution_log_record_omits_empty_error_type(tmp_path: Path) -> None:
    """When error_type is empty, it is absent from the JSONL entry."""
    log_file = tmp_path / "job_execution_log.jsonl"
    execution_log = JobExecutionLog(log_file=str(log_file))

    execution_log.record(
        tag="ok_job",
        task="do work",
        result="Task completed successfully",
        success=True,
        error_type="",
    )

    with open(log_file, encoding="utf-8") as fh:
        lines = [line.strip() for line in fh if line.strip()]

    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "error_type" not in entry
    assert entry["success"] is True


# ---------------------------------------------------------------------------
# Fail-closed fallback when the scheduled-profile executor is unavailable
# ---------------------------------------------------------------------------


def _fallback_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scheduler with no builtin executor, so only the direct fallback remains."""
    from unittest.mock import MagicMock

    from scheduler import Scheduler
    from xdg import xdg_paths

    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    s = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(return_value="operator agent ran"),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
    )
    s._jobs_meta["nightly"] = {
        "task": "summarize",
        "enabled": True,
        "notify": False,
        "_next_run": "2026-10-04T02:00:00",
    }
    return s


def test_fallback_fails_closed_without_operator_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scheduled executor: the singleton operator agent is never invoked."""
    s = _fallback_scheduler(tmp_path, monkeypatch)

    s._run_job("nightly")

    s.agent.assert_not_called()


def test_fallback_failure_uses_direct_launch_failure_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed launch failure is alerted directly even with notify=false."""
    s = _fallback_scheduler(tmp_path, monkeypatch)

    s._run_job("nightly")

    s.notify.assert_called_once()
    alert = s.notify.call_args.args[0]
    assert "nightly" in alert
    assert "failed to launch" in alert.lower()
    assert "Next run: 2026-10-04T02:00:00" in alert
    assert "operator agent ran" not in alert


def test_failed_spawn_alert_is_direct_with_notify_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn launch failure alerts directly even when the job has notify=false."""
    from unittest.mock import MagicMock

    s = _fallback_scheduler(tmp_path, monkeypatch)
    executor = MagicMock()
    executor.spawn_agent = MagicMock(return_value={
        "success": False, "error": "spawn_agent: ConnectError: connection refused",
    })
    s.builtin_executor = executor

    s._run_job("nightly")

    s.notify.assert_called_once()
    assert "failed to spawn" in s.notify.call_args.args[0]


@pytest.mark.parametrize("executor_available", [True, False])
def test_launch_failure_record_carries_classification_and_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executor_available: bool
) -> None:
    """Failed launch records keep error classification and next-run metadata."""
    from unittest.mock import MagicMock

    s = _fallback_scheduler(tmp_path, monkeypatch)
    if executor_available:
        executor = MagicMock()
        executor.spawn_agent = MagicMock(return_value={
            "success": False, "error": "spawn_agent: ConnectError: connection refused",
        })
        s.builtin_executor = executor

    s._run_job("nightly")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert entry["error_type"] == ("connection" if executor_available else "unknown")
    assert entry["next_run"] == "2026-10-04T02:00:00"
