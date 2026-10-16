"""Strict TOML persistence for managed provider model catalog state."""

from __future__ import annotations

import json
import os
import re
import shutil
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
    MetadataField,
    MetadataSource,
    ModelMetadata,
    ModelRecord,
    ProviderState,
    SupplementRecord,
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
class SettingsLoadResult:
    """Outcome of loading managed settings without implicit model selection."""

    status: Literal["valid", "missing", "corrupt"]
    document: CatalogDocument | None = None
    preserved_path: Path | None = None


class ModelSettings:
    """Load and atomically write one validated v1 TOML catalog document."""

    def __init__(self, path: Path) -> None:
        """Bind managed settings to ``path`` without creating it."""
        self.path = Path(path)
        self._preserved_content: bytes | None = None
        self._preservation_failed = False

    def load(self) -> SettingsLoadResult:
        """Return valid, missing, or preserved-corrupt settings state."""
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return SettingsLoadResult("missing")
        except OSError:
            raise ModelSettingsError("read_failed") from None

        try:
            parsed = _tomllib.loads(content.decode("utf-8"))
            document = _document_from_mapping(parsed)
        except (UnicodeDecodeError, _tomllib.TOMLDecodeError, ModelSettingsError, TypeError, ValueError):
            preserved = self._preserve_corrupt()
            return SettingsLoadResult("corrupt", preserved_path=preserved)
        return SettingsLoadResult("valid", document)

    def write(self, document: CatalogDocument) -> None:
        """Validate and atomically replace the settings file."""
        _validate_document(document)
        payload = _document_to_toml(document).encode("utf-8")
        temporary: str | None = None
        try:
            self._preserve_existing_corrupt_content()
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _preserve_corrupt(self) -> Path:
        """Copy corrupt bytes to a unique sibling before exposing corrupt state."""
        backup = self.path.with_name(f"{self.path.name}.corrupt")
        suffix = 1
        while backup.exists():
            backup = self.path.with_name(f"{self.path.name}.corrupt.{suffix}")
            suffix += 1
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            self._preservation_failed = True
            raise ModelSettingsError("preserve_failed") from None
        try:
            self._preserved_content = self.path.read_bytes()
        except OSError:
            self._preservation_failed = True
            raise ModelSettingsError("preserve_failed") from None
        return backup

    def _preserve_existing_corrupt_content(self) -> None:
        """Refuse overwrite until the current corrupt bytes have a safe copy."""
        if self._preservation_failed:
            raise ModelSettingsError("preserve_failed")
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raise ModelSettingsError("read_failed") from None
        if self._preserved_content == content or _is_valid_content(content):
            return
        self._preserve_corrupt()


def _is_valid_content(content: bytes) -> bool:
    """Return whether existing bytes satisfy the complete managed schema."""
    try:
        _document_from_mapping(_tomllib.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, _tomllib.TOMLDecodeError, ModelSettingsError, TypeError, ValueError):
        return False
    return True


def _document_from_mapping(data: Any) -> CatalogDocument:
    """Decode a parsed TOML object using explicit versioned allowlists."""
    if not isinstance(data, dict):
        raise ModelSettingsError("invalid_schema")
    _keys(data, {"schema_version", "revision", "default_model", "providers", "models", "supplemental"})
    schema_version = _integer(data.get("schema_version"), "schema_version", minimum=1)
    if schema_version != 1:
        raise ModelSettingsError("unsupported_version")
    revision = _integer(data.get("revision"), "revision", minimum=0)
    default_model = data.get("default_model")
    if default_model is not None:
        default_model = _safe_string(default_model, "default_model")
        remembered_provider, separator, remembered_upstream = default_model.partition("/")
        if not separator:
            raise ModelSettingsError("invalid_default_model")
        _identity(default_model, _provider_id(remembered_provider), remembered_upstream)

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

    raw_supplemental = data.get("supplemental", [])
    if not isinstance(raw_supplemental, list):
        raise ModelSettingsError("invalid_schema")
    supplemental: list[SupplementRecord] = []
    seen_supplements: set[tuple[str, str]] = set()
    for raw_item in raw_supplemental:
        if not isinstance(raw_item, dict):
            raise ModelSettingsError("invalid_schema")
        _keys(raw_item, {"adapter", "upstream_id", "fetched_at", *_FIELDS, "sources"})
        adapter = _safe_string(raw_item.get("adapter"), "adapter")
        upstream_id = _safe_string(raw_item.get("upstream_id"), "upstream_id")
        fetched_at = _timestamp(raw_item.get("fetched_at"))
        if adapter not in _ADAPTERS:
            raise ModelSettingsError("invalid_adapter")
        key = (adapter, upstream_id)
        if key in seen_supplements:
            raise ModelSettingsError("duplicate_supplement")
        seen_supplements.add(key)
        supplemental.append(
            SupplementRecord(adapter, upstream_id, fetched_at, _metadata_from_mapping(raw_item, supplemental=True))
        )

    return CatalogDocument(schema_version, revision, default_model, tuple(providers), tuple(models), tuple(supplemental))


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

    supplemental = []
    for item in document.supplemental:
        if not isinstance(item, SupplementRecord):
            raise ModelSettingsError("invalid_document")
        row = {"adapter": item.adapter, "upstream_id": item.upstream_id, "fetched_at": item.fetched_at}
        row.update(_metadata_to_mapping(item.metadata))
        supplemental.append(row)

    result: dict[str, Any] = {
        "schema_version": document.schema_version,
        "revision": document.revision,
        "providers": providers,
        "models": models,
        "supplemental": supplemental,
    }
    if document.default_model is not None:
        result["default_model"] = document.default_model
    return result


