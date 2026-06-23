"""P2: vision-aware fallback routing tests.

Covers LLMClient.chat_with_fallback() image handling:
- Image request with a non-vision primary and a vision fallback uses the vision
  fallback and never calls the non-vision primary.
- Image request with no vision-capable candidate raises a clear permanent error.
- Text-only requests keep the existing candidate order / fallback behaviour.
- Active model index is restored on the no-vision error path.
"""

from __future__ import annotations

import pytest

from llm_client import LLMClient, LLMError, LLMPermanentError


def _config(models: list[dict], default: str, fallback: list[str]) -> dict:
    return {
        "models": models,
        "agent": {"default_model": default, "fallback_models": fallback},
    }


_NONVISION = {
    "name": "text-model", "provider": "openai", "model": "text-x",
    "api_key": "sk-x", "base_url": "https://api.openai.com/v1",
}
_VISION = {
    "name": "vision-model", "provider": "openai", "model": "vision-x",
    "api_key": "sk-x", "base_url": "https://api.openai.com/v1", "vision": True,
}


def _make_client(models, default, fallback):
    return LLMClient(_config(models, default, fallback))


class TestVisionFallback:
    def test_image_skips_nonvision_primary_for_vision_fallback(self, monkeypatch):
        client = _make_client([_NONVISION, _VISION], "text-x", ["vision-x"])
        used: list[str] = []

        def fake_chat(messages, system=None, progress_cb=None, json_mode=False):
            used.append(client._models[client._active_idx]["model"])
            return "ok"

        monkeypatch.setattr(client, "chat", fake_chat)
        msgs = [{"role": "user", "content": "what is this?", "images": ["/tmp/x.png"]}]
        out = client.chat_with_fallback(msgs)
        assert out == "ok"
        # Non-vision primary must never be invoked.
        assert used == ["vision-x"]

    def test_image_no_vision_candidate_raises_permanent(self):
        client = _make_client([_NONVISION], "text-x", [])
        msgs = [{"role": "user", "content": "see this", "images": ["/tmp/x.png"]}]
        with pytest.raises(LLMPermanentError) as ei:
            client.chat_with_fallback(msgs)
        assert "vision-capable" in str(ei.value)

    def test_no_vision_error_restores_active_idx(self):
        client = _make_client([_VISION, _NONVISION], "vision-x", [])
        # Force active to the non-vision model.
        client._active_idx = 1
        msgs = [{"role": "user", "content": "see", "images": ["/tmp/x.png"]}]
        with pytest.raises(LLMPermanentError):
            client.chat_with_fallback(msgs)
        assert client._active_idx == 1  # restored to the primary it started from

    def test_multimodal_content_list_detected(self, monkeypatch):
        client = _make_client([_NONVISION, _VISION], "text-x", ["vision-x"])
        used: list[str] = []
        monkeypatch.setattr(client, "chat",
                            lambda *a, **k: used.append(client._models[client._active_idx]["model"]) or "ok")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]
        client.chat_with_fallback(msgs)
        assert used == ["vision-x"]

    def test_progress_message_on_vision_switch(self, monkeypatch):
        client = _make_client([_NONVISION, _VISION], "text-x", ["vision-x"])
        monkeypatch.setattr(client, "chat", lambda *a, **k: "ok")
        msgs_seen: list[str] = []
        client.chat_with_fallback(
            [{"role": "user", "content": "x", "images": ["/tmp/x.png"]}],
            progress_cb=msgs_seen.append,
        )
        assert any("not vision-capable" in m for m in msgs_seen)


class TestTextOnlyUnaffected:
    def test_text_only_uses_primary_first(self, monkeypatch):
        client = _make_client([_NONVISION, _VISION], "text-x", ["vision-x"])
        used: list[str] = []
        monkeypatch.setattr(client, "chat",
                            lambda *a, **k: used.append(client._models[client._active_idx]["model"]) or "ok")
        out = client.chat_with_fallback([{"role": "user", "content": "plain text"}])
        assert out == "ok"
        assert used == ["text-x"]  # primary, no vision filtering

    def test_text_only_falls_back_on_error(self, monkeypatch):
        client = _make_client([_NONVISION, _VISION], "text-x", ["vision-x"])
        used: list[str] = []

        def fake_chat(*a, **k):
            model = client._models[client._active_idx]["model"]
            used.append(model)
            if model == "text-x":
                raise LLMError("transient")
            return "ok"

        monkeypatch.setattr(client, "chat", fake_chat)
        out = client.chat_with_fallback([{"role": "user", "content": "plain"}])
        assert out == "ok"
        assert used == ["text-x", "vision-x"]  # tried primary then fallback
