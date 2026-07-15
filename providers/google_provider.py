"""
providers/google_provider.py
----------------------------
Google Gemini chat + embeddings backend extracted from ``llm_client.py``
(Phase 2.3).

A single :func:`chat` serves both paths, selected by whether ``tools`` is
supplied. Unlike OpenAI — where the text and tool paths share one wire format —
Google splits across two endpoints:

* **text path** (``tools`` is ``None``) uses the native Gemini
  ``:generateContent`` REST surface with ``contents``/``generationConfig`` and
  embeds the system prompt as a leading user/model turn pair.
* **tool path** (``tools`` supplied) uses Google's OpenAI-compatible surface at
  ``/v1beta/openai/chat/completions``, whose request/response shape matches
  :mod:`providers.openai_provider` (the native ``:generateContent`` endpoint
  does not understand OpenAI-format tools).

Both paths authenticate with the ``x-goog-api-key`` header. :func:`embed` calls
the Gemini ``:embedContent`` endpoint, which has its own request/response shape.
Shared retry, image-encoding, thinking-tag, and diagnostic helpers come from
``providers._utils``; the error hierarchy comes from ``providers._errors``.
Mutable client state is supplied by the caller through :class:`ProviderContext`
rather than an ``LLMClient`` instance.
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
    _strip_thinking_tags,
    _with_retry,
    diagnose_empty_response,
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
    """Send a Google Gemini chat request.

    When ``tools`` is ``None`` this behaves like the original ``_google_chat``:
    it uses the native ``:generateContent`` endpoint and returns the assistant
    text as ``str`` (``json_mode`` toggles ``responseMimeType``). When ``tools``
    is provided it behaves like ``_google_chat_with_tools``: it uses the
    OpenAI-compatible ``/v1beta/openai/chat/completions`` endpoint, parses any
    ``tool_calls`` in the response, and returns a :class:`ChatResponse`.
    ``json_mode`` is honoured only on the text-only path.
    """
    if tools is not None:
        return _tool_chat(ctx, messages, system, tools, progress_cb=progress_cb)
    return _text_chat(ctx, messages, system, json_mode=json_mode, progress_cb=progress_cb)


def _text_chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    json_mode: bool,
    progress_cb=None,
) -> str:
    """Native Gemini ``:generateContent`` text path (no tool calling)."""
    # Convert to Gemini format
    contents = []
    if system:
        contents.append({"role": "user", "parts": [{"text": f"[System]: {system}"}]})
        contents.append({"role": "model", "parts": [{"text": "Understood."}]})
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        imgs = m.get("images")
        if imgs:
            encoded = _encode_images(imgs)
            if encoded:
                parts: list[Any] = [{"text": m.get("content", "")}]
                for b64, mime in encoded:
                    parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                contents.append({"role": role, "parts": parts})
                continue
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})

    _initial_model = ctx.get_cfg()["model"]

    def _on_retry(attempt, max_retries, reason):
        if progress_cb:
            progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

    def _do_request():
        # Re-read active model config on every attempt so that a mid-flight
        # model switch (set_model) is picked up by subsequent retries.
        cfg = ctx.get_cfg()
        api_key = cfg["api_key"]
        model = cfg["model"]
        google_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        google_headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        google_payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": cfg.get("max_tokens", 1024),
                "temperature": cfg.get("temperature", 0.2),
            },
        }
        if json_mode:
            google_payload["generationConfig"]["responseMimeType"] = "application/json"
        top_p = cfg.get("top_p")
        if top_p is not None:
            google_payload["generationConfig"]["topP"] = top_p
        r = ctx.http.post(google_url, headers=google_headers, json=google_payload)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            err = d["error"]
            err_code = str(err.get("code") or err.get("status") or "").lower()
            exc_class = LLMPermanentError if err_code in _PERMANENT_ERROR_CODES else LLMError
            raise exc_class(f"Google API error (model: {model}): {err.get('message', err)}")
        meta = d.get("usageMetadata", {})
        ctx.track_usage(meta.get("promptTokenCount", 0), meta.get("candidatesTokenCount", 0))
        candidates = d.get("candidates") or []
        if not candidates:
            logger.error("Google model '%s': response missing 'candidates'. Raw: %s", model, str(d)[:500])
            raise LLMEmptyResponseError(f"Google model '{model}' returned no candidates. Body: {str(d)[:300]}")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text = (parts[0].get("text", "") if parts else "").strip()
        if not text:
            if ctx.diagnose_empty:
                curl_cmd = [
                    "curl", "-s", "-X", "POST", google_url,
                    "-H", f"x-goog-api-key: {api_key}",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(google_payload),
                ]
                report = diagnose_empty_response(r, "google", model, curl_cmd=curl_cmd)
                raise LLMEmptyResponseError(
                    f"Google returned empty content (model: {model})\n{report}"
                )
            raise LLMEmptyResponseError(f"Google returned empty content (model: {model})")
        return text

    return _with_retry(_do_request, ctx.max_retries, ctx.retry_delay, on_retry=_on_retry,
                       cancel_event=ctx.cancel_event, model_name=_initial_model, caller_tag=ctx.caller_tag)


def _tool_chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    tools: list[dict],
    *,
    progress_cb=None,
) -> ChatResponse:
    """Native tool-calling path via Google's OpenAI-compatible endpoint.

    Google exposes an OpenAI-compatible surface at
    ``/v1beta/openai/chat/completions`` that accepts the same
    ``tools``/``tool_choice`` payload and returns ``choices[0].message.tool_calls``
    in OpenAI format. This mirrors :func:`providers.openai_provider.chat`'s
    tool path exactly except for the request URL, the ``x-goog-api-key`` auth
    header, and the ``max_tokens`` token param — the native Gemini
    ``:generateContent`` endpoint does not understand OpenAI-format tools.
    """
    _initial_model = ctx.get_cfg()["model"]

    # Pre-encode images once. Native tool-calling fields are carried through so
    # multi-turn payloads stay well-formed.
    encoded_messages: list[dict] = []
    for m in messages:
        imgs = m.get("images")
        if imgs:
            encoded = _encode_images(imgs)
            if encoded:
                encoded_messages.append({
                    "_role": m["role"],
                    "_content": m.get("content", ""),
                    "_encoded": encoded,
                    "_tool_calls": m.get("tool_calls"),
                    "_tool_call_id": m.get("tool_call_id"),
                })
                continue
        encoded_messages.append({
            "_role": m["role"],
            "_content": m.get("content", ""),
            "_encoded": None,
            "_tool_calls": m.get("tool_calls"),
            "_tool_call_id": m.get("tool_call_id"),
        })

    def _on_retry(attempt, max_retries, reason):
        if progress_cb:
            progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

    def _do_request():
        cfg = ctx.get_cfg()
        model = cfg["model"]

        payload_messages: list[dict[str, Any]] = []
        if system:
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
                if em.get("_tool_calls"):
                    pm["tool_calls"] = em["_tool_calls"]
                if em.get("_tool_call_id"):
                    pm["tool_call_id"] = em["_tool_call_id"]
                payload_messages.append(pm)

        payload: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            # Google's OpenAI-compat surface expects `max_tokens`, not
            # `max_completion_tokens` — the latter 400s and drops the run to
            # the json_mode fallback (see M1, native-tool-calling review).
            "max_tokens": cfg.get("max_tokens", 1024),
            "tools": tools,
            "tool_choice": "auto",
            "temperature": cfg.get("temperature", 0.2),
        }
        top_p = cfg.get("top_p")
        if top_p is not None:
            payload["top_p"] = top_p
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {
            "x-goog-api-key": cfg["api_key"],
            "Content-Type": "application/json",
        }
        r = ctx.http.post(url, headers=headers, json=payload)
        r.raise_for_status()
        d = r.json()
        usage = d.get("usage", {})
        ctx.track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

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
                "Google model '%s': response missing 'choices'. Raw body: %s",
                model, str(d)[:500],
            )
            raise LLMEmptyResponseError(
                f"Google model '{model}' returned no choices. Body: {str(d)[:300]}"
            )

        msg = choices[0].get("message") or {}

        # Check for native tool calls (OpenAI format)
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

        # No tool calls — return text
        text = (msg.get("content") or "").strip()
        text = _strip_thinking_tags(text)
        if not text:
            raise LLMEmptyResponseError(f"Google returned empty content (model: {model})")
        return ChatResponse(text=text)

    return _with_retry(_do_request, ctx.max_retries, ctx.retry_delay, on_retry=_on_retry,
                       cancel_event=ctx.cancel_event, model_name=_initial_model, caller_tag=ctx.caller_tag)


def embed(ctx: ProviderContext, text: str) -> list[float]:
    """Return an embedding vector via the Gemini ``:embedContent`` endpoint.

    Uses ``ctx.emb_cfg`` (the resolved embeddings config, which may differ from
    the active chat model) for the API key and model name. The Gemini embedding
    REST surface and response shape differ entirely from the OpenAI-compatible
    ``/embeddings`` endpoint, so the request is built explicitly here.
    """
    api_key = ctx.emb_cfg["api_key"]
    model = ctx.emb_cfg.get("model", "models/text-embedding-004")
    resp = _with_retry(
        lambda: ctx.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={api_key}",
            json={"content": {"parts": [{"text": text}]}},
        ),
        ctx.max_retries, ctx.retry_delay,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]["values"]
