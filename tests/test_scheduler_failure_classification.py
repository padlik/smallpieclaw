"""
Tests for scheduled job failure classification and error-log enrichment.

Part of the "llm-timeout-hardening" OpenSpec change. The final section (task
7.3, provider-model-catalog) drives scheduled admission failures through the
actual production root with ``tests/root_startup_harness.py``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterator

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime import RuntimeProfile
from scheduler import JobExecutionLog, _classify_job_failure
from tests.root_startup_harness import (
    GEM_HOST,
    INVALID_SAVED,
    UNCONFIGURED_ID,
    remembered_for,
    MAIN_ID,
    OTHER_ID,
    SCHEDULED_TAG,
    RootHarness,
    ScheduledRootProbe,
    run_scheduled_root,
)


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


# ---------------------------------------------------------------------------
# Scheduled outcome classification, failure notice, and log fields
# ---------------------------------------------------------------------------

_NEXT_RUN = "2026-10-04T02:00:00"


def _notifying_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scheduler with notify=true that captures supervision options and notices."""
    from unittest.mock import MagicMock

    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"]["notify"] = True
    captured: list = []
    executor = MagicMock()

    def spawn(_args, options):
        captured.append(options)
        return {"success": True, "agent_id": "sa-1"}

    executor.spawn_agent = MagicMock(side_effect=spawn)
    s.builtin_executor = executor
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    return s, captured, sink


def _complete(options, status: str, result: str) -> None:
    """Drive the recorder then the completion callback as the supervisor does."""
    from sub_agent_supervisor import RunCompletion

    options.result_log_cb(
        tag=options.job_tag, task="summarize", result=result,
        success=status == "done", elapsed_s=4, model="m",
    )
    options.completion_cb(
        RunCompletion(
            tag=options.job_tag, status=status, result=result,
            elapsed_s=4, model="m", agent_id="sa-1",
        )
    )


@pytest.mark.parametrize(
    ("result", "error_type"),
    [
        ("❌ LLM error: TimeoutException: timed out", "timeout"),
        ("❌ LLM error: HTTPStatusError: 429 Too Many Requests", "rate_limit"),
        ("❌ LLM error: SomethingElse: bad", "unknown"),
    ],
)
def test_error_sentinel_result_is_recorded_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str, error_type: str
) -> None:
    """A supervisor-'done' result starting with ❌ is a classified failed run."""
    s, captured, _sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "done", result)

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert entry["success"] is False
    assert entry["error_type"] == error_type
    assert entry["next_run"] == _NEXT_RUN
    assert entry["result"] == result


def test_failure_notice_has_header_error_type_and_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ❌ result produces one failure notice with header, error type, next run."""
    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "done", "❌ LLM error: TimeoutException: timed out")

    sink.assert_called_once()
    notice = sink.call_args.args[0].text
    assert "⚠️ Scheduled job failed: nightly" in notice
    assert "timeout" in notice
    assert _NEXT_RUN in notice
    s.notify.assert_not_called()


def test_exception_failure_notice_has_header_error_type_and_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor 'failed' terminal gets the same classified failure notice."""
    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "failed", "Error: ConnectError: connection refused")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert entry["error_type"] == "connection"
    notice = sink.call_args.args[0].text
    assert "⚠️ Scheduled job failed: nightly" in notice
    assert "connection" in notice
    assert _NEXT_RUN in notice


def test_success_result_has_no_classification_or_failure_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful result carries no error type, next run, or failure header."""
    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "done", "Summary: all good")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "done"
    assert entry["success"] is True
    assert "error_type" not in entry
    assert "next_run" not in entry
    sink.assert_called_once()
    assert "Scheduled job failed" not in sink.call_args.args[0].text


def test_short_result_notice_expands_in_place_with_summary_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fitting result sits inside an expandable quote, summary outside it."""
    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "done", "Summary: all <good>")

    notice = sink.call_args.args[0]
    summary, _, quote = notice.text.partition("<blockquote expandable>")
    assert "Scheduled job finished: nightly" in summary
    assert quote == "Summary: all &lt;good&gt;</blockquote>"
    assert notice.details_run_id == ""


