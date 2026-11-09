"""Fetch complete provider inventories and normalize their reliable metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from model_registry import ModelMetadata, ModelRecord
from model_settings import ModelSettingsError, _identity, _provider_id, _safe_string

if TYPE_CHECKING:
    from config_schema import ResolvedProviderConnection

_OLLAMA_ORIGIN = "https://ollama.com"


class ModelDiscoveryError(RuntimeError):
    """Safe discovery failure exposing only a stable error code."""

    def __init__(self, code: str) -> None:
        """Initialize without retaining an HTTP exception or sensitive URL."""
        self.code = code
        super().__init__(f"model discovery {code.replace('_', ' ')}")


@dataclass(frozen=True)
class GoogleModelDescriptor:
    """Google-only discovery details that are not part of the managed model schema."""

    canonical_id: str
    resource_name: str
    generation_methods: tuple[str, ...] | None

    def __post_init__(self) -> None:
        """Copy generation methods into an immutable tuple when present."""
        if self.generation_methods is not None:
            object.__setattr__(self, "generation_methods", tuple(self.generation_methods))


@dataclass(frozen=True)
class ProviderInventory:
    """A fully fetched immutable inventory and adapter descriptors."""

    provider_id: str
    adapter: str
    models: tuple[ModelRecord, ...]
    last_success_at: str
    descriptors: tuple[GoogleModelDescriptor, ...] = ()

    def __post_init__(self) -> None:
        """Copy inventory collections to prevent retaining mutable caller aliases."""
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "descriptors", tuple(self.descriptors))

    def __repr__(self) -> str:
        """Return inventory identity without any connection or credential fields."""
        return (
            f"ProviderInventory(provider_id={self.provider_id!r}, adapter={self.adapter!r}, "
            f"models={self.models!r}, "
            f"last_success_at={self.last_success_at!r}, descriptors={self.descriptors!r})"
        )


class ModelDiscovery:
    """Discover one configured provider without retaining catalog state."""

    def __init__(
        self,
        client: httpx.Client,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Inject the provider HTTP client and a clock; only provider endpoints are contacted."""
        self._client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, connection: ResolvedProviderConnection) -> ProviderInventory:
        """Return only a complete validated inventory; provider failures are atomic."""
        provider_id = getattr(connection, "provider_id")
        adapter = getattr(connection, "adapter")
        try:
            _provider_id(provider_id)
        except ModelSettingsError:
            raise ModelDiscoveryError("invalid_connection") from None
        if adapter not in {"openai", "anthropic", "google", "openrouter", "ollama"}:
            raise ModelDiscoveryError("unsupported_adapter")
        base_url = getattr(connection, "base_url", None)
        api_key = getattr(connection, "api_key", None)
        if adapter == "ollama":
            if not isinstance(api_key, str) or not api_key:
                raise ModelDiscoveryError("authentication_required")
            if base_url is not None and not _is_official_ollama_url(base_url):
                raise ModelDiscoveryError("invalid_endpoint")
            base_url = base_url or _OLLAMA_ORIGIN
        endpoint = _endpoint(adapter, base_url)
        headers = _auth_headers(adapter, api_key)
        try:
            raw_models = self._fetch_pages(adapter, endpoint, headers, getattr(connection, "request_timeout", None))
        except ModelDiscoveryError:
            raise
        except Exception:
            raise ModelDiscoveryError("provider_request_failed") from None

        descriptors = (
            tuple(_google_descriptor(provider_id, row) for row in raw_models)
            if adapter == "google"
            else ()
        )
        models = tuple(_normalize_model(provider_id, adapter, row) for row in raw_models)
        if len({model.id for model in models}) != len(models):
            raise ModelDiscoveryError("duplicate_model")
        fetched_at = _timestamp(self._clock)
        return ProviderInventory(provider_id, adapter, models, fetched_at, descriptors)

    def _fetch_pages(
        self, adapter: str, endpoint: str, headers: dict[str, str], timeout: float | None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        url = endpoint
        params: dict[str, str] | None = None
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        while True:
            _require_same_origin(endpoint, url)
            page_key = (url, tuple(sorted((params or {}).items())))
            if page_key in seen:
                raise ModelDiscoveryError("pagination_cycle")
            seen.add(page_key)
            try:
                request = httpx.Request(
                    "GET",
                    url,
                    params=params,
                    headers={**headers, "Accept": "application/json"},
                    extensions={"timeout": _timeout_extensions(timeout)},
                )
                response = self._client.send(request, auth=None, follow_redirects=False)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                raise ModelDiscoveryError("provider_request_failed") from None
            if adapter in {"openai", "anthropic"}:
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ModelDiscoveryError("invalid_response")
                page = payload["data"]
                _model_rows(page)
                rows.extend(page)
                if adapter == "openai":
                    break
                has_more = payload.get("has_more")
                if type(has_more) is not bool:
                    raise ModelDiscoveryError("invalid_pagination")
                if not has_more:
                    break
                last_id = payload.get("last_id")
                if not isinstance(last_id, str) or not last_id:
                    raise ModelDiscoveryError("invalid_pagination")
                url, params = endpoint, {"after_id": last_id}
            elif adapter == "google":
                if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                    raise ModelDiscoveryError("invalid_response")
                page = payload["models"]
                _model_rows(page, google=True)
                rows.extend(page)
                token = payload.get("nextPageToken")
                if token is None:
                    break
                if not isinstance(token, str) or not token:
                    raise ModelDiscoveryError("invalid_pagination")
                url, params = endpoint, {"pageToken": token}
            elif adapter == "openrouter":
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ModelDiscoveryError("invalid_response")
                page = payload["data"]
                _model_rows(page)
                rows.extend(page)
                links = payload.get("links", {})
                if not isinstance(links, dict):
                    raise ModelDiscoveryError("invalid_pagination")
                next_url = links.get("next")
                if next_url is None:
                    break
                if not isinstance(next_url, str):
                    raise ModelDiscoveryError("invalid_pagination")
                url = urljoin(endpoint, next_url)
                params = None
            else:  # cloud Ollama has a single /api/tags inventory response
                if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                    raise ModelDiscoveryError("invalid_response")
                page = payload["models"]
                if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in page):
                    raise ModelDiscoveryError("invalid_response")
                rows.extend(page)
                break
        return rows


