"""
Tests for shared provider utilities and backend helpers.
"""

from __future__ import annotations


import pytest


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
