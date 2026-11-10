"""Independent provider dispatch and bounded cache for embedding requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from config_schema import (
    EmbeddingsConfig,
    _expand_value,
    _load_vault,
)

_SUPPORTED = frozenset({"openai", "openrouter", "google"})
_REQUEST_TIMEOUT = 120.0
_CACHE_MAX = 512


class EmbeddingUnavailableError(RuntimeError):
    """Safe, actionable error for unavailable embedding configuration or calls."""


@dataclass(frozen=True, repr=False)
class _ResolvedSettings:
    provider: str
    model: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)


class _NoInheritedAuth(httpx.Auth):
    """Prevent an injected client's default auth from changing request identity."""

    def auth_flow(self, request: httpx.Request):
        yield request


class EmbeddingClient:
    """Dispatch embedding requests using only their explicit configuration.

    Credentials and endpoints are resolved once and kept private. The client
    supports OpenAI, OpenRouter, and Google embedding wire formats; chat-only
    adapters are deliberately unavailable.
    """

    def __init__(
        self,
        embeddings: EmbeddingsConfig,
        *,
        vault_file: str | None = None,
        http_client: httpx.Client | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Resolve the four explicit settings and create an isolated transport."""
        self._cancel_event = cancel_event
        self._cache: dict[str, list[float]] = {}
        self._cache_lock = threading.Lock()
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client()
        self._settings: _ResolvedSettings | None = None
        self._timeout = _REQUEST_TIMEOUT
        self._unavailable_reason = ""
        try:
            self._settings = self._resolve(embeddings, vault_file)
        except ValueError as exc:
            self._unavailable_reason = str(exc)
        except Exception:
            # Credential resolver/config exceptions can contain a source value or
            # path; retain only an actionable category for degraded startup.
            self._unavailable_reason = "embedding settings or credential are unavailable"

    @property
    def available(self) -> bool:
        """Whether explicit embedding settings resolved to a supported adapter."""
        return self._settings is not None

    @property
    def unavailable_reason(self) -> str:
        """Return a sanitized reason suitable for an operator warning."""
        return self._unavailable_reason

    @property
    def model_name(self) -> str:
        """Return the configured embedding model, or an empty string if invalid."""
        return self._settings.model if self._settings else ""

    def embed(self, text: str) -> list[float]:
        """Embed one string, returning a defensive copy of cached vectors."""
        self._require_available()
        with self._cache_lock:
            cached = self._cache.get(text)
            if cached is not None:
                return list(cached)
        vectors = self._request([text])
        vector = vectors[0]
        self._cache_vectors([text], vectors)
        return list(vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch while preserving order, duplicates, and cache hits."""
        if not texts:
            return []
        self._require_available()
        results: list[list[float] | None] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_indices: list[int] = []
        with self._cache_lock:
            for index, text in enumerate(texts):
                vector = self._cache.get(text)
                if vector is not None:
                    results[index] = list(vector)
                else:
                    missing_texts.append(text)
                    missing_indices.append(index)
        if missing_texts:
            vectors = self._request(missing_texts)
            self._cache_vectors(missing_texts, vectors)
            for index, vector in zip(missing_indices, vectors, strict=True):
                results[index] = list(vector)
        return [vector for vector in results if vector is not None]

    def close(self) -> None:
        """Close the HTTP client only when this instance created it."""
        if self._owns_client:
            self._http.close()

    def _resolve(
        self,
        embeddings: EmbeddingsConfig,
        vault_file: str | None,
    ) -> _ResolvedSettings:
        if embeddings.unavailable_reason:
            raise ValueError("embedding configuration unavailable")
        missing = [
            name for name, value in (
                ("provider", embeddings.provider),
                ("base_url", embeddings.base_url),
                ("api_key", embeddings.api_key),
                ("model", embeddings.model),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            names = ", ".join(f"embeddings.{name}" for name in missing)
            raise ValueError(f"{names} must be configured explicitly")
        if embeddings.provider not in _SUPPORTED:
            raise ValueError("unsupported embedding adapter; configure a supported provider")
        api_key = _resolve_credential(embeddings.api_key, vault_file)
        if not api_key:
            raise ValueError("embedding credential required")
        _validate_endpoint(embeddings.base_url)
        return _ResolvedSettings(
            embeddings.provider, embeddings.model, embeddings.base_url.rstrip("/"), api_key
        )

    def _require_available(self) -> _ResolvedSettings:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise EmbeddingUnavailableError("Embedding request cancelled.")
        if self._settings is None:
            raise EmbeddingUnavailableError(
                "embedding configuration is unavailable; configure an explicit supported provider, model, and credential."
            )
        return self._settings

    def _request(self, texts: list[str]) -> list[list[float]]:
        settings = self._require_available()
        try:
            if settings.provider == "google":
                vectors = self._google_request(settings, texts)
            else:
                vectors = self._openai_request(settings, texts)
            if len(vectors) != len(texts):
                raise EmbeddingUnavailableError("Embedding provider returned an unexpected vector count.")
            return vectors
        except EmbeddingUnavailableError:
            raise
        except Exception:
            raise EmbeddingUnavailableError(
                "Embedding provider request failed; verify the configured embedding provider and model."
            ) from None

    def _send(self, url: str, key_header: tuple[str, str], payload: dict) -> dict:
        request = httpx.Request(
            "POST",
            url,
            headers={key_header[0]: key_header[1], "Content-Type": "application/json"},
            json=payload,
        )
        request.extensions["timeout"] = httpx.Timeout(self._timeout).as_dict()
        response = self._http.send(
            request, auth=_NoInheritedAuth(), follow_redirects=False
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid embedding response")
        return data

    def _openai_request(self, settings: _ResolvedSettings, texts: list[str]) -> list[list[float]]:
        data = self._send(
            f"{settings.endpoint}/embeddings",
            ("Authorization", f"Bearer {settings.api_key}"),
            {"model": settings.model, "input": texts[0] if len(texts) == 1 else texts},
        )
        items = data.get("data")
        if not isinstance(items, list):
            raise ValueError("invalid embedding response")
        return [_vector(item["embedding"]) for item in items]

    def _google_request(self, settings: _ResolvedSettings, texts: list[str]) -> list[list[float]]:
        model = settings.model if settings.model.startswith("models/") else f"models/{settings.model}"
        if len(texts) == 1:
            data = self._send(
                f"{settings.endpoint}/{model}:embedContent",
                ("x-goog-api-key", settings.api_key),
                {"content": {"parts": [{"text": texts[0]}]}},
            )
            return [_vector(data["embedding"]["values"])]
        requests = [
            {"model": model, "content": {"parts": [{"text": text}]}}
            for text in texts
        ]
        data = self._send(
            f"{settings.endpoint}/{model}:batchEmbedContents",
            ("x-goog-api-key", settings.api_key),
            {"requests": requests},
        )
        items = data.get("embeddings")
        if not isinstance(items, list):
            raise ValueError("invalid embedding response")
        return [_vector(item["values"]) for item in items]

    def _cache_vectors(self, texts: list[str], vectors: list[list[float]]) -> None:
        with self._cache_lock:
            for text, vector in zip(texts, vectors, strict=True):
                if text in self._cache:
                    continue
                if len(self._cache) >= _CACHE_MAX:
                    del self._cache[next(iter(self._cache))]
                self._cache[text] = list(vector)

    def __repr__(self) -> str:
        return f"EmbeddingClient(available={self.available}, model_name={self.model_name!r})"


def _resolve_credential(source: str, vault_file: str | None) -> str:
    if not isinstance(source, str) or not source:
        return ""
    try:
        if source.startswith("env:") or source.startswith("sec:"):
            vault = _load_vault(vault_file) if source.startswith("sec:") and vault_file else None
            resolved = _expand_value(source, path="embedding credential", vault=vault)
            return resolved if isinstance(resolved, str) else ""
        return source
    except Exception:
        raise ValueError("credential unavailable") from None


def _validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid = valid and parsed.username is None and parsed.password is None
        valid = valid and not parsed.query and not parsed.fragment
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("embedding endpoint must be a valid HTTP(S) URL")


def _vector(value: object) -> list[float]:
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError("invalid vector")
    return [float(item) for item in value]
