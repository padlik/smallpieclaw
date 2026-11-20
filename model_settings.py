"""Persistence for the discovery cache and the durable model selection preference."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

try:
    import tomli as _tomllib  # type: ignore[import]
except ImportError:
    import tomllib as _tomllib  # type: ignore[no-redef]  # Python 3.11+

from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
)

_FIELDS: tuple[str, ...] = (
    "context_tokens", "input_tokens", "output_tokens", "vision", "tool_calling", "reasoning"
)
_INTEGER_FIELDS = frozenset({"context_tokens", "input_tokens", "output_tokens"})
_BOOLEAN_FIELDS = frozenset({"vision", "tool_calling", "reasoning"})
_ADAPTERS = frozenset({"openai", "openrouter", "anthropic", "google", "ollama"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRETISH = re.compile(
    r"(?i)(?:\b(?:bearer|authorization|api[_ -]?key|secret|password|token)\b\s*[:= ]|"
    r"\bsk-[A-Za-z0-9_-]{8,}|\b(?:env|sec):[A-Za-z0-9_.-]+)"
)


class ModelSettingsError(ValueError):
    """A safe, categorized managed-settings failure."""

    def __init__(self, code: str) -> None:
        """Create an error that exposes only a stable safe failure code."""
        self.code = code
        super().__init__(f"model settings {code.replace('_', ' ')}")


@dataclass(frozen=True)
class PreferenceLoadResult:
    """Outcome of reading the durable selection preference (never writes)."""

    status: Literal["valid", "missing", "corrupt"]
    model: str | None = None


class ModelPreference:
    """Durable deliberate main selection: exactly ``model = "<conn>/<upstream>"``.

    The file never holds credentials, credential references, endpoints or
    catalog data (ADR-0040). Loading never rewrites, backs up or quarantines
    corrupt bytes; only a deliberate ``write`` replaces the file, by temporary
    file, ``fsync`` and ``os.replace`` in the same directory, so a failure
    leaves the prior bytes (or absence) in place.
    """

    def __init__(self, path: Path) -> None:
        """Bind the preference to ``path`` without touching the filesystem."""
        self.path = Path(path)

    def load(self) -> PreferenceLoadResult:
        """Return the saved selector, ``missing``, or ``corrupt`` (bytes untouched)."""
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return PreferenceLoadResult("missing")
        except OSError:
            return PreferenceLoadResult("corrupt")
        try:
            data = _tomllib.loads(content.decode("utf-8"))
            _keys(data, {"model"})
            selector = _preference_selector(data.get("model"))
        except (UnicodeDecodeError, _tomllib.TOMLDecodeError, ModelSettingsError, TypeError, ValueError):
            return PreferenceLoadResult("corrupt")
        return PreferenceLoadResult("valid", selector)

    def write(self, selector: str) -> None:
        """Validate ``selector`` and atomically replace the preference file.

        Raises ``ModelSettingsError`` with a detail-free code; an invalid
        selector is rejected before any filesystem access.
        """
        payload = f"model = {_quote(_preference_selector(selector))}\n".encode("utf-8")
        temporary: str | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ModelSettingsError("write_failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


@dataclass(frozen=True)
class CacheLoadResult:
    """Outcome of reading the disposable discovery cache (never writes)."""

    status: Literal["valid", "missing", "corrupt"]
    document: CatalogDocument | None = None


class ModelCache:
    """Disposable, best-effort discovered-candidate cache (ADR-0040).

    The JSON file holds only a schema version, per-provider fresh/stale
    state and provider-reported display data for canonical candidate ids. It
    never holds the selection, credentials, credential references, endpoints,
    public-supplement data or raw errors. ``load`` treats a missing,
    unreadable or invalid file as absent without touching it (no backup, no
    quarantine); ``write`` replaces the file atomically (temporary file,
    ``fsync``, ``os.replace``) so a failure leaves the prior bytes or absence.
    """

    def __init__(self, path: Path) -> None:
        """Bind the cache to ``path`` without touching the filesystem."""
        self.path = Path(path)

    def load(self) -> CacheLoadResult:
        """Return the cached candidates, ``missing`` or ``corrupt`` (bytes untouched)."""
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return CacheLoadResult("missing")
        except OSError:
            return CacheLoadResult("corrupt")
        try:
            data = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_constant=_reject_constant,
            )
            document = _cache_document_from_mapping(data)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            return CacheLoadResult("corrupt")
        return CacheLoadResult("valid", document)

    def write(self, document: CatalogDocument) -> None:
        """Validate, project to the cache whitelist and atomically replace the file.

        Raises ``ModelSettingsError`` with a detail-free code; an invalid or
        unsafe document is rejected before any filesystem access.
        """
        _validate_document(document)
        wire = _cache_wire_from_mapping(_document_to_mapping(document))
        _cache_document_from_mapping(wire)  # the projection must satisfy the loader
        payload = (json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temporary: str | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ModelSettingsError("write_failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


_CACHE_MODEL_KEYS = {"id", "provider_id", "upstream_id", "display_name", *_FIELDS}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys instead of silently keeping the last."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelSettingsError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    """Reject ``NaN``/``Infinity``, which are not part of the cache schema."""
    raise ModelSettingsError("invalid_number")


def _cache_document_from_mapping(data: Any) -> CatalogDocument:
    """Decode a parsed cache object via the managed-schema validators, whitelist first."""
    if not isinstance(data, dict):
        raise ModelSettingsError("invalid_schema")
    _keys(data, {"schema_version", "providers", "models"})
    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        raise ModelSettingsError("invalid_schema")
    models: list[Any] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ModelSettingsError("invalid_schema")
        _keys(raw_model, _CACHE_MODEL_KEYS)
        models.append(dict(raw_model))
    return _document_from_mapping({
        "schema_version": data.get("schema_version"),
        "revision": 0,
        "providers": data.get("providers", {}),
        "models": models,
    })


def _cache_wire_from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Project a validated catalog mapping onto the cache whitelist."""
    models: list[dict[str, Any]] = []
    for model in mapping["models"]:
        models.append({
            name: value for name, value in model.items()
            if name in {"id", "provider_id", "upstream_id", "display_name", *_FIELDS}
        })
    return {"schema_version": mapping["schema_version"], "providers": mapping["providers"], "models": models}


