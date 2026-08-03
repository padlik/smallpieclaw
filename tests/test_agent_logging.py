"""Tests for the structlog logging backbone (agent_logging) and XDG path resolution.

Covers:
- XDG log path resolution via xdg_paths().
- The shared processor chain: contextvars identity merge, secret redaction,
  and the JSONL render shape.
- LogEvent taxonomy emission with structured fields.

An autouse fixture snapshots and restores the root logger's handlers and resets
structlog after each test so configuring logging here does not leak into the
rest of the suite.
"""

import json
import logging

import pytest
import structlog

import agent_logging as al
from xdg import xdg_paths


@pytest.fixture(autouse=True)
def _isolate_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    structlog.contextvars.clear_contextvars()
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _last_json(path: str) -> dict:
    return json.loads(_read(path).strip().splitlines()[-1])


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


class TestXdgPathResolution:
    def test_default_agent(self, tmp_xdg):
        p = xdg_paths("piclaw").log_file
        assert p == tmp_xdg / "state" / "piclaw" / "logs" / "agent.log"

    def test_custom_agent_name(self, tmp_xdg):
        d = xdg_paths("mybot").logs_dir
        assert d == tmp_xdg / "state" / "mybot" / "logs"

    def test_logs_always_under_state_home(self, tmp_xdg):
        p = xdg_paths("piclaw").log_file
        assert "logs" in p.parts
        assert "piclaw" in p.parts


class TestProcessorChain:
    def test_dual_sink_identity_and_json_shape(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file, backup_count=2, secret_values=["S3CR3T"])
        al.bind_run_context(trace="r-1", agent="sa-9")
        al.log_event(
            al.LogEvent.TOOL_END, "done",
            logger=al.get_logger("t"), tool="shell", exit=0, dur_ms=3,
        )
        _flush()

        obj = _last_json(json_file)
        assert {"ts", "level", "logger", "msg"} <= set(obj)
        assert obj["trace"] == "r-1" and obj["agent"] == "sa-9"
        assert obj["event_type"] == "TOOL_END"
        assert obj["tool"] == "shell" and obj["exit"] == 0 and obj["dur_ms"] == 3

        prose = _read(log_file)
        assert "[sa-9 r-1]" in prose  # prose prefix reproduced from structured fields

    def test_secret_redacted_from_both_sinks(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file, secret_values=["TOPSECRET"])
        logging.getLogger("x").warning("leak TOPSECRET here")  # foreign stdlib record
        _flush()
        assert "TOPSECRET" not in _read(json_file)
        assert "TOPSECRET" not in _read(log_file)

    def test_foreign_record_gets_identity(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file)
        al.bind_run_context(trace="r-42", agent="main")
        logging.getLogger("foreign").info("hello from stdlib")
        _flush()
        obj = _last_json(json_file)
        assert obj["trace"] == "r-42" and obj["agent"] == "main"

    def test_missing_run_context_is_graceful(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file)
        logging.getLogger("nobody").info("no identity bound")
        _flush()
        obj = _last_json(json_file)
        assert "trace" not in obj and "agent" not in obj


class TestLogEventTaxonomy:
    def test_members_are_string_valued(self):
        for event in al.LogEvent:
            assert isinstance(event.value, str)
            assert str(event) == event.value

    def test_emit_sets_event_type_and_level(self, tmp_path):
        json_file = al.setup_logging(str(tmp_path / "agent.log"))
        al.log_event(
            al.LogEvent.TOOL_FAILED, "boom", level=logging.ERROR,
            logger=al.get_logger("t"), tool="git", exit=1, dur_ms=7, err="nope",
        )
        _flush()
        obj = _last_json(json_file)
        assert obj["event_type"] == "TOOL_FAILED"
        assert obj["level"] == "error"
        assert obj["tool"] == "git" and obj["exit"] == 1 and obj["dur_ms"] == 7
        assert obj["err"] == "nope"
