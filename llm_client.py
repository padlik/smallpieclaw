"""
llm_client.py
-------------
Unified LLM client supporting OpenAI-compatible APIs, Google Gemini, and Anthropic.
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
    max_retries     = 3     # attempts before giving up (default: 3)
    retry_delay     = 2     # base delay in seconds; doubles each attempt (default: 2)
"""

import json
import logging
import math
import re
import subprocess
import time
from datetime import date
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# HTTP status codes that are safe to retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMEmptyResponseError(RuntimeError):
    """Raised when the LLM provider returns an empty or whitespace-only response."""


def _with_retry(fn, max_retries: int, base_delay: float, on_retry=None):
    """
    Call fn(), retrying on transient httpx errors and retryable HTTP status codes.
    Uses exponential backoff: base_delay, base_delay*2, base_delay*4, …
    Non-retryable exceptions (e.g. 400 Bad Request) propagate immediately.
    on_retry(attempt, max_retries, exc_str) is called before each retry delay.
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("Request timed out (attempt %d/%d): %s", attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"timeout: {exc}")
        except LLMEmptyResponseError as exc:
            last_exc = exc
            logger.warning("Empty LLM response (attempt %d/%d)", attempt, max_retries)
            if on_retry:
                on_retry(attempt, max_retries, "empty response")
        except httpx.RemoteProtocolError as exc:
            last_exc = exc
            logger.warning("Remote protocol error (attempt %d/%d): %s", attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"protocol error: {exc}")
        except httpx.ConnectError as exc:
            last_exc = exc
            logger.warning("Connection error (attempt %d/%d): %s", attempt, max_retries, exc)
            if on_retry:
                on_retry(attempt, max_retries, f"connection error: {exc}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _RETRYABLE_STATUS:
                last_exc = exc
                logger.warning(
                    "HTTP %d (attempt %d/%d): %s",
                    exc.response.status_code, attempt, max_retries, exc,
                )
                if on_retry:
                    on_retry(attempt, max_retries, f"HTTP {exc.response.status_code}")
            else:
                # Log full response body to aid debugging (e.g. "invalid parameter" messages)
                logger.error(
                    "HTTP %d error — response body: %s",
                    exc.response.status_code,
                    exc.response.text[:2000],
                )
                raise  # 4xx errors are not retryable
        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            logger.info("Retrying in %.1fs…", delay)
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

    Multi-model support:
      Define [[models]] in config.toml (array of tables). Each entry must have:
        name, provider, api_key, model, base_url, max_tokens, temperature, hint,
        request_timeout, max_retries, retry_delay.
      Set agent.default_model to the 'model' value of the entry to use by default.
    """

    def __init__(self, config: dict):
        self.cfg = config

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

        # Embeddings config (with fallback to active LLM key/base_url)
        emb_cfg = dict(config.get("embeddings", self._models[self._active_idx]))
        if not emb_cfg.get("api_key"):
            emb_cfg["api_key"] = self._models[self._active_idx].get("api_key", "")
        if not emb_cfg.get("base_url"):
            emb_cfg["base_url"] = self._models[self._active_idx].get("base_url", "")
        self.emb_cfg = emb_cfg

        # Retry / timeout from active model config
        _ref = self._models[self._active_idx]
        self._max_retries: int = _ref.get("max_retries", 3)
        self._retry_delay: float = float(_ref.get("retry_delay", 2))
        timeout_secs: float = float(_ref.get("request_timeout", 120))
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_secs, connect=10.0)
        )

        # Diagnostic mode for empty responses
        self._diagnose_empty: bool = bool(
            config.get("agent", {}).get("diagnose_empty_responses", False)
        )

        # Daily token usage tracking
        self._usage_date: date = date.today()
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0

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
            result.append({
                "name": m.get("name", f"model_{i}"),
                "model": m.get("model", "?"),
                "provider": m.get("provider", "?"),
                "hint": m.get("hint", ""),
                "active": i == self._active_idx,
            })
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

    def select_model_by_hint(self, goal: str) -> Optional[str]:
        """
        Check if any word in the goal matches any hint keyword of a non-active model.
        If a match is found, switch to that model and return its name.
        Returns None if no switch was made.
        """
        goal_words = set(re.findall(r"\w+", goal.lower()))
        for i, m in enumerate(self._models):
            if i == self._active_idx:
                continue
            hint_words = set(re.findall(r"\w+", m.get("hint", "").lower()))
            if goal_words & hint_words:
                self._active_idx = i
                name = m.get("name", f"model_{i}")
                logger.info("Auto-selected model '%s' based on hint match", name)
                return name
        return None

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

    def chat(self, messages: list[dict], system: str | None = None, progress_cb=None) -> str:
        """Send a chat request and return the assistant text."""
        provider = self.llm_cfg["provider"]
        try:
            if provider in ("openai", "openrouter"):
                return self._openai_chat(messages, system, progress_cb=progress_cb)
            elif provider == "google":
                return self._google_chat(messages, system, progress_cb=progress_cb)
            elif provider == "anthropic":
                return self._anthropic_chat(messages, system, progress_cb=progress_cb)
            else:
                raise ValueError(f"Unknown LLM provider: {provider}")
        except Exception as exc:
            logger.error("LLM chat error: %s", exc)
            raise

    def _openai_chat(self, messages: list[dict], system: str | None, progress_cb=None) -> str:
        model = self.llm_cfg["model"]
        reasoning = _is_reasoning_model(model)

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
        payload_messages.extend(messages)

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

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        url = f"{self.llm_cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.llm_cfg['api_key']}",
            "Content-Type": "application/json",
        }

        def _do_request():
            r = self._http.post(url, headers=headers, json=payload)
            r.raise_for_status()
            d = r.json()
            usage = d.get("usage", {})
            self._track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            text = (d["choices"][0]["message"]["content"] or "").strip()
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

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry)

    def _google_chat(self, messages: list[dict], system: str | None, progress_cb=None) -> str:
        # Convert to Gemini format
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": f"[System]: {system}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood."}]})
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})

        api_key = self.llm_cfg["api_key"]
        model = self.llm_cfg["model"]
        google_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        google_payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": self.llm_cfg.get("max_tokens", 1024),
                "temperature": self.llm_cfg.get("temperature", 0.2),
            },
        }

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        def _do_request():
            r = self._http.post(google_url, json=google_payload)
            r.raise_for_status()
            d = r.json()
            meta = d.get("usageMetadata", {})
            self._track_usage(meta.get("promptTokenCount", 0), meta.get("candidatesTokenCount", 0))
            text = (d["candidates"][0]["content"]["parts"][0]["text"] or "").strip()
            if not text:
                if self._diagnose_empty:
                    curl_cmd = [
                        "curl", "-s", "-X", "POST", google_url,
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps(google_payload),
                    ]
                    report = self._diagnose_empty_response(r, "google", model, curl_cmd=curl_cmd)
                    raise LLMEmptyResponseError(
                        f"Google returned empty content (model: {model})\n{report}"
                    )
                raise LLMEmptyResponseError(f"Google returned empty content (model: {model})")
            return text

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry)

    def _anthropic_chat(self, messages: list[dict], system: str | None, progress_cb=None) -> str:
        model = self.llm_cfg["model"]
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.llm_cfg.get("max_tokens", 1024),
            "messages": messages,
        }
        if system:
            payload["system"] = system

        anthropic_url = "https://api.anthropic.com/v1/messages"
        anthropic_headers = {
            "x-api-key": self.llm_cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        def _on_retry(attempt, max_retries, reason):
            if progress_cb:
                progress_cb(f"⏳ LLM request failed ({reason}), retry {attempt}/{max_retries}…")

        def _do_request():
            r = self._http.post(anthropic_url, headers=anthropic_headers, json=payload)
            r.raise_for_status()
            d = r.json()
            usage = d.get("usage", {})
            self._track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            text = (d["content"][0]["text"] or "").strip()
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

        return _with_retry(_do_request, self._max_retries, self._retry_delay, on_retry=_on_retry)

    # ------------------------------------------------------------------
    # Empty-response diagnostics
    # ------------------------------------------------------------------

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
            except Exception as exc:
                lines.append(f"curl attempt failed: {exc}")

        lines.append("=======================================")
        report = "\n".join(lines)
        logger.error("Empty LLM response diagnostic:\n%s", report)
        return report

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Return an embedding vector for the given text."""
        provider = self.emb_cfg.get("provider", "openai")
        try:
            if provider in ("openai", "openrouter"):
                return self._openai_embed(text)
            elif provider == "google":
                return self._google_embed(text)
            else:
                # Fallback: OpenAI-compatible
                return self._openai_embed(text)
        except Exception as exc:
            logger.error("Embedding error: %s", exc)
            raise

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

