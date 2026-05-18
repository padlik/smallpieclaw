"""
llm_client.py
-------------
Unified LLM client supporting OpenAI-compatible APIs, Google Gemini, Anthropic, and Ollama.
Handles both chat completions and embeddings, configured separately.

Note on OpenAI reasoning models (o1, o1-mini, o3, o3-mini, o4-mini, gpt-5, etc.):
  These models do NOT accept a `temperature` parameter — passing any value causes
  a 400 "Unsupported parameter" error. The client detects these models by name
  and omits temperature (and other unsupported sampling params) automatically.

Retry behaviour:
  Transient errors (timeouts, connection resets, 5xx responses) are retried with
  exponential backoff. Configure via config.toml:
    [llm]
    request_timeout = 120   # seconds per attempt (default: 120)
    max_retries     = 5     # attempts before giving up (default: 5)
    retry_delay     = 2     # base delay in seconds; doubles each attempt (default: 2)
"""

from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import re
import subprocess
import time
from datetime import date
from typing import Any, Optional

import httpx
import ollama as _ollama_lib

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# HTTP status codes that are safe to retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMEmptyResponseError(RuntimeError):
    """Raised when the LLM provider returns an empty or whitespace-only response."""


class LLMError(RuntimeError):
    """Raised when the LLM provider returns an API-level error (HTTP 200 with error body)."""


class LLMPermanentError(LLMError):
    """
    Raised for API-level errors that should never be retried — e.g. content filter
    violations, invalid API keys, bad request parameters.  Propagates immediately out
    of _with_retry without consuming any retry attempts.
    """


# Error codes (from OpenAI / OpenRouter / Anthropic) that are permanent — retrying
# them wastes quota, burns time, and can trigger duplicate billing on some providers.
_PERMANENT_ERROR_CODES = frozenset({
    # OpenAI / OpenRouter
    "content_filter",
    "content_policy_violation",
    "invalid_request_error",
    "invalid_api_key",
    "authentication_error",
    "model_not_found",
    # Anthropic
    "invalid_api_key",
    "permission_error",
    "not_found_error",
    "invalid_request",
})


class LLMCancelledError(RuntimeError):
    """Raised when the LLM request is cancelled via cancel_event."""


_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_CONTENT_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)

