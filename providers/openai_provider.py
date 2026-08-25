"""
providers/openai_provider.py
----------------------------
OpenAI-compatible chat + embeddings backend extracted from ``llm_client.py``
(Phase 2.2).

A single :func:`chat` serves both the text-only path (returns ``str``) and the
native tool-calling path (returns :class:`ChatResponse`), selected by whether
``tools`` is supplied — this merges the former ``_openai_chat`` and
``_openai_chat_with_tools`` methods, whose payload construction and response
parsing were nearly identical. :func:`embed` covers OpenAI-compatible
embeddings. Shared retry, image-encoding, reasoning-model detection,
thinking-tag, and diagnostic helpers come from ``providers._utils``; the error
hierarchy comes from ``providers._errors``. Mutable client state is supplied by
the caller through :class:`ProviderContext` rather than an ``LLMClient``
instance.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from interfaces import ChatResponse, ProviderContext, ToolCall
from providers._errors import (
    _PERMANENT_ERROR_CODES,
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)
from providers._utils import (
    _encode_images,
    _is_reasoning_model,
    _strip_thinking_tags,
    _with_retry,
    diagnose_empty_response,
    make_on_retry,
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
    """Send an OpenAI-compatible chat request.

    When ``tools`` is ``None`` this behaves like the original ``_openai_chat``
    and returns the assistant text as ``str``. When ``tools`` is provided it
    behaves like ``_openai_chat_with_tools``: ``tools``/``tool_choice`` are added
    to the payload, ``response_format`` is omitted (mutually exclusive with
    tools), any ``tool_calls`` in the response are parsed, and a
    :class:`ChatResponse` is returned. ``json_mode`` is honoured only on the
    text-only path.
    """
    _initial_model = ctx.get_cfg()["model"]
    use_tools = tools is not None

    # Pre-encode images once — pure data transformation independent of the active
    # model, so results are reused across retries. Native tool-calling fields
    # (`tool_calls` on assistant turns, `tool_call_id` on tool turns) are carried
    # through so multi-turn tool payloads stay well-formed; harmless for text-only
    # calls where these keys are absent.
    encoded_messages: list[dict] = []
    for m in messages:
        imgs = m.get("images")
        encoded = _encode_images(imgs) if imgs else None
        encoded_messages.append({
            "_role": m["role"],
            "_content": m.get("content", ""),
            "_encoded": encoded if encoded else None,
            "_tool_calls": m.get("tool_calls"),
            "_tool_call_id": m.get("tool_call_id"),
        })

    _on_retry = make_on_retry(progress_cb)

    def _do_request():
        # Re-read active model config on every attempt so that a mid-flight
        # model switch (set_model) is picked up by subsequent retries.
        cfg = ctx.get_cfg()
        model = cfg["model"]
        reasoning = _is_reasoning_model(model)

        # Rebuild payload_messages each attempt: the system role format depends
        # on whether the (potentially new) model is a reasoning model.
        payload_messages: list[dict[str, Any]] = []
        if system:
            if reasoning:
                # o-series models don't support the "system" role — embed it as
                # the first user turn so context is still passed through.
                payload_messages.append({
                    "role": "user",
                    "content": f"[Instructions]\n{system}",
                })
            else:
                payload_messages.append({"role": "system", "content": system})
        for em in encoded_messages:
            if em["_encoded"]:
                img_content: list[Any] = [{"type": "text", "text": em["_content"]}]
                for b64, mime in em["_encoded"]:
                    img_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                payload_messages.append({"role": em["_role"], "content": img_content})
            else:
                pm: dict[str, Any] = {"role": em["_role"], "content": em["_content"]}
                # Preserve native tool-calling fields on the wire: assistant
                # messages carry `tool_calls`, tool-result messages carry
                # `tool_call_id`. Dropping either yields an API 400 on turn 2.
                if em.get("_tool_calls"):
                    pm["tool_calls"] = em["_tool_calls"]
                if em.get("_tool_call_id"):
                    pm["tool_call_id"] = em["_tool_call_id"]
                payload_messages.append(pm)

        payload: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            # `max_completion_tokens` is correct for OpenAI. NOTE: some
            # OpenRouter-proxied models expect `max_tokens` instead and will
            # reject this key — resolve that via provider config, not here.
            "max_completion_tokens": cfg.get("max_tokens", 1024),
        }
        if use_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if reasoning:
            # Reasoning models (o1, o3, o4-mini, gpt-5, …) reject temperature,
            # top_p, frequency_penalty, and presence_penalty entirely.
            logger.debug("Reasoning model detected (%s) — omitting sampling params", model)
        else:
            payload["temperature"] = cfg.get("temperature", 0.2)
            top_p = cfg.get("top_p")
            if top_p is not None:
                payload["top_p"] = top_p
            # `response_format` is mutually exclusive with `tools`; request strict
            # JSON only on the text-only path. Not supported by reasoning models.
            if json_mode and not use_tools:
                payload["response_format"] = {"type": "json_object"}

        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        r = ctx.http.post(url, headers=headers, json=payload)
        r.raise_for_status()
        d = r.json()
        usage = d.get("usage", {})
        ctx.track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

        # Some APIs return HTTP 200 with an error body (e.g. rate limit, content
        # filter). Detect this before trying to access 'choices'.
        if "error" in d and "choices" not in d:
            err = d["error"]
            err_msg = err.get("message") or err.get("msg") or str(err)
            err_code = str(err.get("code") or err.get("type") or "").lower()
            exc_class = LLMPermanentError if err_code in _PERMANENT_ERROR_CODES else LLMError
            raise exc_class(
                f"API error from model '{model}'"
                + (f" [{err_code}]" if err_code else "")
                + f": {err_msg}"
            )

        choices = d.get("choices") or []
        if not choices:
            logger.error(
                "Model '%s': response missing 'choices'. Raw body: %s",
                model, str(d)[:500],
            )
            if ctx.diagnose_empty:
                curl_cmd = [
                    "curl", "-s", "-X", "POST", url,
                    "-H", f"Authorization: Bearer {cfg['api_key']}",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(payload),
                ]
                report = diagnose_empty_response(r, "openai", model, curl_cmd=curl_cmd)
                raise LLMEmptyResponseError(
                    f"Model '{model}' returned no choices.\n{report}"
                )
            raise LLMEmptyResponseError(
                f"Model '{model}' returned no choices. Body: {str(d)[:300]}"
            )

        msg = choices[0].get("message") or {}

        # On the tool-calling path, native tool calls take priority over text.
        if use_tools:
            tool_calls_raw = msg.get("tool_calls") or []
            if tool_calls_raw:
                parsed: list[ToolCall] = []
                for tc in tool_calls_raw:
                    func = tc.get("function") or {}
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    parsed.append(ToolCall(
                        id=tc.get("id", ""),
                        name=func.get("name", ""),
                        arguments=args,
                    ))
                return ChatResponse(tool_calls=parsed)

        text = (msg.get("content") or "").strip()
        # Strip inline <think>…</think> reasoning blocks before any further checks.
        text = _strip_thinking_tags(text)

        # Some reasoning/thinking models (DeepSeek-R1, Kimi K2.5, QwQ, etc.)
        # leave "content" empty and put the actual response in "reasoning" or
        # "reasoning_content". Fall back to those fields transparently.
        if not text:
            for fallback_key in ("reasoning", "reasoning_content"):
                fallback = (msg.get(fallback_key) or "").strip()
                if fallback:
                    logger.warning(
                        "Model '%s': content field is empty, using '%s' field as fallback",
                        model, fallback_key,
                    )
                    return ChatResponse(text=fallback) if use_tools else fallback

        if not text:
            if ctx.diagnose_empty:
                curl_cmd = [
                    "curl", "-s", "-X", "POST", url,
                    "-H", f"Authorization: Bearer {cfg['api_key']}",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(payload),
                ]
                report = diagnose_empty_response(r, "openai", model, curl_cmd=curl_cmd)
                raise LLMEmptyResponseError(
                    f"OpenAI returned empty content (model: {model})\n{report}"
                )
            raise LLMEmptyResponseError(f"OpenAI returned empty content (model: {model})")
        return ChatResponse(text=text) if use_tools else text

    return _with_retry(_do_request, ctx.max_retries, ctx.retry_delay, on_retry=_on_retry,
                       cancel_event=ctx.cancel_event, model_name=_initial_model,
                       caller_tag=ctx.caller_tag)


def embed(ctx: ProviderContext, text: str) -> list[float]:
    """Return an embedding vector via an OpenAI-compatible ``/embeddings`` endpoint.

    Uses ``ctx.emb_cfg`` (the resolved embeddings config, which may differ from
    the active chat model) for the base URL, API key, and model name.
    """
    resp = _with_retry(
        lambda: ctx.http.post(
            f"{ctx.emb_cfg['base_url'].rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {ctx.emb_cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={"model": ctx.emb_cfg["model"], "input": text},
        ),
        ctx.max_retries, ctx.retry_delay,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
