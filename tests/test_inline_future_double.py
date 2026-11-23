"""Faithful inline pool double: worker faults live in the Future, not at submit."""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from sub_agent_registry import SubAgentRecord, SubAgentRegistry
from tests.inline_futures import run_inline_future
from tests.test_sub_agent_supervisor import FakeRunner, _seq_factory


def test_helper_returns_completed_future_with_result() -> None:
    """A successful callable yields a done Future carrying its result."""
    future = run_inline_future(lambda: 7)

    assert isinstance(future, concurrent.futures.Future)
    assert future.done() and future.result() == 7


def test_helper_captures_exception_in_future() -> None:
    """An ordinary exception is captured, not raised synchronously."""
    error = RuntimeError("boom")

    def fail() -> None:
        raise error

    future = run_inline_future(fail)

    with pytest.raises(RuntimeError) as caught:
        future.result()
    assert caught.value is error


def test_helper_captures_base_exception_in_future() -> None:
    """BaseException (cancellation-style) is captured like a real worker."""
    error = KeyboardInterrupt()

    def fail() -> None:
        raise error

    future = run_inline_future(fail)

    assert future.exception() is error


def _spawn_with_terminal_fault(
    exc: Any, runner: FakeRunner, error: Exception, submit: Callable[..., object]
) -> tuple[dict[str, Any], SubAgentRegistry]:
    """Spawn one child whose terminal stage raises *error* via *submit*."""
    local_reg = SubAgentRegistry()
    with patch("sub_agent_registry.get_registry", return_value=local_reg), \
         patch.object(SubAgentRecord, "signal_result", side_effect=error), \
         patch.object(exc._supervisor._pool, "submit", side_effect=submit):
        result = exc._exec_spawn_agent(
            {"task": "child work"}, trace_id="r-owner"
        )
    return result, local_reg


def test_inline_worker_fault_is_accepted_and_held_by_future(
    make_builtin_executor: Callable[..., object], tmp_path: Path
) -> None:
    """Submission is accepted; the same worker fault is held by the Future."""
    runner = FakeRunner(agent_id="sa-inline-fault")
    runner.close = MagicMock(side_effect=runner.close)
    exc = make_builtin_executor(
        sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path)
    )
    error = RuntimeError("terminal fault")

    futures: list[concurrent.futures.Future[None]] = []

    def submit(fn: Callable[[], None], *_a: object, **_k: object) -> object:
        futures.append(run_inline_future(fn))
        return futures[-1]

    result, local_reg = _spawn_with_terminal_fault(exc, runner, error, submit)

    assert result["success"] is True
    with pytest.raises(RuntimeError) as caught:
        futures[0].result()
    assert caught.value is error
    assert runner.close.call_count == 1
    assert local_reg.get(runner.agent_id) is None


def test_real_submit_rejection_remains_distinct_from_worker_fault(
    make_builtin_executor: Callable[..., object], tmp_path: Path
) -> None:
    """A submit rejection raises before the worker runs and rolls back once."""
    runner = FakeRunner(agent_id="sa-inline-reject")
    runner.close = MagicMock(side_effect=runner.close)
    exc = make_builtin_executor(
        sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path)
    )
    ran: list[bool] = []

    def reject(fn: Callable[[], None], *_a: object, **_k: object) -> None:
        raise RuntimeError("pool closed")

    local_reg = SubAgentRegistry()
    with patch("sub_agent_registry.get_registry", return_value=local_reg), \
         patch.object(exc._supervisor._pool, "submit", side_effect=reject), \
         patch.object(runner, "run", side_effect=lambda *a, **k: ran.append(True)):
        with pytest.raises(RuntimeError, match="pool closed"):
            exc._exec_spawn_agent({"task": "child work"}, trace_id="r-owner")

    assert ran == []
    assert runner.close.call_count == 1
    assert local_reg.get(runner.agent_id) is None


# ---------------------------------------------------------------------------
# Terminal-fault diagnostic (ordinary fault crossing the terminal branch)
# ---------------------------------------------------------------------------

SECRET = "SECRET-MARKER-do-not-log"


def _spawn_inline_fault(
    make_builtin_executor: Callable[..., Any], tmp_path: Path, agent_id: str,
    error: Exception,
) -> tuple[FakeRunner, concurrent.futures.Future[None], Any]:
    """Run one accepted child inline whose terminal stage raises *error*."""
    runner = FakeRunner(agent_id=agent_id)
    runner.close = MagicMock(side_effect=runner.close)
    exc = make_builtin_executor(
        sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path)
    )
    futures: list[concurrent.futures.Future[None]] = []

    def submit(fn: Callable[[], None], *_a: object, **_k: object) -> object:
        futures.append(run_inline_future(fn))
        return futures[-1]

    result, _ = _spawn_with_terminal_fault(exc, runner, error, submit)
    assert result["success"] is True
    return runner, futures[0], exc


def _diagnostics(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return supervisor ERROR records for terminal-stage faults."""
    return [
        r for r in caplog.records
        if r.name == "sub_agent_supervisor" and r.levelno == logging.ERROR
    ]


def test_terminal_fault_emits_one_class_only_diagnostic(
    make_builtin_executor: Callable[..., Any], tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One diagnostic names agent and class, never text or traceback."""
    error = ValueError(SECRET)
    with caplog.at_level(logging.DEBUG):
        runner, future, _ = _spawn_inline_fault(
            make_builtin_executor, tmp_path, "sa-diag-once", error
        )

    records = _diagnostics(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "sa-diag-once" in message and "ValueError" in message
    assert records[0].exc_info is None
    assert SECRET not in caplog.text
    assert future.exception() is error
    assert runner.close.call_count == 1


def test_diagnostic_logger_failure_cannot_mask_primary(
    make_builtin_executor: Callable[..., Any], tmp_path: Path,
) -> None:
    """An ordinary logger failure leaves the original error in the Future."""
    error = RuntimeError("primary")
    with patch(
        "sub_agent_supervisor.logger.error", side_effect=OSError("log broke")
    ) as log_error:
        runner, future, _ = _spawn_inline_fault(
            make_builtin_executor, tmp_path, "sa-diag-oserr", error
        )

    assert log_error.call_count == 1
    assert future.exception() is error
    assert runner.close.call_count == 1


def test_diagnostic_cancellation_failure_cannot_mask_primary(
    make_builtin_executor: Callable[..., Any], tmp_path: Path,
) -> None:
    """A cancellation-derived logger failure also leaves the primary intact."""
    error = RuntimeError("primary")
    with patch(
        "sub_agent_supervisor.logger.error", side_effect=KeyboardInterrupt
    ) as log_error:
        runner, future, _ = _spawn_inline_fault(
            make_builtin_executor, tmp_path, "sa-diag-kbd", error
        )

    assert log_error.call_count == 1
    assert future.exception() is error
    assert runner.close.call_count == 1
