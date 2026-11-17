"""Anthropic Messages API chat adapter (JSON-only tool flow)."""

from __future__ import annotations

import json
from typing import Any

from interfaces import ProviderContext
from providers._errors import LLMEmptyResponseError, LLMError
from providers._utils import _encode_images, raise_provider_api_error


def build_payload(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Build the provider-native message body, preserving nested content."""
    del tools, json_mode
    converted: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        images = item.pop("images", None)
        if images:
            encoded = _encode_images(images)
            if encoded:
                blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": item.get("content", "")}
                ]
                blocks.extend(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": data},
                    }
                    for data, mime in encoded
                )
                item["content"] = blocks
        item.setdefault("content", "")
        converted.append(item)
    payload: dict[str, Any] = {
        "model": ctx.model.upstream_id,
        "messages": converted,
    }
    if system:
        payload["system"] = system
    return payload


def chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    progress_cb=None,
) -> str:
    """Send one Anthropic request and return its text block."""
    del progress_cb
    response = ctx.send_json(
        build_payload(ctx, messages, system, tools=tools, json_mode=json_mode)
    )
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        raise LLMError("Provider returned an invalid response") from None
    if not isinstance(data, dict):
        raise LLMError("Provider returned an invalid response")
    error = data.get("error")
    if error is not None:
        raise_provider_api_error(error)
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        ctx.track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise LLMEmptyResponseError("Provider returned no content")
    text = str(content[0].get("text") or "") if isinstance(content[0], dict) else ""
    if not text.strip():
        raise LLMEmptyResponseError("Provider returned empty content")
    return text.strip()
