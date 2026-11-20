"""Immutable value records shared by the provider model catalog."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Literal, Mapping

if TYPE_CHECKING:
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_discovery import ModelDiscovery, ProviderInventory
    from model_settings import ModelCache, ModelPreference

ProviderStatus = Literal["fresh", "stale"]
SelectionStatus = Literal["active", "no_selection", "saved_selection_unavailable"]
logger = logging.getLogger(__name__)
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
        "malformed_selector",
        "model_not_found",
        "no_active_selection",
        "persistence_failed",
        "selection_changed",
        "settings_load_failed",
        "unknown_connection",
    }
)
_ERROR_MESSAGES = {
    "malformed_selector": (
        "model selector must be '<connection>/<model>' with a non-empty connection and model"
    ),
    "unknown_connection": (
        "model selector names a connection that is not configured; the connection must match "
        "a [providers.<id>] entry exactly"
    ),
}
_SELECTOR_EVIDENCE_BOUND = 256
_CACHE_UNUSABLE_NOTICE = (
    "The model cache could not be read and is treated as empty; it is not rewritten at startup. "
    "Use /models to refresh it."
)
_SELECTION_INVALID_NOTICE = (
    "The saved model selection is invalid or unreadable; no model is active and none was chosen "
    "automatically. The file is left unchanged. Fix or delete it, or choose a model with /models."
)
_SELECTION_UNCONFIGURED_NOTICE = (
    "The saved model selection names a connection that is not configured; no model is active and "
    "none was chosen automatically. The file is left unchanged. Restore the connection in the "
    "configuration, or choose a model with /models."
)


def bounded_selector_evidence(raw: object, *, redact: Callable[[str], str] | None = None) -> str:
    """One bounded, redacted representation of a saved selector (task 12.22).

    ``inherit`` for absent, a classification label (never the contents) for
    blank and non-string values, and a length-bounded secret-redacted string
    for exact strings. The authoritative raw value stays in the document.
    """
    if raw is None:
        return "inherit"
    if not isinstance(raw, str):
        return "malformed (not a string)"
    if not raw.strip():
        return "blank"
    value = redact(raw) if redact is not None else raw
    if len(value) > _SELECTOR_EVIDENCE_BOUND:
        value = value[: _SELECTOR_EVIDENCE_BOUND - 3] + "..."
    return value


@dataclass(frozen=True)
class ModelMetadata:
    """Provider-reported limits used by current budgeting, and display-only capabilities."""

    context_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    vision: bool | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None


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
class CatalogDocument:
    """Versioned cached discovery catalog (the selection lives in the preference file)."""

    schema_version: int = 1
    revision: int = 0
    providers: tuple[ProviderState, ...] = ()
    models: tuple[ModelRecord, ...] = ()

    def __post_init__(self) -> None:
        """Copy nested record collections into immutable tuples."""
        object.__setattr__(self, "providers", tuple(self.providers))
        object.__setattr__(self, "models", tuple(self.models))


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
        super().__init__(_ERROR_MESSAGES.get(self.code, f"model registry {self.code.replace('_', ' ')}"))


class ModelRegistry:
    """Own catalog inventory, persisted selection, refresh, and run admission."""

    def __init__(
        self,
        connections: Mapping[str, ProviderConfig],
        cache: ModelCache,
        discovery: ModelDiscovery,
        credential_resolver: Callable[[ProviderConfig], ResolvedProviderConnection],
        *,
        preference: ModelPreference,
        selector_redactor: Callable[[str], str] | None = None,
    ) -> None:
        """Load the cache and preference and bind injected connection/discovery services.

        ``selector_redactor`` optionally redacts saved scheduled selector strings
        before they are bounded into a scheduled-selector warning.
        ``preference`` is the durable deliberate selection (ADR-0040) and the only
        selection source and sink; the cache never holds a selection. A
        corrupt preference leaves no remembered selection and its bytes are
        never rewritten except by a deliberate ``select``.
        """
        self._lock = threading.RLock()
        self._selector_redactor = selector_redactor
        self._cache = cache
        self._discovery = discovery
        self._credential_resolver = credential_resolver
        # Fixed for the process lifetime (ADR-0040): a read-only private copy;
        # connection edits take effect only when a restarted process rebuilds it.
        fixed = dict(connections)
        for provider_id, value in fixed.items():
            _validate_connection(provider_id, value)
        self._connections: Mapping[str, ProviderConfig] = MappingProxyType(fixed)
        self._refresh_generation = {key: 0 for key in self._connections}
        self._completed_generation: dict[str, int] = {}
        self._restore_called = False
        self._settings_error_code: str | None = None
        self._cache_notice: str | None = None
        self._selection_notice: str | None = None
        self._preference_status: str | None = None
        self._active_model_id: str | None = None
        self._selection_revision = 0
        document = CatalogDocument()
        try:
            loaded = cache.load()
        except (OSError, ValueError) as exc:
            self._settings_error_code = _safe_code(getattr(exc, "code", None), "settings_load_failed")
        else:
            if getattr(loaded, "status", None) == "valid" and isinstance(getattr(loaded, "document", None), CatalogDocument):
                document = loaded.document
            elif getattr(loaded, "status", None) == "corrupt":
                self._cache_notice = _CACHE_UNUSABLE_NOTICE
            elif getattr(loaded, "status", None) != "missing":
                self._settings_error_code = "settings_load_failed"
        self._document = _filter_document_to_connections(document, self._connections)
        self._preference = preference
        saved = preference.load()
        self._preference_status = saved.status
        self._remembered_model_id = saved.model if saved.status == "valid" else None
        if saved.status == "corrupt":
            self._selection_notice = _SELECTION_INVALID_NOTICE
        elif saved.model is not None and saved.model.partition("/")[0] not in self._connections:
            self._selection_notice = _SELECTION_UNCONFIGURED_NOTICE
        self._models = {record.id: record for record in self._document.models}
        self._provider_states = {item.provider_id: item for item in self._document.providers}
        # The automatic choice is made from the cache as loaded (connection-filtered),
        # so a later refresh that replaces the catalog cannot change it.
        self._initial_automatic_choice: str | None = min(self._models) if self._models else None

    @property
    def cache_notice(self) -> str | None:
        """Bounded, content-free notice when the cache was unreadable at construction."""
        return self._cache_notice

    @property
    def selection_notice(self) -> str | None:
        """Bounded, content-free notice when the saved selection cannot be used.

        Set when the preference file is invalid/unreadable or names a connection
        that is not configured; never contains the saved text or a path.
        """
        return self._selection_notice

    @property
    def needs_first_start_discovery(self) -> bool:
        """Whether startup should run its single discovery pass (nothing is known).

        True only when no preference file exists (a corrupt or unreadable one
        counts as existing) and the connection-filtered cache loaded at
        construction had zero candidates (an unreadable cache counts as empty).
        """
        if self._preference_status != "missing":
            return False
        return self._remembered_model_id is None and self._initial_automatic_choice is None

    def view(self, pinned: RunModel | None = None) -> CatalogView:
        """Return immutable catalog data without connections or credentials."""
        with self._lock:
            # Honest selector: after a failed cache write the active choice can name
            # a candidate the (unsaved) discovery no longer lists.
            active_id = self._active_model_id
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

    def select(self, canonical_id: str) -> CatalogView:
        """Persist a deliberate selection before making it active.

        Only the exact first-slash selector shape and a configured connection
        are validated (``malformed_selector`` / ``unknown_connection``); the
        cache is not a whitelist, so a candidate a later refresh removed still
        selects and no provider is contacted.
        """
        with self._lock:
            parse_selector(canonical_id, self._connections)
            # Durable before published: a failed write leaves prior bytes
            # and the prior active choice untouched.
            try:
                self._preference.write(canonical_id)
            except (OSError, ValueError) as exc:
                # ``ModelPreference.write`` validates before any I/O (any coded
                # ModelSettingsError other than ``write_failed`` is a bad selector);
                # everything else, including a code-less ValueError, is persistence.
                code = getattr(exc, "code", None)
                if isinstance(code, str) and code != "write_failed":
                    raise ModelRegistryError("malformed_selector") from None
                raise ModelRegistryError("persistence_failed") from None
            self._remembered_model_id = canonical_id
            self._active_model_id = canonical_id
            self._selection_revision += 1
            # A durable deliberate selection repairs a corrupt/unconfigured saved one.
            self._selection_notice = None
            self._preference_status = "valid"
            return self.view()

    def restore_selection(self) -> CatalogView:
        """Restore remembered selection once, only after the caller refreshes."""
        with self._lock:
            if self._restore_called:
                return self.view()
            self._restore_called = True
            remembered = self._remembered_model_id
            if self._selection_notice is not None:
                self._active_model_id = None  # actionable unset: never an automatic replacement
            elif remembered is not None:
                # Saved and configured: active as saved, independent of cache membership.
                self._active_model_id = remembered
            elif self._preference_status == "missing" and (self._initial_automatic_choice or self._models):
                # Deterministic in-memory choice (ordinal order); never saved. Taken from the
                # initially loaded cache; only an empty initial cache uses refreshed candidates.
                self._active_model_id = self._initial_automatic_choice or min(self._models)
            else:
                self._active_model_id = None
            return self.view()

    def admit(self, selector: str | None = None) -> RunModel:
        """Resolve one exact target and its credential once, without catalog membership.

        An explicit ``selector`` is split at its first ``/`` and resolved against
        the fixed connection map; it needs neither an active Main nor a cached
        candidate. ``None`` snapshots the active Main and fails with
        ``no_active_selection`` when none exists. Selectors are never trimmed,
        case-folded or aliased: a malformed shape fails ``malformed_selector``
        and an absent or padded connection ``unknown_connection``, both before
        any credential resolution or provider I/O. Credentials are resolved once
        outside the lock; no catalog, selection or Main recheck follows, so a
        concurrent refresh or ``select`` cannot invalidate the admitted target.
        """
        with self._lock:
            if selector is None:
                selector = self._active_model_id
                if selector is None:
                    raise ModelRegistryError("no_active_selection")
            model, config = self._route(selector)
            catalog_revision = self._document.revision
            selection_revision = self._selection_revision
        return self._pin(model, config, catalog_revision, selection_revision)

    def admit_scheduled(self, raw_selector: object = None) -> RunModel:
        """Pin a scheduled run to its saved selector exactly, or inherit Main.

        The saved value is inherited or exact, never substituted. ``None``
        snapshots the active Main once and fails with ``no_active_selection``
        when none exists. Any other value is routed exactly once and needs
        neither an active Main nor a cached candidate: a blank, whitespace-only
        or non-string value or any other malformed shape fails
        ``malformed_selector``, and an absent or padded connection
        ``unknown_connection``, before any credential resolution. A well-formed
        selector on a configured connection is used verbatim (the upstream
        id is never trimmed); a provider rejecting it is an ordinary run
        failure. Credentials are resolved once outside the lock and nothing is
        rechecked afterwards.
        """
        with self._lock:
            target = raw_selector
            if target is None:
                target = self._active_model_id
                if target is None:
                    raise ModelRegistryError("no_active_selection")
            model, config = self._route(target)
            catalog_revision = self._document.revision
            selection_revision = self._selection_revision
        return self._pin(model, config, catalog_revision, selection_revision)

    def _route(self, selector: object) -> tuple[ModelRecord, ProviderConfig]:
        """Resolve ``selector`` to its record and configured connection (lock held).

        A cached candidate supplies its provider-reported record; any other
        well-formed selector gets a minimal identity with unknown metadata. The
        cache is never consulted for permission and never written.
        """
        connection_id, upstream_id = parse_selector(selector, self._connections)
        canonical_id = f"{connection_id}/{upstream_id}"
        record = self._models.get(canonical_id)
        if record is None:
            record = ModelRecord(canonical_id, connection_id, upstream_id, upstream_id)
        return record, self._connections[connection_id]

    def _pin(
        self, model: ModelRecord, config: ProviderConfig, catalog_revision: int, selection_revision: int
    ) -> RunModel:
        """Resolve the connection credential once and return the immutable pin."""
        try:
            connection = self._credential_resolver(config)
        except Exception:
            raise ModelRegistryError("credential_resolution_failed") from None
        if not _resolved_connection_matches(model.provider_id, config, connection):
            raise ModelRegistryError("credential_resolution_failed")
        return RunModel(model, connection, catalog_revision, selection_revision)

    def refresh(self) -> RefreshReport:
        """Fetch providers outside the lock and merge only validated current results."""
        with self._lock:
            captures: list[tuple[str, ProviderConfig, int]] = []
            for provider_id, config in self._connections.items():
                generation = self._refresh_generation.get(provider_id, 0) + 1
                self._refresh_generation[provider_id] = generation
                captures.append((provider_id, config, generation))

        outcomes: list[tuple[str, int, object | None, str | None]] = []
        for provider_id, config, generation in captures:
            try:
                connection = self._credential_resolver(config)
            except Exception:
                outcomes.append(
                    (provider_id, generation, None, "credential_resolution_failed")
                )
                continue
            if not _resolved_connection_matches(provider_id, config, connection):
                outcomes.append(
                    (provider_id, generation, None, "credential_resolution_failed")
                )
                continue
            try:
                inventory = self._discovery.fetch(connection)
                _validate_inventory(provider_id, _adapter(config), inventory)
                _reject_reflected_credential(connection, inventory)
                outcomes.append((provider_id, generation, inventory, None))
            except (OSError, ValueError, RuntimeError) as exc:
                code = _safe_code(getattr(exc, "code", None), "discovery_failed")
                if code not in {"incomplete_inventory", "invalid_inventory"}:
                    code = "discovery_failed"
                outcomes.append((provider_id, generation, None, code))
            except (AttributeError, TypeError, KeyError):
                outcomes.append((provider_id, generation, None, "invalid_inventory"))

        with self._lock:
            refreshed: list[str] = []
            stale: list[str] = []
            discarded: list[str] = []
            errors: list[tuple[str, str]] = []
            candidate_models = dict(self._models)
            candidate_states = dict(self._provider_states)
            for provider_id, generation, inventory, error in outcomes:
                # Concurrent refresh ordering only: a newer completed refresh
                # of the same fixed connection supersedes an older late result.
                if self._completed_generation.get(provider_id, 0) > generation:
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

            if refreshed or stale:
                candidate = replace(
                    self._document,
                    revision=self._document.revision + 1,
                    providers=tuple(candidate_states[key] for key in sorted(candidate_states)),
                    models=tuple(candidate_models[key] for key in sorted(candidate_models)),
                )
                try:
                    from model_settings import _validate_document

                    _validate_document(candidate)
                except (AttributeError, TypeError, ValueError):
                    self._settings_error_code = "invalid_inventory"
                    errors.append(("catalog", "invalid_inventory"))
                    persisted = False
                else:
                    # The cache is advisory and best-effort: a failed write is
                    # reported but never rolls back usable in-memory discovery,
                    # and never touches the preference or the active choice.
                    try:
                        self._cache.write(candidate)
                    except (OSError, ValueError):
                        self._settings_error_code = "persistence_failed"
                        errors.append(("catalog", "persistence_failed"))
                        persisted = False
                    else:
                        self._settings_error_code = None
                        persisted = True
                    # Refresh never changes the selection or issued pins: an
                    # active choice missing from the new inventory stays as is
                    # and the view reports it truthfully.
                    self._publish_document(candidate)
            else:
                persisted = self._settings_error_code is None
            return RefreshReport(tuple(refreshed), tuple(stale), tuple(discarded), tuple(errors), persisted)


def parse_selector(selector: object, connections: Mapping[str, object]) -> tuple[str, str]:
    """Split ``<connection>/<upstream>`` at the first slash against configured connections.

    The connection must equal a configured identifier exactly (no trimming or
    case folding); the upstream is every character after the first slash,
    preserved verbatim including slashes and whitespace. Raises
    ``malformed_selector`` for a non-string, a missing slash, or an empty part,
    and ``unknown_connection`` for a connection that is not configured.
    """
    if not isinstance(selector, str):
        raise ModelRegistryError("malformed_selector")
    connection_id, separator, upstream_id = selector.partition("/")
    if not separator or not connection_id or not upstream_id:
        raise ModelRegistryError("malformed_selector")
    if connection_id not in connections:
        raise ModelRegistryError("unknown_connection")
    return connection_id, upstream_id


def _reject_reflected_credential(connection: object, inventory: ProviderInventory) -> None:
    """Fail closed when provider-supplied text contains the resolved credential.

    Pattern heuristics cannot recognise an opaque credential, so the exact
    resolved value (any non-empty length) is compared with every string that
    would be published or persisted. The error is detail-free.
    """
    secret = getattr(connection, "api_key", None)
    if not isinstance(secret, str) or not secret:
        return
    texts = [inventory.provider_id, inventory.adapter, inventory.last_success_at]
    for record in inventory.models:
        texts.extend((record.id, record.provider_id, record.upstream_id, record.display_name))
    if any(secret in text for text in texts):
        raise ModelRegistryError("invalid_inventory")


def _validate_connection(provider_id: str, config: ProviderConfig) -> None:
    """Reject a structurally invalid connection entry with a detail-free error."""
    if not isinstance(provider_id, str) or not _SAFE_ID.fullmatch(provider_id):
        raise ModelRegistryError("invalid_connection")
    if getattr(config, "name", provider_id) != provider_id:
        raise ModelRegistryError("invalid_connection")
    _adapter(config)
    for attribute in ("api_key", "base_url", "request_timeout"):
        if not hasattr(config, attribute):
            raise ModelRegistryError("invalid_connection")


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


def _validate_metadata(metadata: ModelMetadata) -> None:
    """Validate tri-state metadata types and positive limits."""
    if not isinstance(metadata, ModelMetadata):
        raise ModelRegistryError("invalid_inventory")
    for name in ("context_tokens", "input_tokens", "output_tokens"):
        value = getattr(metadata, name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ModelRegistryError("invalid_inventory")
    for name in ("vision", "tool_calling", "reasoning"):
        value = getattr(metadata, name)
        if value is not None and type(value) is not bool:
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
