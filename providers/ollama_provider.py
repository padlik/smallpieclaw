"""
providers/ollama_provider.py
----------------------------
Ollama chat backend extracted from ``llm_client.py`` (Phase 2.4).

A single :func:`chat` serves both the text-only path (returns ``str``) and the
native tool-calling path (returns :class:`ChatResponse`), selected by whether
``tools`` is supplied — this merges the former ``_ollama_chat`` and
``_ollama_chat_with_tools`` methods, whose message construction and
``ollama.Client.chat`` invocation were nearly identical. Ollama has no
embeddings support in this client, so no ``embed`` function is provided.

Unlike the OpenAI/Google/Anthropic backends, Ollama does not use the shared
httpx client in :class:`ProviderContext` (``ctx.http`` is ignored). Ollama has
its own ``ollama.Client`` SDK — one instance per ollama-provider model entry —
so the active client is obtained via ``ctx.get_ollama_client()``, a live accessor
re-read on each retry (so a mid-flight ``set_model()`` switch is honoured) that
keeps ``ProviderContext`` provider-agnostic. Shared retry, image-encoding, and
thinking-tag helpers come from ``providers._utils``; the error hierarchy comes
from ``providers._errors``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import ollama as _ollama_lib

from interfaces import ChatResponse, ProviderContext, ToolCall
from providers._errors import (
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)
from providers._utils import (
    _encode_images,
    _extract_thinking_content,
    _strip_thinking_tags,
    run_with_retry,
)

logger = logging.getLogger(__name__)


def chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    progress_cb=None,
) -> str | ChatResponse:
    """Send a chat request via the Ollama Python library.

    Supports both the Ollama Cloud API (https://ollama.com) and a local Ollama
    instance; the host and optional bearer token are baked into the active
    ``ollama.Client`` (resolved via ``ctx.get_ollama_client``) at construction
    time.

    When ``tools`` is ``None`` this behaves like the original ``_ollama_chat``
    and returns the assistant text as ``str``. When ``tools`` is provided it
    behaves like ``_ollama_chat_with_tools``: ``tools`` is passed to the Ollama
    client, any ``tool_calls`` in the response are parsed, and a
    :class:`ChatResponse` is returned. ``json_mode`` is honoured only on the
    text-only path.

    ``progress_cb`` is used only for retry status messages, never for raw
    content tokens. Reasoning/thinking content wrapped in ``<think>…</think>``
    tags is stripped from the final text before returning.
    """
    use_tools = tools is not None

    # Build the payload messages once — pure data transformation reused across
    # retries. Ollama supports the system role natively.
    payload_messages: list[dict] = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    # Pre-pass: build tool_call_id → tool_name map for role:"tool" result
    # messages. The Ollama SDK Message model uses tool_name (not tool_call_id)
    # to associate tool results with prior tool calls. Harmless (empty) on the
    # text-only path where no message carries tool_calls.
    _call_id_to_name: dict[str, str] = {}
    for _m in messages:
        for _tc in _m.get("tool_calls") or []:
            _tc_id = _tc.get("id") or ""
            _tc_name = (_tc.get("function") or {}).get("name") or ""
            if _tc_id and _tc_name:
                _call_id_to_name[_tc_id] = _tc_name
    for m in messages:
        imgs = m.get("images")
        if imgs:
            encoded = _encode_images(imgs)
            if encoded:
                # Ollama vision: pass base64 strings in the "images" field.
                payload_messages.append({
                    "role": m["role"],
                    "content": m.get("content", ""),
                    "images": [b64 for b64, _ in encoded],
                })
                continue
        # Preserve native tool-calling fields across turns: assistant messages
        # carry `tool_calls`, tool-result messages carry `tool_call_id`.
        # `content` is coerced to "" (never None) for Ollama.
        pm: dict[str, Any] = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            # The native feedback stores arguments as json.dumps(dict) for the
            # OpenAI HTTP wire format, but the Ollama SDK Pydantic Message model
            # requires arguments to be a dict. Normalize on the way in.
            normalized_tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                normalized_tcs.append({**tc, "function": {**fn, "arguments": args}})
            pm["tool_calls"] = normalized_tcs
        if m.get("tool_call_id"):
            # Ollama SDK uses tool_name, not tool_call_id; resolve via lookup.
            _resolved_name = _call_id_to_name.get(m["tool_call_id"], "")
            if _resolved_name:
                pm["tool_name"] = _resolved_name
        payload_messages.append(pm)

    def _do_request():
        # Resolve the active ollama.Client on every attempt via the context
        # accessor so a mid-flight set_model() to a different ollama host is
        # POSTed to the correct client rather than a stale snapshot.
        if ctx.get_ollama_client is None:
            raise LLMError("get_ollama_client not set in ProviderContext")
        client = ctx.get_ollama_client()
        # Re-read active model config on every attempt so that a mid-flight
        # model switch (set_model) is picked up by subsequent retries.
        model = ctx.get_cfg()["model"]
        # If set_model switched to a non-Ollama provider mid-retry, abort
        # immediately rather than attempting to use a mismatched client.
        if ctx.get_cfg().get("provider") != "ollama":
            raise LLMPermanentError(
                f"Active model switched away from Ollama provider during retry "
                f"(now '{ctx.get_cfg().get('provider')}' / '{model}'). Aborting."
            )
        if client is None:
            raise RuntimeError(
                f"ollama_provider.chat called but no ollama.Client was provided "
                f"for model '{model}'. This is a bug — check _ollama_clients init."
            )
        options = {
            "num_predict": ctx.get_cfg().get("max_tokens", 1024),
            "temperature": ctx.get_cfg().get("temperature", 0.2),
        }
        top_p = ctx.get_cfg().get("top_p")
        if top_p is not None:
            options["top_p"] = top_p
        try:
            response = client.chat(
                model=model,
                messages=payload_messages,
                options=options,
                **(
                    {"tools": tools}
                    if use_tools
                    else ({"format": "json"} if json_mode else {})
                ),
            )
            # Track usage before the tool-call check so it runs regardless of
            # whether the response carries tool calls or plain text.
            _eval_count = getattr(response, "eval_count", None) or 0
            _prompt_eval_count = getattr(response, "prompt_eval_count", None) or 0
            if _prompt_eval_count or _eval_count:
                ctx.track_usage(_prompt_eval_count, _eval_count)
            # On the tool-calling path, native tool calls take priority over text.
            if use_tools and response.message.tool_calls:
                parsed: list[ToolCall] = []
                for tc in response.message.tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    parsed.append(ToolCall(
                        id=getattr(tc, "id", "") or "",
                        name=tc.function.name,
                        arguments=args,  # type: ignore[arg-type]
                    ))
                return ChatResponse(tool_calls=parsed)

            text = (response.message.content or "").strip()
        except _ollama_lib.ResponseError as exc:
            # Map to our error hierarchy so _with_retry handles retries correctly.
            status = exc.status_code
            _PERMANENT_STATUSES = {401, 403, 404}
            if status in _PERMANENT_STATUSES:
                raise LLMPermanentError(
                    f"Ollama permanent error (HTTP {status}) for model '{model}': {exc.error}"
                ) from exc
            raise LLMError(
                f"Ollama API error (HTTP {status}) for model '{model}': {exc.error}"
            ) from exc

        # Strip inline <think>…</think> reasoning blocks (DeepSeek-R1, QwQ, etc.).
        raw_text = text
        text = _strip_thinking_tags(raw_text)

        # Some Ollama thinking models (Kimi K2.5, DeepSeek-R1, QwQ, etc.) leave
        # "content" empty or wrap their entire answer in <think> tags. Apply a
        # two-level fallback:
        #   1. response.message.thinking — dedicated field (populated when the
        #      Ollama server separates thinking from content)
        #   2. content of the <think> tags themselves — when the model placed
        #      its answer inside the tags with nothing outside
        if not text:
            _tag = f"[{ctx.caller_tag}/{model}]" if ctx.caller_tag else f"[{model}]"
            thinking_field = (getattr(response.message, "thinking", None) or "").strip()
            if thinking_field:
                logger.warning(
                    "%s content field is empty — using 'thinking' field as fallback",
                    _tag,
                )
                return ChatResponse(text=thinking_field) if use_tools else thinking_field
            think_content = _extract_thinking_content(raw_text)
            if think_content:
                logger.warning(
                    "%s content empty after stripping — using <think> block content as fallback",
                    _tag,
                )
                return ChatResponse(text=think_content) if use_tools else think_content

        if not text:
            raise LLMEmptyResponseError(f"Ollama returned empty content (model: {model})")
        return ChatResponse(text=text) if use_tools else text

    return run_with_retry(ctx, _do_request, progress_cb=progress_cb)