def _preference_selector(value: Any) -> str:
    """Validate one canonical ``<connection>/<upstream>`` selector."""
    selector = _safe_string(value, "model")
    provider_id, separator, upstream_id = selector.partition("/")
    if not separator:
        raise ModelSettingsError("invalid_model")
    _identity(selector, _provider_id(provider_id), upstream_id)
    return selector


def _document_from_mapping(data: Any) -> CatalogDocument:
    """Decode a parsed mapping (from the JSON cache) using explicit versioned allowlists."""
    if not isinstance(data, dict):
        raise ModelSettingsError("invalid_schema")
    _keys(data, {"schema_version", "revision", "providers", "models"})
    schema_version = _integer(data.get("schema_version"), "schema_version", minimum=1)
    if schema_version != 1:
        raise ModelSettingsError("unsupported_version")
    revision = _integer(data.get("revision"), "revision", minimum=0)
    providers_table = data.get("providers", {})
    if not isinstance(providers_table, dict):
        raise ModelSettingsError("invalid_schema")
    providers: list[ProviderState] = []
    for provider_id, raw_provider in providers_table.items():
        _provider_id(provider_id)
        if not isinstance(raw_provider, dict):
            raise ModelSettingsError("invalid_schema")
        _keys(raw_provider, {"adapter", "last_success_at", "status", "sanitized_last_error_code"})
        adapter = _safe_string(raw_provider.get("adapter"), "adapter")
        if adapter not in _ADAPTERS:
            raise ModelSettingsError("invalid_adapter")
        timestamp = raw_provider.get("last_success_at")
        if timestamp is not None:
            timestamp = _timestamp(timestamp)
        status = _safe_string(raw_provider.get("status"), "status")
        if status not in {"fresh", "stale"}:
            raise ModelSettingsError("invalid_status")
        error_code = raw_provider.get("sanitized_last_error_code")
        if error_code is not None:
            error_code = _safe_string(error_code, "sanitized_last_error_code")
            if not _ERROR_CODE.fullmatch(error_code):
                raise ModelSettingsError("invalid_error_code")
        providers.append(ProviderState(provider_id, adapter, timestamp, status, error_code))

    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        raise ModelSettingsError("invalid_schema")
    models: list[ModelRecord] = []
    seen_ids: set[str] = set()
    provider_ids = {provider.provider_id for provider in providers}
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ModelSettingsError("invalid_schema")
        _keys(raw_model, {"id", "provider_id", "upstream_id", "display_name", *_FIELDS, "sources"})
        model_id = _safe_string(raw_model.get("id"), "id")
        provider_id = _provider_id(raw_model.get("provider_id"))
        upstream_id = _safe_string(raw_model.get("upstream_id"), "upstream_id")
        display_name = _safe_string(raw_model.get("display_name"), "display_name", presentation=True)
        _identity(model_id, provider_id, upstream_id)
        if provider_id not in provider_ids:
            raise ModelSettingsError("unknown_provider")
        if model_id in seen_ids:
            raise ModelSettingsError("duplicate_model")
        seen_ids.add(model_id)
        metadata = _metadata_from_mapping(raw_model)
        models.append(ModelRecord(model_id, provider_id, upstream_id, display_name, metadata))

    return CatalogDocument(schema_version, revision, tuple(providers), tuple(models))


