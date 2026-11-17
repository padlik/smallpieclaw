"""
token_estimator.py
------------------
Context-window token accounting for prompt sizing and compaction.

Two layers are provided:

1. A conservative, dependency-free heuristic that is *always* available. It is
   intentionally pessimistic (it tends to over-count rather than under-count) so
   the compaction logic triggers early enough to avoid mid-run context overflow.
   The heuristic is content-aware:
     - code / JSON / log-like text uses a lower chars-per-token ratio than prose
       (symbols and punctuation tokenize densely);
     - CJK / wide characters are counted at roughly one token each, since the old
       ``len // 4`` rule badly under-counts them;
     - per-message framing overhead is added so a multi-message context is not
       under-counted versus the same text concatenated into one blob.

2. An optional ``tiktoken`` tokenizer path for OpenAI-compatible models. When a
   model name resolves to a tiktoken encoding, exact counts are used. Any
   import/lookup failure falls back to the heuristic, so Ollama, Google,
   Anthropic, and unknown providers/models remain reliable and offline-safe.

``prompt_builder`` re-exports ``estimate_tokens`` / ``estimate_messages_tokens``
for backward compatibility, so existing imports keep working.
"""

from __future__ import annotations

import logging
import math
import os
import json
import threading
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic tuning constants
# ---------------------------------------------------------------------------

# Chars-per-token ratios. Lower ratio => more tokens (more conservative).
# Prose stays just below the classic "4 chars per token" so plain English is
# never estimated *less* conservatively than the previous len // 4 rule.
_CHARS_PER_TOKEN_PROSE = 3.6
# Code / JSON / logs tokenize more densely because of symbols and punctuation.
_CHARS_PER_TOKEN_CODE = 2.8

# Each CJK / wide character is counted as at least this many tokens. Real models
# often emit 1–2 tokens per CJK glyph; one token each is a safe minimum that is
# far more accurate than counting four CJK chars as a single token.
_CJK_TOKENS_PER_CHAR = 1.0

# Symbol density (symbols / non-CJK chars) above which text is treated as
# code/JSON/log-like rather than prose.
_CODE_DENSITY_THRESHOLD = 0.12

# Per-message framing overhead (role markers, delimiters, reply priming). OpenAI
# chat formats add a handful of tokens per message; this keeps multi-message
# contexts from being under-counted.
_PER_MESSAGE_OVERHEAD = 4

# Flat token budget charged for an attached image. Images dominate provider cost
# and the real token usage is provider/resolution dependent; a fixed, generous
# estimate keeps compaction conservative.
_IMAGE_TOKENS = 1000

# Maximum image file size (bytes) we still bother to count. Above this the image
# is assumed to be rejected/resized upstream, but we still charge the budget.
_IMAGE_MAX_BYTES = 20 * 1024 * 1024

# Characters treated as "code-like" for density classification.
_CODE_SYMBOLS = frozenset("{}[]()<>=;:/\\|&^%$#@~`\"'*+-_")


# ---------------------------------------------------------------------------
# Optional tiktoken tokenizer support
# ---------------------------------------------------------------------------

# Cache resolved encoders keyed by model name (or "" for the default encoding).
_ENCODER_CACHE: dict[str, object] = {}
_TIKTOKEN_AVAILABLE: Optional[bool] = None
_PAYLOAD_CACHE: OrderedDict[tuple[str, str | None, str | None], int] = OrderedDict()
_PAYLOAD_CACHE_LOCK = threading.Lock()
_PAYLOAD_CACHE_SIZE = 64
_PAYLOAD_CACHE_MAX_KEY_CHARS = 1_000_000


def _tiktoken_module():
    """Import tiktoken lazily; return the module or None if unavailable."""
    global _TIKTOKEN_AVAILABLE
    if _TIKTOKEN_AVAILABLE is False:
        return None
    try:
        import tiktoken  # noqa: PLC0415 — optional dependency, imported on demand
        _TIKTOKEN_AVAILABLE = True
        return tiktoken
    except Exception as exc:  # noqa: BLE001
        if _TIKTOKEN_AVAILABLE is None:
            logger.debug("tiktoken unavailable, using heuristic token estimate: %s", exc)
        _TIKTOKEN_AVAILABLE = False
        return None


