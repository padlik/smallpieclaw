"""6.6 prerequisite correction found in 6.7 review: cancellation precedes failure logging.

A cancellation the ReAct loop owns (``ctx.cancel_event``) or the client owns must never be
recorded by the client as an ERROR ``LLM_FAILED`` request failure, even when the provider
reply that raced it was a failure. Ordinary uncancelled failures keep their ERROR event.

Every node runs under the PRODUCTION logging pipeline (``agent_logging.setup_bootstrap()``
installed by a local fixture and fully restored afterwards), the real pinned ``LLMClient``,
``httpx.MockTransport`` and the real ``_prepare_request``.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog

import agent_logging
from model_registry import ModelMetadata
from agent_logging import LogEvent
from providers._errors import LLMCancelledError
from react_loop import _LoopState, _prepare_request
from tests.execution_harness import RecordingExecutor, build_context
from tests.run_model_fixtures import make_run_model_client
from tests.test_compaction_failure_6_6 import _assert_only_client_request_failures

CANARY = "CANARY-PREREQ-PROVIDER-BODY-8842"
SUMMARY_OK = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}


@contextmanager
def production_logging_context() -> Iterator[None]:
    """Install the production bootstrap pipeline; restore structlog config and root logging."""
    root = logging.getLogger()
    saved_config = structlog.get_config()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)  # bootstrap must not close someone else's handlers
    try:
        agent_logging.setup_bootstrap()
    except BaseException:
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        structlog.configure(**saved_config)
        raise
    bootstrap_handlers = list(root.handlers)
    try:
        yield
    finally:
        for handler in bootstrap_handlers:
            if handler in root.handlers:
                root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)
        structlog.configure(**saved_config)


@pytest.fixture
def production_logging() -> Iterator[None]:
    with production_logging_context():
        yield


def _history() -> list[dict[str, Any]]:
    huge = "H" * 6000
    return [
        {"role": "user", "content": "goal " + huge},
        {"role": "assistant", "content": "old1 " + huge},
        {"role": "user", "content": "old2 " + huge},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "r2"},
    ]


def _failed(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if _client_failure(r)]


def _client_failure(record: logging.LogRecord) -> bool:
    """Recognize a structured client failure, not a matching prose message."""
    return (record.name == "llm_client" and record.levelno == logging.ERROR
            and isinstance(record.msg, dict)
            and record.msg.get("event_type") == LogEvent.LLM_FAILED.value)


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _assert_exactly_client_failures(caplog: pytest.LogCaptureFixture, count: int) -> None:
    errors = _errors(caplog)
    assert all(_client_failure(r) for r in errors), errors
    assert len(errors) == count


def _compaction_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "Automatic compaction failed" in r.getMessage()]


class _Rig:
    """Real client + loop context over a scripted transport; one summary reply per request."""

    def __init__(self, reply: str, *, client_event: threading.Event | None = None,
                 fire: str = "none") -> None:
        self.reply = reply
        self.fire = fire  # which event the in-flight request sets: ctx / client / both / none
        self.client_event = client_event
        self.requests = 0
        self.client = make_run_model_client(
            "openai", httpx.MockTransport(self._handler),
            metadata=ModelMetadata(context_tokens=9_000, output_tokens=200),
            upstream_id="prereq-model", cancel_event=client_event,
        )
        self.ctx = build_context(self.client, RecordingExecutor(),
                                 compaction_threshold_tokens=10, max_retries=0)

    def _handler(self, _request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.fire in ("ctx", "both"):
            self.ctx.cancel_event.set()
        if self.fire in ("client", "both") and self.client_event is not None:
            self.client_event.set()
        if self.reply == "503":
            return httpx.Response(503, json={"error": {"message": CANARY}})
        if self.reply == "blank":
            return httpx.Response(200, json={"choices": [{"message": {"content": " \n\t "}}]})
        return httpx.Response(200, json=SUMMARY_OK)

    def prepare(self) -> _LoopState:
        state = _LoopState(messages=_history(), goal_idx=0, max_steps=3)
        self.state = state
        _prepare_request(self.ctx, state, "sys", 0)
        return state


@pytest.mark.usefixtures("production_logging")
class TestCancellationPrecedesFailureLogging:
    @pytest.mark.parametrize("fire", ["ctx", "client", "both"])
    @pytest.mark.parametrize("reply", ["503", "blank"])
    def test_cancel_racing_a_failed_reply_is_a_cancel_not_a_failure_event(
        self, fire: str, reply: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = _Rig(reply, client_event=threading.Event() if fire != "ctx" else None, fire=fire)
        before = copy.deepcopy(_history())
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                rig.prepare()
        finally:
            rig.client.close()
        assert rig.requests == 1
        assert _failed(caplog) == [] and _errors(caplog) == []
        assert _compaction_warnings(caplog) == []
        assert rig.state.auto_compaction_disabled is False and rig.state.messages == before
        assert CANARY not in repr([r.__dict__ for r in caplog.records])

    def test_cancel_after_a_successful_reply_records_no_failure(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = _Rig("ok", fire="ctx")
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                rig.prepare()
        finally:
            rig.client.close()
        assert _failed(caplog) == [] and _errors(caplog) == []
        assert any("LLM request" in r.getMessage() for r in caplog.records)  # the normal call event

    def test_bound_cancel_does_not_leak_into_later_requests(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = _Rig("503", fire="ctx")
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                rig.prepare()
            assert _failed(caplog) == []
            # A later, uncancelled request on the SAME client keeps the ordinary failure event.
            rig.fire = "none"
            with pytest.raises(Exception):
                rig.client.chat([{"role": "user", "content": "main"}])
            assert len(_failed(caplog)) == 1 and _failed(caplog)[0].levelno == logging.ERROR
            # A fresh run context (new event) on the same client summarizes normally again.
            rig.reply = "ok"
            ctx2 = build_context(rig.client, RecordingExecutor(), compaction_threshold_tokens=10,
                                 max_retries=0)
            state = _LoopState(messages=_history(), goal_idx=0, max_steps=3)
            _prepare_request(ctx2, state, "sys", 0)
            assert any("Compacted context" in str(m.get("content")) for m in state.messages)
        finally:
            rig.client.close()

    def test_a_persistent_client_cancel_still_cancels_without_being_cleared(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        own = threading.Event()
        own.set()
        rig = _Rig("503", client_event=own)
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMCancelledError):
                rig.client.chat([{"role": "user", "content": "x"}])
        finally:
            rig.client.close()
        assert own.is_set()  # never cleared by the client


@pytest.mark.usefixtures("production_logging")
class TestOrdinaryFailureEventsArePreserved:
    def test_uncancelled_summary_failure_keeps_the_error_event_and_one_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = _Rig("503")
        before = copy.deepcopy(_history())
        try:
            with caplog.at_level(logging.DEBUG):
                state = rig.prepare()
        finally:
            rig.client.close()
        failed = _failed(caplog)
        assert len(failed) == 1 and failed[0].levelno == logging.ERROR
        assert len(_compaction_warnings(caplog)) == 1
        assert state.auto_compaction_disabled is True and state.messages == before
        assert CANARY not in repr([r.__dict__ for r in caplog.records])

    def test_uncancelled_main_style_request_failure_keeps_the_error_event(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = _Rig("503")
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(Exception):
                rig.client.chat([{"role": "user", "content": "main"}])
        finally:
            rig.client.close()
        failed = _failed(caplog)
        assert len(failed) == 1 and failed[0].levelno == logging.ERROR

    def test_failure_event_keeps_run_identity_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = _Rig("503")
        tokens = agent_logging.bind_run_context(trace="r-prereq01", agent="main")
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(Exception):
                rig.client.chat([{"role": "user", "content": "main"}])
        finally:
            rig.client.close()
            agent_logging.reset_run_context(tokens)
        record = _failed(caplog)[0]
        blob = repr(record.__dict__)
        assert "r-prereq01" in blob and "main" in blob


class _SpyHandler(logging.Handler):
    """Pre-existing external handler: records messages and counts close() calls."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []
        self.close_calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class TestProductionLoggingFixtureIsolation:
    def test_preexisting_handlers_levels_and_config_survive_untouched(self) -> None:
        root = logging.getLogger()
        spy = _SpyHandler()
        root.addHandler(spy)
        saved_level = root.level
        root.setLevel(logging.WARNING)
        config_before = structlog.get_config()
        try:
            with production_logging_context():
                assert spy.close_calls == 0 and spy not in root.handlers  # detached, not closed
                installed = list(root.handlers)
                assert installed and all(h is not spy for h in installed)
                assert root.level == logging.INFO  # bootstrap really ran
                logging.getLogger("x").warning("inside")
            assert spy.close_calls == 0  # never closed by the fixture
            assert spy in root.handlers and root.level == logging.WARNING
            assert all(h not in root.handlers for h in installed)  # bootstrap handlers removed
            assert structlog.get_config() == config_before
            logging.getLogger("x").warning("after")
            assert spy.messages == ["after"] and spy.close_calls == 0  # still open and delivering
        finally:
            root.removeHandler(spy)
            root.setLevel(saved_level)
            structlog.configure(**config_before)

    def test_restoration_also_happens_when_the_body_raises(self) -> None:
        root = logging.getLogger()
        spy = _SpyHandler()
        root.addHandler(spy)
        config_before = structlog.get_config()
        try:
            with pytest.raises(RuntimeError), production_logging_context():
                raise RuntimeError("boom")
            assert spy in root.handlers and spy.close_calls == 0
            assert structlog.get_config() == config_before
        finally:
            root.removeHandler(spy)
            structlog.configure(**config_before)


