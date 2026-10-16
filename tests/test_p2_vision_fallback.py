"""Vision capability is explicit and requests remain pinned to one model."""

from __future__ import annotations

import json

import httpx
import pytest

from llm_client import LLMPermanentError
from model_registry import ModelMetadata
from tests.run_model_fixtures import make_run_model_client


def _response() -> httpx.Response:
    """Return a deterministic OpenAI-compatible text response."""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
    )


@pytest.mark.parametrize("vision", [False, None], ids=["verified-false", "unknown"])
def test_unverified_pinned_model_rejects_images_before_io(vision: bool | None) -> None:
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or _response()),
        metadata=ModelMetadata(
            context_tokens=8192,
            output_tokens=1024,
            vision=vision,
            tool_calling=True,
        ),
        upstream_id="text-only-model",
    )
    try:
        with pytest.raises(LLMPermanentError, match="verified vision support"):
            client.chat([{
                "role": "user",
                "content": "what is this?",
                "images": ["does-not-need-to-exist.png"],
            }])
        assert captured == []
        assert client.llm_cfg["model"] == "text-only-model"
    finally:
        client.close()


def test_verified_vision_model_sends_images_on_its_pinned_route(tmp_path) -> None:
    captured: list[httpx.Request] = []
    image = tmp_path / "fake.png"
    image.write_bytes(b"fake-image-bytes")
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or _response()),
        metadata=ModelMetadata(
            context_tokens=8192,
            output_tokens=1024,
            vision=True,
            tool_calling=True,
        ),
        upstream_id="verified-vision-model",
    )
    try:
        assert client.chat_with_fallback([{
            "role": "user", "content": "what is this?", "images": [str(image)]
        }]) == "ok"
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["model"] == "verified-vision-model"
        assert body["messages"][0]["content"][1]["type"] == "image_url"
        with pytest.raises(LLMPermanentError, match="LLMClient is pinned"):
            client.set_model("another-model")
        assert client.list_models()[0]["model"] == "verified-vision-model"
    finally:
        client.close()


def test_text_request_uses_same_pinned_model_without_vision_routing() -> None:
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or _response()),
        metadata=ModelMetadata(
            context_tokens=8192,
            output_tokens=1024,
            vision=False,
            tool_calling=True,
        ),
        upstream_id="chosen-text-model",
    )
    progress: list[str] = []
    try:
        assert client.chat_with_fallback(
            [{"role": "user", "content": "plain text"}], progress_cb=progress.append
        ) == "ok"
        assert len(captured) == 1
        assert json.loads(captured[0].content)["model"] == "chosen-text-model"
        assert progress == []
    finally:
        client.close()


@pytest.mark.parametrize("vision", [False, None], ids=["verified-false", "unknown"])
def test_provider_style_image_content_is_gated_by_pinned_metadata(
    vision: bool | None,
) -> None:
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or _response()),
        metadata=ModelMetadata(
            context_tokens=8192,
            output_tokens=1024,
            vision=vision,
            tool_calling=True,
        ),
        upstream_id="multimodal-model",
    )
    try:
        with pytest.raises(LLMPermanentError, match="verified vision support"):
            client.chat([{
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}},
                ],
            }])
        assert captured == []
        assert client.llm_cfg["model"] == "multimodal-model"
    finally:
        client.close()


def test_text_only_provider_failure_does_not_retry_or_retarget() -> None:
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(
            lambda request: captured.append(request)
            or httpx.Response(503, text="temporary provider failure")
        ),
        metadata=ModelMetadata(
            context_tokens=8192,
            output_tokens=1024,
            vision=False,
            tool_calling=True,
        ),
        upstream_id="chosen-text-model",
    )
    pinned_model = client.llm_cfg["model"]
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.chat_with_fallback([{"role": "user", "content": "plain text"}])

        assert len(captured) == 1
        assert json.loads(captured[0].content)["model"] == "chosen-text-model"
        assert client.llm_cfg["model"] == pinned_model
        assert client.list_models() == [{**client.llm_cfg, "active": True}]
    finally:
        client.close()