def _get_encoder(model: Optional[str]):
    """Return a tiktoken encoder for *model*, or None to use the heuristic.

    Only OpenAI-compatible model names that tiktoken recognises are resolved.
    Unknown models return None so the conservative heuristic is used instead of
    a wrong-tokenizer guess.
    """
    tiktoken = _tiktoken_module()
    if tiktoken is None or not model:
        return None
    key = model
    if key in _ENCODER_CACHE:
        return _ENCODER_CACHE[key]
    encoder = None
    try:
        encoder = tiktoken.encoding_for_model(model)
    except Exception:  # noqa: BLE001 — unknown model is expected for many providers
        encoder = None
    _ENCODER_CACHE[key] = encoder
    return encoder


# ---------------------------------------------------------------------------
# Text classification (exposed for tests/auditing)
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    """True if *ch* is a CJK / wide ideograph or kana that tokenizes densely."""
    code = ord(ch)
    return (
        0x3000 <= code <= 0x9FFF       # CJK symbols, punctuation, ideographs, kana
        or 0xAC00 <= code <= 0xD7A3    # Hangul syllables
        or 0xF900 <= code <= 0xFAFF    # CJK compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF    # full-width forms
        or 0x20000 <= code <= 0x2FA1F  # CJK extension B+ (supplementary planes)
    )


def classify_text(text: str) -> str:
    """Classify *text* as ``"empty"``, ``"cjk"``, ``"code"``, or ``"prose"``.

    Exposed so tests and debugging can inspect why a given input received a
    particular estimate. CJK-dominant text is reported as ``"cjk"`` regardless of
    symbol density.
    """
    if not text:
        return "empty"
    cjk = sum(1 for ch in text if _is_cjk(ch))
    if cjk * 2 >= len(text):
        return "cjk"
    rest = len(text) - cjk
    symbols = sum(1 for ch in text if ch in _CODE_SYMBOLS)
    density = symbols / max(1, rest)
    return "code" if density > _CODE_DENSITY_THRESHOLD else "prose"


# ---------------------------------------------------------------------------
# Public estimation API
# ---------------------------------------------------------------------------