def test_long_result_notice_is_compact_and_points_to_retained_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized result is not inlined; the notice names the retained run ID."""
    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")
    long_result = "line of output\n" * 600 + "TAIL-MARKER"

    _complete(captured[0], "done", long_result)

    notice = sink.call_args.args[0]
    run_id = s.execution_log.read_recent()[0]["run_id"]
    assert notice.details_run_id == run_id
    assert run_id in notice.text
    assert "TAIL-MARKER" not in notice.text
    assert len(notice.text) < 1000


def test_pressing_details_on_long_result_notice_returns_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice's Details callback resolves, on a real Scheduler, to the full result."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from telegram_callbacks import cb_job_details
    from telegram_formatter import details_callback_data

    s, captured, sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")
    long_result = "line of output\n" * 600 + "TAIL-MARKER"
    _complete(captured[0], "done", long_result)
    notice = sink.call_args.args[0]

    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
    query = MagicMock()
    query.data = details_callback_data(notice.details_run_id)
    query.from_user = SimpleNamespace(id=1)
    query.message = message
    query.answer = AsyncMock()
    iface = SimpleNamespace(scheduler=s, _is_authorized=lambda _uid: True)
    asyncio.run(cb_job_details(iface, SimpleNamespace(callback_query=query), MagicMock()))

    document = message.reply_document.call_args.kwargs["document"].read().decode("utf-8")
    assert document.endswith(long_result)


def test_failed_log_entry_carries_run_id_and_classification_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted failure record has run_id, status, error_type and next_run."""
    import re

    s, captured, _sink = _notifying_scheduler(tmp_path, monkeypatch)
    s._run_job("nightly")

    _complete(captured[0], "done", "❌ LLM error: quota exceeded")

    entry = s.execution_log.read_recent()[0]
    assert re.match(r"^run-[0-9a-f]{32}$", entry["run_id"])
    assert (entry["status"], entry["error_type"], entry["next_run"]) == (
        "failed", "quota", _NEXT_RUN,
    )


# ---------------------------------------------------------------------------
# Terminal outcomes through the real supervisor: notify, cancel, stale timeout
# ---------------------------------------------------------------------------


class _OutcomeRunner:
    """Minimal sub-agent runner stand-in for supervisor-driven outcome tests."""

    def __init__(self, result: str = "job result", error: Exception | None = None):
        import threading
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from memory_store import ShortTermMemory

        self.agent_id = "sa-outcome"
        self.model_id = "test-model"
        self._model_id = "test-model"
        self._cancel_event = threading.Event()
        self._llm = MagicMock()
        self._agent = SimpleNamespace(max_iterations=8, _on_step=None, _trace_id=None)
        self.short_term = ShortTermMemory(max_turns=50)
        self.trace_id = None
        self._result = result
        self._error = error

    def run(self, _task):
        if self._cancel_event.is_set():
            return "[Cancelled]"
        if self._error is not None:
            raise self._error
        return self._result

    def notify_fn(self, _msg) -> None:
        raise AssertionError("the supervisor must not send result notices itself")

    def close(self) -> None:
        pass


def _drive_scheduled_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_builtin_executor,
    terminal: str,
    *,
    notify: bool,
    history_writable: bool = True,
):
    """Run one scheduled job through the real executor/supervisor to ``terminal``."""
    import os
    from unittest.mock import MagicMock, patch

    from sub_agent_registry import SubAgentRegistry

    runner = _OutcomeRunner(
        result="❌ LLM error: TimeoutException: timed out"
        if terminal == "error_sentinel" else "job result",
        error=RuntimeError("boom") if terminal == "failed" else None,
    )
    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"]["notify"] = notify
    executor = make_builtin_executor(
        sub_agent_factory=lambda **_kwargs: runner, data_dir=str(tmp_path),
    )
    s.builtin_executor = executor
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    if not history_writable:
        os.makedirs(os.path.dirname(s.execution_log._log_file), exist_ok=True)
        os.makedirs(s.execution_log._log_file + ".tmp")
    local_reg = SubAgentRegistry()

    def run_now(fn, *_args, **_kwargs):
        record = local_reg.get(runner.agent_id)
        assert record is not None
        if terminal in ("cancelled", "timeout_cancelled"):
            if terminal == "timeout_cancelled":
                record._timeout_cancelled = True
            record.cancel()
        fn()
        return MagicMock()

    with patch("sub_agent_registry.get_registry", return_value=local_reg), \
         patch.object(executor._supervisor._pool, "submit", side_effect=run_now):
        s._run_job("nightly")
    return s, sink


