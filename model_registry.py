"""Immutable value records shared by the provider model catalog."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Literal, Mapping

if TYPE_CHECKING:
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_discovery import ModelDiscovery, ProviderInventory
    from model_settings import ModelSettings

MetadataField = Literal[
    "context_tokens",
    "input_tokens",
    "output_tokens",
    "vision",
    "tool_calling",
    "reasoning",
]
MetadataSource = Literal["provider", "models.dev"]
ProviderStatus = Literal["fresh", "stale"]
SelectionStatus = Literal["active", "no_selection", "saved_selection_unavailable"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRETISH = re.compile(
    r"(?i)(?:\b(?:bearer|authorization|api[_ -]?key|secret|password|token)\b\s*[:= ]|"
    r"\bsk-[A-Za-z0-9_-]{8,}|\b(?:env|sec):[A-Za-z0-9_.-]+)"
)
_SAFE_ERROR_CODES = frozenset(
    {
        "connection_changed",
        "credential_resolution_failed",
        "discovery_failed",
        "incomplete_inventory",
        "invalid_connection",
        "invalid_inventory",
        "invalid_request",
        "model_not_found",
        "no_active_selection",
        "persistence_failed",
        "settings_load_failed",
    }
)


@dataclass(frozen=True)
class ModelMetadata:
    """Known model limits/capabilities and the source for each known field."""

    context_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    vision: bool | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None
    sources: tuple[tuple[MetadataField, MetadataSource], ...] = ()

    def __post_init__(self) -> None:
        """Copy nested source collections into immutable tuple values."""
        object.__setattr__(self, "sources", tuple(tuple(source) for source in self.sources))


@dataclass(frozen=True)
class ModelRecord:
    """One discovered model with canonical and exact upstream identity."""

    id: str
    provider_id: str
    upstream_id: str
    display_name: str
    metadata: ModelMetadata = field(default_factory=ModelMetadata)


@dataclass(frozen=True)
class ProviderState:
    """Persisted freshness and sanitized status for one provider connection."""

    provider_id: str
    adapter: str
    last_success_at: str | None
    status: ProviderStatus
    sanitized_last_error_code: str | None = None


@dataclass(frozen=True)
class SupplementRecord:
    """Normalized supplemental metadata, kept separate from inventory."""

    adapter: str
    upstream_id: str
    fetched_at: str
    metadata: ModelMetadata = field(default_factory=ModelMetadata)


@dataclass(frozen=True)
class CatalogDocument:
    """Versioned persisted catalog and remembered model selection."""

    schema_version: int = 1
    revision: int = 0
    default_model: str | None = None
    providers: tuple[ProviderState, ...] = ()
    models: tuple[ModelRecord, ...] = ()
    supplemental: tuple[SupplementRecord, ...] = ()

    def __post_init__(self) -> None:
        """Copy nested record collections into immutable tuples."""
        object.__setattr__(self, "providers", tuple(self.providers))
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "supplemental", tuple(self.supplemental))


@dataclass(frozen=True)
class RunModel:
    """Admission-time model snapshot with a repr-hidden resolved connection."""

    model: ModelRecord
    connection: ResolvedProviderConnection = field(repr=False)
    catalog_revision: int
    selection_revision: int


@dataclass(frozen=True)
class CatalogView:
    """Sanitized immutable catalog state for authorized presentation surfaces."""

    revision: int
    remembered_model_id: str | None
    active_model_id: str | None
    selection_status: SelectionStatus
    providers: tuple[ProviderState, ...]
    models: tuple[ModelRecord, ...]
    pinned_model: ModelRecord | None = None
    selection_revision: int = 0


@dataclass(frozen=True)
class RefreshReport:
    """Safe summary of provider refresh outcomes and persistence durability."""

    refreshed_provider_ids: tuple[str, ...] = ()
    stale_provider_ids: tuple[str, ...] = ()
    discarded_provider_ids: tuple[str, ...] = ()
    error_codes: tuple[tuple[str, str], ...] = ()
    persisted: bool = True


class ModelRegistryError(ValueError):
    """A safe, categorized model selection or admission failure."""

    def __init__(self, code: str) -> None:
        """Expose a stable error code without selectors or secret details."""
        self.code = code if code in _SAFE_ERROR_CODES else "invalid_request"
        super().__init__(f"model registry {self.code.replace('_', ' ')}")


class ModelRegistry:
    """Own catalog inventory, persisted selection, refresh, and run admission."""

    def __init__(
        self,
        connections: Mapping[str, ProviderConfig],
        settings: ModelSettings,
        discovery: ModelDiscovery,
        credential_resolver: Callable[[ProviderConfig], ResolvedProviderConnection],
    ) -> None:
        """Load cached settings and bind injected connection/discovery services."""
        self._lock = threading.RLock()
        self._settings = settings
        self._discovery = discovery
        self._credential_resolver = credential_resolver
        self._connections: dict[str, ProviderConfig] = dict(connections)
        self._fingerprints = {key: _connection_fingerprint(key, value) for key, value in self._connections.items()}
        self._configuration_generation = {key: 0 for key in self._connections}
        self._refresh_generation = {key: 0 for key in self._connections}
        self._completed_generation: dict[str, int] = {}
        self._restore_called = False
        self._settings_error_code: str | None = None
        self._active_model_id: str | None = None
        self._selection_revision = 0
        document = CatalogDocument()
        try:
            loaded = settings.load()
        except (OSError, ValueError) as exc:
            self._settings_error_code = _safe_code(getattr(exc, "code", None), "settings_load_failed")
        else:
            if getattr(loaded, "status", None) == "valid" and isinstance(getattr(loaded, "document", None), CatalogDocument):
                document = loaded.document
            elif getattr(loaded, "status", None) not in {"missing", "corrupt"}:
                self._settings_error_code = "settings_load_failed"
        self._document = _filter_document_to_connections(document, self._connections)
        self._remembered_model_id = document.default_model
        if self._remembered_model_id is not None:
            self._selection_revision = document.revision
        self._models = {record.id: record for record in self._document.models}
        self._provider_states = {item.provider_id: item for item in self._document.providers}

    def view(self, pinned: RunModel | None = None) -> CatalogView:
        """Return immutable catalog data without connections or credentials."""
        with self._lock:
            active_id = self._active_model_id if self._active_model_id in self._models else None
            remembered = self._remembered_model_id
            selection_status: SelectionStatus
            if active_id is not None:
                selection_status = "active"
            elif remembered is not None:
                selection_status = "saved_selection_unavailable"
            else:
                selection_status = "no_selection"
            providers = tuple(
                self._provider_states.get(
                    provider_id,
                    ProviderState(provider_id, _adapter(config), None, "stale"),
                )
                for provider_id, config in sorted(self._connections.items())
            )
            pinned_model = pinned.model if pinned is not None else None
            return CatalogView(
                self._document.revision,
                remembered,
                active_id,
                selection_status,
                providers,
                tuple(self._models[key] for key in sorted(self._models)),
                pinned_model,
                self._selection_revision,
            )

    def _publish_document(self, document: CatalogDocument) -> None:
        """Publish a candidate and synchronize its indexed record views."""
        self._document = document
        self._models = {record.id: record for record in document.models}
        self._provider_states = {item.provider_id: item for item in document.providers}

    def _write_candidate(self, candidate: CatalogDocument) -> None:
        """Persist one candidate or raise a detail-free registry failure."""
        try:
            self._settings.write(candidate)
        except (OSError, ValueError):
            self._settings_error_code = "persistence_failed"
            raise ModelRegistryError("persistence_failed") from None
        self._settings_error_code = None

    def select(self, canonical_id: str) -> CatalogView:
        """Persist an inventory-backed selection before making it active."""
        with self._lock:
            model = self._models.get(canonical_id)
            if model is None:
                raise ModelRegistryError("model_not_found")
            if model.provider_id not in self._connections:
                raise ModelRegistryError("connection_changed")
            candidate = replace(
                self._document,
                revision=self._document.revision + 1,
                default_model=canonical_id,
                providers=tuple(self._provider_states[key] for key in sorted(self._provider_states)),
                models=tuple(self._models[key] for key in sorted(self._models)),
            )
            self._write_candidate(candidate)
            self._publish_document(candidate)
            self._remembered_model_id = canonical_id
            self._active_model_id = canonical_id
            self._selection_revision = candidate.revision
            return self.view()

    def restore_selection(self) -> CatalogView:
        """Restore remembered selection once, only after the caller refreshes."""
        with self._lock:
            if self._restore_called:
                return self.view()
            self._restore_called = True
            remembered = self._remembered_model_id
            self._active_model_id = remembered if remembered in self._models else None
            return self.view()

    def admit(self, selector: str | None = None) -> RunModel:
        """Resolve one model/connection snapshot, requiring an active main choice."""
        with self._lock:
            active_id = self._active_model_id
            if active_id is None or active_id not in self._models:
                raise ModelRegistryError("no_active_selection")
            model_id = active_id if selector is None else selector
            model = self._models.get(model_id)
            if model is None:
                raise ModelRegistryError("model_not_found")
            provider_id = model.provider_id
            config = self._connections.get(provider_id)
            if config is None:
                raise ModelRegistryError("connection_changed")
            fingerprint = self._fingerprints[provider_id]
            configuration_generation = self._configuration_generation[provider_id]
            catalog_revision = self._document.revision
            selection_revision = self._selection_revision

        try:
            connection = self._credential_resolver(config)
        except Exception:
            raise ModelRegistryError("credential_resolution_failed") from None

        with self._lock:
            if (
                provider_id not in self._connections
                or self._fingerprints.get(provider_id) != fingerprint
                or self._configuration_generation.get(provider_id) != configuration_generation
                or self._models.get(model_id) != model
            ):
                raise ModelRegistryError("connection_changed")
            if self._active_model_id is None or self._active_model_id not in self._models:
                raise ModelRegistryError("no_active_selection")
        if not _resolved_connection_matches(provider_id, config, connection):
            raise ModelRegistryError("credential_resolution_failed")
        return RunModel(model, connection, catalog_revision, selection_revision)

    def replace_connections(self, connections: Mapping[str, ProviderConfig]) -> None:
        """Replace provider configs and immediately purge changed/deleted caches."""
        copied = dict(connections)
        new_fingerprints = {key: _connection_fingerprint(key, value) for key, value in copied.items()}
        with self._lock:
            changed = {
                key
                for key in set(self._connections) | set(copied)
                if self._fingerprints.get(key) != new_fingerprints.get(key)
            }
            if not changed:
                self._connections = copied
                return
            for provider_id in changed:
                self._configuration_generation[provider_id] = self._configuration_generation.get(provider_id, 0) + 1
                self._refresh_generation.pop(provider_id, None)
                self._completed_generation.pop(provider_id, None)
                self._provider_states.pop(provider_id, None)
            self._connections = copied
            self._fingerprints = new_fingerprints
            self._provider_states = {
                key: state for key, state in self._provider_states.items() if key in copied
            }
            self._models = {
                model_id: model for model_id, model in self._models.items() if model.provider_id not in changed
            }
            if self._active_model_id not in self._models:
                self._active_model_id = None
            candidate = replace(
                self._document,
                revision=self._document.revision + 1,
                providers=tuple(self._provider_states[key] for key in sorted(self._provider_states)),
                models=tuple(self._models[key] for key in sorted(self._models)),
            )
            self._publish_document(candidate)
            try:
                self._settings.write(candidate)
            except (OSError, ValueError):
                self._settings_error_code = "persistence_failed"
                raise ModelRegistryError("persistence_failed") from None
            self._settings_error_code = None

    def refresh(self) -> RefreshReport:
        """Fetch providers outside the lock and merge only validated current results."""
        with self._lock:
            captures: list[tuple[str, ProviderConfig, tuple[object, ...], int, int]] = []
            for provider_id, config in self._connections.items():
                generation = self._refresh_generation.get(provider_id, 0) + 1
                self._refresh_generation[provider_id] = generation
                captures.append(
                    (
                        provider_id,
                        config,
                        self._fingerprints[provider_id],
                        self._configuration_generation.get(provider_id, 0),
                        generation,
                    )
                )

        outcomes: list[tuple[str, tuple[object, ...], int, int, object | None, str | None]] = []
        for provider_id, config, fingerprint, config_generation, generation in captures:
            try:
                connection = self._credential_resolver(config)
            except Exception:
                outcomes.append(
                    (provider_id, fingerprint, config_generation, generation, None, "credential_resolution_failed")
                )
                continue
            if not _resolved_connection_matches(provider_id, config, connection):
                outcomes.append(
                    (provider_id, fingerprint, config_generation, generation, None, "credential_resolution_failed")
                )
                continue
            try:
                inventory = self._discovery.fetch(connection)
                _validate_inventory(provider_id, _adapter(config), inventory)
                outcomes.append((provider_id, fingerprint, config_generation, generation, inventory, None))
            except (OSError, ValueError, RuntimeError) as exc:
                code = _safe_code(getattr(exc, "code", None), "discovery_failed")
                if code not in {"incomplete_inventory", "invalid_inventory"}:
                    code = "discovery_failed"
                outcomes.append((provider_id, fingerprint, config_generation, generation, None, code))
            except (AttributeError, TypeError, KeyError):
                outcomes.append((provider_id, fingerprint, config_generation, generation, None, "invalid_inventory"))

        with self._lock:
            refreshed: list[str] = []
            stale: list[str] = []
            discarded: list[str] = []
            errors: list[tuple[str, str]] = []
            candidate_models = dict(self._models)
            candidate_states = dict(self._provider_states)
            candidate_supplements = {
                (item.adapter, item.upstream_id): item
                for item in self._document.supplemental
            }
            for provider_id, fingerprint, config_generation, generation, inventory, error in outcomes:
                if (
                    provider_id not in self._connections
                    or self._fingerprints.get(provider_id) != fingerprint
                    or self._configuration_generation.get(provider_id) != config_generation
                    or self._completed_generation.get(provider_id, 0) > generation
                ):
                    discarded.append(provider_id)
                    continue
                self._completed_generation[provider_id] = generation
                config = self._connections[provider_id]
                if error is not None:
                    stale.append(provider_id)
                    errors.append((provider_id, error))
                    prior = candidate_states.get(provider_id)
                    candidate_states[provider_id] = ProviderState(
                        provider_id,
                        _adapter(config),
                        prior.last_success_at if prior is not None else None,
                        "stale",
                        error,
                    )
                    continue
                if inventory is None:
                    stale.append(provider_id)
                    errors.append((provider_id, "invalid_inventory"))
                    continue
                refreshed.append(provider_id)
                prior_models = {
                    model_id: record
                    for model_id, record in candidate_models.items()
                    if record.provider_id != provider_id
                }
                for model in inventory.models:
                    prior_models[model.id] = model
                candidate_models = prior_models
                candidate_states[provider_id] = ProviderState(
                    provider_id,
                    _adapter(config),
                    inventory.last_success_at,
                    "fresh",
                    None,
                )
                candidate_supplements = {
                    key: item
                    for key, item in candidate_supplements.items()
                    if item.adapter != _adapter(config)
                }
                candidate_supplements.update(
                    {(item.adapter, item.upstream_id): item for item in inventory.supplemental}
                )

            if refreshed or stale:
                candidate = replace(
                    self._document,
                    revision=self._document.revision + 1,
                    default_model=self._remembered_model_id,
                    providers=tuple(candidate_states[key] for key in sorted(candidate_states)),
                    models=tuple(candidate_models[key] for key in sorted(candidate_models)),
                    supplemental=tuple(
                        candidate_supplements[key] for key in sorted(candidate_supplements)
                    ),
                )
                write_failed = False
                try:
                    from model_settings import _validate_document

                    _validate_document(candidate)
                except (AttributeError, TypeError, ValueError):
                    self._settings_error_code = "invalid_inventory"
                    errors.append(("catalog", "invalid_inventory"))
                    persisted = False
                else:
                    try:
                        self._settings.write(candidate)
                    except (OSError, ValueError):
                        self._settings_error_code = "persistence_failed"
                        errors.append(("catalog", "persistence_failed"))
                        persisted = False
                        write_failed = True
                    else:
                        self._settings_error_code = None
                        self._publish_document(candidate)
                        if self._active_model_id not in self._models:
                            self._active_model_id = None
                        persisted = True

                if write_failed:
                    # A successful complete inventory is authoritative about
                    # removals even when persistence fails. Apply only this
                    # fail-closed invalidation; do not publish additions,
                    # metadata, freshness, supplements, or a new revision.
                    removed_ids = set(self._models) - set(candidate_models)
                    if removed_ids:
                        safe_models = {
                            model_id: record
                            for model_id, record in self._models.items()
                            if model_id not in removed_ids
                        }
                        safe_document = replace(
                            self._document,
                            models=tuple(safe_models[key] for key in sorted(safe_models)),
                        )
                        self._publish_document(safe_document)
                        if self._active_model_id not in self._models:
                            self._active_model_id = None
            else:
                persisted = self._settings_error_code is None
            return RefreshReport(tuple(refreshed), tuple(stale), tuple(discarded), tuple(errors), persisted)


def _connection_fingerprint(provider_id: str, config: ProviderConfig) -> tuple[object, ...]:
    """Return a non-presentational identity for connection-change detection."""
    if not isinstance(provider_id, str) or not _SAFE_ID.fullmatch(provider_id):
        raise ModelRegistryError("invalid_connection")
    if getattr(config, "name", provider_id) != provider_id:
        raise ModelRegistryError("invalid_connection")
    adapter = _adapter(config)
    try:
        return (
            provider_id,
            adapter,
            getattr(config, "api_key"),
            getattr(config, "base_url"),
            getattr(config, "request_timeout"),
        )
    except AttributeError:
        raise ModelRegistryError("invalid_connection") from None


def _adapter(config: ProviderConfig) -> str:
    """Return a supported adapter name without exposing config values."""
    value = getattr(config, "type", None)
    if value not in {"openai", "openrouter", "anthropic", "google", "ollama"}:
        raise ModelRegistryError("invalid_connection")
    return value


def _resolved_connection_matches(
    provider_id: str, config: ProviderConfig, connection: object
) -> bool:
    """Ensure a resolved credential snapshot belongs to its configured route."""
    try:
        return (
            getattr(connection, "provider_id", None) == provider_id
            and getattr(connection, "adapter", None) == _adapter(config)
            and getattr(connection, "base_url", None) == getattr(config, "base_url", None)
            and getattr(connection, "request_timeout", None)
            == getattr(config, "request_timeout", None)
        )
    except Exception:
        return False


def _safe_code(value: object, fallback: str) -> str:
    """Use only allowlisted error codes in registry state and reports."""
    if isinstance(value, str) and value in _SAFE_ERROR_CODES:
        return value
    return fallback


def _filter_document_to_connections(
    document: CatalogDocument, connections: Mapping[str, ProviderConfig]
) -> CatalogDocument:
    """Keep persisted inventory only for currently configured adapter identities."""
    valid_providers = {
        provider_id
        for provider_id, state in ((item.provider_id, item) for item in document.providers)
        if provider_id in connections and state.adapter == _adapter(connections[provider_id])
    }
    providers = tuple(item for item in document.providers if item.provider_id in valid_providers)
    models = tuple(item for item in document.models if item.provider_id in valid_providers)
    return replace(document, providers=providers, models=models)


def _validate_inventory(provider_id: str, adapter: str, inventory: ProviderInventory) -> None:
    """Validate complete provider inventory records before any merge occurs."""
    if (
        getattr(inventory, "provider_id", None) != provider_id
        or getattr(inventory, "adapter", None) != adapter
        or not isinstance(getattr(inventory, "models", None), tuple)
        or not isinstance(getattr(inventory, "supplemental", None), tuple)
    ):
        raise ModelRegistryError("invalid_inventory")
    timestamp = getattr(inventory, "last_success_at", None)
    if not _valid_timestamp(timestamp):
        raise ModelRegistryError("invalid_inventory") from None
    model_ids: set[str] = set()
    for model in inventory.models:
        if not isinstance(model, ModelRecord) or model.provider_id != provider_id:
            raise ModelRegistryError("invalid_inventory")
        if (
            not _SAFE_ID.fullmatch(model.provider_id)
            or not _safe_catalog_text(model.upstream_id)
            or model.id != f"{provider_id}/{model.upstream_id}"
            or not _safe_catalog_text(model.id)
            or not _safe_catalog_text(model.display_name, presentation=True)
        ):
            raise ModelRegistryError("invalid_inventory")
        if model.id in model_ids:
            raise ModelRegistryError("invalid_inventory")
        model_ids.add(model.id)
        _validate_metadata(model.metadata)
    supplement_keys: set[tuple[str, str]] = set()
    for item in inventory.supplemental:
        if (
            not isinstance(item, SupplementRecord)
            or item.adapter != adapter
            or not _safe_catalog_text(item.upstream_id)
            or not _valid_timestamp(item.fetched_at)
        ):
            raise ModelRegistryError("invalid_inventory")
        key = (item.adapter, item.upstream_id)
        if key in supplement_keys:
            raise ModelRegistryError("invalid_inventory")
        supplement_keys.add(key)
        _validate_metadata(item.metadata, supplemental=True)


def _validate_metadata(metadata: ModelMetadata, *, supplemental: bool = False) -> None:
    """Validate tri-state metadata, positive limits, and provenance completeness."""
    if not isinstance(metadata, ModelMetadata):
        raise ModelRegistryError("invalid_inventory")
    populated: set[str] = set()
    for name in ("context_tokens", "input_tokens", "output_tokens"):
        value = getattr(metadata, name)
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ModelRegistryError("invalid_inventory")
            populated.add(name)
    for name in ("vision", "tool_calling", "reasoning"):
        value = getattr(metadata, name)
        if value is not None:
            if type(value) is not bool:
                raise ModelRegistryError("invalid_inventory")
            populated.add(name)
    sources: dict[str, str] = {}
    for pair in metadata.sources:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ModelRegistryError("invalid_inventory")
        name, source = pair
        if name in sources or name not in populated or source not in {"provider", "models.dev"}:
            raise ModelRegistryError("invalid_inventory")
        sources[name] = source
    if set(sources) != populated:
        raise ModelRegistryError("invalid_inventory")
    if supplemental and any(source != "models.dev" for source in sources.values()):
        raise ModelRegistryError("invalid_inventory")


def _safe_catalog_text(value: object, *, presentation: bool = False) -> bool:
    """Reject unsafe strings before inventory can enter runtime catalog state."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if "://" in value or value.lower().startswith(("www.", "env:", "sec:")) or _SECRETISH.search(value):
        return False
    if not presentation or not value.lstrip().startswith(("{", "[")):
        return True
    try:
        decoded, end = json.JSONDecoder().raw_decode(value.lstrip())
    except json.JSONDecodeError:
        return True
    except (RecursionError, ValueError):
        return False
    return not (isinstance(decoded, (dict, list)) and not value.lstrip()[end:].strip())


def _valid_timestamp(value: object) -> bool:
    """Validate an ISO timestamp without retaining parser error text."""
    if not isinstance(value, str) or not _safe_catalog_text(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
