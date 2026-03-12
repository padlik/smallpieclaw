"""
llm_client.py
-------------
Unified LLM client supporting OpenAI-compatible APIs, Google Gemini, and Anthropic.
Handles both chat completions and embeddings, configured separately.
"""

import json
import logging
import math
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
        self.emb_cfg = config.get("embeddings", config["llm"])
        self._http = httpx.Client(timeout=30)

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
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        resp = self._http.post(
            f"{self.llm_cfg['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.llm_cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.llm_cfg["model"],
                "messages": payload_messages,
                "max_tokens": self.llm_cfg.get("max_tokens", 1024),
                "temperature": self.llm_cfg.get("temperature", 0.2),
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

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
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

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
        return resp.json()["content"][0]["text"]

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
