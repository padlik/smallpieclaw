"""P2: vision-aware routing tests (all-models scan + revert).

Covers LLMClient.chat_with_fallback() image handling after the fallback removal:
- Image request with a non-vision primary routes to the first vision-capable
  model by config order (scanning ALL configured models, not a fallback list).
- The active model index is restored to the primary after the request completes
  (success or error).
- Image request with no vision-capable model configured raises a clear
  permanent error.
- Active model already vision-capable → no switch.
- Text-only requests use the primary model directly (single-model, no fallback).
"""

from __future__ import annotations

import pytest

from llm_client import LLMClient, LLMError, LLMPermanentError


def _config(models: list[dict], default: str) -> dict:
    """Return a minimal config dict for the given models and default model."""
    return {
        "models": models,
        "agent": {"default_model": default},
    }


_NONVISION = {
    "name": "text-model", "provider": "openai", "model": "text-x",
    "api_key": "sk-x", "base_url": "https://api.openai.com/v1",
}
_VISION = {
    "name": "vision-model", "provider": "openai", "model": "vision-x",
    "api_key": "sk-x", "base_url": "https://api.openai.com/v1", "vision": True,
}
_VISION2 = {
    "name": "vision-model-2", "provider": "openai", "model": "vision-y",
    "api_key": "sk-x", "base_url": "https://api.openai.com/v1", "vision": True,
}


def _make_client(models, default):
    """Build an LLMClient from model definitions and a default model name."""
    return LLMClient(_config(models, default))


class TestVisionAllModelsScan:
    def test_image_routes_to_first_vision_model_by_config_order(self, monkeypatch):
        """Image routes to the first vision-capable model by config order."""
        client = _make_client([_NONVISION, _VISION, _VISION2], "text-x")
        used: list[str] = []

        def fake_chat(messages, system=None, progress_cb=None, json_mode=False):
            used.append(client._models[client._active_idx]["model"])
            return "ok"

        monkeypatch.setattr(client, "chat", fake_chat)
        msgs = [{"role": "user", "content": "what is this?", "images": ["/tmp/x.png"]}]
        out = client.chat_with_fallback(msgs)
        assert out == "ok"
        # Non-vision primary must never be invoked; first vision model by order.
        assert used == ["vision-x"]

    def test_primary_restored_after_vision_request(self, monkeypatch):
        """Active model index restored to primary after image request succeeds."""
        client = _make_client([_NONVISION, _VISION], "text-x")
        monkeypatch.setattr(client, "chat", lambda *a, **k: "ok")
        msgs = [{"role": "user", "content": "x", "images": ["/tmp/x.png"]}]
        client.chat_with_fallback(msgs)
        assert client._active_idx == 0  # restored to primary (text-x at index 0)

    def test_primary_restored_after_vision_error(self, monkeypatch):
        """Active model index restored to primary even when the vision call errors."""
        client = _make_client([_NONVISION, _VISION], "text-x")

        def boom(*a, **k):
            raise LLMError("vision model down")

        monkeypatch.setattr(client, "chat", boom)
        msgs = [{"role": "user", "content": "x", "images": ["/tmp/x.png"]}]
        with pytest.raises(LLMError):
            client.chat_with_fallback(msgs)
        assert client._active_idx == 0  # restored to primary despite error

    def test_no_vision_model_raises_permanent(self):
        """No vision-capable model configured → LLMPermanentError."""
        client = _make_client([_NONVISION], "text-x")
        msgs = [{"role": "user", "content": "see this", "images": ["/tmp/x.png"]}]
        with pytest.raises(LLMPermanentError) as ei:
            client.chat_with_fallback(msgs)
        assert "vision-capable" in str(ei.value)

    def test_no_vision_error_restores_active_idx(self):
        """Active idx restored even on the no-vision-model error path."""
        client = _make_client([_NONVISION], "text-x")
        client._active_idx = 0
        with pytest.raises(LLMPermanentError):
            client.chat_with_fallback(
                [{"role": "user", "content": "see", "images": ["/tmp/x.png"]}]
            )
        assert client._active_idx == 0  # restored

    def test_active_model_already_vision_no_switch(self, monkeypatch):
        """Active model is vision-capable → no switch, request sent directly."""
        client = _make_client([_VISION, _NONVISION], "vision-x")
        used: list[str] = []
        monkeypatch.setattr(
            client, "chat",
            lambda *a, **k: used.append(client._models[client._active_idx]["model"]) or "ok",
        )
        msgs = [{"role": "user", "content": "x", "images": ["/tmp/x.png"]}]
        client.chat_with_fallback(msgs)
        assert used == ["vision-x"]  # no switch — active model used directly
        assert client._active_idx == 0  # unchanged

    def test_multimodal_content_list_detected(self, monkeypatch):
        """Provider-style multimodal content lists are detected as images."""
        client = _make_client([_NONVISION, _VISION], "text-x")
        used: list[str] = []
        monkeypatch.setattr(
            client, "chat",
            lambda *a, **k: used.append(client._models[client._active_idx]["model"]) or "ok",
        )
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]
        client.chat_with_fallback(msgs)
        assert used == ["vision-x"]

    def test_progress_message_on_vision_switch(self, monkeypatch):
        """Progress callback receives the vision-switch message."""
        client = _make_client([_NONVISION, _VISION], "text-x")
        monkeypatch.setattr(client, "chat", lambda *a, **k: "ok")
        msgs_seen: list[str] = []
        client.chat_with_fallback(
            [{"role": "user", "content": "x", "images": ["/tmp/x.png"]}],
            progress_cb=msgs_seen.append,
        )
        assert any("not vision-capable" in m for m in msgs_seen)


class TestTextOnlySingleModel:
    def test_text_only_uses_primary(self, monkeypatch):
        """Text-only request uses the primary model directly (no vision filtering)."""
        client = _make_client([_NONVISION, _VISION], "text-x")
        used: list[str] = []
        monkeypatch.setattr(
            client, "chat",
            lambda *a, **k: used.append(client._models[client._active_idx]["model"]) or "ok",
        )
        out = client.chat_with_fallback([{"role": "user", "content": "plain text"}])
        assert out == "ok"
        assert used == ["text-x"]  # primary, no switch

    def test_text_only_error_propagates_no_fallback(self, monkeypatch):
        """Transient error on primary propagates — no fallback attempt (single-model)."""
        client = _make_client([_NONVISION, _VISION], "text-x")
        used: list[str] = []

        def fake_chat(*a, **k):
            model = client._models[client._active_idx]["model"]
            used.append(model)
            raise LLMError("transient")

        monkeypatch.setattr(client, "chat", fake_chat)
        with pytest.raises(LLMError):
            client.chat_with_fallback([{"role": "user", "content": "plain"}])
        # Only the primary was tried — no fallback to vision-x.
        assert used == ["text-x"]
