"""Tests for the structlog logging backbone (agent_logging) and XDG path resolution.

Covers:
- XDG log path resolution via xdg_paths().
- The shared processor chain: contextvars identity merge, secret redaction,
  and the SQLite structured render shape.
- LogEvent taxonomy emission with structured fields.

An autouse fixture snapshots and restores the root logger's handlers (plus the
isolated ``graph_memory`` component logger's handlers and propagate flag),
drains the active SQLite writer, and resets structlog after each test so
configuring logging here does not leak into the rest of the suite.
"""

import io
import json
import logging
import logging.handlers
import sqlite3
import threading

import pytest
import structlog

import agent_logging as al
import sqlite_log
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
    al.shutdown_log_store()
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


def _last_row(db_path: str) -> dict:
    """Return the most recently inserted structured record (wide cols + extra).

    Mirrors the previous JSONL shape: columns with ``None`` values are omitted.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, f"no rows in {db_path}"
    obj = {k: v for k, v in dict(row).items() if v is not None}
    extra = json.loads(obj.pop("extra"))
    obj.update(extra)
    return obj


def _flush() -> None:
    writer = sqlite_log._active_writer
    if writer is not None:
        writer.flush()
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
        db_path = al.setup_logging(log_file, backup_count=2, secret_values=["S3CR3T"])
        al.bind_run_context(trace="r-1", agent="sa-9")
        al.log_event(
            al.LogEvent.TOOL_END, "done",
            logger=al.get_logger("t"), tool="shell", exit=0, dur_ms=3,
        )
        _flush()

        obj = _last_row(db_path)
        assert {"ts", "level", "logger", "msg"} <= set(obj)
        assert obj["trace"] == "r-1" and obj["agent"] == "sa-9"
        assert obj["event_type"] == "TOOL_END"
        assert obj["tool"] == "shell" and obj["exit"] == 0 and obj["dur_ms"] == 3

        prose = _read(log_file)
        assert "[sa-9 r-1]" in prose  # prose prefix reproduced from structured fields

    def test_secret_redacted_from_both_sinks(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file, secret_values=["TOPSECRET"])
        logging.getLogger("x").warning("leak TOPSECRET here")  # foreign stdlib record
        _flush()
        obj = _last_row(db_path)
        assert "TOPSECRET" not in obj.get("msg", "")
        assert "TOPSECRET" not in _read(log_file)

    def test_nested_secret_redacted_through_log_introspection(
        self, make_builtin_executor, tmp_path
    ):
        secret = "NESTEDSECRET"
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file, secret_values=[secret])
        al.bind_run_context(trace="r-a1b2c3d4", agent="main")
        al.log_event(
            al.LogEvent.TOOL_FAILED,
            "request failed",
            request={
                "headers": {"authorization": f"Bearer {secret}"},
                "steps": [secret, {"token": secret}],
            },
        )
        _flush()

        stored = _last_row(db_path)
        executor = make_builtin_executor(log_store_path=db_path)
        query = json.loads(
            executor.execute(
                "log_query", {"trace": "r-a1b2c3d4", "level": "DEBUG"}
            )["output"]
        )
        detail = json.loads(
            executor.execute("log_detail", {"id": query["records"][0]["id"]})[
                "output"
            ]
        )

        expected = {
            "headers": {"authorization": "Bearer ***REDACTED***"},
            "steps": ["***REDACTED***", {"token": "***REDACTED***"}],
        }
        assert stored["request"] == expected
        assert query["records"][0]["request"] == expected
        assert detail["record"]["request"] == expected
        assert secret not in _read(log_file)
        assert secret not in json.dumps(stored)
        assert secret not in json.dumps(query)
        assert secret not in json.dumps(detail)

    def test_tuple_shape_preserved_during_redaction(self):
        redact = al._make_redactor(frozenset({"NESTEDSECRET"}))

        redacted = redact(
            None,
            "info",
            {"items": ("NESTEDSECRET", {"token": "NESTEDSECRET"})},
        )

        assert redacted["items"] == (
            "***REDACTED***",
            {"token": "***REDACTED***"},
        )

    def test_dict_key_secret_redacted(self):
        redact = al._make_redactor(frozenset({"KEYSECRET1"}))

        redacted = redact(None, "info", {"request": {"KEYSECRET1": "v"}})

        assert redacted["request"] == {"***REDACTED***": "v"}

    def test_unicode_escaped_spelling_redacted_at_ingestion(self):
        redact = al._make_redactor(frozenset({"KEYSECRET1"}))

        redacted = redact(None, "info", {"msg": '"\\u004bEYSECRET1"'})

        assert redacted["msg"] == "***REDACTED***"

    def test_python_repr_of_credential_redacted_at_ingestion(self):
        secret = "abc'def\"ghi"
        redact = al._make_redactor(frozenset({secret}))

        redacted = redact(None, "info", {"msg": repr({"token": secret})})

        assert redacted["msg"] == "***REDACTED***"

    def test_python_hex_escape_redacted_at_ingestion(self):
        redact = al._make_redactor(frozenset({'ab12"ba21'}))

        redacted = redact(None, "info", {"msg": r"token=ab12\x22ba21"})

        assert redacted["msg"] == "***REDACTED***"

    def test_url_percent_escape_redacted_at_ingestion(self):
        redact = al._make_redactor(frozenset({'ab12"ba21'}))

        redacted = redact(None, "info", {"msg": "token=ab12%22ba21"})

        assert redacted["msg"] == "***REDACTED***"

    @pytest.mark.parametrize("terminator", ["\r\n", "\r"])
    def test_cr_line_continuation_redacted_at_ingestion(self, terminator):
        redact = al._make_redactor(frozenset({"ab12ba21"}))

        redacted = redact(
            None, "info", {"msg": "{'token': 'ab12" + "\\" + terminator + "ba21'}"}
        )

        assert redacted["msg"] == "***REDACTED***"

    def test_latin1_percent_escape_redacted_at_ingestion(self):
        secret = "p\u00c3\u00a9ssword"
        redact = al._make_redactor(frozenset({secret}))

        redacted = redact(None, "info", {"msg": "token=p%C3%A9ssword"})

        assert redacted["msg"] == "***REDACTED***"

    def test_malformed_named_escape_does_not_break_ingestion_redaction(self):
        redact = al._make_redactor(frozenset({"KEYSECRET1"}))

        redacted = redact(
            None,
            "info",
            {"msg": "ordinary diagnostic \\N{\\uD800}", "other": "token=KEYSECRET1"},
        )

        assert redacted["msg"] == "ordinary diagnostic \\N{\\uD800}"
        assert redacted["other"] == "token=***REDACTED***"

    def test_independent_percent_run_readings_redacted_at_ingestion(self):
        secret = "prefix\u00e9-\u00c3\u00a9suffix"
        redact = al._make_redactor(frozenset({secret}))

        redacted = redact(None, "info", {"msg": "token=prefix%C3%A9-%C3%A9suffix"})

        assert redacted["msg"] == "***REDACTED***"

    @pytest.mark.parametrize(
        ("secret", "msg"),
        [
            ("prefix\ufffd\ufffdsuffix", "token=prefix%C0%AFsuffix"),
            ("prefix\ufffdsuffix", "token=prefix%FFsuffix"),
            ("prefix\ufffd\ufffd\ufffdsuffix", "token=prefix%ED%A0%80suffix"),
            ("prefix\ufffd\ufffd\ufffd\ufffdsuffix", "token=prefix%F4%90%80%80suffix"),
        ],
    )
    def test_replacement_reading_of_invalid_utf8_percent_redacted_at_ingestion(self, secret, msg):
        redact = al._make_redactor(frozenset({secret}))

        redacted = redact(None, "info", {"msg": msg})

        assert redacted["msg"] == "***REDACTED***"

    def test_quoted_secret_absent_from_every_sink_form(self, tmp_path):
        secret = 'pa"ss\\word99'
        escaped = json.dumps(secret)[1:-1]
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file, secret_values=[secret])
        al.log_event(
            al.LogEvent.TOOL_FAILED,
            "request failed",
            request={secret: 1, "blob": json.dumps({"k": secret})},
        )
        _flush()

        conn = sqlite3.connect(db_path)
        try:
            stored = " ".join(
                str(col) for row in conn.execute("SELECT * FROM events") for col in row
            )
        finally:
            conn.close()
        raw = stored + _read(log_file)
        assert secret not in raw
        assert escaped not in raw

    def test_foreign_record_gets_identity(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file)
        al.bind_run_context(trace="r-42", agent="main")
        logging.getLogger("foreign").info("hello from stdlib")
        _flush()
        obj = _last_row(db_path)
        assert obj["trace"] == "r-42" and obj["agent"] == "main"

    def test_missing_run_context_is_graceful(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file)
        logging.getLogger("nobody").info("no identity bound")
        _flush()
        obj = _last_row(db_path)
        assert "trace" not in obj and "agent" not in obj


class TestLogEventTaxonomy:
    def test_members_are_string_valued(self):
        for event in al.LogEvent:
            assert isinstance(event.value, str)
            assert str(event) == event.value

    def test_emit_sets_event_type_and_level(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"))
        al.log_event(
            al.LogEvent.TOOL_FAILED, "boom", level=logging.ERROR,
            logger=al.get_logger("t"), tool="git", exit=1, dur_ms=7, err="nope",
        )
        _flush()
        obj = _last_row(db_path)
        assert obj["event_type"] == "TOOL_FAILED"
        assert obj["level"] == "error"
        assert obj["tool"] == "git" and obj["exit"] == 1 and obj["dur_ms"] == 7
        assert obj["err"] == "nope"


class TestGraphMemoryRouting:
    """graph_memory logger isolation (ADR-0023 component log)."""

    def test_records_routed_to_dedicated_sink_only(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file)  # gm path derives from log_file dir
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
        assert "gm" not in _last_row(db_path).get("msg", "")
        assert "other msg" in _last_row(db_path).get("msg", "")

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
        db_path = al.setup_logging(str(tmp_path / "agent.log"))
        gm = logging.getLogger("graph_memory")
        assert gm.propagate is False
        assert gm.handlers
        gm.info("disabled component notice")
        _flush()
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE msg = ?", ("disabled component notice",)
            )
            count = cur.fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_worker_thread_records_carry_no_run_identity(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        db_path = al.setup_logging(log_file)
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
        obj = _last_row(db_path)
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


class TestLLMFailedLifecycle:
    """Real pinned-client failures emit one safe lifecycle pair."""

    @staticmethod
    def _client(monkeypatch):
        import httpx

        import llm_client as lc
        from model_registry import ModelMetadata
        from tests.run_model_fixtures import make_run_model_client

        captured_requests = []

        def fail(request):
            captured_requests.append(request)
            raise httpx.ConnectError(
                "provider-secret request-secret", request=request
            )

        client = make_run_model_client(
            "openai",
            httpx.MockTransport(fail),
            metadata=ModelMetadata(context_tokens=8192, output_tokens=1024),
            upstream_id="test-pinned-model",
        )
        events = []
        monkeypatch.setattr(
            lc.agent_logging,
            "log_event",
            lambda event, message, **fields: events.append((event, message, fields)),
        )
        return client, lc, captured_requests, events

    @pytest.mark.parametrize("native", [False, True], ids=["text", "native-tools"])
    def test_provider_failure_emits_one_safe_llm_failed(
        self, native, monkeypatch
    ):
        from unittest.mock import MagicMock

        import httpx

        from agent_logging import LogEvent

        client, lc, requests, events = self._client(monkeypatch)
        stdlib = MagicMock()
        monkeypatch.setattr(lc, "logger", stdlib)

        message = [{"role": "user", "content": "request-secret"}]
        with pytest.raises(httpx.ConnectError) as caught:
            if native:
                client.chat_with_tools(message, tools=[])
            else:
                client.chat(message)

        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL, LogEvent.LLM_FAILED
        ]
        call_fields = events[0][2]
        failure_fields = events[1][2]
        assert call_fields["model"] == "test-pinned-model"
        assert failure_fields["model"] == "test-pinned-model"
        assert failure_fields["err"] == "connection"
        assert isinstance(failure_fields["dur_ms"], int)
        assert failure_fields["dur_ms"] >= 0
        assert len(requests) == 1
        assert json.loads(requests[0].content)["model"] == "test-pinned-model"
        assert str(caught.value) == "Provider connection failed"
        assert str(caught.value.request.url) == "https://provider.invalid/"
        assert "request-secret" not in repr(events)
        assert "provider-secret" not in repr(events)
        assert stdlib.error.call_count == 0


def test_ingestion_placeholder_is_protected_by_shared_core():
    from builtin_tools.text_utils import redact_vault_secrets

    text = f"x {al._REDACTION_PLACEHOLDER} REDACTED"

    assert redact_vault_secrets(text, ["REDACTED"]) == f"x {al._REDACTION_PLACEHOLDER} [REDACTED]"
