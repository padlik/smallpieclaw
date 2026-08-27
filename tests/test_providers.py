"""
Tests for shared provider utilities and backend helpers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from providers._utils import make_on_retry, run_with_retry


class TestMakeOnRetry:
    """Exercise the shared retry-notification factory used by all LLM providers."""

    def test_calls_progress_cb_with_retry_message(self):
        progress_cb = MagicMock()
        on_retry = make_on_retry(progress_cb)

        on_retry(2, 5, "timeout: connect timed out")

        progress_cb.assert_called_once_with(
            "⏳ LLM request failed (timeout: connect timed out), retry 2/5…"
        )

    def test_no_progress_cb_does_nothing(self):
        on_retry = make_on_retry(None)

        # Should not raise and should return None.
        assert on_retry(1, 3, "HTTP 503") is None

    def test_progress_cb_falsey_does_not_call(self):
        progress_cb = MagicMock()
        on_retry = make_on_retry(None)

        on_retry(1, 3, "HTTP 503")

        progress_cb.assert_not_called()

    def test_signature_matches_with_retry(self):
        on_retry = make_on_retry(MagicMock())

        # _with_retry passes (attempt, max_retries, reason).
        assert on_retry.__code__.co_argcount == 3


class TestRunWithRetry:
    """Exercise the shared retry runner that wires ``make_on_retry`` + ``_with_retry``."""

    def _ctx(self, max_retries=2, retry_delay=0.0, cancel_event=None):
        from interfaces import ProviderContext
        return ProviderContext(
            get_cfg=lambda: {"model": "test-model"},
            http=MagicMock(),
            max_retries=max_retries,
            retry_delay=retry_delay,
            cancel_event=cancel_event,
            caller_tag="test",
            diagnose_empty=False,
            track_usage=MagicMock(),
        )

    def test_returns_first_success(self):
        ctx = self._ctx()
        do_request = MagicMock(return_value="ok")

        result = run_with_retry(ctx, do_request)

        assert result == "ok"
        do_request.assert_called_once()

    def test_retries_and_calls_progress_cb(self):
        progress_cb = MagicMock()
        ctx = self._ctx(max_retries=2, retry_delay=0.0)
        import httpx
        do_request = MagicMock(
            side_effect=[httpx.TimeoutException("boom"), "ok"]
        )

        result = run_with_retry(ctx, do_request, progress_cb=progress_cb)

        assert result == "ok"
        assert do_request.call_count == 2
        progress_cb.assert_called_once()
        assert "retry 1/2" in progress_cb.call_args[0][0]

    def test_propagates_after_exhaustion(self):
        import httpx
        ctx = self._ctx(max_retries=1, retry_delay=0.0)
        do_request = MagicMock(side_effect=httpx.TimeoutException("final"))

        with pytest.raises(httpx.TimeoutException, match="final"):
            run_with_retry(ctx, do_request)

        assert do_request.call_count == 1

    def test_cancellation_propagates(self):
        from threading import Event
        cancel_event = Event()
        cancel_event.set()
        ctx = self._ctx(cancel_event=cancel_event)
        do_request = MagicMock()

        from providers._errors import LLMCancelledError
        with pytest.raises(LLMCancelledError):
            run_with_retry(ctx, do_request)

        do_request.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