_LLM_CALL_ERRORS = (
    LLMError,
    httpx.HTTPError,
    json.JSONDecodeError,
    KeyError,
    ValueError,
)
_LLM_CHAT_ERRORS = _LLM_CALL_ERRORS + (LLMCancelledError,)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity — no numpy required."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Thin wrapper around remote LLM APIs.
    Supports:
      - OpenAI  (and any OpenAI-compatible endpoint such as OpenRouter)
      - Google Gemini (via generateContent REST API)
      - Anthropic Claude
      - Ollama (cloud API at https://ollama.com or local at http://localhost:11434)

    Multi-model support:
      Define [[models]] in config.toml (array of tables). Each entry must have:
        name, provider, api_key, model, base_url, max_tokens, temperature,
        request_timeout, max_retries, retry_delay.
      Set agent.default_model to the 'model' value of the entry to use by default.
    """

    def __init__(self, config: dict, usage_registry=None, cancel_event=None,
                 fallback_models: list[str] | None = None, caller_tag: str | None = None):
        """
        Args:
            config: full agent config dict
            usage_registry: optional TokenUsageRegistry for cross-instance token aggregation
            cancel_event: optional threading.Event; when set, in-progress LLM calls raise LLMCancelledError
            fallback_models: optional list of model IDs (matching [[models]] entries) to try
                             in order when the primary model fails. Overrides config
                             agent.fallback_models when provided (even as empty list).
            caller_tag: optional label for log messages (e.g. "main", "sa-fcf85d"). Used to
                        identify which agent triggered an LLM call in concurrent log streams.
        """
        self.cfg = config
        self._caller_tag = caller_tag or ""

        # Require [[models]] — no legacy [llm] fallback
        models = config.get("models")
        if not models or not isinstance(models, list):
            raise ValueError(
                "config.toml must define at least one [[models]] entry. "
                "The legacy [llm] block is no longer supported."
            )
        self._models: list[dict] = models

        # Resolve default model from agent.default_model
        default_model: str = config.get("agent", {}).get("default_model", "")
        self._active_idx: int = 0
        if default_model:
            for i, m in enumerate(self._models):
                if m.get("model") == default_model:
                    self._active_idx = i
                    break
            else:
                raise ValueError(
                    f"agent.default_model = {default_model!r} does not match "
                    f"any [[models]] entry. Available: "
                    f"{[m.get('model') for m in self._models]}"
                )

        # Resolve fallback model indices
        # Explicit param overrides config; None means use config; [] disables fallback
        if fallback_models is None:
            fallback_models = config.get("agent", {}).get("fallback_models", [])
        self._fallback_indices: list[int] = []
        for fb_model_id in (fallback_models or []):
            for i, m in enumerate(self._models):
                if m.get("model") == fb_model_id:
                    self._fallback_indices.append(i)
                    break
            else:
                logger.warning(
                    "fallback_models: model %r not found in [[models]] — ignoring",
                    fb_model_id,
                )

        # Embeddings config (with fallback to active LLM key/base_url)
        emb_cfg = dict(config.get("embeddings", self._models[self._active_idx]))
        if not emb_cfg.get("api_key"):
            emb_cfg["api_key"] = self._models[self._active_idx].get("api_key", "")
        if not emb_cfg.get("base_url"):
            emb_cfg["base_url"] = self._models[self._active_idx].get("base_url", "")
        self.emb_cfg = emb_cfg

        # Embedding cache: keyed on text, bounded to avoid unbounded growth in long sessions.
        # Avoids redundant API round-trips when the same text is embedded multiple times
        # (e.g. tool-index queries repeated across user turns).
        self._embed_cache: dict[str, list[float]] = {}
        self._embed_cache_max: int = 512

        # Retry / timeout from active model config
        _ref = self._models[self._active_idx]
        self._max_retries: int = _ref.get("max_retries", 5)
        self._retry_delay: float = float(_ref.get("retry_delay", 2))
        timeout_secs: float = float(_ref.get("request_timeout", 120))
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_secs, connect=10.0)
        )

        # Build one ollama.Client per ollama-provider model entry (None for others).
        # Stored in a list parallel to self._models so lookups are O(1) by index.
        self._ollama_clients: list[Optional[_ollama_lib.Client]] = []
        for m in self._models:
            if m.get("provider") == "ollama":
                host = m.get("base_url") or "http://localhost:11434"
                api_key = m.get("api_key", "")
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                timeout_val = float(m.get("request_timeout", 120))
                oc = _ollama_lib.Client(host=host, headers=headers,
                                        timeout=timeout_val)
                self._ollama_clients.append(oc)
            else:
                self._ollama_clients.append(None)

        # Diagnostic mode for empty responses
        self._diagnose_empty: bool = bool(
            config.get("agent", {}).get("diagnose_empty_responses", False)
        )

        # Daily token usage tracking (per-instance)
        self._usage_date: date = date.today()
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0

        # Cross-instance token registry (shared across main + sub-agents)
        self._usage_registry = usage_registry

        # Cancellation support (for sub-agents)
        self._cancel_event = cancel_event  # Optional[threading.Event]

    # ------------------------------------------------------------------
    # Multi-model API
    # ------------------------------------------------------------------

    @property
    def llm_cfg(self) -> dict:
        """Return the currently active model config."""
        return self._models[self._active_idx]

    def list_models(self) -> list[dict]:
        """Return summary of all configured models."""
        result = []
        for i, m in enumerate(self._models):
            entry: dict = {
                "name": m.get("name", f"model_{i}"),
                "model": m.get("model", "?"),
                "provider": m.get("provider", "?"),
                "active": i == self._active_idx,
            }
            if m.get("vision"):
                entry["vision"] = True
            result.append(entry)
        return result

    def set_model(self, name: str) -> bool:
        """Switch the active model by name. Returns False if not found."""
        for i, m in enumerate(self._models):
            if m.get("name", "") == name:
                self._active_idx = i
                logger.info("Switched active model to '%s' (%s)", name, m.get("model", "?"))
                return True
        logger.warning("Model '%s' not found in configured models", name)
        return False

    def close_http(self) -> None:
        """
        Close the HTTP transport for the currently active model.

        Used by SubAgentRecord.cancel() to interrupt an in-flight LLM request
        immediately rather than waiting for it to time out.
        """
        # Always close the shared httpx client (used by openai/google/anthropic)
        try:
            self._http.close()
        except Exception:
            pass
        # Also close the ollama client's internal httpx transport if active
        oc = self._ollama_clients[self._active_idx] if self._ollama_clients else None
        if oc is not None:
            try:
                oc._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    def _track_usage(self, prompt: int, completion: int) -> None:
        today = date.today()
        if today != self._usage_date:
            self._usage_date = today
            self._prompt_tokens = 0
            self._completion_tokens = 0
        self._prompt_tokens += prompt
        self._completion_tokens += completion
        # Also record in the shared cross-instance registry
        if self._usage_registry is not None:
            model_name = self._models[self._active_idx].get("model", "unknown")
            self._usage_registry.record(model_name, prompt, completion)

    def get_today_usage(self) -> dict:
        return {
            "date": self._usage_date.isoformat(),
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._prompt_tokens + self._completion_tokens,
        }

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def chat(self, messages: list[dict], system: str | None = None, progress_cb=None,
             json_mode: bool = False) -> str:
        """Send a chat request and return the assistant text.

        json_mode=True requests the provider to constrain output to valid JSON.
        Supported by OpenAI/OpenRouter (response_format), Google (responseMimeType),
        and Ollama (format="json"). Anthropic falls back to prompt-only enforcement.
        """
        provider = self.llm_cfg["provider"]
        try:
            if provider in ("openai", "openrouter"):
                return self._openai_chat(messages, system, progress_cb=progress_cb, json_mode=json_mode)
            elif provider == "google":
                return self._google_chat(messages, system, progress_cb=progress_cb, json_mode=json_mode)
            elif provider == "anthropic":
                return self._anthropic_chat(messages, system, progress_cb=progress_cb)
            elif provider == "ollama":
                return self._ollama_chat(messages, system, progress_cb=progress_cb, json_mode=json_mode)
            else:
                raise ValueError(f"Unknown LLM provider: {provider}")
        except _LLM_CHAT_ERRORS as exc:
            logger.error("LLM chat error: %s", exc)
            raise

    def chat_with_fallback(self, messages: list[dict], system: str | None = None,
                           progress_cb=None, json_mode: bool = False) -> str:
        """
        Like chat(), but tries each fallback model in order if the primary fails
        with a transient error. LLMPermanentError is never retried via fallback.

        On success the active model index is NOT restored — the working model
        persists for subsequent calls within this job/run() lifetime.
        AgentController.run() restores _active_idx when the job ends so the
        next job always starts with the primary model.

        On error _active_idx is always restored to primary before propagating.
        """
        primary_idx = self._active_idx
        last_exc: Exception | None = None

        candidates = [primary_idx] + self._fallback_indices
        for seq, idx in enumerate(candidates):
            self._active_idx = idx
            model_id = self._models[idx].get("model", f"model_{idx}")
            if seq > 0:
                logger.warning(
                    "Falling back to model '%s' after error: %s",
                    model_id, last_exc,
                )
                if progress_cb:
                    progress_cb(f"⚠️ Switching to fallback model '{model_id}'…")
            try:
                return self.chat(messages, system, progress_cb=progress_cb, json_mode=json_mode)
                # On success: _active_idx stays at idx so next step reuses same model
            except LLMPermanentError:
                self._active_idx = primary_idx  # restore before propagating
                raise  # permanent errors — no point trying fallbacks
            except LLMCancelledError:
                self._active_idx = primary_idx  # restore before propagating
                raise  # user cancelled — don't waste quota on fallbacks
            except _LLM_CALL_ERRORS as exc:
                last_exc = exc
                self._active_idx = primary_idx  # reset before trying next candidate
                if seq < len(candidates) - 1:
                    logger.warning(
                        "Model '%s' failed (will try next fallback): %s",
                        model_id, exc,
                    )
                else:
                    logger.error("All models (primary + %d fallback(s)) failed.", len(self._fallback_indices))

        raise last_exc  # type: ignore[misc]

    def _openai_chat(self, messages: list[dict], system: str | None, progress_cb=None,
                     json_mode: bool = False) -> str:
        _initial_model = self.llm_cfg["model"]

        # Pre-encode images once — this is pure data transformation that does not
        # depend on the active model. Results are reused across retries.
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
                    })
                    continue
            encoded_messages.append({"_role": m["role"], "_content": m.get("content", ""), "_encoded": None})

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        def _do_request():
            # Re-read active model config on every attempt so that a mid-flight
            # model switch (set_model) is picked up by subsequent retries.
            model = self.llm_cfg["model"]
            reasoning = _is_reasoning_model(model)

            # Rebuild payload_messages each attempt: the system role format depends
            # on whether the (potentially new) model is a reasoning model.
            payload_messages = []
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
            # Encode images for any messages that carry them; build multipart content
            # for providers that support vision (all 4 providers use the same path here).
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
                    payload_messages.append({"role": em["_role"], "content": em["_content"]})

            payload: dict[str, Any] = {
                "model": model,
                "messages": payload_messages,
                "max_completion_tokens": self.llm_cfg.get("max_tokens", 1024),
            }
            if reasoning:
                # Reasoning models (o1, o3, o4-mini, gpt-5, …) reject temperature,
                # top_p, frequency_penalty, and presence_penalty entirely.
                logger.debug("Reasoning model detected (%s) — omitting sampling params", model)
            else:
                payload["temperature"] = self.llm_cfg.get("temperature", 0.2)
                top_p = self.llm_cfg.get("top_p")
                if top_p is not None:
                    payload["top_p"] = top_p
                if json_mode:
                    # Request strict JSON output. Not supported by reasoning models.
                    payload["response_format"] = {"type": "json_object"}
            url = f"{self.llm_cfg['base_url'].rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.llm_cfg['api_key']}",
                "Content-Type": "application/json",
            }
            r = self._http.post(url, headers=headers, json=payload)
            r.raise_for_status()
            d = r.json()
            usage = d.get("usage", {})
            self._track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

            # Some APIs return HTTP 200 with an error body (e.g. rate limit, content filter).
            # Detect this before trying to access 'choices'.
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
                if self._diagnose_empty:
                    curl_cmd = [
                        "curl", "-s", "-X", "POST", url,
                        "-H", f"Authorization: Bearer {self.llm_cfg['api_key']}",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(payload),
                    ]
                    report = self._diagnose_empty_response(r, "openai", model, curl_cmd=curl_cmd)
                    raise LLMEmptyResponseError(
                        f"Model '{model}' returned no choices.\n{report}"
                    )
                raise LLMEmptyResponseError(
                    f"Model '{model}' returned no choices. Body: {str(d)[:300]}"
                )

            msg = choices[0].get("message") or {}
            text = (msg.get("content") or "").strip()
            # Strip inline <think>…</think> reasoning blocks before any further checks
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
                        return fallback

            if not text:
                if self._diagnose_empty:
                    curl_cmd = [
                        "curl", "-s", "-X", "POST", url,
                        "-H", f"Authorization: Bearer {self.llm_cfg['api_key']}",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(payload),
                    ]
                    report = self._diagnose_empty_response(r, "openai", model, curl_cmd=curl_cmd)
                    raise LLMEmptyResponseError(
                        f"OpenAI returned empty content (model: {model})\n{report}"
                    )
                raise LLMEmptyResponseError(f"OpenAI returned empty content (model: {model})")
            return text

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry,
                           cancel_event=self._cancel_event, model_name=_initial_model, caller_tag=self._caller_tag)

    def _google_chat(self, messages: list[dict], system: str | None, progress_cb=None,
                     json_mode: bool = False) -> str:
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

        _initial_model = self.llm_cfg["model"]

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        def _do_request():
            # Re-read active model config on every attempt so that a mid-flight
            # model switch (set_model) is picked up by subsequent retries.
            api_key = self.llm_cfg["api_key"]
            model = self.llm_cfg["model"]
            google_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            google_headers = {
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            }
            google_payload = {
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": self.llm_cfg.get("max_tokens", 1024),
                    "temperature": self.llm_cfg.get("temperature", 0.2),
                },
            }
            if json_mode:
                google_payload["generationConfig"]["responseMimeType"] = "application/json"
            top_p = self.llm_cfg.get("top_p")
            if top_p is not None:
                google_payload["generationConfig"]["topP"] = top_p
            r = self._http.post(google_url, headers=google_headers, json=google_payload)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                err = d["error"]
                err_code = str(err.get("code") or err.get("status") or "").lower()
                exc_class = LLMPermanentError if err_code in _PERMANENT_ERROR_CODES else LLMError
                raise exc_class(f"Google API error (model: {model}): {err.get('message', err)}")
            meta = d.get("usageMetadata", {})
            self._track_usage(meta.get("promptTokenCount", 0), meta.get("candidatesTokenCount", 0))
            candidates = d.get("candidates") or []
            if not candidates:
                logger.error("Google model '%s': response missing 'candidates'. Raw: %s", model, str(d)[:500])
                raise LLMEmptyResponseError(f"Google model '{model}' returned no candidates. Body: {str(d)[:300]}")
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            text = (parts[0].get("text", "") if parts else "").strip()
            if not text:
                if self._diagnose_empty:
                    curl_cmd = [
                        "curl", "-s", "-X", "POST", google_url,
                        "-H", f"x-goog-api-key: {api_key}",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(google_payload),
                    ]
                    report = self._diagnose_empty_response(r, "google", model, curl_cmd=curl_cmd)
                    raise LLMEmptyResponseError(
                        f"Google returned empty content (model: {model})\n{report}"
                    )
                raise LLMEmptyResponseError(f"Google returned empty content (model: {model})")
            return text

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry,
                           cancel_event=self._cancel_event, model_name=_initial_model, caller_tag=self._caller_tag)

    def _anthropic_chat(self, messages: list[dict], system: str | None, progress_cb=None) -> str:
        _initial_model = self.llm_cfg["model"]
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
            model = self.llm_cfg["model"]
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": self.llm_cfg.get("max_tokens", 1024),
                "messages": anthropic_messages,
            }
            if system:
                payload["system"] = system
            top_p = self.llm_cfg.get("top_p")
            if top_p is not None:
                payload["top_p"] = top_p
            anthropic_url = "https://api.anthropic.com/v1/messages"
            anthropic_headers = {
                "x-api-key": self.llm_cfg["api_key"],
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            r = self._http.post(anthropic_url, headers=anthropic_headers, json=payload)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                err = d["error"]
                err_code = str(err.get("type") or err.get("code") or "").lower()
                exc_class = LLMPermanentError if err_code in _PERMANENT_ERROR_CODES else LLMError
                raise exc_class(f"Anthropic API error (model: {model}): {err.get('message', err)}")
            usage = d.get("usage", {})
            self._track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            content_blocks = d.get("content") or []
            if not content_blocks:
                logger.error("Anthropic model '%s': response missing 'content'. Raw: %s", model, str(d)[:500])
                raise LLMEmptyResponseError(f"Anthropic model '{model}' returned no content. Body: {str(d)[:300]}")
            text = (content_blocks[0].get("text", "") or "").strip()
            if not text:
                if self._diagnose_empty:
                    curl_cmd = [
                        "curl", "-s", "-X", "POST", anthropic_url,
                        "-H", f"x-api-key: {self.llm_cfg['api_key']}",
                        "-H", "anthropic-version: 2023-06-01",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(payload),
                    ]
                    report = self._diagnose_empty_response(r, "anthropic", model, curl_cmd=curl_cmd)
                    raise LLMEmptyResponseError(
                        f"Anthropic returned empty content (model: {model})\n{report}"
                    )
                raise LLMEmptyResponseError(f"Anthropic returned empty content (model: {model})")
            return text

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry,
                           cancel_event=self._cancel_event, model_name=_initial_model, caller_tag=self._caller_tag)

    def _ollama_chat(self, messages: list[dict], system: str | None, progress_cb=None,
                     json_mode: bool = False) -> str:
        """
        Chat via the Ollama Python library.

        Supports both the Ollama Cloud API (https://ollama.com) and a local Ollama
        instance. The host and optional bearer token come from the model's base_url
        and api_key config fields.

        progress_cb is used only for retry status messages, never for raw content
        tokens. Reasoning/thinking content wrapped in <think>…</think> tags is
        stripped from the final response before returning.
        """
        _initial_model = self.llm_cfg["model"]

        # Build message list (Ollama supports the system role natively)
        payload_messages: list[dict] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        for m in messages:
            imgs = m.get("images")
            if imgs:
                encoded = _encode_images(imgs)
                if encoded:
                    # Ollama vision: pass base64 strings in the "images" field
                    payload_messages.append({
                        "role": m["role"],
                        "content": m.get("content", ""),
                        "images": [b64 for b64, _ in encoded],
                    })
                    continue
            payload_messages.append({"role": m["role"], "content": m.get("content", "")})

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        def _do_request():
            # Re-read active model config and client on every attempt so that a
            # mid-flight model switch (set_model) is picked up by subsequent retries.
            model = self.llm_cfg["model"]
            # If set_model switched to a non-Ollama provider mid-retry, abort
            # immediately rather than attempting to use a None client.
            if self.llm_cfg.get("provider") != "ollama":
                raise LLMPermanentError(
                    f"Active model switched away from Ollama provider during retry "
                    f"(now '{self.llm_cfg.get('provider')}' / '{model}'). Aborting."
                )
            client = self._ollama_clients[self._active_idx]
            if client is None:
                raise RuntimeError(
                    f"_ollama_chat called but no ollama.Client found for model '{model}' "
                    f"(index {self._active_idx}). This is a bug — check _ollama_clients init."
                )
            options = {
                "num_predict": self.llm_cfg.get("max_tokens", 1024),
                "temperature": self.llm_cfg.get("temperature", 0.2),
            }
            top_p = self.llm_cfg.get("top_p")
            if top_p is not None:
                options["top_p"] = top_p
            try:
                response = client.chat(
                    model=model,
                    messages=payload_messages,
                    options=options,
                    **({"format": "json"} if json_mode else {}),
                )
                text = (response.message.content or "").strip()
                # Track token usage from response metadata if available
                _eval_count = getattr(response, "eval_count", None) or 0
                _prompt_eval_count = getattr(response, "prompt_eval_count", None) or 0
                if _prompt_eval_count or _eval_count:
                    self._track_usage(_prompt_eval_count, _eval_count)
            except _ollama_lib.ResponseError as exc:
                # Map to our error hierarchy so _with_retry handles retries correctly
                status = exc.status_code
                _PERMANENT_STATUSES = {401, 403, 404}
                if status in _PERMANENT_STATUSES:
                    raise LLMPermanentError(
                        f"Ollama permanent error (HTTP {status}) for model '{model}': {exc.error}"
                    ) from exc
                raise LLMError(
                    f"Ollama API error (HTTP {status}) for model '{model}': {exc.error}"
                ) from exc

            # Strip inline <think>…</think> reasoning blocks (DeepSeek-R1, QwQ, etc.)
            raw_text = text
            text = _strip_thinking_tags(raw_text)

            # Some Ollama thinking models (Kimi K2.5, DeepSeek-R1, QwQ, etc.) leave
            # "content" empty or wrap their entire answer in <think> tags. Apply the
            # same two-level fallback as the OpenAI path:
            #   1. response.message.thinking — dedicated field (populated when
            #      the Ollama server separates thinking from content)
            #   2. content of the <think> tags themselves — when the model
            #      placed its answer inside the tags with nothing outside
            if not text:
                _tag = f"[{self._caller_tag}/{model}]" if self._caller_tag else f"[{model}]"
                thinking_field = (getattr(response.message, "thinking", None) or "").strip()
                if thinking_field:
                    logger.warning(
                        "%s content field is empty — using 'thinking' field as fallback",
                        _tag,
                    )
                    return thinking_field
                think_content = _extract_thinking_content(raw_text)
                if think_content:
                    logger.warning(
                        "%s content empty after stripping — using <think> block content as fallback",
                        _tag,
                    )
                    return think_content

            if not text:
                raise LLMEmptyResponseError(f"Ollama returned empty content (model: {model})")
            return text

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry,
                           cancel_event=self._cancel_event, model_name=_initial_model, caller_tag=self._caller_tag)

    def _diagnose_empty_response(
        self,
        raw_response,   # httpx.Response captured before raising
        provider: str,
        model: str,
        curl_cmd: Optional[list] = None,
    ) -> str:
        """
        Run diagnostic checks after an empty LLM response and return a
        human-readable report string. Also logs at ERROR level.
        """
        lines = [
            "=== Empty LLM Response Diagnostics ===",
            f"Provider: {provider} | Model: {model}",
        ]

        # 1. Raw HTTP response
        status = getattr(raw_response, "status_code", "N/A")
        lines.append(f"HTTP status: {status}")

        try:
            hdrs = dict(raw_response.headers)
            hdr_str = ", ".join(f"{k}={v}" for k, v in list(hdrs.items())[:10])
            lines.append(f"Headers: {hdr_str}")
        except Exception:
            lines.append("Headers: (unavailable)")

        try:
            raw_body = raw_response.text[:4000]
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

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Return an embedding vector for the given text.

        Results are cached in-memory (up to _embed_cache_max entries) so repeated
        calls with the same text — common during tool-index searches across turns —
        skip the API round-trip entirely.
        """
        if text in self._embed_cache:
            return self._embed_cache[text]

        provider = self.emb_cfg.get("provider", "openai")
        try:
            if provider in ("openai", "openrouter"):
                vector = self._openai_embed(text)
            elif provider == "google":
                vector = self._google_embed(text)
            else:
                # Fallback: OpenAI-compatible
                vector = self._openai_embed(text)
        except _LLM_CALL_ERRORS as exc:
            logger.error("Embedding error: %s", exc)
            raise

        # Evict oldest entry when full (FIFO via dict insertion order, Python 3.7+)
        if len(self._embed_cache) >= self._embed_cache_max:
            oldest = next(iter(self._embed_cache))
            del self._embed_cache[oldest]
        self._embed_cache[text] = vector
        return vector

    def _openai_embed(self, text: str) -> list[float]:
        resp = _with_retry(
            lambda: self._http.post(
                f"{self.emb_cfg['base_url'].rstrip('/')}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.emb_cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={"model": self.emb_cfg["model"], "input": text},
            ),
            self._max_retries, self._retry_delay,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    def _google_embed(self, text: str) -> list[float]:
        api_key = self.emb_cfg["api_key"]
        model = self.emb_cfg.get("model", "models/text-embedding-004")
        resp = _with_retry(
            lambda: self._http.post(
                f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={api_key}",
                json={"content": {"parts": [{"text": text}]}},
            ),
            self._max_retries, self._retry_delay,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]["values"]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        return _cosine_similarity(a, b)

    def close(self):
        self._http.close()
