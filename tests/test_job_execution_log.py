"""
Tests for JobExecutionLog — scheduled job execution history.

Covers:
- Basic record() and persistence
- Rotation by age (max_age_hours)
- Rotation by per-job count (max_per_job)
- format_for_prompt() output
- read_recent()
- Atomic write (no partial file corruption)
- Result-log callback wiring through per-submission supervision options
- Scheduler wires execution_log and passes result_log_cb outside model-facing args
- Scheduler legacy path calls execution_log.record()
- Config keys execution_log_max_age_hours / execution_log_max_per_job
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import JobExecutionLog, Scheduler
from xdg import xdg_paths


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "job_execution_log.jsonl")


@pytest.fixture
def log(log_path):
    return JobExecutionLog(log_path, max_age_hours=48, max_per_job=10)


def _sched(tmp_path, monkeypatch, extra_config=None):
    """Helper to create a minimal Scheduler for testing."""
    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    config = {
        "scheduler": {"enabled": False, **(extra_config or {})},
        "agent": {"scheduled_max_iterations": 10},
    }
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Scheduler(
        config=config,
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
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


def test_record_retains_complete_long_result(log):
    long_result = "x" * 50_000 + "END"
    log.record("t", "task", long_result, success=True)
    entries = log.read_recent()
    assert entries[0]["result"] == long_result


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


def test_scheduler_has_execution_log(tmp_path, monkeypatch):
    s = _sched(tmp_path, monkeypatch)
    assert hasattr(s, "execution_log")
    assert isinstance(s.execution_log, JobExecutionLog)


def test_scheduler_uses_config_for_log_params(tmp_path, monkeypatch):
    s = _sched(tmp_path, monkeypatch, extra_config={
        "execution_log_max_age_hours": 12,
        "execution_log_max_per_job": 5,
    })
    assert s.execution_log._max_age_hours == 12
    assert s.execution_log._max_per_job == 5


def test_unavailable_scheduled_executor_does_not_invoke_operator_agent(
    tmp_path, monkeypatch
):
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["test_job"] = {"task": "do something", "enabled": True, "notify": False}
    s.agent = MagicMock(return_value="all done")
    assert s.builtin_executor is None

    s._run_job("test_job")

    s.agent.assert_not_called()


def test_unavailable_scheduled_executor_records_actionable_launch_failure(
    tmp_path, monkeypatch
):
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["test_job"] = {"task": "do something", "enabled": True, "notify": False}

    s._run_job("test_job")

    last_error = s._run_history["test_job"]["last_error"]
    assert "scheduled executor" in last_error.lower()
    assert "not run" in last_error.lower()
    assert "test_job" not in s._running_jobs


def test_scheduler_spawn_path_passes_result_log_cb(tmp_path, monkeypatch):
    """Scheduler delivers result_log_cb via supervision options, not spawn_args.

    Migrated from asserting an ``_result_log_cb`` key inside the model-facing
    spawn_args dict: scheduler/internal controls now flow through the
    per-submission ``SupervisionOptions`` channel.
    """
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["spawn_job"] = {"task": "do spawn task", "enabled": True, "notify": False}
    mock_executor = MagicMock()
    mock_executor.spawn_agent = MagicMock(return_value={"success": True})
    s.builtin_executor = mock_executor

    s._run_job("spawn_job")

    mock_executor.spawn_agent.assert_called_once()
    # Internal controls must NOT leak into the model-facing args dict.
    spawn_args = mock_executor.spawn_agent.call_args[0][0]
    assert "_result_log_cb" not in spawn_args
    assert "_finish_cb" not in spawn_args
    assert "_job_tag" not in spawn_args
    assert "_notify" not in spawn_args
    assert "expandable" not in spawn_args
    # They arrive through the supervision options channel instead.
    options = mock_executor.spawn_agent.call_args.kwargs["options"]
    assert options.job_tag == "spawn_job"
    assert callable(options.result_log_cb)
    assert callable(options.finish_cb)
    assert callable(options.completion_cb)
    assert options.notify is False


# ---------------------------------------------------------------------------
# Complete, bounded job execution records (run IDs + terminal metadata)
# ---------------------------------------------------------------------------

_RUN_ID_RE = r"^run-[0-9a-f]{32}$"


def _spawn_sched(tmp_path, monkeypatch, overlap_policy="skip"):
    """Scheduler with a spawn-capable executor that captures supervision options."""
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["report"] = {
        "task": "write report",
        "enabled": True,
        "notify": False,
        "overlap_policy": overlap_policy,
        "_next_run": "2026-10-03T09:00:00",
    }
    captured = []
    executor = MagicMock()

    def spawn(_args, options):
        captured.append(options)
        return {"success": True, "agent_id": f"sa-{len(captured)}"}

    executor.spawn_agent = MagicMock(side_effect=spawn)
    s.builtin_executor = executor
    return s, captured


def _finish(options, result, success):
    """Invoke the scheduler result callback exactly as the supervisor does."""
    options.result_log_cb(
        tag=options.job_tag, task="write report", result=result,
        success=success, elapsed_s=3, model="m",
    )


def test_record_stores_run_id_and_terminal_status(log):
    log.record(
        "t", "task", "result", success=True, run_id="run-" + "a" * 32, status="done",
    )
    entry = log.read_recent()[0]
    assert entry["run_id"] == "run-" + "a" * 32
    assert entry["status"] == "done"


def test_parallel_runs_of_one_tag_get_distinct_run_ids(tmp_path, monkeypatch):
    import re

    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")
    _finish(captured[1], "second complete", True)
    _finish(captured[0], "first complete", True)

    entries = s.execution_log.read_recent()
    run_ids = [e["run_id"] for e in entries]
    assert len(set(run_ids)) == 2
    assert all(re.match(_RUN_ID_RE, run_id) for run_id in run_ids)
    assert not any(run_id.startswith("sa-") for run_id in run_ids)
    by_result = {e["result"]: e["run_id"] for e in entries}
    assert set(by_result) == {"first complete", "second complete"}


def test_scheduled_record_retains_complete_result(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    full = "line\n" * 2_000 + "TAIL"
    _finish(captured[0], full, True)

    entry = s.execution_log.read_recent()[0]
    assert entry["result"] == full
    assert entry["tag"] == "report"
    assert entry["status"] == "done"
    assert "ts" in entry


@pytest.mark.parametrize(
    ("result", "success", "status"),
    [
        ("❌ connection refused", False, "failed"),
        ("[Cancelled]", False, "cancelled"),
    ],
)
def test_unsuccessful_record_carries_terminal_metadata(
    tmp_path, monkeypatch, result, success, status
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    _finish(captured[0], result, success)

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == status
    assert entry["result"] == result
    if status == "failed":
        assert entry["error_type"] == "connection"
        assert entry["next_run"] == "2026-10-03T09:00:00"


def test_scheduler_log_retention_defaults(tmp_path, monkeypatch):
    s = _sched(tmp_path, monkeypatch)
    assert s.execution_log._max_age_hours == 48
    assert s.execution_log._max_per_job == 10


# ---------------------------------------------------------------------------
# Launch failures are recorded; skips and reminders are not agentic runs
# ---------------------------------------------------------------------------


def test_failed_spawn_persists_failed_record_with_admitted_run_id(
    tmp_path, monkeypatch
):
    import re

    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["report"] = {"task": "write report", "enabled": True, "notify": False}
    executor = MagicMock()
    executor.spawn_agent = MagicMock(return_value={
        "success": False, "error": "spawn_agent: max_subagents cap reached (6/6).",
    })
    s.builtin_executor = executor

    s._run_job("report")

    entries = s.execution_log.read_recent()
    assert len(entries) == 1
    entry = entries[0]
    assert re.match(_RUN_ID_RE, entry["run_id"])
    assert entry["status"] == "failed"
    assert entry["success"] is False
    assert entry["result"] == "spawn_agent: max_subagents cap reached (6/6)."


def test_fail_closed_fallback_persists_failed_record_with_admitted_run_id(
    tmp_path, monkeypatch
):
    import re

    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["report"] = {"task": "write report", "enabled": True, "notify": False}

    s._run_job("report")

    entries = s.execution_log.read_recent()
    assert len(entries) == 1
    entry = entries[0]
    assert re.match(_RUN_ID_RE, entry["run_id"])
    assert entry["status"] == "failed"
    assert entry["result"] == s._run_history["report"]["last_error"]


def test_overlap_skip_receives_no_run_id_or_record(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="skip")
    s._running_jobs.add("report")

    with patch("scheduler._new_run_id") as mint:
        s._run_job("report")

    mint.assert_not_called()
    assert captured == []
    assert s.execution_log.read_recent() == []


def test_empty_task_reminder_receives_no_run_id_or_record(tmp_path, monkeypatch):
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["stretch"] = {"task": "", "enabled": True, "notify": True}

    with patch("scheduler._new_run_id") as mint:
        s._run_job("stretch")

    mint.assert_not_called()
    assert s.execution_log.read_recent() == []


# ---------------------------------------------------------------------------
# Legacy mapping and expiry-aware reads
# ---------------------------------------------------------------------------


def _ts(hours_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_raw(log, entries):
    """Write entries directly, bypassing record() so no rotation happens."""
    with open(log._log_file, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _legacy(tag, success, hours_ago=1.0):
    return {
        "ts": _ts(hours_ago), "tag": tag, "task": "t", "result": "trunc" * 10,
        "success": success, "elapsed_s": 1, "model": "",
    }


def _complete(tag, run_id, hours_ago=1.0, result="full result"):
    return {
        "ts": _ts(hours_ago), "tag": tag, "task": "t", "result": result,
        "success": True, "elapsed_s": 1, "model": "", "run_id": run_id,
        "status": "done",
    }


@pytest.mark.parametrize(("success", "status"), [(True, "done"), (False, "failed")])
def test_history_maps_legacy_record_to_partial_status(log, success, status):
    _write_raw(log, [_legacy("old_job", success)])

    entry = log.history("old_job")[0]

    assert entry["status"] == status
    assert entry["legacy"] is True


def test_history_does_not_fabricate_legacy_id_or_result(log):
    _write_raw(log, [_legacy("old_job", True)])

    entry = log.history("old_job")[0]

    assert entry["run_id"] is None
    assert "result" not in entry


def test_history_lists_complete_record_as_not_legacy(log):
    _write_raw(log, [_complete("job", "run-" + "1" * 32)])

    entry = log.history("job")[0]

    assert entry["run_id"] == "run-" + "1" * 32
    assert entry["status"] == "done"
    assert entry["legacy"] is False
    assert entry["ts"]


def test_get_result_returns_complete_retained_record(log):
    _write_raw(log, [_complete("job", "run-" + "2" * 32, result="whole text")])

    record = log.get_result("run-" + "2" * 32)

    assert record["result"] == "whole text"


def test_history_enforces_age_expiry_without_a_write(log):
    _write_raw(log, [_complete("job", "run-" + "3" * 32, hours_ago=50)])

    assert log.history("job") == []


def test_read_recent_enforces_age_expiry_without_a_write(log):
    _write_raw(log, [_complete("job", "run-" + "3" * 32, hours_ago=50)])

    assert log.read_recent() == []


def test_history_enforces_per_tag_cap_without_a_write(log):
    run_ids = [f"run-{i:032x}" for i in range(12)]
    _write_raw(log, [
        _complete("job", run_id, hours_ago=12 - i) for i, run_id in enumerate(run_ids)
    ])

    listed = [entry["run_id"] for entry in log.history("job")]

    assert listed == list(reversed(run_ids[2:]))


def test_full_detail_unavailable_after_age_retention(log):
    _write_raw(log, [_complete("job", "run-" + "4" * 32, hours_ago=50)])

    assert log.get_result("run-" + "4" * 32) is None


def test_full_detail_unavailable_outside_per_tag_cap(log):
    run_ids = [f"run-{i:032x}" for i in range(12)]
    _write_raw(log, [
        _complete("job", run_id, hours_ago=12 - i) for i, run_id in enumerate(run_ids)
    ])

    assert log.get_result(run_ids[0]) is None


def test_tag_reuse_after_removed_once_job_distinguishes_runs(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["schedule_type"] = "once"
    s._run_job("report")
    assert "report" not in s._jobs_meta  # once-job removed after launch
    _finish(captured[0], "once-job output", True)
    captured[0].finish_cb("report")

    s._jobs_meta["report"] = {"task": "write report", "enabled": True, "notify": False}
    s._run_job("report")
    _finish(captured[1], "new definition output", True)

    history = s.execution_log.history("report")
    assert len(history) == 2
    assert len({entry["run_id"] for entry in history}) == 2
    assert all(entry["ts"] for entry in history)
    results = {s.execution_log.get_result(e["run_id"])["result"] for e in history}
    assert results == {"once-job output", "new definition output"}


# ---------------------------------------------------------------------------
# History write failures: precedence and marker-aware recovery
# ---------------------------------------------------------------------------

_PERSIST_MARKER = "history persistence failure"


def _break_history_writes(s):
    """Make the next history write fail at the filesystem (tmp path is a dir)."""
    os.makedirs(os.path.dirname(s.execution_log._log_file), exist_ok=True)
    os.makedirs(s.execution_log._log_file + ".tmp")


def _restore_history_writes(s):
    os.rmdir(s.execution_log._log_file + ".tmp")


def test_history_write_failure_is_always_logged(tmp_path, monkeypatch, caplog):
    import logging

    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    _break_history_writes(s)

    with caplog.at_level(logging.WARNING, logger="scheduler"):
        _finish(captured[0], "complete output", True)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        _PERSIST_MARKER in message and "report" in message for message in warnings
    )


def test_persistence_marker_overrides_prior_last_error(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["last_error"] = "❌ earlier real failure"
    s._run_job("report")
    _break_history_writes(s)

    _finish(captured[0], "complete output", True)

    last_error = s._run_history["report"]["last_error"]
    assert _PERSIST_MARKER in last_error
    assert "earlier real failure" not in last_error
    assert s._jobs_meta["report"]["last_error"] == last_error


def test_history_write_failure_retains_real_outcome_in_logs(
    tmp_path, monkeypatch, caplog
):
    import logging

    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    _break_history_writes(s)

    with caplog.at_level(logging.WARNING, logger="scheduler"):
        _finish(captured[0], "❌ connection refused by upstream", False)

    assert "❌ connection refused by upstream" in caplog.text
    assert "failed" in caplog.text


def test_removed_once_job_persistence_marker_remains_in_scheduler_state(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["schedule_type"] = "once"
    s._run_job("report")
    assert "report" not in s._jobs_meta
    _break_history_writes(s)

    _finish(captured[0], "complete output", True)

    with open(s._state_file, encoding="utf-8") as fh:
        state = json.load(fh)
    assert _PERSIST_MARKER in state["history"]["report"]["last_error"]


@pytest.mark.parametrize("notify", [True, False])
def test_persistence_failure_notice_only_with_notify_true(
    tmp_path, monkeypatch, notify
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = notify
    s._run_job("report")
    _break_history_writes(s)

    if not notify:
        _finish(captured[0], "complete output", True)
        s.notify.assert_not_called()
        return
    _complete_with_notice(captured[0], "complete output", True)
    s.notify.assert_called_once()
    notice = s.notify.call_args.args[0]
    assert "not retained" in notice
    assert "Details are unavailable" in notice


def test_successful_write_clears_still_current_persistence_marker(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")
    _break_history_writes(s)
    _finish(captured[0], "lost output", True)
    assert _PERSIST_MARKER in s._run_history["report"]["last_error"]
    _restore_history_writes(s)

    _finish(captured[1], "kept output", True)

    assert s._run_history["report"]["last_error"] is None
    assert s._jobs_meta["report"].get("last_error") is None


def test_successful_write_preserves_non_marker_last_error(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["last_error"] = "❌ earlier real failure"
    s._run_history["report"] = {"last_run": None, "last_error": "❌ earlier real failure"}
    s._run_job("report")

    _finish(captured[0], "kept output", True)

    assert s._run_history["report"]["last_error"] == "❌ earlier real failure"


# ---------------------------------------------------------------------------
# Overlap policy, per-run status, asynchronous run_now, and schedule show
# ---------------------------------------------------------------------------


def _show_output(s, tag="report"):
    from builtin_tools.schedule import exec_schedule

    result = exec_schedule(s, {"action": "show", "tag": tag})
    assert result["success"] is True
    return result["output"]


def test_skip_policy_skips_trigger_while_a_run_is_active(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="skip")
    s._run_job("report")

    s._run_job("report")

    assert len(captured) == 1


def test_parallel_policy_admits_concurrent_runs(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")

    s._run_job("report")

    assert len(captured) == 2


def test_parallel_tag_stays_running_until_every_run_finishes(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")

    captured[0].finish_cb("report")
    assert s.show_job("report")["is_running"] is True

    captured[1].finish_cb("report")
    assert s.show_job("report")["is_running"] is False


def test_parallel_launch_failure_keeps_other_run_active(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s.builtin_executor.spawn_agent.side_effect = None
    s.builtin_executor.spawn_agent.return_value = {
        "success": False, "error": "spawn_agent: max_subagents cap reached (6/6).",
    }

    s._run_job("report")

    assert s.show_job("report")["is_running"] is True


def test_duplicate_finish_signal_does_not_release_another_run(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")

    captured[0].finish_cb("report")
    captured[0].finish_cb("report")

    assert s.show_job("report")["is_running"] is True


def test_run_now_returns_before_the_run_is_launched(tmp_path, monkeypatch):
    import threading

    s, _captured = _spawn_sched(tmp_path, monkeypatch)
    release = threading.Event()
    launched = threading.Event()

    def blocking_spawn(_args, options):
        launched.set()
        release.wait(timeout=5.0)
        return {"success": True, "agent_id": "sa-async"}

    s.builtin_executor.spawn_agent.side_effect = blocking_spawn
    try:
        assert s.run_now("report") == {"success": True}
        assert launched.wait(timeout=2.0)
        assert release.is_set() is False
    finally:
        release.set()


def test_schedule_show_reports_running_while_a_parallel_run_remains(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")

    captured[1].finish_cb("report")

    assert "is_running: True" in _show_output(s)


def test_schedule_show_keeps_compact_field_set(tmp_path, monkeypatch):
    s, _captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")

    assert set(s.show_job("report")) == {
        "tag", "task", "schedule", "schedule_type", "cron", "run_at", "next_run",
        "last_run", "last_error", "notify", "model", "preserve_context",
        "context_max_messages", "overlap_policy", "source", "enabled",
        "is_running", "created_at",
    }
    assert "run-" not in _show_output(s)


# ---------------------------------------------------------------------------
# Review fixes: raising spawn, atomic admission, atomic persistence, read errors
# ---------------------------------------------------------------------------


def _raising_spawn_sched(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="skip")
    s.builtin_executor.spawn_agent.side_effect = RuntimeError(
        "cannot schedule new futures after shutdown"
    )
    return s, captured


def test_raising_spawn_records_failed_run_with_admitted_id(tmp_path, monkeypatch):
    import re

    s, _captured = _raising_spawn_sched(tmp_path, monkeypatch)

    s._run_job("report")

    entry = s.execution_log.read_recent()[0]
    assert re.match(_RUN_ID_RE, entry["run_id"])
    assert entry["status"] == "failed"
    assert "cannot schedule new futures after shutdown" in entry["result"]


def test_raising_spawn_sends_direct_launch_failure_alert(tmp_path, monkeypatch):
    s, _captured = _raising_spawn_sched(tmp_path, monkeypatch)

    s._run_job("report")

    s.notify.assert_called_once()
    assert "failed to spawn" in s.notify.call_args.args[0]


def test_raising_spawn_releases_its_run(tmp_path, monkeypatch):
    s, _captured = _raising_spawn_sched(tmp_path, monkeypatch)

    s._run_job("report")

    assert s._running_jobs == set()
    assert s.show_job("report")["is_running"] is False


def test_skip_policy_job_fires_again_after_raising_spawn(tmp_path, monkeypatch):
    s, captured = _raising_spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    s.builtin_executor.spawn_agent.side_effect = (
        lambda _args, options: captured.append(options) or {"success": True}
    )

    s._run_job("report")

    assert len(captured) == 1


def test_sibling_finish_during_admission_keeps_new_parallel_run_active(
    tmp_path, monkeypatch
):
    import scheduler as scheduler_module

    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")  # run A admitted and active
    real_mint = scheduler_module._new_run_id

    def mint_while_sibling_finishes():
        # Run A finishes exactly while run B is being admitted.
        captured[0].finish_cb("report")
        return real_mint()

    with patch("scheduler._new_run_id", side_effect=mint_while_sibling_finishes):
        s._run_job("report")  # run B

    assert len(captured) == 2
    assert s.show_job("report")["is_running"] is True


def test_later_failure_marker_survives_earlier_success_recovery(tmp_path, monkeypatch):
    import threading

    s, captured = _spawn_sched(tmp_path, monkeypatch, overlap_policy="parallel")
    s._run_job("report")
    s._run_job("report")
    s._jobs_meta["report"]["last_error"] = f"{_PERSIST_MARKER}: older lost run"
    s._run_history["report"]["last_error"] = s._jobs_meta["report"]["last_error"]
    real_record = s.execution_log.record
    b_done = threading.Event()
    threads = []

    def record_then_race(**kwargs):
        persisted = real_record(**kwargs)
        if kwargs["result"] == "A persisted" and not threads:
            # B's write fails after A persisted but before A's recovery clear.
            _break_history_writes(s)
            thread = threading.Thread(
                target=lambda: (_finish(captured[1], "B lost", True), b_done.set())
            )
            threads.append(thread)
            thread.start()
            b_done.wait(timeout=0.3)
        return persisted

    with patch.object(s.execution_log, "record", side_effect=record_then_race):
        _finish(captured[0], "A persisted", True)
        threads[0].join(timeout=5.0)

    last_error = s._run_history["report"]["last_error"]
    assert last_error is not None
    assert _PERSIST_MARKER in last_error
    assert "older lost run" not in last_error


def test_last_error_mutation_waits_for_in_progress_state_save(tmp_path, monkeypatch):
    import threading

    import scheduler as scheduler_module

    s, _captured = _spawn_sched(tmp_path, monkeypatch)
    real_dump = scheduler_module.json.dump
    mutated_during_dump = []
    started = []

    def dump_with_concurrent_mutation(obj, fh, **kwargs):
        if not started:
            started.append(True)
            thread = threading.Thread(
                target=s._set_job_last_error, args=("other", {}, "concurrent value"),
            )
            thread.start()
            thread.join(timeout=0.3)
            # The history being serialized must not change while it is dumped.
            mutated_during_dump.append("other" in s._run_history)
        return real_dump(obj, fh, **kwargs)

    with patch("scheduler.json.dump", side_effect=dump_with_concurrent_mutation):
        s._save_state()
        for _ in range(50):
            if s._run_history.get("other", {}).get("last_error"):
                break
            threading.Event().wait(0.05)

    assert mutated_during_dump == [False]
    assert s._run_history["other"]["last_error"] == "concurrent value"


@pytest.fixture
def unreadable_log(log):
    """A log with retained history whose file cannot be read (EACCES)."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permission checks")
    _write_raw(log, [_complete("job", "run-" + "5" * 32)])
    with open(log._log_file, "rb") as fh:
        original = fh.read()
    os.chmod(log._log_file, 0)
    yield log, original
    os.chmod(log._log_file, 0o600)