def _validate_document(document: CatalogDocument) -> None:
    """Apply the same constraints to caller-created records before writing."""
    if not isinstance(document, CatalogDocument):
        raise ModelSettingsError("invalid_document")
    mapping = _document_to_mapping(document)
    _document_from_mapping(mapping)


def _document_to_mapping(document: CatalogDocument) -> dict[str, Any]:
    """Project only declared schema values into a validation mapping."""
    providers: dict[str, Any] = {}
    for provider in document.providers:
        if not isinstance(provider, ProviderState):
            raise ModelSettingsError("invalid_document")
        if provider.provider_id in providers:
            raise ModelSettingsError("duplicate_provider")
        row: dict[str, Any] = {
            "adapter": provider.adapter,
            "status": provider.status,
        }
        if provider.last_success_at is not None:
            row["last_success_at"] = provider.last_success_at
        if provider.sanitized_last_error_code is not None:
            row["sanitized_last_error_code"] = provider.sanitized_last_error_code
        providers[provider.provider_id] = row

    models = []
    for model in document.models:
        if not isinstance(model, ModelRecord):
            raise ModelSettingsError("invalid_document")
        row = {
            "id": model.id,
            "provider_id": model.provider_id,
            "upstream_id": model.upstream_id,
            "display_name": model.display_name,
        }
        row.update(_metadata_to_mapping(model.metadata))
        models.append(row)

    result: dict[str, Any] = {
        "schema_version": document.schema_version,
        "revision": document.revision,
        "providers": providers,
        "models": models,
    }
    return result


def _metadata_to_mapping(metadata: ModelMetadata) -> dict[str, Any]:
    """Project the six normalized provider-reported metadata fields."""
    if not isinstance(metadata, ModelMetadata):
        raise ModelSettingsError("invalid_metadata")
    result: dict[str, Any] = {}
    for name in _FIELDS:
        value = getattr(metadata, name)
        if value is not None:
            result[name] = value
    return result


def _metadata_from_mapping(data: dict[str, Any]) -> ModelMetadata:
    """Validate and decode normalized metadata fields (retired ``sources`` is discarded)."""
    values: dict[str, Any] = {}
    for name in _FIELDS:
        if name not in data:
            continue
        value = data[name]
        if name in _INTEGER_FIELDS:
            values[name] = _integer(value, name, minimum=1)
        elif type(value) is not bool:
            raise ModelSettingsError("invalid_metadata")
        else:
            values[name] = value
    return ModelMetadata(**values)


def _keys(mapping: dict[str, Any], allowed: set[str]) -> None:
    """Reject undeclared keys without echoing their potentially sensitive values."""
    if any(not isinstance(key, str) or key not in allowed for key in mapping):
        raise ModelSettingsError("unknown_field")


def _integer(value: Any, field_name: str, *, minimum: int) -> int:
    """Require a real integer (never bool) within the field's lower bound."""
    if type(value) is not int or value < minimum:
        raise ModelSettingsError(f"invalid_{field_name}")
    return value


def _safe_string(value: Any, field_name: str, *, presentation: bool = False) -> str:
    """Reject values that could carry references, endpoints, secrets, or controls."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelSettingsError(f"invalid_{field_name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ModelSettingsError(f"unsafe_{field_name}")
    stripped = value.lstrip()
    if (
        "://" in value
        or value.lower().startswith(("www.", "env:", "sec:"))
        or (presentation and _is_json_container(stripped))
        or _SECRETISH.search(value)
    ):
        raise ModelSettingsError(f"unsafe_{field_name}")
    return value


def _is_json_container(value: str) -> bool:
    """Recognize valid JSON object/array bodies despite legal whitespace."""
    if not value.startswith(("{", "[")):
        return False
    try:
        decoded, end = json.JSONDecoder().raw_decode(value)
    except json.JSONDecodeError:
        return False
    except (RecursionError, ValueError):
        raise ModelSettingsError("unsafe_json_value") from None
    return isinstance(decoded, (dict, list)) and not value[end:].strip()


def _provider_id(value: Any) -> str:
    """Validate a canonical provider connection identifier."""
    value = _safe_string(value, "provider_id")
    if not _IDENTIFIER.fullmatch(value):
        raise ModelSettingsError("invalid_provider_id")
    return value


def _identity(model_id: str, provider_id: str, upstream_id: str) -> None:
    """Require canonical identity with exactly the provider's first slash."""
    if not upstream_id or model_id != f"{provider_id}/{upstream_id}":
        raise ModelSettingsError("invalid_model_identity")


def _timestamp(value: Any) -> str:
    """Validate an ISO-8601 timestamp without leaking parser details."""
    value = _safe_string(value, "timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ModelSettingsError("invalid_timestamp") from None
    return value


def _quote(value: str) -> str:
    """Encode a string using TOML-compatible JSON basic-string escaping."""
    return json.dumps(value, ensure_ascii=False)