@pytest.mark.parametrize(
    ("terminal", "status", "header"),
    [
        ("done", "done", "Scheduled job finished: nightly"),
        ("error_sentinel", "failed", "⚠️ Scheduled job failed: nightly"),
        ("failed", "failed", "⚠️ Scheduled job failed: nightly"),
        ("cancelled", "cancelled", "Scheduled job cancelled: nightly"),
    ],
)
def test_notify_true_terminal_outcome_enters_notice_sink_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
    terminal: str, status: str, header: str,
) -> None:
    """Explicit completion, failure and cancellation each queue exactly one notice."""
    s, sink = _drive_scheduled_run(
        tmp_path, monkeypatch, make_builtin_executor, terminal, notify=True,
    )

    sink.assert_called_once()
    assert header in sink.call_args.args[0].text
    s.notify.assert_not_called()
    assert s.execution_log.read_recent()[0]["status"] == status


@pytest.mark.parametrize(
    ("terminal", "status"),
    [
        ("done", "done"),
        ("error_sentinel", "failed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("timeout_cancelled", "cancelled"),
    ],
)
def test_notify_false_suppresses_every_notice_but_records_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
    terminal: str, status: str,
) -> None:
    """notify=false: no queued or direct notice for any terminal; history is kept."""
    s, sink = _drive_scheduled_run(
        tmp_path, monkeypatch, make_builtin_executor, terminal, notify=False,
    )

    sink.assert_not_called()
    s.notify.assert_not_called()
    assert s.execution_log.read_recent()[0]["status"] == status


def test_timeout_cancelled_run_keeps_stale_suppression_but_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
) -> None:
    """A get_agent_result timeout cancel queues nothing even with notify=true."""
    s, sink = _drive_scheduled_run(
        tmp_path, monkeypatch, make_builtin_executor, "timeout_cancelled", notify=True,
    )

    sink.assert_not_called()
    s.notify.assert_not_called()
    assert s.execution_log.read_recent()[0]["status"] == "cancelled"


def test_timeout_cancelled_run_with_unwritable_history_queues_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
) -> None:
    """The not-retained notice honours the stale timeout-cancel suppression."""
    s, sink = _drive_scheduled_run(
        tmp_path, monkeypatch, make_builtin_executor, "timeout_cancelled",
        notify=True, history_writable=False,
    )

    sink.assert_not_called()
    s.notify.assert_not_called()
    assert s._unretained_run_ids == set()


def test_unwritable_history_run_queues_one_outcome_notice_and_clears_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor,
) -> None:
    """Through the real supervisor a failed write yields one outcome + not-retained notice."""
    s, sink = _drive_scheduled_run(
        tmp_path, monkeypatch, make_builtin_executor, "error_sentinel",
        notify=True, history_writable=False,
    )

    sink.assert_called_once()
    text = sink.call_args.args[0].text
    assert "⚠️ Scheduled job failed: nightly" in text
    assert "not retained" in text
    assert s._unretained_run_ids == set()


