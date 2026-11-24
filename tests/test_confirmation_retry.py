"""The retry-only ConfirmationManager surface is retired; unrelated flows remain."""

from __future__ import annotations

import threading

import confirmation
from confirmation import ConfirmationManager


def test_llm_retry_api_is_removed_from_the_confirmation_manager() -> None:
    manager = ConfirmationManager()
    for name in ("request_retry", "signal_retry", "_retry_events", "_retry_results"):
        assert not hasattr(manager, name)
    assert not hasattr(confirmation, "RETRY_PREFIX")


def test_tool_confirmation_and_step_extension_flows_remain() -> None:
    manager = ConfirmationManager()
    markers: list[str] = []
    prompted = threading.Event()
    results: dict[str, bool] = {}

    def progress(message: str) -> None:
        markers.append(message)
        prompted.set()

    thread = threading.Thread(
        target=lambda: results.update(
            confirmed=manager.request_confirmation("tok", "shell", "run ls", progress)
        )
    )
    thread.start()
    assert prompted.wait(2)
    manager.signal_confirmation("tok", True)
    thread.join(timeout=3)
    assert markers[0].startswith(f"{confirmation.CONFIRM_PREFIX}:tok:")
    assert results == {"confirmed": True}
    assert hasattr(manager, "request_extension") and hasattr(manager, "signal_extension")
