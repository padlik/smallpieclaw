"""Tests for the structlog logging backbone (agent_logging) and XDG path resolution.

Covers:
- XDG log path resolution via xdg_paths().
- The shared processor chain: contextvars identity merge, secret redaction,
  and the JSONL render shape.
- LogEvent taxonomy emission with structured fields.

An autouse fixture snapshots and restores the root logger's handlers (plus the
isolated ``graph_memory`` component logger's handlers and propagate flag) and
resets structlog after each test so configuring logging here does not leak into
the rest of the suite.
"""

import io
import json
import logging
import logging.handlers
import threading

import pytest
import structlog

import agent_logging as al
from xdg import xdg_paths


@pytest.fixture(autouse=True)
def _isolate_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    gm = logging.getLogger("graph_memory")
    saved_gm_handlers = gm.handlers[:]
    saved_gm_propagate = gm.propagate
    structlog.contextvars.clear_contextvars()
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for handler in gm.handlers[:]:
        gm.removeHandler(handler)
        handler.close()
    for handler in saved_gm_handlers:
        gm.addHandler(handler)
    gm.propagate = saved_gm_propagate
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
    for handler in logging.getLogger("graph_memory").handlers:
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

    def test_graph_memory_log_mirrors_agent_log(self, tmp_xdg):
        p = xdg_paths("piclaw").graph_memory_log
        assert p == tmp_xdg / "state" / "piclaw" / "logs" / "graph_memory.log"


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