def _record(name: str, level: int, msg: Any) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


GENUINE = {"event_type": LogEvent.LLM_FAILED.value, "logger": "llm_client", "msg": "LLM request failed: m"}
SPOOFS = {
    "other-logger-same-text": [_record("some.other", logging.ERROR, "LLM request failed: forged")],
    "client-logger-text-only": [_record("llm_client", logging.ERROR, "LLM request failed: m")],
    "client-logger-missing-event-type": [_record("llm_client", logging.ERROR, {"msg": "LLM request failed: m"})],
    "client-logger-wrong-event-type": [_record("llm_client", logging.ERROR,
                                               {**GENUINE, "event_type": LogEvent.RUN_END.value})],
    "right-event-wrong-logger": [_record("agent", logging.ERROR, dict(GENUINE))],
    "extra-unrelated-error-beside-genuine": [_record("llm_client", logging.ERROR, dict(GENUINE)),
                                             _record("react_loop", logging.ERROR, "classified as failed")],
}


def _exact_one_failure(caplog: Any) -> None:
    _assert_exactly_client_failures(caplog, 1)


@pytest.mark.parametrize("case", sorted(SPOOFS))
def test_failure_lookup_ignores_spoofed_events(case: str) -> None:
    """Text matches are not sufficient to identify a structured client failure."""
    assert _failed(SimpleNamespace(records=SPOOFS[case])) == (
        SPOOFS[case][:1] if case == "extra-unrelated-error-beside-genuine" else []
    )


@pytest.mark.parametrize("helper", [_exact_one_failure, lambda c: _assert_only_client_request_failures(c, 1)],
                         ids=["prereq-exact", "6.6-bounded"])
class TestFailureEventIdentityIsStructured:

    @pytest.mark.parametrize("case", sorted(SPOOFS))
    def test_spoofed_or_unstructured_error_records_are_rejected(self, helper: Any, case: str) -> None:
        with pytest.raises(AssertionError):
            helper(SimpleNamespace(records=SPOOFS[case]))

    def test_the_genuine_structured_client_failure_is_accepted(self, helper: Any) -> None:
        helper(SimpleNamespace(records=[_record("llm_client", logging.ERROR, dict(GENUINE))]))

    def test_more_failures_than_requests_are_rejected(self, helper: Any) -> None:
        two = [_record("llm_client", logging.ERROR, dict(GENUINE)) for _ in range(2)]
        with pytest.raises(AssertionError):
            helper(SimpleNamespace(records=two))
