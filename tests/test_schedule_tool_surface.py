"""Tests for the upgraded ``schedule`` built-in tool surface.

Covers:
- ``exec_schedule`` new ``show``/``modify`` branches and updated ``run_now``
- depth-guard allowlist
- ``overlap_policy`` validation
- ``Scheduler.show_job`` / ``Scheduler.modify_job`` semantics
- persistence round-trip and manual-override behavior
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builtin_tools.schedule import exec_schedule
from scheduler import Scheduler
from tests import scheduler_legacy_fixtures as legacy_fx
from xdg import xdg_paths


def _make_scheduler(tmp_path: Path, monkeypatch, config_path: Path | None = None) -> Scheduler:
    """Return a Scheduler whose persistence is rooted under *tmp_path*."""
    if config_path is None:
        config_path = tmp_path / "scheduler.toml"
        config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"scheduled_max_iterations": 10}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
    )


def _add_report_job(s: Scheduler) -> None:
    """Add a representative cron job used by several scenarios."""
    s.add_job(
        tag="report",
        schedule_type="cron",
        task="original task",
        cron="0 */6 * * *",
        notify=False,
        model="kimi-k2.5:cloud",
        preserve_context=True,
        context_max_messages=80,
        overlap_policy="parallel",
    )


# ---------------------------------------------------------------------------
# 4.1 / 4.2 — exec_schedule show and modify via mocked Scheduler
# ---------------------------------------------------------------------------


class TestExecScheduleShow:
    """``show`` returns full metadata or an unknown-tag error."""

    def test_show_returns_full_metadata(self) -> None:
        scheduler = MagicMock()
        scheduler.selector_redactor = None
        scheduler.show_job.return_value = {
            "tag": "report",
            "task": "do work",
            "schedule": "cron: 0 */6 * * * (every 6h)",
            "schedule_type": "cron",
            "cron": "0 */6 * * *",
            "run_at": None,
            "next_run": "2026-09-11T12:00:00",
            "last_run": "2026-09-11T06:00:00",
            "last_error": None,
            "notify": False,
            "model": "kimi-k2.5:cloud",
            "preserve_context": True,
            "context_max_messages": 80,
            "overlap_policy": "parallel",
            "source": "dynamic",
            "enabled": True,
            "is_running": False,
            "created_at": "2026-09-10T00:00:00",
        }
        result = exec_schedule(scheduler, {"action": "show", "tag": "report"})
        assert result["success"] is True
        out = result["output"]
        assert "tag: report" in out
        assert "task: do work" in out
        assert "schedule: cron: 0 */6 * * * (every 6h)" in out
        assert "cron: 0 */6 * * *" in out
        assert "run_at:" not in out  # not emitted for cron jobs
        assert "schedule_type: cron" in out
        assert "next_run: 2026-09-11T12:00:00" in out
        assert "enabled: True" in out
        assert "is_running: False" in out
        assert "source: dynamic" in out
        assert "notify: False" in out
        assert "model: kimi-k2.5:cloud" in out
        assert "preserve_context: True" in out
        assert "context_max_messages: 80" in out
        assert "overlap_policy: parallel" in out
        assert "last_run: 2026-09-11T06:00:00" in out
        assert "last_error: none" in out
        assert "created_at: 2026-09-10T00:00:00" in out

    def test_show_once_job_includes_run_at(self) -> None:
        scheduler = MagicMock()
        scheduler.show_job.return_value = {
            "tag": "reminder",
            "task": "message",
            "schedule": "once at 18:30",
            "schedule_type": "once",
            "cron": None,
            "run_at": "18:30",
            "next_run": None,
            "last_run": None,
            "last_error": None,
            "notify": True,
            "model": None,
            "preserve_context": False,
            "context_max_messages": 50,
            "overlap_policy": "skip",
            "source": "dynamic",
            "enabled": True,
            "is_running": False,
            "created_at": "2026-09-10T00:00:00",
        }
        result = exec_schedule(scheduler, {"action": "show", "tag": "reminder"})
        assert result["success"] is True
        assert "run_at: 18:30" in result["output"]
        assert "cron:" not in result["output"]

    def test_show_unknown_tag_lists_known_tags(self) -> None:
        scheduler = MagicMock()
        scheduler.show_job.return_value = None
        scheduler.list_jobs.return_value = [
            {"tag": "report", "schedule": "cron: 0 */6 * * *", "enabled": True},
            {"tag": "reminder", "schedule": "once at 18:30", "enabled": True},
        ]
        result = exec_schedule(scheduler, {"action": "show", "tag": "missing"})
        assert result["success"] is False
        assert "Job 'missing' not found. Known jobs: report, reminder" in result["error"]

    def test_show_requires_tag(self) -> None:
        scheduler = MagicMock()
        result = exec_schedule(scheduler, {"action": "show"})
        assert result["success"] is False
        assert "tag is required for show" in result["error"]


class TestExecScheduleModify:
    """``modify`` partial-patch semantics and error mapping."""

    def test_modify_task_preserves_other_fields(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        result = exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "task": "new task text"},
        )
        assert result["success"] is True
        assert result["output"] == "Job 'report' modified."
        call = scheduler.modify_job.call_args
        assert call.args[0] == "report"
        assert call.args[1]["task"] == "new task text"

    def test_modify_forwards_enabled_to_scheduler(self) -> None:
        """Review fix: enabled is forwarded so Scheduler returns the pause/resume error."""
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {
            "success": False,
            "error": "schedule modify: 'enabled' is controlled by the pause and resume actions...",
        }
        result = exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "enabled": False},
        )
        assert result["success"] is False
        call = scheduler.modify_job.call_args
        assert call.args[1]["enabled"] is False

    def test_modify_unknown_tag_error(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {
            "success": False,
            "error": "Job 'report' not found. Known jobs: none",
        }
        result = exec_schedule(scheduler, {"action": "modify", "tag": "report", "task": "x"})
        assert result["success"] is False
        assert "Job 'report' not found" in result["error"]
        assert result.get("recoverable") is True
        assert result.get("error_type") == "validation_error"

    def test_modify_overlap_policy_validation(self) -> None:
        scheduler = MagicMock()
        result = exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "overlap_policy": "sometimes"},
        )
        assert result["success"] is False
        assert "schedule modify: overlap_policy must be one of: skip, parallel." in result["error"]
        assert result.get("recoverable") is True
        assert result.get("error_type") == "validation_error"
        scheduler.modify_job.assert_not_called()

    def test_modify_passes_context_max_messages_and_overlap_policy(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {
                "action": "modify",
                "tag": "report",
                "context_max_messages": 20,
                "overlap_policy": "parallel",
            },
        )
        updates = scheduler.modify_job.call_args.args[1]
        assert updates["context_max_messages"] == 20
        assert updates["overlap_policy"] == "parallel"

    def test_modify_string_context_max_messages_coerced(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "context_max_messages": "20"},
        )
        assert scheduler.modify_job.call_args.args[1]["context_max_messages"] == 20


# ---------------------------------------------------------------------------
# 4.2 — list output remains compact
# ---------------------------------------------------------------------------


class TestExecScheduleListUnchanged:
    """``list`` output stays compact and omits the new full-metadata fields."""

    def test_list_output_unchanged(self) -> None:
        scheduler = MagicMock()
        scheduler.list_jobs.return_value = [
            {
                "tag": "report",
                "schedule": "cron: 0 */6 * * * (every 6h)",
                "schedule_type": "cron",
                "cron": "0 */6 * * *",
                "next_run": "2026-09-11T12:00:00",
                "enabled": True,
                "last_run": None,
                "last_error": None,
                "task": "do work",
                "notify": True,
                "source": "dynamic",
                "model": "kimi-k2.5:cloud",
                "preserve_context": False,
                "is_running": False,
            }
        ]
        result = exec_schedule(scheduler, {"action": "list"})
        assert result["success"] is True
        out = result["output"]
        assert "report" in out
        assert "cron: 0 */6 * * *" in out
        assert "Task: do work" in out
        assert "context_max_messages" not in out
        assert "overlap_policy" not in out
        assert "created_at" not in out
        assert "source" not in out
        assert "model" not in out

    def test_list_empty(self) -> None:
        scheduler = MagicMock()
        scheduler.list_jobs.return_value = []
        result = exec_schedule(scheduler, {"action": "list"})
        assert result["success"] is True
        assert result["output"] == "No scheduled jobs."


# ---------------------------------------------------------------------------
# 4.3 — depth-guard allowlist
# ---------------------------------------------------------------------------


class TestExecScheduleDepthGuard:
    """Sub-agents may only read; main agents may mutate."""

    def test_sub_agent_read_actions_succeed(self) -> None:
        scheduler = MagicMock()
        scheduler.list_jobs.return_value = []
        scheduler.show_job.return_value = {
            "tag": "x",
            "task": "t",
            "schedule": "once at 18:30",
            "schedule_type": "once",
            "cron": None,
            "run_at": "18:30",
            "next_run": None,
            "last_run": None,
            "last_error": None,
            "notify": True,
            "model": None,
            "preserve_context": False,
            "context_max_messages": 50,
            "overlap_policy": "skip",
            "source": "dynamic",
            "enabled": True,
            "is_running": False,
            "created_at": "2026-09-10T00:00:00",
        }
        assert exec_schedule(scheduler, {"action": "list"}, caller_depth=1)["success"] is True
        assert exec_schedule(scheduler, {"action": "show", "tag": "x"}, caller_depth=1)["success"] is True

    @pytest.mark.parametrize("action", ["add", "remove", "pause", "resume", "modify", "run_now"])
    def test_sub_agent_mutations_blocked(self, action: str) -> None:
        scheduler = MagicMock()
        result = exec_schedule(scheduler, {"action": action, "tag": "x"}, caller_depth=1)
        assert result["success"] is False
        assert "schedule: sub-agents cannot modify scheduled jobs" in result["error"]
        assert f"(action '{action}' blocked at depth 1)" in result["error"]
        assert "Only 'list' and 'show' are available to sub-agents." in result["error"]
        assert result.get("error_type") == "fundamentally_wrong_approach"
        assert result.get("recoverable") is False

    def test_main_agent_all_eight_actions_dispatch(self) -> None:
        """Every valid action is accepted at depth 0 (mocked Scheduler)."""
        scheduler = MagicMock()
        scheduler.list_jobs.return_value = []
        scheduler.show_job.return_value = {
            "tag": "x",
            "task": "t",
            "schedule": "once at 18:30",
            "schedule_type": "once",
            "cron": None,
            "run_at": "18:30",
            "next_run": None,
            "last_run": None,
            "last_error": None,
            "notify": True,
            "model": None,
            "preserve_context": False,
            "context_max_messages": 50,
            "overlap_policy": "skip",
            "source": "dynamic",
            "enabled": True,
            "is_running": False,
            "created_at": "2026-09-10T00:00:00",
        }
        scheduler.add_job.return_value = {"success": True}
        scheduler.remove_job.return_value = True
        scheduler.pause_job.return_value = True
        scheduler.resume_job.return_value = True
        scheduler.modify_job.return_value = {"success": True}
        scheduler.run_now.return_value = {"success": True}

        for action in ("list", "show", "add", "remove", "pause", "resume", "modify", "run_now"):
            args = {"action": action, "tag": "x", "task": "t"}
            result = exec_schedule(scheduler, args, caller_depth=0)
            assert result["success"] is True, f"action {action} failed: {result}"


# ---------------------------------------------------------------------------
# 4.4 — run_now output and overlap validation
# ---------------------------------------------------------------------------


class TestExecScheduleRunNow:
    """``run_now`` semantics: supervised sub-agent, paused override, once removal."""

    def test_run_now_output_enabled_cron(self) -> None:
        scheduler = MagicMock()
        scheduler.run_now.return_value = {"success": True}
        scheduler.show_job.return_value = {
            "tag": "report",
            "enabled": True,
            "schedule_type": "cron",
            "overlap_policy": "parallel",
        }
        result = exec_schedule(scheduler, {"action": "run_now", "tag": "report"})
        assert result["success"] is True
        out = result["output"]
        assert "triggered to run now through the standard scheduled-run machinery" in out
        assert "supervised scheduled-run sub-agent" in out
        assert "delivered per the job's notify setting" in out
        assert "Follow the outcome with 'schedule show report'." in out
        assert "overlap_policy (parallel)" in out
        assert "remains paused" not in out
        assert "removed after the run" not in out

    def test_run_now_output_paused_job(self) -> None:
        scheduler = MagicMock()
        scheduler.run_now.return_value = {"success": True}
        scheduler.show_job.return_value = {
            "tag": "report",
            "enabled": False,
            "schedule_type": "cron",
            "overlap_policy": "skip",
        }
        result = exec_schedule(scheduler, {"action": "run_now", "tag": "report"})
        out = result["output"]
        assert "The job remains paused (enabled=false); scheduled firings will still skip it." in out
        assert "overlap_policy (skip)" in out

    def test_run_now_output_once_job(self) -> None:
        scheduler = MagicMock()
        scheduler.run_now.return_value = {"success": True}
        scheduler.show_job.return_value = {
            "tag": "reminder",
            "enabled": True,
            "schedule_type": "once",
            "overlap_policy": "skip",
        }
        result = exec_schedule(scheduler, {"action": "run_now", "tag": "reminder"})
        out = result["output"]
        assert "This is a once job: it will be removed after the run completes." in out

    def test_run_now_unknown_tag_lists_known_tags(self) -> None:
        scheduler = MagicMock()
        scheduler.run_now.return_value = {
            "success": False,
            "error": "Job 'missing' not found. Known jobs: report",
        }
        result = exec_schedule(scheduler, {"action": "run_now", "tag": "missing"})
        assert result["success"] is False
        assert "Job 'missing' not found. Known jobs: report" in result["error"]


class TestExecScheduleOverlapValidation:
    """overlap_policy is validated for add and modify before reaching Scheduler."""

    def test_add_invalid_overlap_policy(self) -> None:
        scheduler = MagicMock()
        result = exec_schedule(
            scheduler,
            {"action": "add", "tag": "longjob", "task": "heavy work", "cron": "0 */2 * * *", "overlap_policy": "sometimes"},
        )
        assert result["success"] is False
        assert "schedule add: overlap_policy must be one of: skip, parallel." in result["error"]
        assert result.get("recoverable") is True
        scheduler.add_job.assert_not_called()

    def test_add_parallel_overlap_policy_passes(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job.return_value = {"success": True}
        result = exec_schedule(
            scheduler,
            {"action": "add", "tag": "longjob", "task": "heavy work", "cron": "0 */2 * * *", "overlap_policy": "parallel"},
        )
        assert result["success"] is True
        assert scheduler.add_job.call_args.kwargs["overlap_policy"] == "parallel"

    def test_add_passes_context_max_messages(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "add", "tag": "j", "task": "t", "cron": "0 * * * *", "context_max_messages": 42},
        )
        assert scheduler.add_job.call_args.kwargs["context_max_messages"] == 42

    def test_add_string_context_max_messages_coerced(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "add", "tag": "j", "task": "t", "cron": "0 * * * *", "context_max_messages": "42"},
        )
        assert scheduler.add_job.call_args.kwargs["context_max_messages"] == 42


# ---------------------------------------------------------------------------
# 4.5 — real Scheduler unit tests
# ---------------------------------------------------------------------------


class TestSchedulerShowJob:
    """``show_job`` returns full metadata or None for unknown tags."""

    def test_show_job_full_metadata(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        _add_report_job(s)
        # _register_job is invoked by resume/start; add_job does not register.
        s._register_job("report", s._jobs_meta["report"])
        s._jobs_meta["report"]["created_at"] = "2026-09-10T00:00:00+00:00"
        s._jobs_meta["report"]["last_run"] = "2026-09-11T06:00:00+00:00"
        s._jobs_meta["report"]["last_error"] = "boom"

        job = s.show_job("report")
        assert job is not None
        assert job["tag"] == "report"
        assert job["task"] == "original task"
        assert job["cron"] == "0 */6 * * *"
        assert job["schedule_type"] == "cron"
        assert job["run_at"] is None
        assert job["next_run"] is not None  # re-registered above
        assert job["enabled"] is True
        assert job["is_running"] is False
        assert job["source"] == "dynamic"
        assert job["notify"] is False
        assert job["model"] == "kimi-k2.5:cloud"
        assert job["preserve_context"] is True
        assert job["context_max_messages"] == 80
        assert job["overlap_policy"] == "parallel"
        assert job["last_run"] == "2026-09-11T06:00:00+00:00"
        assert job["last_error"] == "boom"
        assert job["created_at"] == "2026-09-10T00:00:00+00:00"

    def test_show_job_unknown(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        assert s.show_job("missing") is None

    def test_show_job_handles_concurrent_removal(self, tmp_path, monkeypatch) -> None:
        """Fix C: a job self-removing after tag resolution must not raise KeyError."""
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 * * * *")

        original_resolve = s._resolve_tag

        def _resolve_and_remove(tag: str) -> str | None:
            canonical = original_resolve(tag)
            if canonical:
                s._jobs_meta.pop(canonical, None)
            return canonical

        s._resolve_tag = _resolve_and_remove
        assert s.show_job("report") is None


class TestSchedulerModifyJob:
    """``modify_job`` partial-patch, validation, and persistence."""

    def test_modify_task_preserves_other_fields(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        _add_report_job(s)
        created_at = s._jobs_meta["report"]["created_at"]
        s._jobs_meta["report"]["last_run"] = "2026-09-11T06:00:00+00:00"

        result = s.modify_job("report", {"task": "new task text"})
        assert result["success"] is True

        meta = s._jobs_meta["report"]
        assert meta["task"] == "new task text"
        assert meta["cron"] == "0 */6 * * *"
        assert meta["model"] == "kimi-k2.5:cloud"
        assert meta["preserve_context"] is True
        assert meta["context_max_messages"] == 80
        assert meta["overlap_policy"] == "parallel"
        assert meta["notify"] is False
        assert meta["created_at"] == created_at
        assert meta["last_run"] == "2026-09-11T06:00:00+00:00"

    def test_modify_cron_re_registers(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        old_next = s._jobs_meta["report"].get("_next_run")
        result = s.modify_job("report", {"cron": "0 2 * * *"})
        assert result["success"] is True
        assert s._jobs_meta["report"]["cron"] == "0 2 * * *"
        assert s._jobs_meta["report"].get("_next_run") != old_next

    def test_modify_invalid_cron_rejected_and_unchanged(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        result = s.modify_job("report", {"cron": "not a cron"})
        assert result["success"] is False
        assert "Invalid cron expression" in result["error"]
        assert s._jobs_meta["report"]["cron"] == "0 */6 * * *"

    def test_modify_empty_patch_rejected(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        result = s.modify_job("report", {})
        assert result["success"] is False
        assert "schedule modify: no updatable fields provided" in result["error"]
        assert "task, cron, schedule_type, run_at, notify, model" in result["error"]

    def test_modify_enabled_rejected(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        result = s.modify_job("report", {"enabled": False})
        assert result["success"] is False
        assert "schedule modify: 'enabled' is controlled by the pause and resume actions" in result["error"]
        assert "pause_job or resume_job" in result["error"]

    def test_modify_unknown_tag(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        result = s.modify_job("missing", {"task": "x"})
        assert result["success"] is False
        assert "Job 'missing' not found. Known jobs: none" in result["error"]

    def test_modify_once_job_keeps_schedule_type_and_run_at(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="reminder", schedule_type="once", task="remind me", run_at="18:30")
        result = s.modify_job("reminder", {"task": "updated reminder text"})
        assert result["success"] is True
        assert s._jobs_meta["reminder"]["schedule_type"] == "once"
        assert s._jobs_meta["reminder"]["run_at"] == "18:30"
        assert s._jobs_meta["reminder"]["task"] == "updated reminder text"

    def test_modify_hidden_params(self, tmp_path, monkeypatch) -> None:
        """Spec scenario: report has preserve_context=true, so context_max_messages=20 is stored."""
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(
            tag="report",
            schedule_type="cron",
            task="t",
            cron="0 */6 * * *",
            preserve_context=True,
        )
        result = s.modify_job(
            "report",
            {"context_max_messages": 20, "overlap_policy": "parallel"},
        )
        assert result["success"] is True
        assert s._jobs_meta["report"]["context_max_messages"] == 20
        assert s._jobs_meta["report"]["overlap_policy"] == "parallel"

    def test_modify_preserve_context_false_removes_context_max_messages(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(
            tag="report",
            schedule_type="cron",
            task="t",
            cron="0 */6 * * *",
            preserve_context=True,
            context_max_messages=80,
        )
        result = s.modify_job("report", {"preserve_context": False})
        assert result["success"] is True
        assert s._jobs_meta["report"]["preserve_context"] is False
        assert "context_max_messages" not in s._jobs_meta["report"]

    def test_modify_persists_to_scheduler_toml(self, tmp_path, monkeypatch) -> None:
        config_path = tmp_path / "scheduler.toml"
        s = _make_scheduler(tmp_path, monkeypatch, config_path=config_path)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        result = s.modify_job("report", {"task": "persisted task", "overlap_policy": "parallel"})
        assert result["success"] is True

        written = config_path.read_text()
        assert 'task = "persisted task"' in written
        assert 'overlap_policy = "parallel"' in written

    def test_modify_cannot_convert_cron_to_once(self, tmp_path, monkeypatch) -> None:
        """Review fix: changing schedule_type to 'once' while keeping cron is rejected."""
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        result = s.modify_job("report", {"schedule_type": "once"})
        assert result["success"] is False
        assert "cannot set schedule_type to 'once'" in result["error"]
        assert "Converting a cron job to a once job requires remove + add." in result["error"]


class TestSchedulerRunNowManualOverride:
    """``run_now(manual=True)`` fires paused jobs; scheduled firing skips them."""

    def test_run_now_manual_executes_paused_job(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        s.pause_job("report")
        assert s._jobs_meta["report"]["enabled"] is False

        executed = threading.Event()

        def fake_run_job(tag: str, manual: bool = False) -> None:
            assert tag == "report"
            assert manual is True
            executed.set()

        with patch.object(s, "_run_job", side_effect=fake_run_job):
            result = s.run_now("report")
            assert result["success"] is True
            executed.wait(timeout=1.0)

    def test_scheduled_firing_skips_paused_job(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        s.pause_job("report")

        with patch.object(s, "_run_via_spawn_agent") as mock_spawn:
            with patch.object(s, "_run_via_agent_fn") as mock_agent_fn:
                s._run_job("report", manual=False)
        mock_spawn.assert_not_called()
        mock_agent_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown action error lists all eight actions
# ---------------------------------------------------------------------------


class TestExecScheduleUnknownAction:
    def test_unknown_action_lists_all_actions(self) -> None:
        scheduler = MagicMock()
        result = exec_schedule(scheduler, {"action": "delete"})
        assert result["success"] is False
        assert "Use: list, show, add, remove, pause, resume, modify, run_now" in result["error"]


# ---------------------------------------------------------------------------
# Post-review hardening tests
# ---------------------------------------------------------------------------


class TestSchedulerModifyPausedDoesNotReRegister:
    """Fix 1: schedule patches on paused jobs must not re-register."""

    def test_paused_modify_cron_does_not_register(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        s._register_job("report", s._jobs_meta["report"])
        old_next_run = s._jobs_meta["report"].get("_next_run")
        s.pause_job("report")
        assert s._jobs_meta["report"]["enabled"] is False

        result = s.modify_job("report", {"cron": "0 2 * * *"})
        assert result["success"] is True
        assert s._jobs_meta["report"]["cron"] == "0 2 * * *"
        assert s._jobs_meta["report"]["enabled"] is False
        # Paused jobs must not be re-registered; _next_run stays at the old value.
        assert s._jobs_meta["report"].get("_next_run") == old_next_run

    def test_paused_modify_cron_registers_on_resume(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(tag="report", schedule_type="cron", task="t", cron="0 */6 * * *")
        s._register_job("report", s._jobs_meta["report"])
        s.pause_job("report")
        s.modify_job("report", {"cron": "0 2 * * *"})
        old_next_run = s._jobs_meta["report"].get("_next_run")

        s.resume_job("report")
        assert s._jobs_meta["report"]["enabled"] is True
        # resume_job re-registers with the patched meta; _next_run is recomputed.
        assert s._jobs_meta["report"].get("_next_run") != old_next_run
        assert s._jobs_meta["report"]["cron"] == "0 2 * * *"


class TestExecScheduleRunNowSnapshot:
    """Fix 2: run_now notes are built from a pre-trigger snapshot."""

    def test_run_now_notes_survive_instant_once_job_completion(self) -> None:
        scheduler = MagicMock()
        job_snapshot = {
            "tag": "reminder",
            "enabled": True,
            "schedule_type": "once",
            "overlap_policy": "skip",
        }
        scheduler.show_job.return_value = job_snapshot

        def _run_now_with_side_effect(tag: str) -> dict:
            # Simulate the once-job completing and removing itself before
            # exec_schedule would have read it back.
            scheduler.show_job.return_value = None
            return {"success": True}

        scheduler.run_now.side_effect = _run_now_with_side_effect
        result = exec_schedule(scheduler, {"action": "run_now", "tag": "reminder"})
        assert result["success"] is True
        out = result["output"]
        assert "triggered to run now through the standard scheduled-run machinery" in out
        assert "This is a once job: it will be removed after the run completes." in out


class TestExecScheduleDepthGuardPrecedence:
    """Coverage gap (a): depth guard fires before validation."""

    @pytest.mark.parametrize("action", ["add", "modify"])
    def test_depth_guard_precedes_validation(self, action: str) -> None:
        scheduler = MagicMock()
        result = exec_schedule(
            scheduler,
            {
                "action": action,
                "tag": "report",
                "overlap_policy": "sometimes",
                "context_max_messages": "garbage",
                "notify": "maybe",
            },
            caller_depth=1,
        )
        assert result["success"] is False
        assert "schedule: sub-agents cannot modify scheduled jobs" in result["error"]
        assert "overlap_policy" not in result["error"]
        assert "context_max_messages" not in result["error"]
        assert "must be a boolean" not in result["error"]


class TestExecScheduleStrictValidation:
    """Fix 3 & 4: explicit validation errors for context_max_messages and booleans."""

    @pytest.mark.parametrize("action", ["add", "modify"])
    def test_context_max_messages_garbage_rejected(self, action: str) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        scheduler.add_job.return_value = {"success": True}
        args = {
            "action": action,
            "tag": "report",
            "task": "t",
            "context_max_messages": "lots",
        }
        if action == "add":
            args["cron"] = "0 * * * *"
        result = exec_schedule(scheduler, args)
        assert result["success"] is False
        assert "context_max_messages must be an integer >= 1" in result["error"]
        assert result.get("error_type") == "validation_error"
        assert result.get("recoverable") is True
        if action == "add":
            scheduler.add_job.assert_not_called()
        else:
            scheduler.modify_job.assert_not_called()

    @pytest.mark.parametrize("action,value", [("add", 0), ("add", -5), ("modify", 0), ("modify", -5)])
    def test_context_max_messages_non_positive_rejected(self, action: str, value: int) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        scheduler.add_job.return_value = {"success": True}
        args = {
            "action": action,
            "tag": "report",
            "task": "t",
            "context_max_messages": value,
        }
        if action == "add":
            args["cron"] = "0 * * * *"
        result = exec_schedule(scheduler, args)
        assert result["success"] is False
        assert "context_max_messages must be an integer >= 1" in result["error"]
        assert result.get("error_type") == "validation_error"
        if action == "add":
            scheduler.add_job.assert_not_called()
        else:
            scheduler.modify_job.assert_not_called()

    def test_modify_notify_false_string_stored_false(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "notify": "false"},
        )
        assert scheduler.modify_job.call_args.args[1]["notify"] is False

    def test_modify_preserve_context_true_string_stored_true(self) -> None:
        scheduler = MagicMock()
        scheduler.modify_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "preserve_context": "true"},
        )
        assert scheduler.modify_job.call_args.args[1]["preserve_context"] is True

    def test_add_preserve_context_false_string_stored_false(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job.return_value = {"success": True}
        exec_schedule(
            scheduler,
            {"action": "add", "tag": "j", "task": "t", "cron": "0 * * * *", "preserve_context": "false"},
        )
        assert scheduler.add_job.call_args.kwargs["preserve_context"] is False

    def test_modify_notify_invalid_rejected(self) -> None:
        scheduler = MagicMock()
        result = exec_schedule(
            scheduler,
            {"action": "modify", "tag": "report", "notify": "maybe"},
        )
        assert result["success"] is False
        assert "schedule modify: notify must be a boolean." in result["error"]
        assert result.get("error_type") == "validation_error"
        scheduler.modify_job.assert_not_called()


class TestSchedulerContextMaxMessagesInvariant:
    """Fix 5: context_max_messages is never stored when preserve_context is falsy."""

    def test_modify_context_max_messages_dropped_when_preserve_false(self, tmp_path, monkeypatch) -> None:
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(
            tag="report",
            schedule_type="cron",
            task="t",
            cron="0 */6 * * *",
            preserve_context=False,
        )
        result = s.modify_job("report", {"context_max_messages": 20})
        assert result["success"] is True
        assert "context_max_messages" not in s._jobs_meta["report"]
        assert s.show_job("report")["context_max_messages"] == 50  # default surface

    def test_modify_context_max_messages_kept_when_preserve_true(self, tmp_path, monkeypatch) -> None:
        """Spec scenario: report fixture has preserve_context=true → 20 is stored."""
        s = _make_scheduler(tmp_path, monkeypatch)
        s.add_job(
            tag="report",
            schedule_type="cron",
            task="t",
            cron="0 */6 * * *",
            preserve_context=True,
        )
        result = s.modify_job("report", {"context_max_messages": 20})
        assert result["success"] is True
        assert s._jobs_meta["report"]["context_max_messages"] == 20
        assert s.show_job("report")["context_max_messages"] == 20


# ---------------------------------------------------------------------------
# Public job field sets
#
# Relocated by task 2.4 from tests/test_scheduler_legacy_readonly.py (ledger
# provenance in openspec/changes/simplify-model-runtime/test-preservation-ledger).
# Keep (ledger 447).
# ---------------------------------------------------------------------------


@pytest.fixture
def make_legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[legacy_fx.MakeLegacyScheduler]:
    """Real legacy-beside-TOML schedulers (shared factory, see the support module)."""
    yield from legacy_fx.legacy_scheduler_factory(tmp_path, monkeypatch)


_SHOW_FIELDS = frozenset({
    "tag", "task", "schedule", "schedule_type", "cron", "run_at", "next_run",
    "last_run", "last_error", "notify", "model", "preserve_context",
    "context_max_messages", "overlap_policy", "source", "enabled", "is_running",
    "created_at",
})

_LIST_FIELDS = frozenset({
    "tag", "schedule", "schedule_type", "cron", "next_run", "enabled", "last_run",
    "last_error", "task", "notify", "source", "model", "preserve_context",
    "is_running", "config_error",
})


@pytest.mark.parametrize("tag", ["alpha"])  # task 8.4: legacy-file jobs no longer exist
def test_public_job_field_sets_are_unchanged(
    make_legacy_scheduler: legacy_fx.MakeLegacyScheduler, tag: str,
) -> None:
    """No new public list/show fields are introduced (a legacy file adds none)."""
    env = make_legacy_scheduler(legacy_fx.TOML, legacy=legacy_fx.LEGACY)

    listed = next(job for job in env.sched.list_jobs() if job["tag"] == tag)

    assert set(env.sched.show_job(tag)) == _SHOW_FIELDS
    assert set(listed) == _LIST_FIELDS
