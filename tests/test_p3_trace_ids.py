"""P3: request-scoped trace ID tests.

Covers per-run trace IDs that correlate one run across ReAct, LLM, and tool logs:

- trace_context helpers generate distinct IDs and propagate a parent trace.
- ReactContext.log_prefix / caller_tag include the label and trace ID.
- LLMClient.set_trace_id weaves the trace into the caller tag and restores it.
- AgentController.run() mints a distinct trace per run, applies it to the LLM,
  passes it into ReactContext, and restores the LLM trace afterward.
- react_loop start logs carry the trace ID.
- SubAgentRunner forwards a parent trace into its inner AgentController.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import Context
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import structlog

import agent_controller
import agent_logging
from config_schema import EmbeddingsConfig, ProviderConfig, ResolvedProviderConnection
from confirmation import ConfirmationManager
from embedding_client import EmbeddingClient
from react_loop import ReactContext, react_loop
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
)
from tests.model_stores import ModelStores
import sqlite_log
from tests.run_model_fixtures import make_run_model_client
from trace_context import child_trace_id, new_trace_id


class TestTraceContextHelpers:
    def test_new_trace_id_format(self):
        tid = new_trace_id()
        assert tid.startswith("r-")
        assert len(tid) == len("r-") + 8

    def test_new_trace_ids_are_distinct(self):
        assert new_trace_id() != new_trace_id()

    def test_child_reuses_parent_when_present(self):
        assert child_trace_id("r-deadbeef") == "r-deadbeef"

    def test_child_generates_fresh_when_absent(self):
        tid = child_trace_id(None)
        assert tid.startswith("r-")
        assert child_trace_id("") != ""


class TestReactContextPrefix:
    def _ctx(self, label="main", trace_id=""):
        return ReactContext(
            llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(), builtin_executor=None,
            mcp_manager=None, skill_registry=None, label=label, trace_id=trace_id,
        )

    def test_log_prefix_with_trace(self):
        assert self._ctx("main", "r-abc12345").log_prefix == "[main r-abc12345] "

    def test_log_prefix_without_trace(self):
        assert self._ctx("main", "").log_prefix == "[main] "

    def test_caller_tag_with_trace(self):
        assert self._ctx("sa-1a2b", "r-abc12345").caller_tag == "sa-1a2b r-abc12345"

    def test_caller_tag_without_trace(self):
        assert self._ctx("sa-1a2b", "").caller_tag == "sa-1a2b"


class TestLLMSetTraceId:
    def _client(self):
        return make_run_model_client(
            "openai",
            httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            metadata=ModelMetadata(context_tokens=8192, output_tokens=1024),
            upstream_id="trace-test-model",
            caller_tag="main",
        )

    def test_set_trace_id_weaves_into_caller_tag(self):
        c = self._client()
        c.set_trace_id("r-abc12345")
        assert c._caller_tag == "main r-abc12345"

    def test_clear_trace_id_restores_base(self):
        c = self._client()
        c.set_trace_id("r-abc12345")
        c.set_trace_id("")
        assert c._caller_tag == "main"

    def test_trace_tag_is_context_local(self):
        c = self._client()

        def set_and_read(trace_id):
            c.set_trace_id(trace_id)
            return c._caller_tag

        ctx_one = Context()
        ctx_two = Context()

        assert ctx_one.run(set_and_read, "r-one1111") == "main r-one1111"
        assert ctx_two.run(set_and_read, "r-two2222") == "main r-two2222"
        assert ctx_one.run(lambda: c._caller_tag) == "main r-one1111"
        assert ctx_two.run(lambda: c._caller_tag) == "main r-two2222"
        assert c._caller_tag == "main"


class TestAgentControllerTrace:
    def _controller(self, trace_id=None):
        from config_schema import AgentConfig, ExecutorPaths

        llm = MagicMock()
        llm._trace_id = ""
        applied = []
        llm.set_trace_id.side_effect = lambda t: applied.append(t)
        ctrl = agent_controller.AgentController(
            agent_cfg=AgentConfig(),
            paths=ExecutorPaths(),
            llm=llm,
            tool_index=MagicMock(),
            memory=MagicMock(),
            trace_id=trace_id,
        )
        return ctrl, llm, applied

    def test_run_generates_distinct_traces(self):
        ctrl, _llm, _ = self._controller()
        seen = []
        with patch.object(agent_controller, "react_loop",
                          side_effect=lambda ctx, *a, **k: seen.append(ctx.trace_id) or "ok"):
            ctrl.run("goal one")
            ctrl.run("goal two")
        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert all(t.startswith("r-") for t in seen)

    def test_run_applies_and_restores_llm_trace(self):
        ctrl, llm, applied = self._controller()
        with patch.object(agent_controller, "react_loop", side_effect=lambda ctx, *a, **k: "ok"):
            ctrl.run("goal")
        # set_trace_id called once with the run trace and once to restore "".
        assert len(applied) == 2
        assert applied[0].startswith("r-")
        assert applied[1] == ""

    def test_run_reuses_propagated_parent_trace(self):
        ctrl, _llm, _ = self._controller(trace_id="r-parent01")
        seen = []
        with patch.object(agent_controller, "react_loop",
                          side_effect=lambda ctx, *a, **k: seen.append(ctx.trace_id) or "ok"):
            ctrl.run("goal")
        assert seen == ["r-parent01"]


class TestReactLoopTraceLogging:
    def test_start_log_contains_trace_id(self, monkeypatch):
        llm = MagicMock()
        llm.chat.return_value = '{"action": "finish", "result": "done"}'
        llm.chat_with_tools.side_effect = NotImplementedError("native tools not mocked")
        llm.llm_cfg = {"model": "test-model"}
        ctx = ReactContext(
            llm=llm, tool_index=MagicMock(), memory=MagicMock(), builtin_executor=None, mcp_manager=None, skill_registry=None,
            cancel_event=threading.Event(), label="main", trace_id="r-trace777",
        )
        # Run identity is bound into structlog contextvars at run entry now, not
        # embedded in the raw log message text.
        import agent_logging
        seen: dict = {}
        monkeypatch.setattr(agent_logging, "bind_run_context", lambda **kw: seen.update(kw))
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            react_loop(ctx, "task")
        assert seen.get("trace") == "r-trace777"
        assert seen.get("agent") == "main"


class TestPinnedProviderTraceCorrelation:
    """The pinned provider invocation receives its context-local trace tag."""

    def _client(self):
        requests = []
        client = make_run_model_client(
            "openai",
            httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
                )
            ),
            metadata=ModelMetadata(context_tokens=8192, output_tokens=1024),
            upstream_id="trace-pinned-model",
            caller_tag="main",
        )
        return client, requests

    def test_provider_context_includes_trace_tag(self):
        from providers import openai_provider

        c, requests = self._client()
        c.set_trace_id("r-fb123456")
        observed = []
        real_chat = openai_provider.chat

        def capture_context(ctx, *args, **kwargs):
            observed.append(ctx.caller_tag)
            return real_chat(ctx, *args, **kwargs)

        with patch.object(openai_provider, "chat", side_effect=capture_context):
            assert c.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert observed == ["main r-fb123456"]
        assert len(requests) == 1
        c.close()


@contextmanager
def _isolated_logging_environment(tmp_path, monkeypatch):
    """Run a real logging setup while preserving prior process-wide state."""
    logger_names = (None, "graph_memory", "sqlite_log_writer", "httpx", "telegram", "telegram.ext")
    loggers = {
        name: logging.getLogger() if name is None else logging.getLogger(name)
        for name in logger_names
    }

    def snapshot(logger):
        return {
            "handlers": list(logger.handlers),
            "level": logger.level,
            "propagate": logger.propagate,
            "disabled": logger.disabled,
            "filters": list(logger.filters),
        }

    def restore(logger, state):
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        for handler in state["handlers"]:
            logger.addHandler(handler)
        logger.setLevel(state["level"])
        logger.propagate = state["propagate"]
        logger.disabled = state["disabled"]
        logger.filters[:] = state["filters"]

    original_logger_states = {name: snapshot(logger) for name, logger in loggers.items()}
    original_handler_closed = {
        handler: getattr(handler, "_closed", False)
        for state in original_logger_states.values()
        for handler in state["handlers"]
    }
    original_structlog_configured = structlog.is_configured()
    original_structlog_config = dict(structlog.get_config())
    original_contextvars = structlog.contextvars.get_contextvars()
    with sqlite_log._active_writer_lock:
        original_writer = sqlite_log._active_writer

    sentinel_writer = None
    protected_writer = original_writer
    def set_writer_without_stopping_old(writer):
        with sqlite_log._active_writer_lock:
            sqlite_log._active_writer = writer
        return writer

    sentinel_states = {}
    sentinel_streams = {}
    sentinel_structlog_configured = False
    sentinel_structlog_config = {}
    sentinel_contextvars = {}
    closed_new_handlers = []
    sentinel_logger_unchanged = False
    sentinel_handlers_usable = False
    sentinel_structlog_unchanged = False
    sentinel_writer_usable = original_writer is not None
    db_path = tmp_path / "agent_logs.sqlite"
    try:
        # Preserve any pre-existing handlers without letting setup_logging
        # close them; install sentinel state to prove restoration and usability.
        for logger in loggers.values():
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)

        if protected_writer is None:
            sentinel_writer = sqlite_log.SqliteLogWriter(
                str(tmp_path / "prior-owner.sqlite")
            )
            sentinel_writer.start()
            sqlite_log.set_active_writer(sentinel_writer)
            protected_writer = sentinel_writer

        for index, (name, logger) in enumerate(loggers.items()):
            stream = StringIO()
            handler = logging.StreamHandler(stream)
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG + index)
            logger.propagate = bool(index % 2)
            logger.disabled = False
            logger.filters[:] = []
            sentinel_states[name] = snapshot(logger)
            sentinel_streams[handler] = stream

        structlog.configure(
            processors=[structlog.processors.add_log_level],
            cache_logger_on_first_use=False,
        )
        structlog.contextvars.bind_contextvars(isolation_probe="keep-me")
        sentinel_structlog_configured = structlog.is_configured()
        sentinel_structlog_config = dict(structlog.get_config())
        sentinel_contextvars = structlog.contextvars.get_contextvars()

        monkeypatch.setattr(sqlite_log, "set_active_writer", set_writer_without_stopping_old)
        # setup_logging closes handlers attached to root and its isolated
        # component loggers. Keep all sentinel handlers detached while it runs.
        for logger in loggers.values():
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
        agent_logging.setup_logging(
            str(tmp_path / "agent.log"),
            db_path=str(db_path),
            graph_memory_log=str(tmp_path / "graph_memory.log"),
            secret_values=(),
            stream=StringIO(),
        )
        yield db_path
    finally:
        try:
            # Shut down only the writer this setup installed; do not pass a
            # pre-existing writer to the production shutdown helper.
            with sqlite_log._active_writer_lock:
                active_writer = sqlite_log._active_writer
            if active_writer is not None and active_writer is not protected_writer:
                agent_logging.shutdown_log_store()

            for name, logger in loggers.items():
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    state = sentinel_states.get(name)
                    if state is None or handler not in state["handlers"]:
                        handler.close()
                        closed_new_handlers.append(handler)
                if (state := sentinel_states.get(name)) is not None:
                    restore(logger, state)

            if sentinel_structlog_configured:
                structlog.contextvars.clear_contextvars()
                structlog.configure(**sentinel_structlog_config)
                structlog.contextvars.bind_contextvars(**sentinel_contextvars)

            # Restore the protected owner without invoking the production
            # replacement behavior, which would stop that prior writer.
            if protected_writer is not None:
                set_writer_without_stopping_old(protected_writer)
            if sentinel_writer is not None:
                try:
                    sentinel_writer.enqueue({
                        "ts": "test", "level": "info", "logger": "sentinel",
                        "msg": "prior writer remains usable",
                    })
                    flushed = sentinel_writer.flush()
                    with sqlite3.connect(tmp_path / "prior-owner.sqlite") as connection:
                        sentinel_row = connection.execute(
                            "SELECT msg FROM events WHERE msg = ?",
                            ("prior writer remains usable",),
                        ).fetchone()
                    sentinel_writer_usable = flushed and sentinel_row is not None
                finally:
                    set_writer_without_stopping_old(None)
                    sentinel_writer.stop()

            # Sentinel state is checked before restoring the caller's exact
            # logger/structlog state, then its own handlers are closed.
            sentinel_logger_unchanged = bool(sentinel_states) and all(
                snapshot(loggers[name]) == state for name, state in sentinel_states.items()
            )
            sentinel_handlers_usable = bool(sentinel_streams)
            for handler, stream in sentinel_streams.items():
                record = logging.LogRecord(
                    "sentinel", logging.INFO, __file__, 1, "isolation-probe", (), None
                )
                handler.handle(record)
                sentinel_handlers_usable &= (
                    "isolation-probe" in stream.getvalue()
                    and not getattr(handler, "_closed", False)
                )
            sentinel_structlog_unchanged = (
                sentinel_structlog_configured
                and structlog.is_configured() == sentinel_structlog_configured
                and structlog.get_config() == sentinel_structlog_config
                and structlog.contextvars.get_contextvars() == sentinel_contextvars
            )

        finally:
            for logger in loggers.values():
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
            for name, logger in loggers.items():
                restore(logger, original_logger_states[name])
            for handler in sentinel_streams:
                handler.close()

            structlog.contextvars.clear_contextvars()
            if original_structlog_configured:
                structlog.configure(**original_structlog_config)
            else:
                structlog.reset_defaults()
            structlog.contextvars.bind_contextvars(**original_contextvars)

            with sqlite_log._active_writer_lock:
                sqlite_log._active_writer = original_writer

        assert sentinel_logger_unchanged
        assert sentinel_handlers_usable
        assert sentinel_structlog_unchanged
        assert all(getattr(handler, "_closed", False) for handler in closed_new_handlers)
        assert sentinel_writer_usable
        assert structlog.is_configured() == original_structlog_configured
        assert structlog.get_config() == original_structlog_config
        assert structlog.contextvars.get_contextvars() == original_contextvars
        assert all(
            getattr(handler, "_closed", False) == closed
            for handler, closed in original_handler_closed.items()
        )
        if original_writer is not None:
            assert sqlite_log._active_writer is original_writer
            assert not original_writer._stop_event.is_set()
        else:
            assert sqlite_log._active_writer is None


def test_provider_failure_warning_and_run_lifecycle_keep_request_trace(tmp_path, monkeypatch):
    """Real provider failure logs correlate to its React run through SQLite."""
    trace_id = "r-fail1234"
    label = "main"
    requests: list[httpx.Request] = []

    def fail_connection(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("synthetic connection failure", request=request)

    client = make_run_model_client(
        "openai",
        httpx.MockTransport(fail_connection),
        metadata=ModelMetadata(context_tokens=8192, output_tokens=1024),
        upstream_id="trace-failure-pinned-model",
        caller_tag=label,
    )
    confirmation = ConfirmationManager()

    try:
        with _isolated_logging_environment(tmp_path, monkeypatch) as db_path:
            ctx = ReactContext(
                llm=client,
                tool_index=MagicMock(),
                memory=MagicMock(),
                builtin_executor=None,
                mcp_manager=None,
                skill_registry=None,
                label=label,
                trace_id=trace_id,
                confirmation=confirmation,
                checkpoint_enabled=False,
                max_retries=0,
            )

            cards: list[str] = []

            def record_progress(message: str) -> None:
                if message.startswith("__LLM_ERROR__:"):
                    cards.append(message)

            with (
                patch("react_loop._build_system_prompt", return_value=("sys", None)),
                patch("react_loop.build_tool_definitions", return_value=[]),
            ):
                result = react_loop(ctx, "test provider failure", record_progress)
            assert cards == []  # no retry card is ever published

            # Headless run (not checkpoint-eligible): the exhausted request ends
            # with fixed terminal guidance and no retry card.
            assert result == (
                "❌ 🔌 Connection failed. Automatic retries were exhausted. "
                "Try the request again later."
            )
            assert len(requests) == 1
            assert json.loads(requests[0].content)["model"] == "trace-failure-pinned-model"
            assert client.llm_cfg["model"] == "trace-failure-pinned-model"
            with sqlite_log._active_writer_lock:
                test_writer = sqlite_log._active_writer
            assert test_writer is not None and test_writer.flush()

            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute(
                    "SELECT id, agent, trace, event_type, level, msg FROM events ORDER BY id"
                )]

        # Select the actual ordinary failure warning by its message, then
        # independently verify its run identity in the SQLite record.
        warning_rows = [
            row for row in rows
            if row["level"] == "warning" and row["msg"].startswith("LLM request failed at step")
        ]
        assert len(warning_rows) == 1
        warning = warning_rows[0]
        assert warning["msg"] == "LLM request failed at step 1: connection"
        assert warning["trace"] == trace_id
        assert warning["agent"] == label

        lifecycle = [row for row in rows if row["event_type"] in {"RUN_BEGIN", "RUN_END"}]
        assert [row["event_type"] for row in lifecycle] == ["RUN_BEGIN", "RUN_END"]
        assert all(row["trace"] == trace_id for row in lifecycle)
        assert all(row["agent"] == label for row in lifecycle)
        assert lifecycle[0]["id"] < warning["id"] < lifecycle[1]["id"]
    finally:
        client.close()


class TestSubAgentTracePropagation:
    def test_subagent_forwards_trace_to_controller(self, tmp_path: Path) -> None:
        captured = {}
        real_init = agent_controller.AgentController.__init__

        def spy_init(self, *args, **kwargs):
            captured["trace_id"] = kwargs.get("trace_id")
            return real_init(self, *args, **kwargs)

        from config_schema import AgentConfig, ExecutorPaths

        class _NoDiscovery:
            def fetch(self, connection: object) -> object:
                raise AssertionError("discovery must not run during admission")

        def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
            return ResolvedProviderConnection(
                provider_id=config.name, adapter=config.type, base_url=config.base_url,
                request_timeout=config.request_timeout, api_key="sk-fake",
            )

        def _unavailable_embedding(event: threading.Event) -> EmbeddingClient:
            return EmbeddingClient(EmbeddingsConfig(), cancel_event=event)

        record = ModelRecord(
            "pa/up/m", "pa", "up/m", "up/m display",
            ModelMetadata(
                5000, None, 2048, False, True, None,
            ),
        )
        providers = {
            "pa": ProviderConfig("pa", "openai", "env:KEY_pa", "https://a.example.test/v1", 9),
        }
        stores = ModelStores.at(tmp_path)
        stores.cache.write(CatalogDocument(
            1, 2, (ProviderState("pa", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (record,),
        ))
        registry = stores.registry(providers, _NoDiscovery(), _resolve)
        registry.select(record.id)
        run_model = registry.admit()

        runner = None
        try:
            with patch.object(agent_controller.AgentController, "__init__", spy_init):
                runner = agent_controller.SubAgentRunner(
                    run_model=run_model,
                    agent_cfg=AgentConfig(),
                    embedding_factory=_unavailable_embedding,
                    paths=ExecutorPaths(),
                    tool_index=MagicMock(), base_memory=MagicMock(), builtin_executor=MagicMock(),
                    trace_id="r-parent99",
                )
            assert captured["trace_id"] == "r-parent99"
            assert runner._agent._trace_id == "r-parent99"
        finally:
            if runner is not None:
                runner.close()