@pytest.mark.parametrize("status", ["done", "failed"])
def test_compact_notice_with_a_huge_tag_still_fits_the_message_limit(status: str) -> None:
    """A very long job tag is bounded in every summary so the notice fits 4096."""
    from telegram_formatter import (
        TELEGRAM_RENDERED_LIMIT,
        build_run_result_notice,
        rendered_length,
    )

    tag = "t" * 4000
    run_id = "run-" + "a" * 32

    notice = build_run_result_notice(
        tag, status, "line\n" * 5000, "timeout" if status == "failed" else "",
        "2026-10-04T02:00:00" if status == "failed" else "", run_id,
    )

    assert rendered_length(notice.text) <= TELEGRAM_RENDERED_LIMIT
    assert notice.details_run_id == run_id


def test_empty_result_notice_with_a_huge_tag_still_fits_the_message_limit() -> None:
    """An empty-result summary bounds the tag rendering too."""
    from telegram_formatter import (
        TELEGRAM_RENDERED_LIMIT,
        build_run_result_notice,
        rendered_length,
    )

    notice = build_run_result_notice("t" * 6000, "done", "", "", "", "run-" + "a" * 32)

    assert rendered_length(notice.text) <= TELEGRAM_RENDERED_LIMIT


def test_empty_task_reminder_stays_direct_with_sink_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-task reminders keep their direct path and never enter the idle queue."""
    from unittest.mock import MagicMock

    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"].update({"task": "", "notify": True})
    sink = MagicMock()
    s.set_result_notice_sink(sink)

    s._run_job("nightly")

    s.notify.assert_called_once()
    assert "Reminder" in s.notify.call_args.args[0]
    sink.assert_not_called()


def test_long_running_warning_stays_direct_with_sink_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long-running agent warnings keep their direct path and never enter the queue."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    s = _fallback_scheduler(tmp_path, monkeypatch)
    sink = MagicMock()
    s.set_result_notice_sink(sink)
    s._warn_minutes = 0
    record = SimpleNamespace(
        agent_id="sa-long", elapsed_seconds=999, label="nightly", model="m",
        task_preview="work", elapsed_str=lambda: "16m",
    )
    registry = SimpleNamespace(list_active=lambda: [record])

    with patch("sub_agent_registry.get_registry", return_value=registry):
        s._check_long_running_agents()

    s.notify.assert_called_once()
    assert "Sub-agent running for" in s.notify.call_args.args[0]
    sink.assert_not_called()


# ---------------------------------------------------------------------------
# Task 12.22: pre-pin launch records carry only bounded selector evidence
# ---------------------------------------------------------------------------

_FAKE_SELECTOR_SECRET = "QZXSENTINEL7f3aRECORD" + "z9" * 60
_FAKE_PLACEHOLDER = "***REDACTED***"


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(123, id="int"),
        pytest.param({"QZXkey": "QZXvalue"}, id="dict"),
        pytest.param(__import__("datetime").datetime(2026, 1, 2, 3, 4), id="datetime"),
    ],
)
def test_pre_pin_launch_record_stores_label_for_non_string_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: object
) -> None:
    """The fail-closed record's model is the classification label, never the raw object."""
    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"]["model"] = model

    s._run_job("nightly")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert entry["model"] == "malformed (not a string)"
    assert s._jobs_meta["nightly"]["model"] == model


