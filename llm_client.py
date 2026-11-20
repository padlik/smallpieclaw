"""Single-run-model provider client with a final, bounded HTTP send gate."""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterator, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import ollama as _ollama_lib

import agent_logging
from call_attempt_state import CallAttemptState, CallAttemptStateError
from config_schema import AgentConfig
from embedding_client import EmbeddingClient, EmbeddingUnavailableError
from interfaces import ChatResponse, ProviderContext
from model_registry import ModelMetadata, RunModel
from providers import anthropic_provider, google_provider, ollama_provider, openai_provider
from providers._errors import (
    LLMCancelledError,
    LLMContextOverflowError as LLMContextOverflowError,
    LLMEmptyResponseError as LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)
from providers._utils import (
    _encode_images as _encode_images,
    _is_reasoning_model,
    is_context_overflow_message,
    provider_error_code,
)
from token_usage import TokenUsageRegistry

logger = logging.getLogger(__name__)
slog = agent_logging.get_logger(__name__)
_ChatResult = TypeVar("_ChatResult", str, ChatResponse)

_PROVIDER_MODULES: dict[str, Any] = {
    "openai": openai_provider,
    "openrouter": openai_provider,
    "anthropic": anthropic_provider,
    "google": google_provider,
    "ollama": ollama_provider,
}
_NATIVE_TOOL_PROVIDERS = frozenset({"openai", "openrouter", "google", "ollama"})
_LLM_CHAT_ERRORS = (
    LLMError,
    LLMEmptyResponseError,
    httpx.HTTPError,
    json.JSONDecodeError,
    KeyError,
    ValueError,
    LLMCancelledError,
)
_DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "ollama": "https://ollama.com",
}


def is_official_ollama_origin(base_url: str) -> bool:
    """Return whether *base_url* is exactly the official cloud Ollama origin.

    Pure string check shared by ``LLMClient`` construction and graph startup
    validation, so no client has to be built to find an unsupported endpoint.
    """
    try:
        parsed = urlsplit(base_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "ollama.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _safe_llm_failure_category(exc: Exception) -> str:
    """Map classified chat failures to fixed, non-sensitive log categories."""
    if isinstance(exc, LLMCancelledError):
        return "cancelled"
    if isinstance(exc, LLMEmptyResponseError):
        return "empty_response"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_status_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "transport_error"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, (KeyError, ValueError)):
        return "invalid_response"
    if isinstance(exc, LLMPermanentError):
        return "permanent"
    return "provider_error"



@dataclass
class _AttemptBinding:
    state: CallAttemptState
    manual: bool
    definition_revision: str | int | None
    sent: bool = False
    before_send: Callable[[], None] | None = None


class CheckpointRetirementBlocked(RuntimeError):
    """A saved checkpoint could not be retired before an outbound attempt."""


@dataclass(frozen=True)
class _OllamaInvocation:
    """Immutable request mode and caller tool JSON for one SDK invocation."""

    mode: str
    tools_json: str | None


@dataclass(frozen=True)
class _GraphPolicy:
    output_cap: int = 1024
    temperature: float = 0.1


class _NoInheritedAuth(httpx.Auth):
    """Prevent a transport client's ambient auth from changing the request."""

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        yield request


