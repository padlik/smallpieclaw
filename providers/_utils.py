"""
providers/_utils.py
-------------------
Shared, provider-agnostic helpers for LLM backends: retry orchestration,
thinking-tag handling, cosine similarity, reasoning-model detection, image
encoding, and empty-response diagnostics.

These are pure functions (no dependency on ``LLMClient`` state) extracted from
``llm_client.py`` so per-provider backend modules can share them. ``llm_client``
re-imports them for backward compatibility.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re

from providers._errors import (
    LLMError,
    LLMPermanentError,
)

logger = logging.getLogger(__name__)

_PERMANENT_PROVIDER_ERROR_CODES = frozenset({
    "invalid_api_key",
    "authentication_error",
    "permission_denied",
    "permission_error",
    "not_found",
    "not_found_error",
    "model_not_found",
    "invalid_argument",
    "invalid_request",
    "invalid_request_error",
    "content_filter",
    "content_policy_violation",
})
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context window",
    "maximum context",
    "prompt is too long",
    "context length exceeded",
    "token limit",
    "exceeds the maximum number of tokens",
    "input token count",
    "maximum number of tokens allowed",
)


def provider_error_code(error: object) -> str | None:
    """Return a provider error code only when it is in the safe allowlist."""
    if not isinstance(error, dict):
        return None
    for key in ("code", "type", "status"):
        value = error.get(key)
        if isinstance(value, str):
            normalized = value.lower()
            if normalized in _PERMANENT_PROVIDER_ERROR_CODES:
                return normalized
    return None


def raise_provider_api_error(error: object) -> None:
    """Raise a safe typed API error without relaying provider-supplied text."""
    code = provider_error_code(error)
    if code is not None:
        raise LLMPermanentError(f"Provider rejected the request ({code})") from None
    raise LLMError("Provider returned an API error") from None


def is_context_overflow_message(message: str) -> bool:
    """Recognize known context-overflow wording without returning provider text."""
    normalized = message.lower()
    return any(marker in normalized for marker in _CONTEXT_OVERFLOW_MARKERS)


_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_CONTENT_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking_tags(text: str) -> str:
    """Remove <think>…</think> reasoning blocks from LLM output.

    Reasoning models (DeepSeek-R1, QwQ, etc.) sometimes embed chain-of-thought
    inside these tags. Strip them before returning so thinking tokens never reach
    the caller or the Telegram UI.
    """
    return _THINK_TAG_RE.sub("", text).strip()


def _extract_thinking_content(text: str) -> str:
    """Extract and concatenate text inside all <think>…</think> blocks.

    Last-resort fallback when a model places its entire answer inside thinking
    tags with nothing outside. Returns empty string if no blocks are found.
    """
    parts = [m.strip() for m in _THINK_CONTENT_RE.findall(text) if m.strip()]
    return " ".join(parts)


# Matches OpenAI reasoning / o-series model names:
#   o1, o1-mini, o1-preview, o3, o3-mini, o4-mini, gpt-5, gpt-5-pro, …
_REASONING_MODEL_RE = re.compile(
    r"^(o\d+(-mini|-preview|-pro)?|gpt-5\S*)$",
    re.IGNORECASE,
)


def _is_reasoning_model(model_name: str) -> bool:
    """
    Return True if the model is an OpenAI reasoning model that does not
    accept temperature, top_p, frequency_penalty, or presence_penalty.
    """
    return bool(_REASONING_MODEL_RE.match(model_name.strip()))


def _encode_images(paths: list[str]) -> list[tuple[str, str]]:
    """
    Load image files and return [(base64_data, mime_type), ...].
    Files that cannot be read or are too large (> 20 MB) are skipped with a
    warning. Telegram photos are typically ≤ 1 MB so the size guard rarely fires.
    """
    result = []
    for path in paths:
        try:
            mime, _ = mimetypes.guess_type(path)
            if not mime or not mime.startswith("image/"):
                mime = "image/jpeg"  # reasonable default for Telegram photos
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) > 20 * 1024 * 1024:
                logger.warning(
                    "Image too large to encode (%d bytes), skipping: %s", len(data), path
                )
                continue
            result.append((base64.b64encode(data).decode("ascii"), mime))
        except OSError as exc:
            logger.warning("Could not encode image %s: %s", path, exc)
    return result