def test_record_reports_failure_when_log_is_unreadable(unreadable_log):
    log, _original = unreadable_log

    assert log.record("job", "task", "new result", success=True) is False


def test_record_does_not_rewrite_unreadable_log(unreadable_log):
    log, original = unreadable_log

    log.record("job", "task", "new result", success=True)

    os.chmod(log._log_file, 0o600)
    with open(log._log_file, "rb") as fh:
        assert fh.read() == original


def test_unreadable_log_sets_persistence_marker(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permission checks")
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._run_job("report")
    _write_raw(s.execution_log, [_complete("report", "run-" + "6" * 32)])
    os.chmod(s.execution_log._log_file, 0)
    try:
        _finish(captured[0], "complete output", True)
    finally:
        os.chmod(s.execution_log._log_file, 0o600)

    assert _PERSIST_MARKER in s._run_history["report"]["last_error"]


def test_launch_failure_with_unset_next_run_records_unknown(tmp_path, monkeypatch):
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["report"] = {
        "task": "write report", "enabled": True, "notify": False, "_next_run": None,
    }

    s._run_job("report")

    assert s.execution_log.read_recent()[0]["next_run"] == "unknown"
    assert "Next run: unknown" in s.notify.call_args.args[0]


def test_failed_run_with_unset_next_run_records_unknown(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["_next_run"] = None
    s._run_job("report")

    _finish(captured[0], "❌ connection refused", False)

    assert s.execution_log.read_recent()[0]["next_run"] == "unknown"


# ---------------------------------------------------------------------------
# Operator prompt wiring (isolate-job-execution, design decision 2)
# ---------------------------------------------------------------------------


def test_format_for_prompt_keeps_exact_line_format(log):
    """format_for_prompt() keeps its formatting contract after operator wiring removal."""
    log.record("nightly", "task", "All good", success=True)
    entry = log.read_recent()[-1]

    result = log.format_for_prompt()

    model_part = f" [{entry['model']}]" if entry.get("model") else ""
    assert result.splitlines() == [
        "SCHEDULED JOB EXECUTION HISTORY (most recent first):",
        f"  ✅ {entry['ts']} | nightly{model_part} ({entry.get('elapsed_s', 0)}s)",
        "     → All good",
    ]


def test_main_does_not_wire_job_history_into_operator_prompt():
    """main._run must not hand scheduled history to the operator agent (formerly main.py:817).

    ``_run`` is the composition root and cannot be executed without the full
    runtime (Telegram, LLM, MCP), so this guards the wiring at source level.
    """
    import inspect

    import main

    source = inspect.getsource(main._run)

    assert "_job_history_fn" not in source
    assert "execution_log.format_for_prompt" not in source


# ---------------------------------------------------------------------------
# Notice queue interplay: truthful persistence-failure notices, restart loss
# ---------------------------------------------------------------------------


def _complete_with_notice(options, result, success):
    """Record then hand off the terminal outcome exactly as the supervisor does."""
    from sub_agent_supervisor import RunCompletion

    _finish(options, result, success)
    options.completion_cb(
        RunCompletion(
            tag=options.job_tag, status="done" if success else "failed",
            result=result, elapsed_s=3, model="m", agent_id="sa-1",
        )
    )


def test_persistence_failure_notice_is_queued_not_sent_directly(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "complete output", True)

    s.notify.assert_not_called()
    sink.assert_called_once()
    notice = sink.call_args.args[0].text
    assert "not retained" in notice
    assert "Details are unavailable" in notice


def test_persistence_failure_notice_has_no_details_button(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "complete output", True)

    assert sink.call_args.args[0].details_run_id == ""


def test_persistence_failure_run_sends_one_truthful_notice_without_details_claim(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "complete output", True)

    assert sink.call_count == 1
    notice = sink.call_args.args[0].text
    assert "Details are unavailable" in notice
    assert "complete output" not in notice
    assert s.execution_log.read_recent() == []


def test_persistence_failure_notice_for_success_carries_outcome_summary(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "complete output", True)

    notice = sink.call_args.args[0].text
    assert "✅ Scheduled job finished: report" in notice
    assert "not retained" in notice


def test_persistence_failure_notice_for_failed_run_keeps_failure_header_error_type_and_next_run(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "❌ connection refused by upstream", False)

    notice = sink.call_args.args[0]
    assert "⚠️ Scheduled job failed: report" in notice.text
    assert "Error type: connection" in notice.text
    assert "Next run: 2026-10-03T09:00:00" in notice.text
    assert "not retained" in notice.text
    assert "Details are unavailable" in notice.text
    assert notice.details_run_id == ""


def test_unretained_run_ids_are_empty_after_the_notice_is_queued(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    s.set_result_notice_sink(MagicMock())
    s._run_job("report")
    _break_history_writes(s)

    _complete_with_notice(captured[0], "complete output", True)
    captured[0].finish_cb("report")

    assert s._unretained_run_ids == set()


def test_unretained_run_ids_are_empty_when_no_notice_is_ever_requested(
    tmp_path, monkeypatch
):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    s.set_result_notice_sink(MagicMock())
    s._run_job("report")
    _break_history_writes(s)

    _finish(captured[0], "complete output", True)
    captured[0].finish_cb("report")

    assert s._unretained_run_ids == set()


def _failing_spawn_sched(tmp_path, monkeypatch, *, notify=True):
    """Scheduler whose spawn_agent always fails, with a notifying skip-policy job."""
    s = _sched(tmp_path, monkeypatch)
    s._jobs_meta["report"] = {
        "task": "write report", "enabled": True, "notify": notify,
        "overlap_policy": "skip", "_next_run": "2026-10-03T09:00:00",
    }
    executor = MagicMock()
    executor.spawn_agent = MagicMock(
        return_value={"success": False, "error": "spawn_agent: ConnectError: refused"}
    )
    s.builtin_executor = executor
    return s


def test_launch_failure_with_unwritable_history_sends_only_the_direct_alert(
    tmp_path, monkeypatch
):
    s = _failing_spawn_sched(tmp_path, monkeypatch)
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    _break_history_writes(s)

    s._run_job("report")

    s.notify.assert_called_once()
    assert "failed to spawn" in s.notify.call_args.args[0]
    sink.assert_not_called()
    assert s._unretained_run_ids == set()


def test_raising_sink_with_unwritable_history_still_alerts_and_releases_the_run(
    tmp_path, monkeypatch
):
    s = _failing_spawn_sched(tmp_path, monkeypatch)
    s.set_result_notice_sink(MagicMock(side_effect=RuntimeError("sink down")))
    _break_history_writes(s)

    s._run_job("report")

    s.notify.assert_called_once()
    assert "failed to spawn" in s.notify.call_args.args[0]
    assert "report" not in s._running_jobs
    assert "report" not in s._active_runs


def test_skip_policy_job_fires_again_after_a_launch_failure_with_raising_sink(
    tmp_path, monkeypatch
):
    s = _failing_spawn_sched(tmp_path, monkeypatch)
    s.set_result_notice_sink(MagicMock(side_effect=RuntimeError("sink down")))
    _break_history_writes(s)

    s._run_job("report")
    s._run_job("report")

    assert s.builtin_executor.spawn_agent.call_count == 2


def test_launch_failure_releases_the_run_when_the_direct_alert_raises(
    tmp_path, monkeypatch
):
    s = _failing_spawn_sched(tmp_path, monkeypatch)
    s.notify.side_effect = RuntimeError("telegram down")

    with pytest.raises(RuntimeError, match="telegram down"):
        s._run_job("report")

    assert "report" not in s._running_jobs
    assert "report" not in s._active_runs


def test_persistence_failure_with_notify_false_queues_nothing(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._run_job("report")
    _break_history_writes(s)

    _finish(captured[0], "complete output", True)

    sink.assert_not_called()
    s.notify.assert_not_called()
    assert _PERSIST_MARKER in s._run_history["report"]["last_error"]


def test_restart_loses_queued_notice_but_retains_outcome(tmp_path, monkeypatch):
    s, captured = _spawn_sched(tmp_path, monkeypatch)
    s._jobs_meta["report"]["notify"] = True
    undelivered = MagicMock()
    s.set_result_notice_sink(undelivered)
    s._run_job("report")
    full = "line\n" * 500 + "TAIL"

    _complete_with_notice(captured[0], full, True)
    undelivered.assert_called_once()
    run_id = s.execution_log.read_recent()[0]["run_id"]

    restarted = _sched(tmp_path, monkeypatch)
    fresh_sink = MagicMock()
    restarted.set_result_notice_sink(fresh_sink)

    fresh_sink.assert_not_called()
    entries = restarted.execution_log.read_recent()
    assert [(e["run_id"], e["result"], e["status"]) for e in entries] == [
        (run_id, full, "done")
    ]
