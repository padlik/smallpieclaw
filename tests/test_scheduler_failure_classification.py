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
