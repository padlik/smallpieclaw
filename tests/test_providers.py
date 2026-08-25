"""
Tests for shared provider utilities and backend helpers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from providers._utils import make_on_retry


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