def test_pre_pin_launch_record_bounds_and_redacts_string_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected fake redactor runs before the 256-char bound on the stored model."""
    from unittest.mock import MagicMock

    from scheduler import Scheduler
    from xdg import xdg_paths

    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    s = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
        selector_redactor=lambda text: text.replace(_FAKE_SELECTOR_SECRET, _FAKE_PLACEHOLDER),
    )
    head = "provider/" + "b" * 200
    raw = head + _FAKE_SELECTOR_SECRET + "-public-suffix" * 50
    s._jobs_meta["nightly"] = {
        "task": "summarize", "enabled": True, "notify": False,
        "_next_run": _NEXT_RUN, "model": raw,
    }

    s._run_job("nightly")

    stored = s.execution_log.read_recent()[0]["model"]
    assert len(stored) <= 256
    assert stored.startswith(head + _FAKE_PLACEHOLDER)
    for fragment in (_FAKE_SELECTOR_SECRET, _FAKE_SELECTOR_SECRET[:16], _FAKE_SELECTOR_SECRET[:8]):
        assert fragment not in stored
    assert s._jobs_meta["nightly"]["model"] == raw


_SHORT_SECRET = "QZXSENTINEL7f3aSHORT-token"


def _fake_redactor(text: str) -> str:
    """Fake vault redaction: replace the short fabricated sentinel only."""
    return text.replace(_SHORT_SECRET, _FAKE_PLACEHOLDER)


def _redacting_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redactor=None):
    """``_fallback_scheduler`` equivalent constructed with an injected redactor."""
    from unittest.mock import MagicMock

    from scheduler import Scheduler
    from xdg import xdg_paths

    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    s = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
        selector_redactor=redactor,
    )
    s._jobs_meta["nightly"] = {
        "task": "summarize", "enabled": True, "notify": False, "_next_run": _NEXT_RUN,
    }
    return s


def _failing_spawn_executor():
    from unittest.mock import MagicMock

    executor = MagicMock()
    executor.spawn_agent = MagicMock(return_value={
        "success": False, "error": "spawn_agent: ConnectError: connection refused",
    })
    return executor


def test_pre_pin_launch_record_empty_model_is_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence comes from the saved value: ``""`` records ``blank``, not ``inherit``."""
    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"]["model"] = ""

    s._run_job("nightly")

    assert s.execution_log.read_recent()[0]["model"] == "blank"


def test_bad_fields_launch_record_stores_label_for_non_string_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invalid-field launch failure records the label, never the raw int."""
    s = _fallback_scheduler(tmp_path, monkeypatch)
    s._jobs_meta["nightly"].update({"notify": "nope", "model": 123})

    s._run_job("nightly")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert "invalid job configuration" in entry["result"]
    assert entry["model"] == "malformed (not a string)"


def test_spawn_failure_record_stores_label_for_dict_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed spawn with a dict model writes its record with the label only."""
    s = _fallback_scheduler(tmp_path, monkeypatch)
    s.builtin_executor = _failing_spawn_executor()
    s._jobs_meta["nightly"]["model"] = {"QZXkey": "QZXvalue"}

    s._run_job("nightly")

    entry = s.execution_log.read_recent()[0]
    assert entry["status"] == "failed"
    assert entry["model"] == "malformed (not a string)"


def test_spawn_failure_record_redacts_short_secret_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-256 secret-bearing selector is redacted by the injected redactor."""
    s = _redacting_scheduler(tmp_path, monkeypatch, _fake_redactor)
    s.builtin_executor = _failing_spawn_executor()
    s._jobs_meta["nightly"]["model"] = f"provider/{_SHORT_SECRET}"

    s._run_job("nightly")

    stored = s.execution_log.read_recent()[0]["model"]
    assert stored == f"provider/{_FAKE_PLACEHOLDER}"
    assert _SHORT_SECRET[:8] not in stored


def test_scheduler_exposes_injected_selector_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _redacting_scheduler(tmp_path, monkeypatch, _fake_redactor)

    assert s.selector_redactor is _fake_redactor


def test_scheduler_default_selector_redactor_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _fallback_scheduler(tmp_path, monkeypatch)

    assert s.selector_redactor is None


# ---------------------------------------------------------------------------
# Task 7.3: actual-root scheduled admission failures
# ---------------------------------------------------------------------------
# main._run → real Scheduler._run_job(manual=True) → root spawn/supervisor →
# main-produced factory → ModelRegistry.admit_scheduled. Every job has
# notify=false, so any alert below is the launch-failure alert.


@pytest.fixture
def scheduled_root(
    tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> Iterator[RootHarness]:
    """Real-scheduler root; production assertions precede fixture hygiene."""
    caplog.set_level(logging.WARNING, logger="model_registry")
    root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
    try:
        yield root
    finally:
        root.release_leftovers()


def _selector_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The registry's emitted scheduled-selector WARN records."""
    return [r for r in caplog.records if getattr(r, "scheduled_selector_warning", False)]


