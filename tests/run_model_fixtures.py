"""Small constructors for tests using immutable admitted model snapshots."""

from __future__ import annotations

import threading

import httpx

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel


def make_run_model_client(
    adapter: str,
    transport: httpx.BaseTransport,
    *,
    metadata: ModelMetadata,
    upstream_id: str = "test-model",
    caller_tag: str = "test",
    cancel_event: threading.Event | None = None,
) -> LLMClient:
    """Build a pinned local-test client with explicit fake credentials/metadata."""
    provider_id = "test-connection"
    model = ModelRecord(
        id=f"{provider_id}/{upstream_id}",
        provider_id=provider_id,
        upstream_id=upstream_id,
        display_name=f"Test {upstream_id}",
        metadata=metadata,
    )
    base_url = {
        "openai": "https://openai.example.test/v1",
        "openrouter": "https://openrouter.example.test/api/v1",
        "google": "https://google.example.test/v1beta",
        "anthropic": "https://anthropic.example.test",
        "ollama": "https://ollama.com",
    }.get(adapter, "https://unknown.example.test")
    connection = ResolvedProviderConnection(
        provider_id=provider_id,
        adapter=adapter,
        base_url=base_url,
        request_timeout=5,
        api_key="fake-test-key",
    )
    run_model = RunModel(
        model=model,
        connection=connection,
        catalog_revision=1,
        selection_revision=1,
    )
    return LLMClient(
        run_model,
        agent_config=AgentConfig(
            ctx_max_tokens=4096,
            max_retries=0,
            retry_delay=0,
        ),
        http_transport=transport,
        cancel_event=cancel_event,
        caller_tag=caller_tag,
    )
