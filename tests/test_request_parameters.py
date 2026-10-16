"""Ordinary request knobs stay absent; graph extraction policy stays fixed."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel


def _run_model(output_tokens: int | None = 2048) -> RunModel:
    model = ModelRecord(
        "p/model", "p", "model", "Model",
        ModelMetadata(context_tokens=4096, output_tokens=output_tokens),
    )
    connection = ResolvedProviderConnection(
        "p", "openai", "https://api.example.test/v1", 15, "fake-key"
    )
    return RunModel(model, connection, 1, 1)


def test_ordinary_chat_request_contains_no_generation_tuning_fields() -> None:
    requests: list[httpx.Request] = []
    client = LLMClient(
        _run_model(),
        agent_config=AgentConfig(ctx_max_tokens=2048),
        http_transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        ),
    )

    client.chat([{"role": "user", "content": "hello"}])

    payload = json.loads(requests[0].content)
    assert not {"max_tokens", "max_completion_tokens", "temperature", "top_p", "options"} & payload.keys()
    with pytest.raises(TypeError):
        LLMClient(_run_model(), agent_config=AgentConfig(), temperature=0.2)  # type: ignore[call-arg]
    client.close()


def test_graph_extraction_policy_is_fixed_and_rejects_known_small_output_limit() -> None:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: requests.append(request)
        or httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    client = LLMClient.for_graph_extraction(
        _run_model(), agent_config=AgentConfig(ctx_max_tokens=2048), http_transport=transport
    )

    client.chat([{"role": "user", "content": "extract"}], json_mode=True)

    payload = json.loads(requests[0].content)
    assert payload["max_completion_tokens"] == 1024
    assert payload["temperature"] == 0.1
    client.close()

    with pytest.raises(Exception, match="output capacity"):
        LLMClient.for_graph_extraction(
            _run_model(output_tokens=512), agent_config=AgentConfig()
        )


def test_public_llm_provider_protocol_signatures_are_unchanged() -> None:
    from interfaces import LLMProvider

    assert tuple(inspect.signature(LLMProvider.chat).parameters) == (
        "self", "messages", "system", "progress_cb", "json_mode"
    )
    assert tuple(inspect.signature(LLMProvider.chat_with_tools).parameters) == (
        "self", "messages", "tools", "system", "progress_cb"
    )