def _normalize_model(provider_id: str, adapter: str, row: dict[str, Any]) -> ModelRecord:
    upstream_id = row.get("id")
    if adapter == "google":
        upstream_id = row.get("baseModelId")
        resource_name = row.get("name")
        if not isinstance(resource_name, str) or not resource_name:
            raise ModelDiscoveryError("invalid_response")
    elif adapter == "ollama":
        upstream_id = row.get("name")
    upstream_id = _safe_response_string(upstream_id, "upstream_id")
    values: dict[str, Any] = {}
    if adapter == "anthropic":
        _positive_optional(row, "max_input_tokens", values, "input_tokens")
        _positive_optional(row, "max_tokens", values, "output_tokens")
        capabilities = row.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ModelDiscoveryError("invalid_response")
        image = capabilities.get("image_input", {})
        if image is not None:
            if not isinstance(image, dict):
                raise ModelDiscoveryError("invalid_response")
            if "supported" in image:
                if image["supported"] is None:
                    pass
                elif type(image["supported"]) is not bool:
                    raise ModelDiscoveryError("invalid_response")
                else:
                    values["vision"] = image["supported"]
    elif adapter == "google":
        _positive_optional(row, "inputTokenLimit", values, "input_tokens")
        _positive_optional(row, "outputTokenLimit", values, "output_tokens")
    elif adapter == "openrouter":
        _positive_optional(row, "context_length", values, "context_tokens")
        architecture = row.get("architecture", {})
        if not isinstance(architecture, dict):
            raise ModelDiscoveryError("invalid_response")
        modalities = architecture.get("input_modalities")
        if modalities is not None:
            if not isinstance(modalities, list) or not all(isinstance(item, str) for item in modalities):
                raise ModelDiscoveryError("invalid_response")
            values["vision"] = "image" in modalities
        top_provider = row.get("top_provider", {})
        if not isinstance(top_provider, dict):
            raise ModelDiscoveryError("invalid_response")
        _positive_optional(top_provider, "max_completion_tokens", values, "output_tokens")
    metadata = ModelMetadata(**values)
    if "display_name" in row:
        raw_display = row["display_name"]
    elif "name" in row:
        raw_display = row["name"]
    else:
        raw_display = upstream_id
    display = _safe_response_string(raw_display, "display_name", presentation=True)
    try:
        _identity(f"{provider_id}/{upstream_id}", provider_id, upstream_id)
    except ModelSettingsError:
        raise ModelDiscoveryError("invalid_response") from None
    return ModelRecord(f"{provider_id}/{upstream_id}", provider_id, upstream_id, display, metadata)