def _metadata_to_mapping(metadata: ModelMetadata) -> dict[str, Any]:
    """Project the six normalized metadata fields and provenance allowlist."""
    if not isinstance(metadata, ModelMetadata):
        raise ModelSettingsError("invalid_metadata")
    result: dict[str, Any] = {}
    for name in _FIELDS:
        value = getattr(metadata, name)
        if value is not None:
            result[name] = value
    if metadata.sources:
        sources: dict[str, str] = {}
        for item in metadata.sources:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ModelSettingsError("invalid_provenance")
            name, source = item
            if name in sources:
                raise ModelSettingsError("duplicate_provenance")
            sources[name] = source
        result["sources"] = sources
    return result


def _metadata_from_mapping(data: dict[str, Any], *, supplemental: bool = False) -> ModelMetadata:
    """Validate and decode normalized metadata fields and exact provenance."""
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
    raw_sources = data.get("sources", {})
    if not isinstance(raw_sources, dict):
        raise ModelSettingsError("invalid_provenance")
    allowed_fields = set(values)
    if supplemental and not raw_sources:
        raw_sources = {name: "models.dev" for name in allowed_fields}
    if set(raw_sources) != allowed_fields:
        raise ModelSettingsError("invalid_provenance")
    sources: list[tuple[MetadataField, MetadataSource]] = []
    for name, source in raw_sources.items():
        if name not in _FIELDS or name not in allowed_fields or source not in {"provider", "models.dev"}:
            raise ModelSettingsError("invalid_provenance")
        sources.append((name, source))
    values["sources"] = tuple(sorted(sources))
    return ModelMetadata(**values)


def _document_to_toml(document: CatalogDocument) -> str:
    """Encode the fixed schema as TOML without generic object serialization."""
    data = _document_to_mapping(document)
    lines = [f"schema_version = {data['schema_version']}", f"revision = {data['revision']}"]
    if "default_model" in data:
        lines.append(f"default_model = {_quote(data['default_model'])}")
    for provider_id, provider in data["providers"].items():
        lines.extend(["", f"[providers.{_quote_key(provider_id)}]"])
        for name in ("adapter", "last_success_at", "status", "sanitized_last_error_code"):
            if name in provider:
                lines.append(f"{name} = {_toml_value(provider[name])}")
    for model in data["models"]:
        lines.extend(["", "[[models]]"])
        for name in ("id", "provider_id", "upstream_id", "display_name", *_FIELDS):
            if name in model:
                lines.append(f"{name} = {_toml_value(model[name])}")
        if "sources" in model:
            lines.extend(["", "[models.sources]"])
            for name, source in model["sources"].items():
                lines.append(f"{name} = {_quote(source)}")
    for item in data["supplemental"]:
        lines.extend(["", "[[supplemental]]"])
        for name in ("adapter", "upstream_id", "fetched_at", *_FIELDS):
            if name in item:
                lines.append(f"{name} = {_toml_value(item[name])}")
        if "sources" in item:
            lines.extend(["", "[supplemental.sources]"])
            for name, source in item["sources"].items():
                lines.append(f"{name} = {_quote(source)}")
    return "\n".join(lines) + "\n"


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


def _quote_key(value: str) -> str:
    """Quote provider table names, including otherwise unsupported punctuation."""
    return _quote(value)


def _toml_value(value: Any) -> str:
    """Encode one schema scalar with no arbitrary Python serialization."""
    if isinstance(value, str):
        return _quote(value)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    raise ModelSettingsError("invalid_scalar")
