"""Google Gemini text and OpenAI-compatible native-tool adapters."""

from __future__ import annotations

import json
from typing import Any

from interfaces import ChatResponse, ProviderContext
from providers import openai_provider
from providers._errors import LLMEmptyResponseError, LLMError
from providers._utils import _encode_images, _strip_thinking_tags, raise_provider_api_error


def build_payload(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Build the exact text or native-tool wire payload for Gemini."""
    if tools is not None:
        return openai_provider.build_payload(ctx, messages, system, tools=tools)
    contents: list[dict[str, Any]] = []
    if system:
        system_instruction: dict[str, Any] = {"parts": [{"text": system}]}
    else:
        system_instruction = {}
    for message in messages:
        item = dict(message)
        images = item.pop("images", None)
        role = "user" if item.pop("role", None) == "user" else "model"
        content = item.pop("content", "")
        parts: list[dict[str, Any]] = [{"text": content}]
        if images:
            encoded = _encode_images(images)
            parts.extend(
                {"inlineData": {"mimeType": mime, "data": data}}
                for data, mime in encoded
            )
        contents.append({"role": role, "parts": parts})
    payload: dict[str, Any] = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = system_instruction
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
    return payload


def chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    progress_cb=None,
) -> str | ChatResponse:
    """Send one pinned Google request and parse its adapter-specific response."""
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
    if ctx.mode == "native":
        usage = data.get("usage")
        if isinstance(usage, dict):
            ctx.track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    else:
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            ctx.track_usage(
                usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
            )

    if tools is not None:
        return openai_provider.parse_chat_response(data, tools=tools is not None)
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LLMEmptyResponseError("Provider returned no candidates")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    text = "".join(
        str(part.get("text") or "") for part in parts or [] if isinstance(part, dict)
    ).strip()
    text = _strip_thinking_tags(text)
    if not text:
        raise LLMEmptyResponseError("Provider returned empty content")
    return text
