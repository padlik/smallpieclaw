"""OpenAI-compatible chat and native-tool response handling."""

from __future__ import annotations

import json
from typing import Any

from interfaces import ChatResponse, ProviderContext, ToolCall
from providers._errors import LLMEmptyResponseError, LLMError
from providers._utils import (
    _encode_images,
    _is_reasoning_model,
    _strip_thinking_tags,
    raise_provider_api_error,
)


def build_payload(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Build the complete OpenAI-compatible payload without generation knobs."""
    model = ctx.model.upstream_id
    reasoning = _is_reasoning_model(model)
    payload_messages: list[dict[str, Any]] = []
    if system:
        if reasoning:
            payload_messages.append({"role": "user", "content": f"[Instructions]\n{system}"})
        else:
            payload_messages.append({"role": "system", "content": system})
    for message in messages:
        item = dict(message)
        images = item.pop("images", None)
        if images:
            encoded = _encode_images(images)
            if encoded:
                parts: list[Any] = [{"type": "text", "text": item.get("content", "")}]
                parts.extend(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
                    for data, mime in encoded
                )
                item["content"] = parts
        item.setdefault("content", "")
        payload_messages.append(item)

    payload: dict[str, Any] = {"model": model, "messages": payload_messages}
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    elif json_mode and not reasoning:
        payload["response_format"] = {"type": "json_object"}
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
    """Send one already-pinned OpenAI-compatible request and parse its response."""
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
        ctx.track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return parse_chat_response(data, tools=tools is not None)


def parse_chat_response(data: dict[str, Any], *, tools: bool) -> str | ChatResponse:
    """Parse a shared OpenAI-compatible completion response."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMEmptyResponseError("Provider returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMEmptyResponseError("Provider returned no message")
    if tools:
        raw_calls = message.get("tool_calls") or []
        if raw_calls:
            parsed: list[ToolCall] = []
            for item in raw_calls:
                function = item.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                parsed.append(
                    ToolCall(
                        id=item.get("id", ""),
                        name=function.get("name", ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            return ChatResponse(tool_calls=parsed)
    text = _strip_thinking_tags(str(message.get("content") or ""))
    if not text:
        for key in ("reasoning", "reasoning_content"):
            candidate = str(message.get(key) or "").strip()
            if candidate:
                text = candidate
                break
    if not text:
        raise LLMEmptyResponseError("Provider returned empty content")
    return ChatResponse(text=text) if tools else text
