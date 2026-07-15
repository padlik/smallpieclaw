"""
providers/anthropic_provider.py
-------------------------------
Anthropic Claude chat backend extracted from ``llm_client.py`` (Phase 2.1).

Anthropic's Messages API has no native tool-calling or embeddings support in
this client, so this module exposes a single ``chat`` function. Shared retry,
image-encoding, and diagnostic helpers come from ``providers._utils``; the error
hierarchy comes from ``providers._errors``. Mutable client state is supplied by
the caller through ``ProviderContext`` rather than an ``LLMClient`` instance.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from interfaces import ProviderContext
from providers._errors import (
    _PERMANENT_ERROR_CODES,
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)
from providers._utils import _encode_images, _with_retry, diagnose_empty_response

logger = logging.getLogger(__name__)


def chat(
    ctx: ProviderContext,
    messages: list[dict],
    system: str | None,
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    progress_cb=None,
) -> str:
    """Send a chat request to Anthropic's Messages API and return the text.

    Anthropic has no native tool-calling or JSON mode in this client; callers
    needing structured output fall back to prompt-only enforcement upstream.
    The ``tools`` and ``json_mode`` parameters are accepted for signature parity
    with the other provider backends (so ``LLMClient._provider_chat`` can invoke
    every backend uniformly) but are ignored.
    """
    _initial_model = ctx.get_cfg()["model"]
    anthropic_messages: list[dict] = []
    for m in messages:
        imgs = m.get("images")
        if imgs:
            encoded = _encode_images(imgs)
            if encoded:
                ant_content: list[Any] = [{"type": "text", "text": m.get("content", "")}]
                for b64, mime in encoded:
                    ant_content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    })
                anthropic_messages.append({"role": m["role"], "content": ant_content})
                continue
        anthropic_messages.append({"role": m["role"], "content": m.get("content", "")})

    def _on_retry(attempt, max_retries, reason):
        if progress_cb:
            progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

    def _do_request():
        # Re-read active model config on every attempt so that a mid-flight
        # model switch (set_model) is picked up by subsequent retries.
        cfg = ctx.get_cfg()
        model = cfg["model"]
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": cfg.get("max_tokens", 1024),
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        temperature = cfg.get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature
        top_p = cfg.get("top_p")
        if top_p is not None:
            payload["top_p"] = top_p
        anthropic_url = "https://api.anthropic.com/v1/messages"
        anthropic_headers = {
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        r = ctx.http.post(anthropic_url, headers=anthropic_headers, json=payload)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            err = d["error"]
            err_code = str(err.get("type") or err.get("code") or "").lower()
            exc_class = LLMPermanentError if err_code in _PERMANENT_ERROR_CODES else LLMError
            raise exc_class(f"Anthropic API error (model: {model}): {err.get('message', err)}")
        usage = d.get("usage", {})
        ctx.track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        content_blocks = d.get("content") or []
        if not content_blocks:
            logger.error("Anthropic model '%s': response missing 'content'. Raw: %s", model, str(d)[:500])
            raise LLMEmptyResponseError(f"Anthropic model '{model}' returned no content. Body: {str(d)[:300]}")
        text = (content_blocks[0].get("text", "") or "").strip()
        if not text:
            if ctx.diagnose_empty:
                curl_cmd = [
                    "curl", "-s", "-X", "POST", anthropic_url,
                    "-H", f"x-api-key: {cfg['api_key']}",
                    "-H", "anthropic-version: 2023-06-01",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(payload),
                ]
                report = diagnose_empty_response(r, "anthropic", model, curl_cmd=curl_cmd)
                raise LLMEmptyResponseError(
                    f"Anthropic returned empty content (model: {model})\n{report}"
                )
            raise LLMEmptyResponseError(f"Anthropic returned empty content (model: {model})")
        return text

    return _with_retry(_do_request, ctx.max_retries, ctx.retry_delay, on_retry=_on_retry,
                       cancel_event=ctx.cancel_event, model_name=_initial_model, caller_tag=ctx.caller_tag)