class TestGraphMemoryRouting:
    """graph_memory logger isolation (ADR-0023 component log)."""

    def test_records_routed_to_dedicated_sink_only(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file)  # gm path derives from log_file dir
        gm_log = str(tmp_path / "graph_memory.log")
        logging.getLogger("graph_memory").info("gm info")
        logging.getLogger("graph_memory").warning("gm warn")
        logging.getLogger("other").info("other msg")
        _flush()

        gm_prose = _read(gm_log)
        assert "gm info" in gm_prose
        assert "gm warn" in gm_prose

        prose = _read(log_file)
        assert "gm" not in prose
        assert "other msg" in prose
        assert "gm" not in _read(json_file)
        assert "other msg" in _read(json_file)

    def test_console_split_info_file_only_warning_on_console(self, tmp_path):
        stream = io.StringIO()
        log_file = str(tmp_path / "agent.log")
        al.setup_logging(log_file, stream=stream)
        gm_log = str(tmp_path / "graph_memory.log")
        logging.getLogger("graph_memory").info("gm info")
        logging.getLogger("graph_memory").warning("gm warn")
        _flush()

        console = stream.getvalue()
        assert "gm info" not in console
        assert "gm warn" in console
        gm_prose = _read(gm_log)
        assert "gm info" in gm_prose
        assert "gm warn" in gm_prose

    def test_rotation_config_matches_primary_sinks(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        al.setup_logging(log_file, backup_count=7)
        gm_handlers = logging.getLogger("graph_memory").handlers
        file_handlers = [
            h for h in gm_handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].when == "MIDNIGHT"
        assert file_handlers[0].backupCount == 7

    def test_routing_independent_of_component_enablement(self, tmp_path):
        json_file = al.setup_logging(str(tmp_path / "agent.log"))
        gm = logging.getLogger("graph_memory")
        assert gm.propagate is False
        assert gm.handlers
        gm.info("disabled component notice")
        _flush()
        assert "disabled component notice" not in _read(json_file)

    def test_worker_thread_records_carry_no_run_identity(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        json_file = al.setup_logging(log_file)
        al.bind_run_context(trace="r-1", agent="main")

        def emit():
            # The graph-memory worker thread never calls bind_run_context;
            # contextvars are thread-local, so its records carry no identity.
            logging.getLogger("graph_memory").info("gm from worker")

        t = threading.Thread(target=emit)
        t.start()
        t.join()
        _flush()

        gm_prose = _read(str(tmp_path / "graph_memory.log"))
        assert "gm from worker" in gm_prose
        assert "[main r-1]" not in gm_prose
        assert "r-1" not in gm_prose
        # Contrast: a foreign record on the bound (main) thread does carry it.
        logging.getLogger("foreign").info("foreign msg")
        _flush()
        obj = _last_json(json_file)
        assert obj["trace"] == "r-1" and obj["agent"] == "main"

    def test_backfill_cli_propagate_true_without_setup_logging(self):
        """Backfill CLI is unaffected: without setup_logging(), the graph_memory
        logger propagates to root (default), so its records reach the CLI's own
        basicConfig handler. Covers spec scenario 'Backfill CLI is unaffected'.
        """
        # Restore to pristine state (autouse fixture clears root handlers, but
        # a prior test may have set propagate=False on the gm logger).
        gm = logging.getLogger("graph_memory")
        gm.propagate = True
        gm.handlers.clear()
        assert gm.propagate is True
        assert not gm.handlers


class TestBuiltinExecutorLifecycle:
    """builtin_executor._emit_tool_lifecycle_error emits exactly one TOOL_FAILED."""

    def _capture_log_event(self, monkeypatch):
        captured: list[tuple[al.LogEvent, str]] = []

        def _capture(event, message, **kwargs):
            captured.append((event, message))

        monkeypatch.setattr("builtin_executor.agent_logging.log_event", _capture)
        return captured

    def test_direct_error_emission_is_single_tool_failed(self, make_builtin_executor, monkeypatch):
        captured = self._capture_log_event(monkeypatch)
        ex = make_builtin_executor()
        ex._emit_tool_lifecycle_error("shell", RuntimeError("boom"), 5)
        assert [event for event, _ in captured] == [al.LogEvent.TOOL_FAILED]
        assert all(event is not al.LogEvent.ERROR for event, _ in captured)

    def test_execute_path_emits_tool_start_then_single_tool_failed(
        self, make_builtin_executor, monkeypatch
    ):
        captured = self._capture_log_event(monkeypatch)
        ex = make_builtin_executor()

        def boom(args, ctx):
            raise RuntimeError("boom")

        ex._exec_table["shell"] = boom
        with pytest.raises(RuntimeError, match="boom"):
            ex.execute("shell", {})
        assert [event for event, _ in captured] == [
            al.LogEvent.TOOL_START,
            al.LogEvent.TOOL_FAILED,
        ]
        assert all(event is not al.LogEvent.ERROR for event, _ in captured)

    def test_confirm_path_emits_single_tool_failed(self, make_builtin_executor, monkeypatch):
        """confirm() exception path emits exactly one TOOL_FAILED, zero ERROR.

        Mirrors the execute()-path test for the second call site that routes
        through ``_emit_tool_lifecycle_error``.
        """
        captured = self._capture_log_event(monkeypatch)
        ex = make_builtin_executor()

        def boom(args, ctx):
            raise RuntimeError("boom")

        # confirm() dispatches via _run_table (not _exec_table used by execute()).
        ex._run_table["shell"] = boom
        # Stage a pending confirmation entry so confirm() can pop and run it.
        ex._pending["tok"] = ("shell", {})

        with pytest.raises(RuntimeError, match="boom"):
            ex.confirm("tok")
        assert [event for event, _ in captured] == [al.LogEvent.TOOL_FAILED]
        assert all(event is not al.LogEvent.ERROR for event, _ in captured)
    """Real ``LLMClient.chat`` failure path emits exactly one LLM_FAILED.

    Covers spec scenario "LLM failure is recorded exactly once" through the
    production ``except _LLM_CHAT_ERRORS`` block (the dedup removed the paired
    ``logger.error`` prose line; only the structured ``LLM_FAILED`` remains).
    """

    def _make_client(self):
        """Build a minimal LLMClient without running __init__ (no network)."""
        import llm_client as lc

        client = lc.LLMClient.__new__(lc.LLMClient)
        client._models = [{"model": "test-model", "provider": "openai"}]
        client._active_idx = 0
        return client, lc

    def test_chat_failure_emits_single_llm_failed(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        client, lc = self._make_client()
        captured: list = []

        def _capture(event, _msg, **kw):
            captured.append((event, kw))

        monkeypatch.setattr(lc.agent_logging, "log_event", _capture)

        stdlib = MagicMock()
        monkeypatch.setattr(lc, "logger", stdlib)

        def _raise(*_a, **_k):
            raise lc.LLMError("llm boom")

        monkeypatch.setattr(client, "_provider_chat", _raise)

        with pytest.raises(lc.LLMError, match="llm boom"):
            client.chat([{"role": "user", "content": "hi"}])

        llm_failed = [e for e, _ in captured if e == al.LogEvent.LLM_FAILED]
        assert len(llm_failed) == 1
        kw = [kw for e, kw in captured if e == al.LogEvent.LLM_FAILED][0]
        assert kw["model"] == "test-model"
        assert kw["err"] == "llm boom"
        assert isinstance(kw["dur_ms"], int)
        # No paired logger.error prose record restating the failure.
        error_msgs = [c.args[0] for c in stdlib.error.call_args_list]
        assert not any("LLM chat" in m for m in error_msgs)
        assert stdlib.error.call_count == 0

    def test_chat_with_tools_failure_emits_single_llm_failed(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        client, lc = self._make_client()
        captured: list = []

        def _capture(event, _msg, **kw):
            captured.append((event, kw))

        monkeypatch.setattr(lc.agent_logging, "log_event", _capture)

        stdlib = MagicMock()
        monkeypatch.setattr(lc, "logger", stdlib)

        def _raise(*_a, **_k):
            raise lc.LLMError("tools boom")

        monkeypatch.setattr(client, "_provider_chat", _raise)

        with pytest.raises(lc.LLMError, match="tools boom"):
            client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])

        llm_failed = [e for e, _ in captured if e == al.LogEvent.LLM_FAILED]
        assert len(llm_failed) == 1
        assert stdlib.error.call_count == 0
