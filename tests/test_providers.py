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
    """Ensure provider compatibility plumbing cannot orchestrate retries."""

    def _ctx(self, cancel_event=None):
        from interfaces import ProviderContext
        from model_registry import ModelRecord
        return ProviderContext(
            model=ModelRecord("p/test-model", "p", "test-model", "Test"),
            adapter="openai",
            mode="json",
            send_json=MagicMock(),
            track_usage=MagicMock(),
            cancel_event=cancel_event,
            caller_tag="test",
            diagnose_empty=False,
        )

    def test_returns_first_success(self):
        ctx = self._ctx()
        do_request = MagicMock(return_value="ok")

        result = run_with_retry(ctx, do_request)

        assert result == "ok"
        do_request.assert_called_once()

    def test_transient_failure_is_not_retried_or_notified(self):
        progress_cb = MagicMock()
        import httpx
        ctx = self._ctx()
        do_request = MagicMock(side_effect=httpx.TimeoutException("boom"))

        with pytest.raises(httpx.TimeoutException, match="boom"):
            run_with_retry(ctx, do_request, progress_cb=progress_cb)

        do_request.assert_called_once()
        progress_cb.assert_not_called()

    def test_propagates_after_exhaustion(self):
        import httpx
        ctx = self._ctx()
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


class TestCloudOllamaClientConstructionGuards:
    """Real LLMClient construction refuses unusable cloud Ollama pins before I/O."""

    _FAKE_KEY = "fake-ollama-guard-key"

    def _construct(self, base_url: str, api_key: str) -> tuple[pytest.ExceptionInfo, list]:
        """Construct a pinned cloud Ollama client expecting rejection; return requests."""
        import httpx

        from config_schema import AgentConfig, ResolvedProviderConnection
        from llm_client import LLMClient
        from model_registry import ModelRecord, RunModel
        from providers._errors import LLMPermanentError

        requests: list = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={})

        model = ModelRecord("cloud/model", "cloud", "model", "Cloud model")
        connection = ResolvedProviderConnection("cloud", "ollama", base_url, 20, api_key)
        with pytest.raises(LLMPermanentError) as caught:
            LLMClient(
                RunModel(model, connection, 1, 1),
                agent_config=AgentConfig(max_retries=0, retry_delay=0),
                http_transport=httpx.MockTransport(handler),
            )
        return caught, requests

    @pytest.mark.parametrize("api_key", ["", "   "])
    def test_empty_cloud_api_key_is_rejected_before_io(self, api_key):
        caught, requests = self._construct("https://ollama.com", api_key)

        assert str(caught.value) == "Cloud Ollama requires an API credential"
        assert requests == []

    @pytest.mark.parametrize("base_url", ["https://ollama.com/api", "https://ollama.com/api/"])
    def test_authenticated_non_root_official_base_is_rejected_before_sdk_request(self, base_url):
        caught, requests = self._construct(base_url, self._FAKE_KEY)

        assert str(caught.value) == "Cloud Ollama requires the official https://ollama.com origin"
        assert self._FAKE_KEY not in str(caught.value)
        assert self._FAKE_KEY not in repr(caught.value)
        assert requests == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
