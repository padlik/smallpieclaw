"""Tests for log_query prompt_id filtering."""

from __future__ import annotations

import json
import logging

import pytest

import agent_logging as al
from builtin_executor import BuiltinExecutor


@pytest.fixture
def executor(tmp_path):
    """A BuiltinExecutor wired to a temp JSONL log file."""
    log_file = str(tmp_path / "agent.log")
    json_file = al.setup_logging(log_file, backup_count=1)
    exc = BuiltinExecutor(log_jsonl_path=json_file)
    yield exc
    for handler in logging.getLogger().handlers[:]:
        root = logging.getLogger()
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    al.clear_run_context()


def _write_records(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


class TestPromptIdFilter:
    def test_filter_by_prompt_id_returns_only_matches(self, executor, tmp_path):
        records = [
            {"trace": "r-1", "prompt_id": 7, "level": "info", "event_type": "TOOL_START", "msg": "a"},
            {"trace": "r-2", "prompt_id": 8, "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"trace": "r-3", "prompt_id": 7, "level": "info", "event_type": "TOOL_START", "msg": "c"},
        ]
        _write_records(executor._log_jsonl_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": 7, "trace": "*"})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 2
        assert {r["msg"] for r in payload["records"]} == {"a", "c"}

    def test_prompt_id_combines_with_trace_filter(self, executor, tmp_path):
        records = [
            {"trace": "r-1", "prompt_id": 7, "level": "info", "event_type": "TOOL_START", "msg": "a"},
            {"trace": "r-2", "prompt_id": 8, "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"trace": "r-1", "prompt_id": 8, "level": "info", "event_type": "TOOL_START", "msg": "c"},
        ]
        _write_records(executor._log_jsonl_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": 8, "trace": "r-2"})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 1
        assert payload["records"][0]["msg"] == "b"

    def test_prompt_id_combines_with_level_and_event(self, executor, tmp_path):
        records = [
            {"trace": "r-1", "prompt_id": 7, "level": "warning", "event_type": "TOOL_FAILED", "msg": "a"},
            {"trace": "r-1", "prompt_id": 7, "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"trace": "r-1", "prompt_id": 8, "level": "warning", "event_type": "TOOL_FAILED", "msg": "c"},
        ]
        _write_records(executor._log_jsonl_path, records)

        result = executor._logquery._exec_log_query(
            {"prompt_id": 7, "level": "WARNING", "event_type": "TOOL_FAILED"},
        )
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 1
        assert payload["records"][0]["msg"] == "a"

    def test_empty_result_is_well_formed(self, executor, tmp_path):
        records = [
            {"trace": "r-1", "prompt_id": 7, "level": "info", "msg": "a"},
        ]
        _write_records(executor._log_jsonl_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": 999})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["records"] == []
        assert payload["count"] == 0
        assert payload["truncated"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