def _google_descriptor(provider_id: str, row: dict[str, Any]) -> GoogleModelDescriptor:
    """Retain Google resource and generation-method values without inference semantics."""
    upstream_id = row.get("baseModelId")
    resource_name = row.get("name")
    upstream_id = _safe_response_string(upstream_id, "upstream_id")
    resource_name = _safe_response_string(resource_name, "resource_name")
    methods: tuple[str, ...] | None = None
    if "supportedGenerationMethods" in row:
        raw_methods = row["supportedGenerationMethods"]
        if not isinstance(raw_methods, list):
            raise ModelDiscoveryError("invalid_response")
        methods = tuple(_safe_response_string(method, "generation_method") for method in raw_methods)
    return GoogleModelDescriptor(f"{provider_id}/{upstream_id}", resource_name, methods)


def _positive_optional(row: dict[str, Any], key: str, values: dict[str, Any], target: str) -> None:
    if key not in row or row[key] is None:
        return
    if not _positive(row[key]):
        raise ModelDiscoveryError("invalid_response")
    values[target] = row[key]


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _model_rows(rows: Any, *, google: bool = False) -> None:
    if any(not isinstance(row, dict) for row in rows):
        raise ModelDiscoveryError("invalid_response")
    key = "baseModelId" if google else "id"
    if any(not isinstance(row.get(key), str) or not row[key] for row in rows):
        raise ModelDiscoveryError("invalid_response")


def _safe_response_string(value: Any, field: str, *, presentation: bool = False) -> str:
    """Apply managed-catalog string safety while exposing only a safe error code."""
    try:
        return _safe_string(value, field, presentation=presentation)
    except ModelSettingsError:
        raise ModelDiscoveryError("invalid_response") from None


def _timeout_extensions(timeout: float | None) -> dict[str, float | None]:
    """Return the timeout extension shape consumed by httpx transports."""
    return {name: timeout for name in ("connect", "read", "write", "pool")}


def _auth_headers(adapter: str, api_key: str | None) -> dict[str, str]:
    if adapter == "ollama" and not api_key:
        raise ModelDiscoveryError("authentication_required")
    if not api_key:
        return {}
    if adapter == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if adapter == "google":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _endpoint(adapter: str, base_url: str | None) -> str:
    default = {
        "openai": "https://api.openai.com",
        "anthropic": "https://api.anthropic.com",
        "google": "https://generativelanguage.googleapis.com",
        "openrouter": "https://openrouter.ai",
        "ollama": _OLLAMA_ORIGIN,
    }[adapter]
    base = (base_url or default).rstrip("/")
    path = {"openai": "/v1/models", "anthropic": "/v1/models", "google": "/v1beta/models",
            "openrouter": "/api/v1/models", "ollama": "/api/tags"}[adapter]
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ModelDiscoveryError("invalid_endpoint")
    prefix = parsed.path.rstrip("/")
    suffix = path
    # Configured URLs may already contain the adapter path; append only the missing suffix.
    if prefix.endswith(path):
        suffix = ""
    elif adapter in {"openai", "anthropic"} and prefix.endswith("/v1"):
        suffix = "/models"
    elif adapter == "openrouter" and prefix.endswith("/api/v1"):
        suffix = "/models"
    elif adapter == "openrouter" and prefix.endswith("/api"):
        suffix = "/v1/models"
    elif adapter == "ollama" and prefix.endswith("/api"):
        suffix = "/tags"
    elif adapter == "google" and prefix.endswith("/v1beta"):
        suffix = "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, prefix + suffix, "", ""))


def _is_official_ollama_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and parsed.netloc.lower() == "ollama.com" and parsed.path in {"", "/", "/api", "/api/tags"} and not parsed.query and not parsed.fragment


def _require_same_origin(endpoint: str, candidate: str) -> None:
    original, target = urlsplit(endpoint), urlsplit(candidate)
    if (original.scheme, original.netloc) != (target.scheme, target.netloc):
        raise ModelDiscoveryError("unsafe_pagination_url")


def _timestamp(clock: Callable[[], datetime]) -> str:
    try:
        value = clock()
        if not isinstance(value, datetime):
            raise ValueError
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    except Exception:
        raise ModelDiscoveryError("clock_failed") from None