def _assert_failed_launch(
    root: RootHarness, probe: ScheduledRootProbe, *, evidence: str, error_text: str,
) -> None:
    """The admission failure is a recorded launch failure of the already admitted run."""
    after = probe.after
    (entry,) = probe.at_admission
    assert entry.running is True and len(entry.run_ids) == 1
    assert entry.persisted_last_run and entry.records == []
    (run_id,) = entry.run_ids
    (record,) = after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (
        run_id, "failed", False, evidence,
    )
    assert error_text in record["result"]
    assert after.state["last_run"] == entry.persisted_last_run
    assert after.state["last_error"] == record["result"]
    (alert,) = after.notices
    assert f"Job {SCHEDULED_TAG} failed to spawn" in alert
    assert after.tracked is False and after.active_children == []
    assert probe.pins == [] and probe.creates == [] and probe.registered == [] and probe.loop_calls == []
    assert after.llm_clients == 0 and after.http_clients == 0 and after.provider_requests == []
    assert after.view == probe.view_before and after.settings == probe.settings_before
    assert after.toml == probe.toml_before
    assert root.unexpected == [] and root.open_clients_at_exit == []


_INACTIVE_MAIN_SELECTORS = [
    pytest.param(None, None, "inherit", id="absent"),
    pytest.param(f'"{OTHER_ID}"', OTHER_ID, OTHER_ID, id="explicit-known"),
    pytest.param('"acme/claude-zzz"', "acme/claude-zzz", "acme/claude-zzz", id="unknown"),
    pytest.param("42", 42, "malformed (not a string)", id="integer"),
]


@pytest.mark.parametrize(("literal", "forwarded", "evidence"), _INACTIVE_MAIN_SELECTORS)
@pytest.mark.parametrize(
    "saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["no-saved-selection", "saved-model-missing"],
)
def test_root_scheduled_job_without_valid_main_fails_before_model_work(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    saved_default: str | None, literal: str | None, forwarded: object, evidence: str,
) -> None:
    """No valid Main fails every selector — a valid explicit one included — before
    selector warnings, credentials, runtime, clients or provider I/O."""
    scheduled_root.serve_provider_chat()

    probe = run_scheduled_root(scheduled_root, monkeypatch, literal, saved_default=saved_default)

    before = probe.view_before
    assert before.active_model_id is None and before.remembered_model_id == remembered_for(saved_default)
    assert OTHER_ID in {model.id for model in before.models}
    assert probe.admits == {"admit": [], "admit_scheduled": [forwarded]}
    assert probe.codes == ["no_active_selection"] and probe.resolved == []
    assert _selector_warnings(caplog) == []
    _assert_failed_launch(
        scheduled_root, probe, evidence=evidence, error_text="model registry no active selection",
    )


@pytest.mark.parametrize(
    ("selector", "missing_key", "provider", "warnings"),
    [
        pytest.param(OTHER_ID, "KEY_GEM", "gem", 0, id="known-selector"),
        pytest.param("ghost/claude-zzz", "KEY_ACME", "acme", 1, id="inherited-main"),
    ],
)
def test_root_scheduled_credential_failure_stays_on_effective_model(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    selector: str, missing_key: str, provider: str, warnings: int,
) -> None:
    """Once the effective model is chosen, its credential failure fails the job:
    one admission, one resolution, no other provider substituted or retried."""
    scheduled_root.serve_provider_chat()

    probe = run_scheduled_root(
        scheduled_root, monkeypatch, f'"{selector}"',
        before_fire=lambda _root: monkeypatch.delenv(missing_key),
    )

    assert probe.view_before.active_model_id == MAIN_ID
    assert probe.admits == {"admit": [], "admit_scheduled": [selector]}
    assert probe.codes == ["credential_resolution_failed"] and probe.resolved == [provider]
    assert len(_selector_warnings(caplog)) == warnings
    _assert_failed_launch(
        scheduled_root, probe, evidence=selector,
        error_text="model registry credential resolution failed",
    )


