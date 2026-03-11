"""
llm_client.py — Unified LLM client supporting OpenAI, Claude, Google, OpenRouter.
"""

from __future__ import annotations
import json
import logging
import re
from typing import Any

import config

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Extract the first JSON object found in a string."""
    # Try raw parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Find first { … } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON found in LLM response:\n{text}")


# ── provider implementations ──────────────────────────────────────────────────

class _OpenAIClient:
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        import openai
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def chat(self, messages: list[dict]) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.2,
            max_tokens=1024,
        )
        return resp.choices[0].message.content or ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(
            model=config.EMBEDDING_MODEL,
            input=texts,
        )
        return [item.embedding for item in resp.data]


class _ClaudeClient:
    def __init__(self, api_key: str, model: str):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def chat(self, messages: list[dict]) -> str:
        # Separate system message if present
        system = ""
        filtered = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                filtered.append(m)
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=1024,
            messages=filtered,
        )
        if system:
            kwargs["system"] = system
        resp = self._client.messages.create(**kwargs)
        return resp.content[0].text if resp.content else ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "Claude does not expose an embeddings API. "
            "Set EMBEDDING_PROVIDER=openai or EMBEDDING_PROVIDER=none."
        )


class _GoogleClient:
    def __init__(self, api_key: str, model: str):
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model)
        self._embed_model = "models/text-embedding-004"

    def chat(self, messages: list[dict]) -> str:
        # Convert to Google format
        history = []
        prompt = ""
        for m in messages:
            if m["role"] == "system":
                # Prepend as first user turn
                history.append({"role": "user", "parts": [m["content"]]})
                history.append({"role": "model", "parts": ["Understood."]})
            elif m["role"] == "user":
                prompt = m["content"]
            else:
                history.append({"role": "model", "parts": [m["content"]]})
        chat = self._model.start_chat(history=history)
        resp = chat.send_message(prompt)
        return resp.text

    def embed(self, texts: list[str]) -> list[list[float]]:
        import google.generativeai as genai
        results = []
        for t in texts:
            r = genai.embed_content(model=self._embed_model, content=t)
            results.append(r["embedding"])
        return results


class _OpenRouterClient(_OpenAIClient):
    """OpenRouter is OpenAI-compatible."""
    def __init__(self, api_key: str, model: str):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url="https://openrouter.ai/api/v1",
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "OpenRouter does not expose embeddings. "
            "Set EMBEDDING_PROVIDER=openai or EMBEDDING_PROVIDER=none."
        )


# ── public interface ───────────────────────────────────────────────────────────

_chat_client = None
_embed_client = None


def _get_chat_client():
    global _chat_client
    if _chat_client is not None:
        return _chat_client
    p = config.LLM_PROVIDER
    if p == "openai":
        _chat_client = _OpenAIClient(config.OPENAI_API_KEY, config.OPENAI_MODEL)
    elif p == "claude":
        _chat_client = _ClaudeClient(config.CLAUDE_API_KEY, config.CLAUDE_MODEL)
    elif p == "google":
        _chat_client = _GoogleClient(config.GOOGLE_API_KEY, config.GOOGLE_MODEL)
    elif p == "openrouter":
        _chat_client = _OpenRouterClient(config.OPENROUTER_API_KEY, config.OPENROUTER_MODEL)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {p}")
    return _chat_client


def _get_embed_client():
    global _embed_client
    if _embed_client is not None:
        return _embed_client
    p = config.EMBEDDING_PROVIDER
    if p == "openai":
        _embed_client = _OpenAIClient(config.OPENAI_API_KEY, config.EMBEDDING_MODEL)
    elif p == "google":
        _embed_client = _GoogleClient(config.GOOGLE_API_KEY, config.GOOGLE_MODEL)
    elif p == "none":
        _embed_client = None
        return None
    else:
        raise ValueError(f"Unknown EMBEDDING_PROVIDER: {p}")
    return _embed_client


def chat(messages: list[dict]) -> str:
    """Send messages to the configured chat LLM and return text."""
    return _get_chat_client().chat(messages)


def chat_json(messages: list[dict]) -> dict:
    """Send messages and parse the response as JSON."""
    raw = chat(messages)
    return _extract_json(raw)


def embed(texts: list[str]) -> list[list[float]] | None:
    """
    Generate embeddings for a list of texts.
    Returns None if embedding is disabled (EMBEDDING_PROVIDER=none).
    """
    client = _get_embed_client()
    if client is None:
        return None
    return client.embed(texts)
