"""Unit tests for the LLM error retry confirmation flow."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from confirmation import ConfirmationManager, RETRY_PREFIX


@pytest.fixture
def manager() -> ConfirmationManager:
    """Return a fresh ConfirmationManager for each test."""
    return ConfirmationManager()


def test_request_retry_retry_response(manager: ConfirmationManager) -> None:
    """Signaling 'retry' returns 'retry' to the blocked caller."""
    token = "test-token-1"
    result_holder: dict[str, str] = {}

    def caller() -> None:
        result_holder["result"] = manager.request_retry(
            token, '{"type":"timeout"}', lambda _msg: None, timeout_seconds=5
        )

    thread = threading.Thread(target=caller)
    thread.start()
    time.sleep(0.1)  # let the thread block on the event
    manager.signal_retry(token, "retry")
    thread.join(timeout=5)

    assert result_holder["result"] == "retry"


def test_request_retry_cancel_response(manager: ConfirmationManager) -> None:
    """Signaling 'cancel' returns 'cancel' to the blocked caller."""
    token = "test-token-2"
    result_holder: dict[str, str] = {}

    def caller() -> None:
        result_holder["result"] = manager.request_retry(
            token, '{"type":"rate_limit"}', lambda _msg: None, timeout_seconds=5
        )

    thread = threading.Thread(target=caller)
    thread.start()
    time.sleep(0.1)
    manager.signal_retry(token, "cancel")
    thread.join(timeout=5)

    assert result_holder["result"] == "cancel"


def test_request_retry_timeout_response(manager: ConfirmationManager) -> None:
    """Without a signal, request_retry returns 'timeout'."""
    token = "test-token-3"
    result_holder: dict[str, str] = {}

    def caller() -> None:
        result_holder["result"] = manager.request_retry(
            token, '{"type":"timeout"}', lambda _msg: None, timeout_seconds=1
        )

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join(timeout=5)

    assert result_holder["result"] == "timeout"


def test_signal_retry_after_timeout_is_no_op(
    manager: ConfirmationManager, caplog: pytest.LogCaptureFixture
) -> None:
    """Signaling after timeout logs a warning and leaves the timeout result."""
    token = "test-token-4"
    result_holder: dict[str, str] = {}

    def caller() -> None:
        result_holder["result"] = manager.request_retry(
            token, '{"type":"timeout"}', lambda _msg: None, timeout_seconds=1
        )

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join(timeout=5)

    assert result_holder["result"] == "timeout"

    with caplog.at_level(logging.WARNING):
        manager.signal_retry(token, "retry")

    assert any("already resolved or timed out" in record.message for record in caplog.records)


def test_request_retry_sends_marker(manager: ConfirmationManager) -> None:
    """request_retry emits the expected RETRY_PREFIX marker to progress_cb."""
    token = "test-marker"
    markers: list[str] = []

    def caller() -> None:
        manager.request_retry(
            token, '{"type":"timeout"}', markers.append, timeout_seconds=1
        )

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join(timeout=2)

    assert any(m.startswith(f"{RETRY_PREFIX}:{token}:") for m in markers)