def test_root_scheduled_provider_failure_is_one_attempt_without_fallback(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider outage on the pinned model is that run's normal failure: one
    request to it, no request to Main's provider, no second admission or replay."""
    scheduled_root.serve_provider_chat(failing_chat_hosts=(GEM_HOST,))

    probe = run_scheduled_root(scheduled_root, monkeypatch, f'"{OTHER_ID}"')

    after = probe.after
    assert probe.view_before.active_model_id == MAIN_ID
    assert probe.admits == {"admit": [], "admit_scheduled": [OTHER_ID]}
    assert probe.codes == [] and [pin.model.id for pin in probe.pins] == [OTHER_ID]
    assert probe.resolved == ["gem"]
    ((profile, options),) = probe.creates
    assert profile is RuntimeProfile.SCHEDULED_AGENT and options.run_model is probe.pins[0]
    assert len(probe.loop_calls) == 1
    assert [(r.method, r.url.host, r.url.path) for r in after.provider_requests] == [
        ("POST", GEM_HOST, "/v1beta/models/gem-1:generateContent"),
    ]
    (entry,) = probe.at_admission
    (run_id,) = entry.run_ids
    (record,) = after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (
        run_id, "failed", False, OTHER_ID,
    )
    assert after.notices == []
    assert after.tracked is False and after.active_children == []
    assert _selector_warnings(caplog) == []
    assert after.view == probe.view_before and after.settings == probe.settings_before
    assert after.toml == probe.toml_before
    assert after.llm_clients == 1 and after.llm_closed == [True] and after.http_open == []
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []


_GEM_CHAT_PATH = "/v1beta/models/gem-1:generateContent"


@pytest.mark.parametrize(
    ("messages", "provider_context_error", "sent", "error_text"),
    [
        pytest.param(
            [{"role": "user", "content": "word " * 60_000}], False, 0,
            "rejected by local budget", id="context-exceeds-local-budget",
        ),
        pytest.param(
            [{"role": "user", "content": "ping"}], True, 1,
            "context_length_exceeded", id="context-rejected-by-provider",
        ),
    ],
)
def test_root_scheduled_pinned_capability_or_context_failure_has_no_fallback(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    messages: list[dict], provider_context_error: bool, sent: int, error_text: str,
) -> None:
    """After the pin, exceeded context fails that run normally: no Main
    substitution, no second admission, no cross-model request."""
    scheduled_root.serve_provider_chat()
    if provider_context_error:
        served = scheduled_root._route

        def route(request: httpx.Request) -> httpx.Response:
            """Reject the pinned model's chat as context-length exceeded."""
            if request.method == "POST" and request.url.path == _GEM_CHAT_PATH:
                request.read()
                scheduled_root.requests.append(request)
                return httpx.Response(
                    400, json={"error": {"message": "context_length_exceeded: input too long"}},
                )
            return served(request)

        monkeypatch.setattr(scheduled_root, "_route", route)

    probe = run_scheduled_root(scheduled_root, monkeypatch, f'"{OTHER_ID}"', messages=messages)

    after = probe.after
    assert probe.view_before.active_model_id == MAIN_ID
    assert probe.admits == {"admit": [], "admit_scheduled": [OTHER_ID]}
    assert probe.codes == [] and [pin.model.id for pin in probe.pins] == [OTHER_ID]
    assert probe.resolved == ["gem"] and len(probe.creates) == 1 and len(probe.loop_calls) == 1
    assert [(r.url.host, r.url.path) for r in after.provider_posts] == [(GEM_HOST, _GEM_CHAT_PATH)] * sent
    (entry,) = probe.at_admission
    (run_id,) = entry.run_ids
    (record,) = after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (
        run_id, "failed", False, OTHER_ID,
    )
    assert error_text in record["result"]
    assert after.notices == []
    assert after.tracked is False and after.active_children == []
    assert _selector_warnings(caplog) == []
    assert after.view == probe.view_before and after.settings == probe.settings_before
    assert after.toml == probe.toml_before
    assert after.llm_clients == 1 and after.llm_closed == [True] and after.http_open == []
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []
