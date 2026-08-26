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

import contextvars
import json
import logging
import threading
import time
from datetime import date
from typing import Any, Callable, Optional

import httpx
import ollama as _ollama_lib

import agent_logging
from interfaces import ChatResponse, ProviderContext
from providers import anthropic_provider, google_provider, ollama_provider, openai_provider
from providers._errors import (
    LLMCancelledError,
    LLMEmptyResponseError,  # noqa: F401 — re-exported for module-path callers
    LLMError,
    LLMPermanentError,
)
from providers._utils import _encode_images  # noqa: F401 — re-exported for module-path callers

logger = logging.getLogger(__name__)
slog = agent_logging.get_logger(__name__)


_LLM_CALL_ERRORS = (
    LLMError,
    httpx.HTTPError,
    json.JSONDecodeError,
    KeyError,
    ValueError,
)
_LLM_CHAT_ERRORS = _LLM_CALL_ERRORS + (LLMCancelledError,)

# Provider dispatch: maps the config `provider` string to the backend module
# whose `chat()` (and, for openai/google, `embed()`) implements it. Adding a
# provider means one new entry here plus the module — not editing routing
# elif-chains scattered across the class.
_PROVIDER_MODULES: dict[str, Any] = {
    "openai": openai_provider,
    "openrouter": openai_provider,   # OpenAI-compatible wire format
    "anthropic": anthropic_provider,
    "google": google_provider,
    "ollama": ollama_provider,
}

# Providers with a native tool-calling implementation. Others (e.g. anthropic)
# raise NotImplementedError from chat_with_tools() so the ReAct loop falls back
# to the json_mode path.
_NATIVE_TOOL_PROVIDERS = {"openai", "openrouter", "google", "ollama"}


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
                 caller_tag: str | None = None):
        """
        Args:
            config: full agent config dict
            usage_registry: optional TokenUsageRegistry for cross-instance token aggregation
            cancel_event: optional threading.Event; when set, in-progress LLM calls raise LLMCancelledError
            caller_tag: optional label for log messages (e.g. "main", "sa-fcf85d"). Used to
                        identify which agent triggered an LLM call in concurrent log streams.
        """
        self.cfg = config
        self._base_caller_tag = caller_tag or ""
        self._trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            f"llm_client_trace_{id(self)}",
            default="",
        )

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

        # Embedding cache: keyed on text, bounded to avoid unbounded growth in long sessions.
        # Avoids redundant API round-trips when the same text is embedded multiple times
        # (e.g. tool-index queries repeated across user turns).
        self._embed_cache: dict[str, list[float]] = {}
        self._embed_cache_max: int = 512
        self._embed_cache_lock = threading.Lock()

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

    @property
    def _trace_id(self) -> str:
        """Return the current execution-context trace ID."""
        return self._trace_id_var.get()

    @_trace_id.setter
    def _trace_id(self, trace_id: str | None) -> None:
        self._trace_id_var.set((trace_id or "").strip())

    @property
    def _caller_tag(self) -> str:
        """Return the base caller label plus the context-local trace ID."""
        trace_id = self._trace_id
        if trace_id:
            base = self._base_caller_tag.strip()
            return f"{base} {trace_id}".strip()
        return self._base_caller_tag

    def set_trace_id(self, trace_id: str | None) -> None:
        """Set (or clear) the request-scoped trace ID woven into log tags.

        The trace ID is stored in a context-local variable, then appended to the
        caller tag so concurrent runs sharing this LLMClient do not overwrite one
        another's log correlation state. Passing an empty value restores the bare
        caller label in the current execution context.

        Structured-log run identity (including ``trace``) is owned solely by
        ``agent_logging.bind_run_context`` at run entry; this method only manages
        the llm-local ``_trace_id`` used by the caller tag.
        """
        self._trace_id = trace_id

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
            # Normalize aliases: accept list or comma-separated string
            raw_aliases = m.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()]
            if raw_aliases:
                entry["aliases"] = raw_aliases
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
    # Provider backend context
    # ------------------------------------------------------------------

    def _ctx(self) -> ProviderContext:
        """Build the dependency bag passed to extracted provider backends.

        Bundles the mutable client state each provider module needs — the
        active-model config accessor, HTTP transport, retry settings,
        cancellation event, caller tag, usage tracking, and empty-response
        diagnostics — so backends run without a reference to LLMClient itself.
        """
        return ProviderContext(
            get_cfg=lambda: self.llm_cfg,
            http=self._http,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
            cancel_event=self._cancel_event,
            caller_tag=self._caller_tag,
            diagnose_empty=self._diagnose_empty,
            track_usage=self._track_usage,
            emb_cfg=self.emb_cfg,
        )

    def _provider_chat(
        self,
        messages: list[dict],
        system: str | None,
        *,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        progress_cb=None,
    ) -> str | ChatResponse:
        """Route a chat request to the active provider's backend ``chat()``.

        The single dispatch seam for every provider: looks up the backend module
        in ``_PROVIDER_MODULES`` by the active model's ``provider`` and forwards
        the shared kwargs. Ollama additionally needs its per-model
        ``ollama.Client``. Returns ``str`` on the text path (``tools`` is None)
        and ``ChatResponse`` on the native tool-calling path.
        """
        provider = self.llm_cfg.get("provider", "openai")
        mod = _PROVIDER_MODULES.get(provider)
        if mod is None:
            raise LLMError(f"Unknown LLM provider: {provider!r}")
        kwargs: dict[str, Any] = dict(
            tools=tools, json_mode=json_mode, progress_cb=progress_cb,
        )
        ctx = self._ctx()
        if provider == "ollama":
            # Resolve the active ollama.Client lazily on each attempt so a
            # mid-flight set_model() to a different ollama host is picked up by
            # retries instead of POSTing the new model to a stale client.
            ctx.get_ollama_client = lambda: self._ollama_clients[self._active_idx]
        return mod.chat(ctx, messages, system, **kwargs)

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
        model_id = self.llm_cfg.get("model", "?")
        agent_logging.log_event(
            agent_logging.LogEvent.LLM_CALL,
            f"LLM request \u2192 {model_id}",
            level=logging.INFO,
            logger=slog,
            model=model_id,
        )
        _t0 = time.perf_counter()
        try:
            return self._provider_chat(
                messages, system, json_mode=json_mode, progress_cb=progress_cb,
            )  # type: ignore[return-value]
        except _LLM_CHAT_ERRORS as exc:
            logger.error("LLM chat error: %s", exc)
            agent_logging.log_event(
                agent_logging.LogEvent.LLM_FAILED,
                f"LLM request failed: {model_id}",
                level=logging.ERROR,
                logger=slog,
                model=model_id,
                dur_ms=int((time.perf_counter() - _t0) * 1000),
                err=str(exc),
            )
            raise

    @staticmethod
    def _messages_have_images(messages: list[dict]) -> bool:
        """True if any message carries image data the model must be able to see.

        Detects the ReAct loop's ``images`` field as well as provider-style
        multimodal content lists (``[{"type": "image_url", ...}]``).
        """
        for m in messages:
            if m.get("images"):
                return True
            content = m.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                        return True
        return False

    def _run_with_fallback(
        self,
        messages: list[dict],
        call_fn: Callable,
        progress_cb=None,
    ) -> Any:
        """Run ``call_fn`` against the active model (single-model — no fallback chain).

        Shared implementation for ``chat_with_fallback`` and
        ``chat_with_tools_fallback``. Handles vision routing: when images are
        present and the active model is not vision-capable, scans all configured
        ``[[models]]`` entries for the first with ``vision = true`` (by config
        order), routes the request there, and reverts to the primary model
        afterward in a ``finally`` block (success or error).

        ``LLMPermanentError`` and ``LLMCancelledError`` propagate directly. On
        any error ``_active_idx`` is restored to the primary model before
        propagating.
        """
        primary_idx = self._active_idx
        _tag = f"[{self._caller_tag.strip()}] " if self._caller_tag.strip() else ""

        # Vision routing: when images are present, ensure a vision-capable model
        # is used. Scan ALL configured models (independent of any fallback list)
        # for the first with vision=true by config order.
        vision_switch = False
        if self._messages_have_images(messages):
            if not self._models[primary_idx].get("vision"):
                vision_models = [
                    i for i, m in enumerate(self._models) if m.get("vision")
                ]
                if not vision_models:
                    self._active_idx = primary_idx  # restore before propagating
                    configured = [m.get("model", "?") for m in self._models]
                    raise LLMPermanentError(
                        "This request includes an image, but no vision-capable model is "
                        "configured. Set `vision = true` on a model in [[models]]. "
                        f"Configured models: {configured}"
                    )
                target = self._models[vision_models[0]].get("model", "?")
                if progress_cb:
                    progress_cb(
                        f"⚠️ Active model is not vision-capable; switching to vision model '{target}'…"
                    )
                self._active_idx = vision_models[0]
                vision_switch = True

        try:
            return call_fn()
        except LLMPermanentError:
            raise  # permanent errors propagate — no retry, no error log
        except LLMCancelledError:
            raise  # user cancelled — no retry, no error log
        except _LLM_CALL_ERRORS as exc:
            logger.error("%sActive model failed: %s", _tag, exc)
            raise
        finally:
            if vision_switch:
                self._active_idx = primary_idx

    def chat_with_fallback(self, messages: list[dict], system: str | None = None,
                           progress_cb=None, json_mode: bool = False) -> str:
        """
        Like chat(), but with vision routing: when the request contains images
        and the active model is not vision-capable, scans all configured
        ``[[models]]`` for the first with ``vision = true`` (by config order),
        routes the request there, and reverts to the primary model afterward.

        The LLM client is single-model — there is no fallback chain. Transient
        errors on the primary model propagate to the caller. The method name is
        preserved for backward compatibility with existing call sites.

        ``LLMPermanentError`` is raised when images are present but no
        vision-capable model is configured.
        """
        return self._run_with_fallback(
            messages,
            lambda: self.chat(messages, system, progress_cb=progress_cb, json_mode=json_mode),
            progress_cb=progress_cb,
        )

    # ------------------------------------------------------------------
    # Native tool calling methods
    # ------------------------------------------------------------------

    def chat_with_tools(
        self, messages: list[dict], tools: list[dict],
        system: str | None = None, progress_cb=None,
    ) -> ChatResponse:
        """Send a chat request with tool definitions and return a structured response.

        Providers without a native implementation (e.g., ``anthropic``) raise
        ``NotImplementedError`` so the caller can fall back to ``chat(json_mode=True)``.
        """
        provider = self.llm_cfg.get("provider", "openai")
        model_id = self.llm_cfg.get("model", "?")
        # Guard before logging: providers without a native backend (e.g.
        # anthropic) must raise NotImplementedError without emitting a phantom
        # LLM_CALL entry for a request that is never dispatched.
        if provider not in _NATIVE_TOOL_PROVIDERS:
            raise NotImplementedError(
                f"Native tool calling not implemented for provider '{provider}'"
            )
        agent_logging.log_event(
            agent_logging.LogEvent.LLM_CALL,
            f"LLM request (tools) → {model_id}",
            level=logging.INFO,
            logger=slog,
            model=model_id,
        )
        _t0 = time.perf_counter()
        try:
            return self._provider_chat(
                messages, system, tools=tools, progress_cb=progress_cb,
            )  # type: ignore[return-value]
        except _LLM_CHAT_ERRORS as exc:
            logger.error("LLM chat (tools) error: %s", exc)
            agent_logging.log_event(
                agent_logging.LogEvent.LLM_FAILED,
                f"LLM request (tools) failed: {model_id}",
                level=logging.ERROR,
                logger=slog,
                model=model_id,
                dur_ms=int((time.perf_counter() - _t0) * 1000),
                err=str(exc),
            )
            raise

    def chat_with_tools_fallback(
        self, messages: list[dict], tools: list[dict],
        system: str | None = None, progress_cb=None,
    ) -> ChatResponse:
        """Like chat_with_tools(), but with vision routing (single-model, no fallback chain).

        Mirrors ``chat_with_fallback()``: same vision all-models scan and
        ``_active_idx`` restore logic. Calls ``chat_with_tools()`` instead of
        ``chat()``. The LLM client is single-model — transient errors propagate
        to the caller with no fallback attempt.
        """
        return self._run_with_fallback(
            messages,
            lambda: self.chat_with_tools(messages, tools, system, progress_cb=progress_cb),
            progress_cb=progress_cb,
        )

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Return an embedding vector for the given text.

        Results are cached in-memory (up to _embed_cache_max entries) so repeated
        calls with the same text — common during tool-index searches across turns —
        skip the API round-trip entirely.
        """
        with self._embed_cache_lock:
            if text in self._embed_cache:
                logger.debug("embed: cache hit (text_len=%d)", len(text))
                return self._embed_cache[text]

        provider = self.emb_cfg.get("provider", "openai")
        logger.debug("embed: calling %s provider (text_len=%d)", provider, len(text))
        try:
            if provider in ("openai", "openrouter"):
                vector = openai_provider.embed(self._ctx(), text)
            elif provider == "google":
                vector = google_provider.embed(self._ctx(), text)
            else:
                logger.warning(
                    "Embedding provider '%s' has no native support; "
                    "attempting OpenAI-compatible endpoint. "
                    "Configure [embeddings] provider as 'openai', 'openrouter', or 'google'.",
                    provider,
                )
                vector = openai_provider.embed(self._ctx(), text)
        except _LLM_CALL_ERRORS as exc:
            logger.error("Embedding error: %s", exc)
            raise

        self._cache_embedding(text, vector)
        return vector

    def _cache_embedding(self, text: str, vector: list[float]) -> None:
        """Store a single embedding vector in the bounded in-memory cache.

        Evicts the oldest entry (FIFO via dict insertion order, Python 3.7+) when
        the cache is full. Locking is required because concurrent sub-agents share
        this LLMClient and can race on the check-evict-insert sequence.
        """
        with self._embed_cache_lock:
            if len(self._embed_cache) >= self._embed_cache_max:
                oldest = next(iter(self._embed_cache))
                del self._embed_cache[oldest]
                logger.debug("embed: cache evicted oldest entry")
            self._embed_cache[text] = vector
            cache_size = len(self._embed_cache)
        logger.debug(
            "embed: cached vector (dim=%d, cache=%d/%d)",
            len(vector),
            cache_size,
            self._embed_cache_max,
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for multiple text strings in one batch call.

        OpenAI/OpenRouter and Google providers use their native batch embedding
        endpoints; other providers fall back to serial ``embed()`` calls. Cache
        hits are served from memory, and misses are cached after the batch call.
        """
        if not texts:
            return []

        results: list[Optional[list[float]]] = [None] * len(texts)
        missing_indices: list[int] = []
        missing_texts: list[str] = []

        with self._embed_cache_lock:
            for idx, text in enumerate(texts):
                if text in self._embed_cache:
                    logger.debug("embed_batch: cache hit (text_len=%d)", len(text))
                    results[idx] = self._embed_cache[text]
                else:
                    missing_indices.append(idx)
                    missing_texts.append(text)

        if not missing_texts:
            return [v for v in results if v is not None]

        provider = self.emb_cfg.get("provider", "openai")
        logger.debug(
            "embed_batch: calling %s provider for %d texts",
            provider,
            len(missing_texts),
        )
        try:
            if provider in ("openai", "openrouter"):
                vectors = openai_provider.embed_batch(self._ctx(), missing_texts)
            elif provider == "google":
                vectors = google_provider.embed_batch(self._ctx(), missing_texts)
            else:
                logger.warning(
                    "Embedding provider '%s' has no native batch support; "
                    "falling back to serial embed() calls.",
                    provider,
                )
                vectors = [self.embed(text) for text in missing_texts]
        except _LLM_CALL_ERRORS as exc:
            logger.error("Batch embedding error: %s", exc)
            raise

        for text, vector in zip(missing_texts, vectors, strict=True):
            self._cache_embedding(text, vector)
        for idx, vector in zip(missing_indices, vectors, strict=True):
            results[idx] = vector

        return [v for v in results if v is not None]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def close(self):
        """Close all HTTP transports owned by this client. Idempotent."""
        try:
            self._http.close()
        except Exception:
            pass
        for oc in self._ollama_clients:
            if oc is not None:
                try:
                    oc._client.close()
                except Exception:
                    pass
