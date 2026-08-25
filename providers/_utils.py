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
import json
import logging
import mimetypes
import re
import subprocess
import time
from typing import Callable

import httpx

from providers._errors import (
    LLMCancelledError,
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)

logger = logging.getLogger(__name__)


def make_on_retry(
    progress_cb: Callable[[str], None] | None = None,
) -> Callable[[int, int, str], None]:
    """Return the retry-notification closure shared by all provider backends.

    The returned callable has the signature expected by :func:`_with_retry`:
    ``on_retry(attempt, max_retries, reason)``. When ``progress_cb`` is provided,
    it is invoked with a human-readable retry status message; otherwise the
    notification is silently dropped.
    """
    def _on_retry(attempt: int, max_retries: int, reason: str) -> None:
        if progress_cb:
            progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")
    return _on_retry


# HTTP status codes that are safe to retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

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


def _with_retry(fn, max_retries: int, base_delay: float, on_retry=None, cancel_event=None,
                model_name: str | None = None, caller_tag: str | None = None):
    """
    Call fn(), retrying on transient httpx errors, retryable HTTP status codes,
    and LLMError (e.g. OpenRouter 200-OK with error body for server overload).
    LLMPermanentError propagates immediately without consuming retries.
    Uses exponential backoff: base_delay, base_delay*2, base_delay*4, …
    Non-retryable exceptions (e.g. 400 Bad Request) propagate immediately.
    on_retry(attempt, max_retries, exc_str) is called before each retry delay.
    model_name and caller_tag are included in log messages when provided.
    """
    _parts = []
    if caller_tag and caller_tag.strip():
        _parts.append(caller_tag.strip())
    if model_name and model_name.strip():
        _parts.append(model_name.strip())
    _model_tag = f"[{'/'.join(_parts)}] " if _parts else ""
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, max_retries + 1):
        # Check cancellation before each attempt (including before the first)
        if cancel_event and cancel_event.is_set():
            raise LLMCancelledError("LLM request cancelled")
        try:
            return fn()
        except LLMCancelledError:
            raise  # propagate immediately — never retry a cancel
        except LLMError as exc:
            if isinstance(exc, LLMPermanentError):
                raise  # permanent errors are never retried
            last_exc = exc
            logger.warning("%sAPI error body (attempt %d/%d): %s", _model_tag, attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"api error: {exc}")
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("%sRequest timed out (attempt %d/%d): %s", _model_tag, attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"timeout: {exc}")
        except LLMEmptyResponseError as exc:
            last_exc = exc
            logger.warning("%sEmpty LLM response (attempt %d/%d)", _model_tag, attempt, max_retries)
            if on_retry:
                on_retry(attempt, max_retries, "empty response")
        except httpx.RemoteProtocolError as exc:
            if cancel_event and cancel_event.is_set():
                raise LLMCancelledError("LLM request cancelled (connection interrupted)")
            last_exc = exc
            logger.warning("%sRemote protocol error (attempt %d/%d): %s", _model_tag, attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"protocol error: {exc}")
        except httpx.ConnectError as exc:
            if cancel_event and cancel_event.is_set():
                raise LLMCancelledError("LLM request cancelled (connection interrupted)")
            last_exc = exc
            logger.warning("%sConnection error (attempt %d/%d): %s", _model_tag, attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"connection error: {exc}")
        except (httpx.TransportError, httpx.PoolTimeout) as exc:
            if cancel_event and cancel_event.is_set():
                raise LLMCancelledError("LLM request cancelled (connection interrupted)")
            last_exc = exc
            logger.warning("%sTransport error (attempt %d/%d): %s", _model_tag, attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"transport error: {exc}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _RETRYABLE_STATUS:
                last_exc = exc
                logger.warning(
                    "%sHTTP %d (attempt %d/%d): %s",
                    _model_tag, exc.response.status_code, attempt, max_retries, exc,
                )
                if on_retry:
                    on_retry(attempt, max_retries, f"HTTP {exc.response.status_code}")
            else:
                # Log full response body to aid debugging (e.g. "invalid parameter" messages)
                logger.error(
                    "%sHTTP %d error — response body: %s",
                    _model_tag, exc.response.status_code,
                    exc.response.text[:2000],
                )
                raise  # 4xx errors are not retryable
        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            # Don't sleep if already cancelled
            if cancel_event:
                # Use event.wait() so cancellation wakes us up immediately
                cancelled = cancel_event.wait(timeout=delay)
                if cancelled:
                    raise LLMCancelledError("LLM request cancelled during retry delay")
            else:
                logger.info("%sRetrying in %.1fs…", _model_tag, delay)
                time.sleep(delay)
    raise last_exc


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


def diagnose_empty_response(response, provider: str, model: str,
                            curl_cmd=None) -> str:
    """
    Run diagnostic checks after an empty LLM response and return a
    human-readable report string. Also logs at ERROR level.
    """
    lines = [
        "=== Empty LLM Response Diagnostics ===",
        f"Provider: {provider} | Model: {model}",
    ]

    # 1. Raw HTTP response
    status = getattr(response, "status_code", "N/A")
    lines.append(f"HTTP status: {status}")

    try:
        hdrs = dict(response.headers)
        hdr_str = ", ".join(f"{k}={v}" for k, v in list(hdrs.items())[:10])
        lines.append(f"Headers: {hdr_str}")
    except Exception:
        lines.append("Headers: (unavailable)")

    try:
        raw_body = response.text[:4000]
    except Exception:
        raw_body = "(could not read body)"
    lines.append(f"Raw body (first 4000 chars):\n{raw_body}")

    # 2. Stream/non-stream mismatch check
    lines.append("--- Checks ---")
    stripped_body = raw_body.lstrip()
    if stripped_body.startswith("data:"):
        lines.append(
            "[STREAM MISMATCH] ⚠️  Raw body starts with 'data:' — this is SSE/streaming format. "
            "The client is not configured for streaming but received a streaming response. "
            "Set stream=false in your API payload or enable streaming in the client."
        )
    else:
        lines.append("[STREAM MISMATCH] OK — body does not look like SSE stream")

    # 3. finish_reason check
    finish_reason = None
    try:
        parsed = json.loads(raw_body)
        choices = parsed.get("choices") or []
        if choices:
            finish_reason = choices[0].get("finish_reason")
        # Anthropic uses stop_reason
        if finish_reason is None:
            finish_reason = parsed.get("stop_reason")
    except Exception:
        pass

    if finish_reason is not None:
        reason_note = {
            "stop": "normal completion",
            "length": "⚠️  max_tokens reached — response truncated",
            "content_filter": "⚠️  content blocked by provider safety filter",
            "null": "⚠️  still streaming or incomplete response",
            "end_turn": "normal completion (Anthropic)",
            "max_tokens": "⚠️  max_tokens reached",
        }.get(str(finish_reason).lower(), "unknown reason")
        lines.append(f"[FINISH REASON]   finish_reason = '{finish_reason}' — {reason_note}")
    else:
        lines.append("[FINISH REASON]   finish_reason not found in response body")

    # 4. curl fallback attempt
    if curl_cmd:
        lines.append("--- curl attempt ---")
        try:
            result = subprocess.run(
                curl_cmd,
                capture_output=True, text=True, timeout=30
            )
            curl_out = (result.stdout + result.stderr)[:2000]
            lines.append(f"curl exit code: {result.returncode}")
            lines.append(f"curl output:\n{curl_out}")
        except subprocess.TimeoutExpired:
            lines.append("curl attempt timed out after 30s")
        except (subprocess.SubprocessError, OSError) as exc:
            lines.append(f"curl attempt failed: {exc}")

    lines.append("=======================================")
    report = "\n".join(lines)
    logger.error("Empty LLM response diagnostic:\n%s", report)
    return report
