"""
llm_client.py
-------------
Unified LLM client supporting OpenAI-compatible APIs, Google Gemini, and Anthropic.
Handles both chat completions and embeddings, configured separately.

Note on OpenAI reasoning models (o1, o1-mini, o3, o3-mini, o4-mini, gpt-5, etc.):
  These models do NOT accept a `temperature` parameter — passing any value causes
  a 400 "Unsupported parameter" error. The client detects these models by name
  and omits temperature (and other unsupported sampling params) automatically.
"""

import logging
import math
import re
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.llm_cfg = config["llm"]
        emb_cfg = dict(config.get("embeddings", config["llm"]))
        # Fall back to LLM credentials if embeddings section is missing them
        if not emb_cfg.get("api_key"):
            emb_cfg["api_key"] = self.llm_cfg["api_key"]
        if not emb_cfg.get("base_url"):
            emb_cfg["base_url"] = self.llm_cfg.get("base_url", "")
        self.emb_cfg = emb_cfg
        self._http = httpx.Client(timeout=30)

        # Daily token usage tracking
        self._usage_date: date = date.today()
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0

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

    def chat(self, messages: list[dict], system: str | None = None) -> str:
        """Send a chat request and return the assistant text."""
        provider = self.llm_cfg["provider"]
        try:
            if provider in ("openai", "openrouter"):
                return self._openai_chat(messages, system)
            elif provider == "google":
                return self._google_chat(messages, system)
            elif provider == "anthropic":
                return self._anthropic_chat(messages, system)
            else:
                raise ValueError(f"Unknown LLM provider: {provider}")
        except Exception as exc:
            logger.error("LLM chat error: %s", exc)
            raise

    def _openai_chat(self, messages: list[dict], system: str | None) -> str:
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

        resp = self._http.post(
            f"{self.llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.llm_cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        self._track_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]["content"]

    def _google_chat(self, messages: list[dict], system: str | None) -> str:
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
        resp = self._http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            json={
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": self.llm_cfg.get("max_tokens", 1024),
                    "temperature": self.llm_cfg.get("temperature", 0.2),
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("usageMetadata", {})
        self._track_usage(meta.get("promptTokenCount", 0), meta.get("candidatesTokenCount", 0))
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def _anthropic_chat(self, messages: list[dict], system: str | None) -> str:
        payload: dict[str, Any] = {
            "model": self.llm_cfg["model"],
            "max_tokens": self.llm_cfg.get("max_tokens", 1024),
            "messages": messages,
        }
        if system:
            payload["system"] = system

        resp = self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.llm_cfg["api_key"],
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        self._track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return data["content"][0]["text"]

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
        resp = self._http.post(
            f"{self.emb_cfg['base_url'].rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {self.emb_cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={"model": self.emb_cfg["model"], "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    def _google_embed(self, text: str) -> list[float]:
        api_key = self.emb_cfg["api_key"]
        model = self.emb_cfg.get("model", "models/text-embedding-004")
        resp = self._http.post(
            f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={api_key}",
            json={"content": {"parts": [{"text": text}]}},
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

