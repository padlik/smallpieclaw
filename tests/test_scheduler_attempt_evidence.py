"""
Attempt-evidence contract for the scheduler backend and the ``schedule`` tool.

``Scheduler.add_job`` / ``Scheduler.modify_job`` accept an optional
keyword-only ``_on_attempt`` callback that fires synchronously at most once,
exactly when the backend is about to attempt (or has operationally conflicted
on) the requested change. The builtin ``exec_schedule`` adapter derives its
private execution disposition only from that signal, never from success flags
or error text. These tests use a REAL Scheduler (never started) and the real
``exec_schedule`` against the same instance.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builtin_tools.schedule import exec_schedule
from outcome_utils import read_execution, strip_execution
from scheduler import Scheduler
from xdg import XDGPaths


def _paths_for(state_dir) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
    state_dir = Path(state_dir)
    logs_dir = state_dir / "logs"
    return XDGPaths(
        config_home=state_dir, data_home=state_dir, state_home=state_dir,
        cache_home=state_dir, runtime_dir=state_dir,
        config_file=state_dir / "config.toml",
        model_selection_file=state_dir / "model-selection.toml",
        models_cache_file=state_dir / "models-cache.json",
        scheduler_config=state_dir / "scheduler.toml",
        memory_file=state_dir / "memory.json",
        graph_memory_db=state_dir / "graph_memory",
        tool_index_file=state_dir / "tool_index.json",
        pid_file=state_dir / "agent.pid",
        secrets_file=state_dir / "secrets.toml",
        mcp_tokens_dir=state_dir / "mcp_tokens",
        logs_dir=logs_dir,
        log_file=logs_dir / "agent.log",
        log_store=logs_dir / "agent_logs.sqlite",
        skills_dir=state_dir / "skills",
        scheduler_state=state_dir / "scheduler_state",
        scheduler_commands=state_dir / "scheduler_commands",
        scheduler_jobs=state_dir / "scheduler_jobs",
        job_execution_log=state_dir / "job_execution_log",
    )


_TOML = """
[jobs.alpha]
enabled = true
schedule = "cron"
cron = "0 2 * * *"
task = "job alpha"
"""

_MARKERS = ("_execution_disposition", "_on_attempt", "on_attempt", "callback")


class _Env:
    def __init__(self, sched: Scheduler, toml_path: Path) -> None:
        self.sched = sched
        self.toml_path = toml_path

    def snapshot(self) -> dict:
        return {
            "meta": copy.deepcopy(self.sched._jobs_meta),
            "jobs": copy.deepcopy(self.sched.list_jobs()),
            "toml": self.toml_path.read_text(),
        }


@pytest.fixture
def env(tmp_path):
    toml_path = tmp_path / "scheduler.toml"
    toml_path.write_text(_TOML)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sched = Scheduler(
        config={"scheduler": {"enabled": False}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=_paths_for(data_dir),
        scheduler_config_path=str(toml_path),
    )
    try:
        yield _Env(sched, toml_path)
    finally:
        sched.stop()


def _public(result: dict) -> dict:
    return strip_execution(result)


def _assert_preconditions(env: _Env) -> None:
    assert "alpha" in env.sched._jobs_meta


# --------------------------------------------------------------------------
# add_job
# --------------------------------------------------------------------------

class TestAddAttempt:
    def test_invalid_cron_add_refused_without_callback(self, env):
        _assert_preconditions(env)
        before = env.snapshot()
        cb = MagicMock()
        result = env.sched.add_job("bad", "cron", "t", cron="not a cron", _on_attempt=cb)
        assert result["success"] is False and result["error"]
        assert cb.call_count == 0
        assert env.snapshot() == before

        out = exec_schedule(env.sched, {"action": "add", "tag": "bad", "task": "t", "cron": "not a cron"})
        assert _public(out)["success"] is False and out["error"]
        assert read_execution(out) == "refused"
        assert env.snapshot() == before

    def test_valid_add_signals_once_and_is_executed(self, env):
        cb = MagicMock()
        result = env.sched.add_job("beta", "cron", "t", cron="0 4 * * *", _on_attempt=cb)
        assert result == {"success": True}
        assert cb.call_count == 1
        assert "beta" in env.sched._jobs_meta

        out = exec_schedule(env.sched, {"action": "add", "tag": "gamma", "task": "t", "cron": "0 5 * * *"})
        assert _public(out) == {
            "success": True, "output": "Job 'gamma' added.", "error": "", "exit_code": 0,
        }
        assert read_execution(out) == "executed"

    def test_duplicate_add_signals_once_and_is_executed(self, env):
        before = env.snapshot()
        cb = MagicMock()
        result = env.sched.add_job("alpha", "cron", "t", cron="0 4 * * *", _on_attempt=cb)
        assert result == {"success": False, "error": "Job 'alpha' already exists."}
        assert cb.call_count == 1
        assert env.snapshot() == before

        out = exec_schedule(env.sched, {"action": "add", "tag": "alpha", "task": "t", "cron": "0 4 * * *"})
        assert _public(out) == {
            "success": False, "output": "", "error": "Job 'alpha' already exists.", "exit_code": -1,
        }
        assert read_execution(out) == "executed"

    def test_duplicate_precedes_invalid_schedule_validation(self, env):
        cb = MagicMock()
        result = env.sched.add_job("alpha", "cron", "t", cron="not a cron", _on_attempt=cb)
        assert result == {"success": False, "error": "Job 'alpha' already exists."}
        assert cb.call_count == 1

    def test_add_without_callback_still_works(self, env):
        assert env.sched.add_job("delta", "cron", "t", cron="0 6 * * *") == {"success": True}
        assert env.sched.add_job("delta", "cron", "t", cron="0 6 * * *") == {
            "success": False, "error": "Job 'delta' already exists.",
        }
        assert env.sched.add_job("bad", "cron", "t", cron="nope")["success"] is False


# --------------------------------------------------------------------------
# modify_job
# --------------------------------------------------------------------------

class TestModifyAttempt:
    @pytest.mark.parametrize(
        "updates",
        [
            {"enabled": False},
            {"enabled": True, "task": "x"},
            {},
            {"task": None},
            {"cron": "not a cron"},
            {"schedule_type": "once"},
        ],
        ids=["enabled-only", "enabled-with-task", "no-update", "none-valued",
             "invalid-cron", "incompatible-conversion"],
    )
    def test_refused_modify_does_not_signal_or_change_state(self, env, updates):
        _assert_preconditions(env)
        before = env.snapshot()
        cb = MagicMock()
        result = env.sched.modify_job("alpha", updates, _on_attempt=cb)
        assert result["success"] is False and result["error"]
        assert cb.call_count == 0
        assert env.snapshot() == before

    @pytest.mark.parametrize(
        "args",
        [
            {"enabled": False},
            {},
            {"cron": "not a cron"},
            {"schedule_type": "once"},
        ],
        ids=["enabled-only", "no-update", "invalid-cron", "incompatible-conversion"],
    )
    def test_adapter_marks_refused_modify_refused(self, env, args):
        before = env.snapshot()
        out = exec_schedule(env.sched, {"action": "modify", "tag": "alpha", **args})
        public = _public(out)
        assert public["success"] is False and public["error"]
        assert public["error_type"] == "validation_error"
        assert read_execution(out) == "refused"
        assert env.snapshot() == before

    def test_valid_modify_signals_once_and_is_executed(self, env):
        cb = MagicMock()
        result = env.sched.modify_job("alpha", {"task": "new task"}, _on_attempt=cb)
        assert result == {"success": True}
        assert cb.call_count == 1
        assert env.sched._jobs_meta["alpha"]["task"] == "new task"

        out = exec_schedule(env.sched, {"action": "modify", "tag": "alpha", "cron": "0 7 * * *"})
        assert _public(out) == {
            "success": True, "output": "Job 'alpha' modified.", "error": "", "exit_code": 0,
        }
        assert read_execution(out) == "executed"

    def test_nonempty_missing_modify_signals_once_and_is_executed(self, env):
        before = env.snapshot()
        cb = MagicMock()
        result = env.sched.modify_job("ghost", {"task": "x"}, _on_attempt=cb)
        assert result["success"] is False and "ghost" in result["error"]
        assert cb.call_count == 1
        assert env.snapshot() == before

        out = exec_schedule(env.sched, {"action": "modify", "tag": "ghost", "task": "x"})
        assert _public(out)["success"] is False
        assert read_execution(out) == "executed"

    def test_missing_lookup_precedes_validation_rejections(self, env):
        for updates in ({"enabled": False}, {}, {"cron": "not a cron"}):
            cb = MagicMock()
            result = env.sched.modify_job("ghost", updates, _on_attempt=cb)
            assert result["success"] is False and "not found" in result["error"]
            assert cb.call_count == 1

    def test_blank_modify_tag_refused_by_adapter(self, env):
        before = env.snapshot()
        out = exec_schedule(env.sched, {"action": "modify", "tag": "  ", "task": "x"})
        assert _public(out) == {
            "success": False, "output": "", "error": "tag is required for modify", "exit_code": -1,
        }
        assert read_execution(out) == "refused"
        assert env.snapshot() == before

    def test_modify_without_callback_still_works(self, env):
        assert env.sched.modify_job("alpha", {"task": "z"}) == {"success": True}
        assert env.sched.modify_job("ghost", {"task": "z"})["success"] is False
        assert env.sched.modify_job("alpha", {"enabled": False})["success"] is False


# --------------------------------------------------------------------------
# remove / pause / resume
# --------------------------------------------------------------------------

class TestTagActionDispositions:
    @pytest.mark.parametrize("action", ["remove", "pause", "resume"])
    @pytest.mark.parametrize("tag", ["", "   "], ids=["empty", "whitespace"])
    def test_blank_tag_refused(self, env, action, tag):
        before = env.snapshot()
        out = exec_schedule(env.sched, {"action": action, "tag": tag})
        public = _public(out)
        assert public == {
            "success": False, "output": "", "error": "Job '' not found.", "exit_code": -1,
        }
        assert read_execution(out) == "refused"
        assert env.snapshot() == before

    @pytest.mark.parametrize("action", ["remove", "pause", "resume"])
    def test_missing_tag_arg_refused(self, env, action):
        out = exec_schedule(env.sched, {"action": action})
        assert read_execution(out) == "refused"

    @pytest.mark.parametrize("action", ["remove", "pause", "resume"])
    def test_nonempty_unknown_tag_executed_failure(self, env, action):
        before = env.snapshot()
        out = exec_schedule(env.sched, {"action": action, "tag": "nope"})
        assert _public(out) == {
            "success": False, "output": "", "error": "Job 'nope' not found.", "exit_code": -1,
        }
        assert read_execution(out) == "executed"
        assert env.snapshot() == before

    @pytest.mark.parametrize(
        "action,verb", [("pause", "paused"), ("resume", "resumed"), ("remove", "removed")],
    )
    def test_known_tag_executed_success(self, env, action, verb):
        out = exec_schedule(env.sched, {"action": action, "tag": "alpha"})
        assert _public(out) == {
            "success": True, "output": f"Job 'alpha' {verb}.", "error": "", "exit_code": 0,
        }
        assert read_execution(out) == "executed"


# --------------------------------------------------------------------------
# no marker leakage / injected backend
# --------------------------------------------------------------------------

class TestNoMarkerLeakage:
    def test_scheduler_results_jobs_and_toml_carry_no_runtime_marker(self, env):
        cb = MagicMock()
        results = [
            env.sched.add_job("beta", "cron", "t", cron="0 4 * * *", _on_attempt=cb),
            env.sched.add_job("beta", "cron", "t", cron="0 4 * * *", _on_attempt=cb),
            env.sched.modify_job("beta", {"task": "u"}, _on_attempt=cb),
            env.sched.modify_job("ghost", {"task": "u"}, _on_attempt=cb),
            env.sched.modify_job("beta", {}, _on_attempt=cb),
            env.sched.add_job("plain", "cron", "t", cron="0 8 * * *"),
        ]
        for result in results:
            assert set(result) <= {"success", "error"}
        blob = repr(env.sched._jobs_meta) + repr(env.sched.list_jobs()) + env.toml_path.read_text()
        for marker in _MARKERS:
            assert marker not in blob
        for job in env.sched._jobs_meta.values():
            assert not any(k.startswith("_on_") or "callback" in k for k in job)
        assert env.sched.show_job("beta") is not None
        assert not any(k in env.sched.show_job("beta") for k in _MARKERS)

    def test_adapter_public_payload_has_no_callback_field(self, env):
        out = exec_schedule(env.sched, {"action": "add", "tag": "eps", "task": "t", "cron": "0 9 * * *"})
        public = _public(out)
        assert not any(k in public for k in _MARKERS if k != "_execution_disposition")
        assert "_execution_disposition" not in public


class _IgnoringBackend:
    """Test backend that never calls ``_on_attempt`` yet reports success."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add_job(self, *args, **kwargs):
        self.calls.append(("add", args, kwargs))
        return {"success": True}

    def modify_job(self, *args, **kwargs):
        self.calls.append(("modify", args, kwargs))
        return {"success": True}


class TestInjectedBackendEarnsNoInferredCredit:
    def test_add_with_ignored_callback_is_not_executed(self):
        backend = _IgnoringBackend()
        out = exec_schedule(backend, {"action": "add", "tag": "x", "task": "t", "cron": "0 1 * * *"})
        assert backend.calls and backend.calls[0][0] == "add"
        assert _public(out)["success"] is True
        assert read_execution(out) == "refused"

    def test_modify_with_ignored_callback_is_not_executed(self):
        backend = _IgnoringBackend()
        out = exec_schedule(backend, {"action": "modify", "tag": "x", "task": "t"})
        assert backend.calls and backend.calls[0][0] == "modify"
        assert _public(out)["success"] is True
        assert read_execution(out) == "refused"