class _OllamaWireTransport(httpx.BaseTransport):
    """Finalize SDK requests before handing them to the owned HTTP transport."""

    def __init__(self, owner: LLMClient, inner: httpx.BaseTransport) -> None:
        self._owner = owner
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Replace SDK-projected tool schemas, budget, count, then delegate once."""
        return self._owner._handle_ollama_transport_request(request, self._inner)

    def close(self) -> None:
        """Close the wrapped transport owned by this SDK client."""
        self._inner.close()



class LLMClient:
    """One immutable admitted model and its matching provider connection."""

    def __init__(
        self,
        run_model: RunModel,
        *,
        agent_config: AgentConfig,
        embedding_client: EmbeddingClient | None = None,
        http_transport: httpx.BaseTransport | None = None,
        cancel_event: threading.Event | None = None,
        usage_registry: TokenUsageRegistry | None = None,
        caller_tag: str | None = None,
    ) -> None:
        """Create a client pinned to one admitted model/connection snapshot."""
        if not isinstance(run_model, RunModel):
            raise LLMPermanentError("LLMClient requires an admitted RunModel")
        if not isinstance(agent_config, AgentConfig):
            raise LLMPermanentError("LLMClient requires typed agent configuration")
        if run_model.model.provider_id != run_model.connection.provider_id:
            raise LLMPermanentError("Pinned model and provider connection do not match")
        if run_model.model.provider_id == "" or run_model.connection.adapter not in _PROVIDER_MODULES:
            raise LLMPermanentError("Pinned provider configuration is invalid")
        if run_model.connection.adapter == "ollama":
            self._validate_ollama_connection(run_model)

        self._run_model = run_model
        self._required_output_cap = int(agent_config.required_output_cap_tokens)
        self._max_retries = int(agent_config.max_retries)
        self._retry_delay = float(agent_config.retry_delay)
        self._embedding_client = embedding_client
        self._cancel_event = cancel_event
        self._usage_registry = usage_registry
        self._base_caller_tag = caller_tag or ""
        self._trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            f"llm_client_trace_{id(self)}", default=""
        )
        self._binding_var: contextvars.ContextVar[_AttemptBinding | None] = contextvars.ContextVar(
            f"llm_client_attempt_{id(self)}", default=None
        )
        self._cancel_var: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
            f"llm_client_cancel_{id(self)}", default=None
        )
        self._ollama_invocation_var: contextvars.ContextVar[_OllamaInvocation | None] = (
            contextvars.ContextVar(f"llm_client_ollama_invocation_{id(self)}", default=None)
        )
        self._usage_date = date.today()
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._graph_policy: _GraphPolicy | None = None
        self._closed = False

        connection = run_model.connection
        timeout = float(connection.request_timeout)
        self._timeout = timeout
        self._endpoint = connection.base_url or _DEFAULT_ENDPOINTS[connection.adapter]
        self._api_key = connection.api_key
        self._ollama_client: _ollama_lib.Client | None = None
        if connection.adapter == "ollama":
            if not self._api_key.strip():
                raise LLMPermanentError("Cloud Ollama requires an API credential")
            self._http: httpx.Client | None = None
            # Ownership of an injected transport transfers to this client; the
            # public SDK close method closes the wrapper and its inner transport.
            inner_transport = http_transport or httpx.HTTPTransport(
                trust_env=False, retries=0
            )
            wire_transport = _OllamaWireTransport(self, inner_transport)
            self._ollama_client = _ollama_lib.Client(
                host=self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                follow_redirects=False,
                trust_env=False,
                transport=wire_transport,
            )
        else:
            self._http = httpx.Client(
                timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                follow_redirects=False,
                trust_env=False,
                transport=http_transport,
            )

    @classmethod
    def for_graph_extraction(
        cls,
        run_model: RunModel,
        *,
        agent_config: AgentConfig,
        embedding_client: EmbeddingClient | None = None,
        http_transport: httpx.BaseTransport | None = None,
        cancel_event: threading.Event | None = None,
        usage_registry: TokenUsageRegistry | None = None,
        caller_tag: str | None = None,
    ) -> LLMClient:
        """Build the private fixed-policy graph extraction client."""
        client = cls(
            run_model,
            agent_config=agent_config,
            embedding_client=embedding_client,
            http_transport=http_transport,
            cancel_event=cancel_event,
            usage_registry=usage_registry,
            caller_tag=caller_tag,
        )
        client._graph_policy = _GraphPolicy()
        return client

    @staticmethod
    def _validate_ollama_connection(run_model: RunModel) -> None:
        """Fail closed unless the admitted Ollama connection is official cloud."""
        if not is_official_ollama_origin(run_model.connection.base_url):
            raise LLMPermanentError("Cloud Ollama requires the official https://ollama.com origin")

    @property
    def metadata(self) -> ModelMetadata:
        """Return the immutable typed metadata of the pinned model."""
        return self._run_model.model.metadata

    @property
    def llm_cfg(self) -> dict[str, Any]:
        """Return a fresh, sanitized projection of the pinned model snapshot."""
        record = self._run_model.model
        metadata = record.metadata
        return {
            "id": record.id,
            "name": record.display_name,
            "model": record.upstream_id,
            "provider": self._run_model.connection.adapter,
            "provider_id": record.provider_id,
            "metadata": {
                "context_tokens": metadata.context_tokens,
                "input_tokens": metadata.input_tokens,
                "output_tokens": metadata.output_tokens,
                "vision": metadata.vision,
                "tool_calling": metadata.tool_calling,
                "reasoning": metadata.reasoning,
            },
            "catalog_revision": self._run_model.catalog_revision,
            "selection_revision": self._run_model.selection_revision,
        }

    @property
    def _trace_id(self) -> str:
        """Return the context-local trace ID used by client log tags."""
        return self._trace_id_var.get()

    @_trace_id.setter
    def _trace_id(self, trace_id: str | None) -> None:
        self._trace_id_var.set((trace_id or "").strip())

    @property
    def _caller_tag(self) -> str:
        trace_id = self._trace_id
        return f"{self._base_caller_tag} {trace_id}".strip()

    def set_trace_id(self, trace_id: str | None) -> None:
        """Set a context-local trace label without changing the pinned model."""
        self._trace_id = trace_id

    def list_models(self) -> list[dict[str, Any]]:
        """Return only the pinned sanitized model projection."""
        return [{**self.llm_cfg, "active": True}]

    def set_model(self, name: str) -> bool:
        """Reject retargeting: an admitted client cannot change models."""
        del name
        raise LLMPermanentError("LLMClient is pinned; start a new run to select another model")

    @contextmanager
    def bind_cancel_event(self, event: threading.Event) -> Iterator[None]:
        """Let a caller-owned cancel event take precedence over failure classification.

        Context-local: nothing is stored on the shared client, the event is never set or
        cleared here, and the binding is reset on exit.
        """
        token = self._cancel_var.set(event)
        try:
            yield
        finally:
            self._cancel_var.reset(token)

    def _cancel_requested(self) -> bool:
        """Whether the client's own or the bound caller's cancel event is set."""
        bound = self._cancel_var.get()
        return (self._cancel_event is not None and self._cancel_event.is_set()) or (
            bound is not None and bound.is_set()
        )

    @contextmanager
    def bind_attempt(
        self,
        state: CallAttemptState,
        *,
        manual: bool = False,
        definition_revision: str | int = 0,
        before_send: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Bind one public invocation to its shared logical-request state."""
        if not isinstance(state, CallAttemptState):
            raise CallAttemptStateError("invalid_state", "state must be CallAttemptState")
        binding = _AttemptBinding(state, manual, definition_revision, before_send=before_send)
        token = self._binding_var.set(binding)
        try:
            yield
        finally:
            self._binding_var.reset(token)

    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        progress_cb=None,
        json_mode: bool = False,
    ) -> str:
        """Send one pinned text/JSON request; retry orchestration belongs to ReAct."""

        def invoke() -> str:
            result = self._invoke_provider(
                messages, system, tools=None, json_mode=json_mode, progress_cb=progress_cb
            )
            if not isinstance(result, str):
                raise LLMError("Provider returned an unexpected response")
            return result

        return self._logged_chat_invocation("text", invoke)

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        progress_cb=None,
    ) -> ChatResponse:
        """Send one native-tool request, or report local unsupported capability."""
        if self._run_model.connection.adapter not in _NATIVE_TOOL_PROVIDERS:
            raise NotImplementedError(
                f"Native tool calling is unsupported for {self._run_model.connection.adapter}"
            )

        def invoke() -> ChatResponse:
            result = self._invoke_provider(
                messages, system, tools=tools, progress_cb=progress_cb
            )
            if not isinstance(result, ChatResponse):
                raise LLMError("Provider returned an unexpected native-tool response")
            return result

        return self._logged_chat_invocation("native tools", invoke)

    def _logged_chat_invocation(
        self,
        mode: str,
        invoke: Callable[[], _ChatResult],
    ) -> _ChatResult:
        """Emit one structured request event and one safe event per classified failure."""
        model = self._run_model.model.upstream_id
        suffix = " (tools)" if mode == "native tools" else ""
        agent_logging.log_event(
            agent_logging.LogEvent.LLM_CALL,
            f"LLM request{suffix} → {model}",
            level=logging.INFO,
            logger=slog,
            model=model,
        )
        started = time.monotonic()
        try:
            return invoke()
        except _LLM_CHAT_ERRORS as exc:
            if not isinstance(exc, LLMCancelledError) and self._cancel_requested():
                # A cancel that raced a failed reply is a cancellation, never a request failure.
                raise LLMCancelledError("LLM request cancelled") from None
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            agent_logging.log_event(
                agent_logging.LogEvent.LLM_FAILED,
                f"LLM request failed: {model}",
                level=logging.ERROR,
                logger=slog,
                model=model,
                dur_ms=duration_ms,
                err=_safe_llm_failure_category(exc),
            )
            raise

    def _invoke_provider(
        self,
        messages: list[dict],
        system: str | None,
        *,
        tools: list[dict] | None,
        json_mode: bool = False,
        progress_cb=None,
    ) -> str | ChatResponse:
        mode = "native" if tools is not None else "json"
        binding = self._binding_var.get()
        if binding is None:
            state = CallAttemptState(self._max_retries, self._retry_delay)
            with self.bind_attempt(state):
                return self._invoke_provider_bound(
                    messages, system, tools=tools, json_mode=json_mode,
                    mode=mode, progress_cb=progress_cb,
                )
        return self._invoke_provider_bound(
            messages, system, tools=tools, json_mode=json_mode,
            mode=mode, progress_cb=progress_cb,
        )

    def _invoke_provider_bound(
        self,
        messages: list[dict],
        system: str | None,
        *,
        tools: list[dict] | None,
        json_mode: bool,
        mode: str,
        progress_cb=None,
    ) -> str | ChatResponse:
        adapter = self._run_model.connection.adapter
        module = _PROVIDER_MODULES[adapter]
        context = self._provider_context(mode)
        invocation_token = None
        if adapter == "ollama":
            tools_json = None
            if tools is not None:
                try:
                    tools_json = json.dumps(
                        tools, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    )
                except (TypeError, ValueError, OverflowError):
                    raise LLMPermanentError(
                        "Cloud Ollama tool definitions must be valid JSON"
                    ) from None
            invocation_token = self._ollama_invocation_var.set(
                _OllamaInvocation(mode=mode, tools_json=tools_json)
            )
        try:
            return module.chat(
                context,
                messages,
                system,
                tools=tools,
                json_mode=json_mode,
                progress_cb=progress_cb,
            )
        finally:
            if invocation_token is not None:
                self._ollama_invocation_var.reset(invocation_token)

    def _provider_context(self, mode: str) -> ProviderContext:
        return ProviderContext(
            model=self._run_model.model,
            adapter=self._run_model.connection.adapter,
            mode=mode,
            send_json=lambda payload: self._send_json(payload, mode),
            track_usage=self._track_usage,
            caller_tag=self._caller_tag,
            cancel_event=self._cancel_event,
            ollama_client=self._ollama_client,
            graph_extraction=self._graph_policy is not None,
        )

    def _send_json(self, payload: dict[str, Any], mode: str) -> httpx.Response:
        if self._run_model.connection.adapter == "ollama":
            raise LLMPermanentError("Ollama requests must use the pinned SDK transport")
        binding = self._binding_var.get()
        if binding is None:
            raise LLMPermanentError("Provider send is missing an attempt binding")
        if binding.sent:
            raise LLMPermanentError("A public provider invocation may send only once")
        request = self._prepare_request(payload, mode)
        self._check_cancelled()
        if binding.before_send is not None:
            binding.before_send()
            self._check_cancelled()
        binding.state.begin_outbound_attempt(mode, manual=binding.manual)
        binding.sent = True
        if self._http is None:
            raise LLMPermanentError("HTTP transport is unavailable")
        try:
            response = self._http.send(
                request, auth=_NoInheritedAuth(), follow_redirects=False
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            self._raise_sanitized_status(exc.response)
        except httpx.HTTPError as exc:
            self._raise_safe_transport_error(exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _safe_request(method: str = "POST") -> httpx.Request:
        """Return a credential-free placeholder request for exposed errors."""
        return httpx.Request(method, "https://provider.invalid/")

    @classmethod
    def _raise_sanitized_status(
        cls,
        response: httpx.Response,
        *,
        error_text: str | None = None,
    ) -> None:
        """Map status categories while detaching original endpoint/body/headers."""
        status = response.status_code
        raw_text = error_text if error_text is not None else response.text
        is_context = status in {400, 413} and is_context_overflow_message(raw_text)
        if is_context:
            raise LLMContextOverflowError(
                "Provider rejected request: context_length_exceeded",
                status_code=status,
            ) from None
        if status in {408, 429, 500, 502, 503, 504}:
            safe_request = cls._safe_request(response.request.method if response.request else "POST")
            safe_response = httpx.Response(status, request=safe_request)
            raise httpx.HTTPStatusError(
                f"Provider returned HTTP {status}",
                request=safe_request,
                response=safe_response,
            ) from None

        permanent_code = None
        try:
            error_payload = json.loads(raw_text)
        except (ValueError, TypeError):
            error_payload = None
        if isinstance(error_payload, dict):
            permanent_code = provider_error_code(error_payload.get("error"))
        if status in {401, 403, 404} or permanent_code is not None:
            message = "Provider rejected the request"
            if permanent_code is not None:
                message += f" ({permanent_code})"
            raise LLMPermanentError(message) from None

        safe_request = cls._safe_request(response.request.method if response.request else "POST")
        safe_response = httpx.Response(status, request=safe_request)
        raise httpx.HTTPStatusError(
            f"Provider returned HTTP {status}",
            request=safe_request,
            response=safe_response,
        ) from None

    def _prepare_request(
        self,
        payload: dict[str, Any],
        mode: str,
    ) -> httpx.Request:
        """Validate and serialize the complete final request before send.

        Nothing here sizes, rejects or repairs a request from cached model limits.
        The only budget-era field kept is the configured mandatory output cap for
        adapters whose API requires one (currently Anthropic).
        """
        adapter = self._run_model.connection.adapter
        body = json.loads(json.dumps(payload, ensure_ascii=False))
        if not isinstance(body, dict):
            raise LLMPermanentError("Provider request payload must be an object")
        upstream = self._run_model.model.upstream_id
        if "model" in body and body["model"] != upstream:
            raise LLMPermanentError("Provider payload model does not match the pinned model")
        if self._graph_policy is not None:
            # Graph extraction: fixed wire policy, no fit/reserve/output-limit gate.
            self._apply_graph_policy(body, adapter, mode)
            return self._make_http_request(body, mode)

        if adapter == "anthropic":
            # Anthropic requires an explicit cap: send the configured one, never a derived limit.
            body["max_tokens"] = self._required_output_cap
        return self._make_http_request(body, mode)

    def _make_http_request(self, payload: dict[str, Any], mode: str) -> httpx.Request:
        adapter = self._run_model.connection.adapter
        url = self._route(mode)
        headers = {"Content-Type": "application/json"}
        if adapter in {"openai", "openrouter"}:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
        elif adapter == "anthropic":
            headers.update({"x-api-key": self._api_key, "anthropic-version": "2023-06-01"})
        elif adapter == "google":
            headers["x-goog-api-key"] = self._api_key
        request = httpx.Request("POST", url, headers=headers, json=payload)
        request.extensions["timeout"] = httpx.Timeout(
            self._timeout, connect=min(10.0, self._timeout)
        ).as_dict()
        return request

    def _route(self, mode: str) -> str:
        adapter = self._run_model.connection.adapter
        if adapter == "openai":
            path = _api_path(self._endpoint, "/v1/chat/completions", "/chat/completions", "/v1")
        elif adapter == "openrouter":
            path = _api_path(self._endpoint, "/api/v1/chat/completions", "/chat/completions", "/api/v1")
        elif adapter == "anthropic":
            path = _api_path(self._endpoint, "/v1/messages", "/messages", "/v1")
        elif adapter == "google":
            if mode == "native":
                path = _api_path(
                    self._endpoint,
                    "/v1beta/openai/chat/completions",
                    "/openai/chat/completions",
                    "/v1beta",
                )
            else:
                model = quote(self._run_model.model.upstream_id, safe="")
                path = _api_path(
                    self._endpoint,
                    f"/v1beta/models/{model}:generateContent",
                    f"/models/{model}:generateContent",
                    "/v1beta",
                )
        else:
            raise LLMPermanentError("Ollama route is provided by its SDK")
        parts = urlsplit(self._endpoint)
        return urlunsplit(parts._replace(path=f"{parts.path.rstrip('/')}{path}"))

    def _apply_graph_policy(self, payload: dict[str, Any], adapter: str, mode: str) -> None:
        policy = self._graph_policy
        if policy is None:
            return
        if adapter in {"openai", "openrouter"} or (adapter == "google" and mode == "native"):
            cap_key = "max_completion_tokens" if adapter in {"openai", "openrouter"} else "max_tokens"
            payload[cap_key] = policy.output_cap
            if not _is_reasoning_model(self._run_model.model.upstream_id):
                payload["temperature"] = policy.temperature
        elif adapter == "anthropic":
            payload["max_tokens"] = policy.output_cap
            payload["temperature"] = policy.temperature
        elif adapter == "google":
            config = payload.setdefault("generationConfig", {})
            config["maxOutputTokens"] = policy.output_cap
            config["temperature"] = policy.temperature
        elif adapter == "ollama":
            options = payload.setdefault("options", {})
            options["num_predict"] = policy.output_cap
            options["temperature"] = policy.temperature

    def _handle_ollama_transport_request(
        self,
        request: httpx.Request,
        inner_transport: httpx.BaseTransport,
    ) -> httpx.Response:
        """Validate/finalize SDK JSON, apply budget/count, and perform one send."""
        if (
            request.method != "POST"
            or request.url.scheme != "https"
            or request.url.host != "ollama.com"
            or request.url.port is not None
            or request.url.path != "/api/chat"
            or request.url.query
            or request.url.fragment
            or request.headers.get("authorization") != f"Bearer {self._api_key}"
        ):
            raise LLMPermanentError("Cloud Ollama SDK request failed its origin or auth policy")
        try:
            payload = json.loads(request.read())
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise LLMPermanentError("Cloud Ollama SDK produced an invalid request") from None
        if not isinstance(payload, dict) or payload.get("model") != self._run_model.model.upstream_id:
            raise LLMPermanentError("Cloud Ollama SDK request does not match the pinned model")
        if payload.get("stream") is not False:
            raise LLMPermanentError("Cloud Ollama requests must be non-streaming")
        binding = self._binding_var.get()
        invocation = self._ollama_invocation_var.get()
        if binding is None or invocation is None:
            raise LLMPermanentError("Cloud Ollama send is missing an invocation binding")
        if binding.sent:
            raise LLMPermanentError("A public provider invocation may send only once")

        sdk_tools = payload.get("tools")
        if invocation.mode == "native":
            if invocation.tools_json is None or (sdk_tools is not None and sdk_tools != []):
                raise LLMPermanentError("Cloud Ollama SDK native tool placeholder is invalid")
            try:
                payload["tools"] = json.loads(invocation.tools_json)
            except (json.JSONDecodeError, TypeError):
                raise LLMPermanentError("Cloud Ollama tool definitions are invalid") from None
        elif sdk_tools and sdk_tools != []:
            raise LLMPermanentError("Cloud Ollama JSON request unexpectedly contains tools")

        if self._graph_policy is not None:
            self._apply_graph_policy(payload, "ollama", invocation.mode)
        headers = [
            (name, value)
            for name, value in request.headers.multi_items()
            if name.lower() not in {"content-length", "transfer-encoding"}
        ]
        final_request = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            json=payload,
            extensions=dict(request.extensions),
        )
        self._check_cancelled()
        if binding.before_send is not None:
            binding.before_send()
            self._check_cancelled()
        binding.state.begin_outbound_attempt(invocation.mode, manual=binding.manual)
        binding.sent = True
        try:
            return inner_transport.handle_request(final_request)
        except httpx.HTTPError as exc:
            self._raise_safe_transport_error(exc)
        except TimeoutError:
            safe_request = self._safe_request()
            raise httpx.TimeoutException(
                "Provider request timed out", request=safe_request
            ) from None
        except ConnectionError:
            safe_request = self._safe_request()
            raise httpx.ConnectError(
                "Provider connection failed", request=safe_request
            ) from None
        raise AssertionError("unreachable")

    def _track_usage(self, prompt: int, completion: int) -> None:
        today = date.today()
        if today != self._usage_date:
            self._usage_date = today
            self._prompt_tokens = 0
            self._completion_tokens = 0
        self._prompt_tokens += prompt
        self._completion_tokens += completion
        if self._usage_registry is not None:
            self._usage_registry.record(self._run_model.model.id, prompt, completion)

    def get_today_usage(self) -> dict[str, Any]:
        """Return per-client token usage counters."""
        return {
            "date": self._usage_date.isoformat(),
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._prompt_tokens + self._completion_tokens,
        }

    def embed(self, text: str) -> list[float]:
        """Delegate embedding work to the independently configured client."""
        if self._embedding_client is None:
            raise EmbeddingUnavailableError("Embedding client was not configured for this run.")
        return self._embedding_client.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Delegate embedding batch work unchanged to its independent owner."""
        if self._embedding_client is None:
            raise EmbeddingUnavailableError("Embedding client was not configured for this run.")
        return self._embedding_client.embed_batch(texts)

    def close_http(self) -> None:
        """Close the owned provider transport to interrupt in-flight requests."""
        self.close()

    def close(self) -> None:
        """Close only transports owned by this run-local client; idempotently."""
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            self._http.close()
        if self._ollama_client is not None:
            self._ollama_client.close()

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise LLMCancelledError("LLM request cancelled")

    @classmethod
    def _raise_safe_transport_error(cls, exc: httpx.HTTPError) -> None:
        if isinstance(exc, httpx.HTTPStatusError):
            cls._raise_sanitized_status(exc.response)
        if isinstance(exc, httpx.TimeoutException):
            raise type(exc)(
                "Provider request timed out", request=cls._safe_request()
            ) from None
        if isinstance(exc, httpx.ConnectError):
            raise httpx.ConnectError(
                "Provider connection failed", request=cls._safe_request()
            ) from None
        if isinstance(exc, httpx.NetworkError):
            raise type(exc)(
                "Provider transport failed", request=cls._safe_request()
            ) from None
        raise LLMError("Provider transport failed") from None


def _api_path(endpoint: str, full_path: str, suffix: str, version_prefix: str) -> str:
    """Append an adapter API path once while retaining a custom gateway prefix."""
    path = urlsplit(endpoint).path.rstrip("/")
    if path.endswith(version_prefix):
        return suffix
    if path.endswith(full_path):
        return ""
    return full_path
