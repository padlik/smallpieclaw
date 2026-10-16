"""Cloud Ollama adapter using its installed SDK and serialized-request hook."""

from __future__ import annotations

import json
from typing import Any

import httpx
import ollama as _ollama_lib

from interfaces import ChatResponse, ProviderContext, ToolCall
from providers._errors import LLMEmptyResponseError, LLMError, LLMPermanentError
from providers._utils import (
    _encode_images,
    _extract_thinking_content,
    _strip_thinking_tags,
    is_context_overflow_message,
    provider_error_code,
)


def build_sdk_arguments(
    messages: list[dict],
    system: str | None,
) -> list[dict[str, Any]]:
    """Build SDK Message inputs while retaining tool-wire fields and images."""
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})
    call_names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if call.get("id") and function.get("name"):
                call_names[call["id"]] = function["name"]
    for message in messages:
        item = dict(message)
        images = item.pop("images", None)
        if images:
            item["images"] = [data for data, _mime in _encode_images(images)]
        if item.get("tool_calls"):
            tool_calls = []
            for call in item["tool_calls"]:
                function = dict(call.get("function") or {})
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                function["arguments"] = arguments if isinstance(arguments, dict) else {}
                tool_calls.append({**call, "function": function})
            item["tool_calls"] = tool_calls
        if item.get("tool_call_id"):
            tool_name = call_names.get(item.pop("tool_call_id"), "")
            if tool_name:
                item["tool_name"] = tool_name
        item["content"] = item.get("content") or ""
        result.append(item)
    return result


def chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    progress_cb=None,
) -> str | ChatResponse:
    """Use the pinned SDK client; its request hook owns count/budget/send."""
    del progress_cb
    if ctx.ollama_client is None:
        raise LLMPermanentError("Cloud Ollama client is unavailable")
    kwargs: dict[str, Any] = {
        "model": ctx.model.upstream_id,
        "messages": build_sdk_arguments(messages, system),
        "stream": False,
    }
    if tools is not None:
        # The HTTP transport restores the immutable caller JSON after the SDK
        # serializes this placeholder; passing schemas here loses valid fields.
        kwargs["tools"] = []
    elif json_mode:
        kwargs["format"] = "json"
    if ctx.graph_extraction:
        kwargs["options"] = {"num_predict": 1024, "temperature": 0.1}
    try:
        response = ctx.ollama_client.chat(**kwargs)
    except _ollama_lib.ResponseError as exc:
        status = getattr(exc, "status_code", None)
        error_text = str(getattr(exc, "error", ""))
        _raise_sdk_response_error(status, error_text)
    except httpx.RequestError as exc:
        _raise_sdk_transport_error(exc)
    except ConnectionError:
        safe_request = httpx.Request("POST", "https://provider.invalid/")
        raise httpx.ConnectError(
            "Provider connection failed", request=safe_request
        ) from None
    except TimeoutError:
        safe_request = httpx.Request("POST", "https://provider.invalid/")
        raise httpx.TimeoutException(
            "Provider request timed out", request=safe_request
        ) from None

    prompt_count = getattr(response, "prompt_eval_count", 0) or 0
    completion_count = getattr(response, "eval_count", 0) or 0
    if prompt_count or completion_count:
        ctx.track_usage(prompt_count, completion_count)
    message = getattr(response, "message", None)
    if message is None:
        raise LLMEmptyResponseError("Cloud Ollama returned no message")
    if tools is not None:
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            parsed = []
            for call in calls:
                function = call.function
                arguments = getattr(function, "arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                parsed.append(
                    ToolCall(
                        id=getattr(call, "id", "") or "",
                        name=function.name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            return ChatResponse(tool_calls=parsed)
    raw_text = str(getattr(message, "content", "") or "")
    text = _strip_thinking_tags(raw_text)
    if not text:
        text = str(getattr(message, "thinking", "") or "").strip()
    if not text:
        text = _extract_thinking_content(raw_text)
    if not text:
        raise LLMEmptyResponseError("Cloud Ollama returned empty content")
    return ChatResponse(text=text) if tools is not None else text


def _raise_sdk_response_error(status: int | None, error_text: str) -> None:
    """Preserve status/context categories without retaining SDK response text."""
    if isinstance(status, int) and status in {400, 413} and is_context_overflow_message(error_text):
        safe_request = httpx.Request("POST", "https://provider.invalid/")
        safe_response = httpx.Response(
            status, request=safe_request, text="context_length_exceeded"
        )
        raise httpx.HTTPStatusError(
            f"Provider returned HTTP {status}: context_length_exceeded",
            request=safe_request,
            response=safe_response,
        ) from None
    if isinstance(status, int) and (status == 429 or status >= 500):
        safe_request = httpx.Request("POST", "https://provider.invalid/")
        safe_response = httpx.Response(status, request=safe_request)
        raise httpx.HTTPStatusError(
            f"Provider returned HTTP {status}", request=safe_request, response=safe_response
        ) from None
    code = None
    try:
        error_payload = json.loads(error_text)
    except (json.JSONDecodeError, TypeError):
        error_payload = None
    if isinstance(error_payload, dict):
        code = provider_error_code(error_payload.get("error"))
    if status in {400, 401, 403, 404, 413} or code is not None:
        message = "Cloud Ollama rejected the request"
        if code is not None:
            message += f" ({code})"
        raise LLMPermanentError(message) from None
    raise LLMError("Cloud Ollama returned an API error") from None


def _raise_sdk_transport_error(exc: httpx.HTTPError) -> None:
    """Raise a sanitized transport error retaining only its safe category."""
    safe_request = httpx.Request("POST", "https://provider.invalid/")
    if isinstance(exc, httpx.TimeoutException):
        raise type(exc)("Provider request timed out", request=safe_request) from None
    if isinstance(exc, httpx.ConnectError):
        raise httpx.ConnectError("Provider connection failed", request=safe_request) from None
    if isinstance(exc, httpx.NetworkError):
        raise type(exc)("Provider transport failed", request=safe_request) from None
    raise LLMError("Provider transport failed") from None