def _heuristic_text_tokens(text: str) -> int:
    """Conservative, content-aware token estimate for a single string."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    rest = len(text) - cjk
    symbols = sum(1 for ch in text if ch in _CODE_SYMBOLS)
    density = symbols / max(1, rest)
    ratio = _CHARS_PER_TOKEN_CODE if density > _CODE_DENSITY_THRESHOLD else _CHARS_PER_TOKEN_PROSE
    tokens = cjk * _CJK_TOKENS_PER_CHAR + (rest / ratio if rest else 0.0)
    return int(math.ceil(tokens))


def estimate_tokens(text: str, model: Optional[str] = None) -> int:
    """Estimate the token count of *text*.

    When *model* resolves to a tiktoken encoding the exact count is returned;
    otherwise a conservative content-aware heuristic is used.
    """
    if not text:
        return 0
    encoder = _get_encoder(model)
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:  # noqa: BLE001 — never let estimation crash a run
            pass
    return _heuristic_text_tokens(text)


def _image_tokens_for_message(m: dict) -> int:
    """Token budget for images referenced by a message (paths + content parts)."""
    total = 0
    for img_path in m.get("images") or []:
        try:
            if os.path.getsize(img_path) <= _IMAGE_MAX_BYTES:
                total += _IMAGE_TOKENS
            else:
                # Oversized images are still charged — they are not free even if
                # resized/rejected downstream — but not double-counted.
                total += _IMAGE_TOKENS
        except OSError:
            # Missing/unreadable path: charge a conservative budget instead of
            # silently counting zero, which previously hid image cost entirely.
            total += _IMAGE_TOKENS
    content = m.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                total += _IMAGE_TOKENS
    return total


def estimate_messages_tokens(messages: list[dict], system: str = "",
                             model: Optional[str] = None) -> int:
    """Estimate total tokens across *messages* plus a *system* prompt.

    Adds per-message framing overhead and image budgets, and honours both the
    ``images`` field and OpenAI-style multimodal content lists.
    """
    total = estimate_tokens(system, model)
    if system:
        total += _PER_MESSAGE_OVERHEAD
    for m in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += estimate_tokens(m.get("role", ""), model)
        content = m.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""), model)
        else:
            total += estimate_tokens(content, model)
        total += _image_tokens_for_message(m)
        # Native tool calls and legacy function calls are part of the actual
        # request even though they are not represented by message content.
        for key in ("tool_calls", "function_call", "name", "tool_call_id"):
            if key in m:
                total += _estimate_payload_value(m[key], model)
    return total


def estimate_request_payload_tokens(
    payload: dict[str, Any] | str,
    *,
    model: str | None = None,
    definition_revision: str | int | None = None,
) -> int:
    """Estimate the complete assembled request payload, including tool schemas.

    Image contents are charged by a conservative fixed estimate instead of
    treating data-URL/base64 bytes as ordinary text. Cache entries are scoped to
    the exact serialized payload, target model, and definition revision. Very
    large payloads bypass caching to avoid retaining image data in memory.
    """
    if isinstance(payload, str):
        wire_payload = payload
        try:
            value: Any = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            value = payload
    elif isinstance(payload, dict):
        value = payload
        try:
            wire_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return _estimate_payload_value(payload, model)
    else:
        raise TypeError("payload must be a mapping or serialized string")

    revision = None if definition_revision is None else str(definition_revision)
    key = (wire_payload, model, revision)
    if len(wire_payload) <= _PAYLOAD_CACHE_MAX_KEY_CHARS:
        with _PAYLOAD_CACHE_LOCK:
            cached = _PAYLOAD_CACHE.get(key)
            if cached is not None:
                _PAYLOAD_CACHE.move_to_end(key)
                return cached
    total = _estimate_request_root(value, model)
    if len(wire_payload) <= _PAYLOAD_CACHE_MAX_KEY_CHARS:
        with _PAYLOAD_CACHE_LOCK:
            _PAYLOAD_CACHE[key] = total
            _PAYLOAD_CACHE.move_to_end(key)
            while len(_PAYLOAD_CACHE) > _PAYLOAD_CACHE_SIZE:
                _PAYLOAD_CACHE.popitem(last=False)
    return total


def _estimate_payload_value(value: Any, model: str | None, *, image_context: bool = False) -> int:
    """Count payload data recursively; image treatment is restricted to wire parts."""
    if isinstance(value, dict):
        total = _PER_MESSAGE_OVERHEAD
        for key, nested in value.items():
            if isinstance(key, str):
                total += estimate_tokens(key, model)
            total += _estimate_payload_value(nested, model)
        return total
    if isinstance(value, (list, tuple)):
        return _PER_MESSAGE_OVERHEAD + sum(
            _estimate_payload_value(item, model, image_context=image_context) for item in value
        )
    if isinstance(value, str):
        return estimate_tokens(value, model)
    if value is None or isinstance(value, (bool, int, float)):
        return estimate_tokens(str(value), model)
    return estimate_tokens(str(value), model)


def _estimate_request_root(value: Any, model: str | None) -> int:
    """Count a root request, recognizing transport containers only at root."""
    if not isinstance(value, dict):
        return _estimate_payload_value(value, model)
    total = _PER_MESSAGE_OVERHEAD
    generation_only = {
        "model",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "response_format",
        "tool_choice",
        "stream",
        "generationConfig",
        "options",
        "format",
    }
    for key, nested in value.items():
        if key in generation_only:
            continue
        if isinstance(key, str):
            total += estimate_tokens(key, model)
        if key == "messages" and isinstance(nested, (list, tuple)):
            total += _PER_MESSAGE_OVERHEAD + sum(
                _estimate_message_payload(message, model, google=False)
                for message in nested
            )
        elif key == "contents" and isinstance(nested, (list, tuple)):
            total += _PER_MESSAGE_OVERHEAD + sum(
                _estimate_message_payload(message, model, google=True)
                for message in nested
            )
        else:
            total += _estimate_payload_value(nested, model)
    return total


def _estimate_message_payload(message: Any, model: str | None, *, google: bool) -> int:
    """Count a message, substituting image allowance only for recognized parts."""
    if not isinstance(message, dict):
        return _estimate_payload_value(message, model)
    total = _PER_MESSAGE_OVERHEAD
    for key, value in message.items():
        if isinstance(key, str):
            total += estimate_tokens(key, model)
        if key == "content" and isinstance(value, list) and not google:
            total += _estimate_content_parts(value, model, google=False)
        elif key == "parts" and isinstance(value, list) and google:
            total += _estimate_content_parts(value, model, google=True)
        elif key == "images" and isinstance(value, (list, tuple)) and not google:
            total += _PER_MESSAGE_OVERHEAD + sum(
                _IMAGE_TOKENS if isinstance(image, str)
                else _estimate_payload_value(image, model)
                for image in value
            )
        else:
            total += _estimate_payload_value(value, model)
    return total


def _estimate_content_parts(parts: list[Any], model: str | None, *, google: bool) -> int:
    """Count multimodal parts, replacing only encoded bytes of image parts."""
    total = _PER_MESSAGE_OVERHEAD
    for part in parts:
        if not isinstance(part, dict):
            total += _estimate_payload_value(part, model)
            continue
        mime = part.get("mimeType", part.get("mime_type"))
        if google:
            image_key = "inlineData" if "inlineData" in part else "inline_data"
            image_value = part.get(image_key)
            if isinstance(image_value, dict):
                mime = mime or image_value.get("mimeType", image_value.get("mime_type"))
            image_mime = isinstance(mime, str) and mime.lower().startswith("image/")
            recognized = isinstance(image_value, dict) and image_mime
        else:
            image_value = part.get("image_url")
            source_value = part.get("source")
            image_url = (
                part.get("type") == "image_url"
                and isinstance(image_value, dict)
                and isinstance(image_value.get("url"), str)
            )
            anthropic_image = (
                part.get("type") == "image"
                and isinstance(source_value, dict)
                and source_value.get("type") == "base64"
                and isinstance(source_value.get("media_type"), str)
                and source_value["media_type"].lower().startswith("image/")
                and isinstance(source_value.get("data"), str)
            )
            recognized = image_url or anthropic_image
        if not recognized:
            total += _estimate_payload_value(part, model)
            continue
        total += _PER_MESSAGE_OVERHEAD
        for key, value in part.items():
            if isinstance(key, str):
                total += estimate_tokens(key, model)
            if google and key == image_key:
                # Preserve inlineData metadata and count all fields except the
                # base64 bytes themselves; text and arbitrary extras stay visible.
                if isinstance(value, dict):
                    total += _PER_MESSAGE_OVERHEAD
                    for nested_key, nested_value in value.items():
                        if isinstance(nested_key, str):
                            total += estimate_tokens(nested_key, model)
                        if nested_key == "data" and isinstance(nested_value, str):
                            total += _IMAGE_TOKENS
                        else:
                            total += _estimate_payload_value(nested_value, model)
                else:
                    total += _estimate_payload_value(value, model)
            elif not google and key == "image_url" and image_url:
                total += _estimate_replace_string_leaf(value, "url", model)
            elif not google and key == "source" and anthropic_image:
                total += _estimate_replace_string_leaf(value, "data", model)
            else:
                total += _estimate_payload_value(value, model)
    return total


def _estimate_replace_string_leaf(value: Any, field: str, model: str | None) -> int:
    """Count an object recursively, replacing only one recognized string leaf."""
    if not isinstance(value, dict):
        return _estimate_payload_value(value, model)
    total = _PER_MESSAGE_OVERHEAD
    for key, nested in value.items():
        if isinstance(key, str):
            total += estimate_tokens(key, model)
        if key == field and isinstance(nested, str):
            total += _IMAGE_TOKENS
        else:
            total += _estimate_payload_value(nested, model)
    return total


def _estimate_image_value(value: Any, model: str | None) -> int:
    """Charge one image for each image value without tokenizing encoded bytes."""
    if isinstance(value, (list, tuple)):
        return sum(_estimate_image_value(item, model) for item in value)
    if isinstance(value, dict):
        # An image_url object is one image; do not descend into its potentially
        # very large base64 data URL.
        return _IMAGE_TOKENS
    if isinstance(value, str):
        return _IMAGE_TOKENS
    return _estimate_payload_value(value, model)
