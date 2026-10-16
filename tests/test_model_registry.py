"""Behavior tests for the provider model catalog state owner."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from dataclasses import replace as dataclass_replace

import pytest

from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
    SupplementRecord,
)
from model_settings import ModelSettings
from config_schema import ProviderConfig, ResolvedProviderConnection


@dataclass(frozen=True)
class FakeProviderConfig:
    name: str
    type: str
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    request_timeout: int = 20


@dataclass(frozen=True)
class FakeResolvedConnection:
    provider_id: str
    adapter: str
    base_url: str = field(repr=False)
    request_timeout: int = 20
    api_key: str = field(repr=False, default="fake-secret")


@dataclass(frozen=True)
class FakeInventory:
    provider_id: str
    adapter: str
    models: tuple[ModelRecord, ...]
    supplemental: tuple[SupplementRecord, ...]
    last_success_at: str


def config(provider_id: str = "p", *, url: str = "https://provider.invalid") -> FakeProviderConfig:
    return FakeProviderConfig(provider_id, "openai", "env:FAKE_KEY", url)


def model(
    upstream_id: str,
    provider_id: str = "p",
    *,
    context: int | None = 100,
    vision: bool | None = False,
    display_name: str | None = None,
) -> ModelRecord:
    fields = []
    if context is not None:
        fields.append(("context_tokens", "provider"))
    if vision is not None:
        fields.append(("vision", "provider"))
    return ModelRecord(
        f"{provider_id}/{upstream_id}",
        provider_id,
        upstream_id,
        display_name or upstream_id,
        ModelMetadata(context, None, None, vision, None, None, tuple(fields)),
    )


def inventory(
    provider_id: str = "p",
    models: tuple[ModelRecord, ...] = (),
    *,
    adapter: str = "openai",
    supplemental: tuple[SupplementRecord, ...] = (),
) -> FakeInventory:
    return FakeInventory(provider_id, adapter, models, supplemental, "2026-10-01T12:00:00Z")


class FakeDiscovery:
    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[FakeResolvedConnection] = []

    def fetch(self, connection: FakeResolvedConnection) -> FakeInventory:
        self.calls.append(connection)
        result = self.responses[connection.provider_id]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def make_registry(
    tmp_path: Path,
    *,
    connections: tuple[FakeProviderConfig, ...] = (config(),),
    models: tuple[ModelRecord, ...] = (),
    remembered: str | None = None,
    discovery: FakeDiscovery | None = None,
) -> tuple[ModelRegistry, ModelSettings, FakeDiscovery]:
    settings = ModelSettings(tmp_path / "models-config.toml")
    if models or remembered is not None:
        settings.write(
            CatalogDocument(
                1,
                2,
                remembered,
                tuple(ProviderState(item.name, item.type, "2026-09-01T00:00:00Z", "fresh") for item in connections),
                models,
                (),
            )
        )
    discovery = discovery or FakeDiscovery()
    conn_map = {item.name: item for item in connections}
    registry = ModelRegistry(conn_map, settings, discovery, resolve)
    return registry, settings, discovery


def resolve(provider: FakeProviderConfig) -> FakeResolvedConnection:
    return FakeResolvedConnection(provider.name, provider.type, provider.base_url)


def test_missing_settings_do_not_infer_selection_and_explicit_admission_is_gated(tmp_path: Path) -> None:
    registry, _, _ = make_registry(tmp_path)

    view = registry.view()
    assert view.active_model_id is None
    assert view.remembered_model_id is None
    assert view.selection_status == "no_selection"
    with pytest.raises(ModelRegistryError) as error:
        registry.admit("p/explicit")
    assert error.value.code == "no_active_selection"
    with pytest.raises(ModelRegistryError) as error:
        registry.select("p/explicit")
    assert error.value.code == "model_not_found"


def test_unavailable_saved_id_is_remembered_but_not_restored_or_admitted(tmp_path: Path) -> None:
    registry, _, discovery = make_registry(tmp_path, remembered="p/old/model")
    discovery.responses["p"] = inventory()

    registry.refresh()
    view = registry.restore_selection()

    assert view.remembered_model_id == "p/old/model"
    assert view.active_model_id is None
    assert view.selection_status == "saved_selection_unavailable"
    with pytest.raises(ModelRegistryError) as error:
        registry.admit("p/new")
    assert error.value.code == "no_active_selection"


def test_selection_persists_before_active_publication_and_write_failure_keeps_prior_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = model("first"), model("second")
    registry, settings, _ = make_registry(tmp_path, models=(first, second))
    registry.select(first.id)
    previous = settings.load().document
    assert previous is not None

    monkeypatch.setattr(settings, "write", lambda _document: (_ for _ in ()).throw(OSError("private detail")))
    with pytest.raises(ModelRegistryError) as error:
        registry.select(second.id)

    assert error.value.code == "persistence_failed"
    view = registry.view()
    assert view.active_model_id == first.id
    assert view.remembered_model_id == first.id
    assert settings.load().document == previous
    assert "private detail" not in str(error.value)


def test_corrupt_managed_state_is_preserved_and_registry_remains_inactive(tmp_path: Path) -> None:
    path = tmp_path / "models-config.toml"
    original = b"schema_version = 1\nrevision = true\n"
    path.write_bytes(original)
    settings = ModelSettings(path)
    registry = ModelRegistry({"p": config()}, settings, FakeDiscovery(), resolve)

    view = registry.restore_selection()

    assert view.selection_status == "no_selection"
    assert view.active_model_id is None
    assert path.read_bytes() == original
    assert (tmp_path / "models-config.toml.corrupt").read_bytes() == original


def test_refresh_replaces_complete_inventory_and_keeps_supplements_separate(tmp_path: Path) -> None:
    old, current = model("old"), model("vendor/model/rev")
    supplemental = SupplementRecord(
        "openai", "vendor/model/rev", "2026-10-01T00:00:00Z", ModelMetadata(250, sources=(("context_tokens", "models.dev"),))
    )
    registry, _, discovery = make_registry(tmp_path, models=(old,))
    discovery.responses["p"] = inventory(models=(current,), supplemental=(supplemental,))

    report = registry.refresh()
    view = registry.view()

    assert report.refreshed_provider_ids == ("p",)
    assert report.persisted
    assert [item.id for item in view.models] == [current.id]
    assert view.models[0].upstream_id == "vendor/model/rev"
    assert view.models[0].metadata.vision is False
    assert view.providers[0].status == "fresh"
    loaded = registry._settings.load().document
    assert loaded is not None
    assert loaded.supplemental == (supplemental,)


def test_failed_refresh_preserves_last_good_models_and_marks_provider_stale(tmp_path: Path) -> None:
    cached = model("cached")
    discovery = FakeDiscovery()
    registry, _, _ = make_registry(tmp_path, models=(cached,), discovery=discovery)
    discovery.responses["p"] = RuntimeError("secret response body")

    report = registry.refresh()
    view = registry.view()

    assert view.models == (cached,)
    assert view.providers[0].status == "stale"
    assert view.providers[0].sanitized_last_error_code == "discovery_failed"
    assert report.error_codes == (("p", "discovery_failed"),)
    assert "secret response body" not in repr(report)


def test_valid_empty_inventory_deactivates_but_remembers_selection_and_rediscovery_does_not_restore(
    tmp_path: Path,
) -> None:
    cached = model("cached")
    registry, _, discovery = make_registry(tmp_path, models=(cached,), remembered=cached.id)
    registry.restore_selection()
    discovery.responses["p"] = inventory()

    report = registry.refresh()

    assert report.refreshed_provider_ids == ("p",)
    assert registry.view().remembered_model_id == cached.id
    assert registry.view().active_model_id is None
    discovery.responses["p"] = inventory(models=(cached,))
    registry.refresh()
    assert registry.view().active_model_id is None


def test_malformed_duplicate_or_unsourced_inventory_is_rejected_without_partial_merge(tmp_path: Path) -> None:
    cached = model("cached")
    duplicate = model("new")
    invalid_metadata = ModelRecord(
        "p/bad", "p", "bad", "Bad", ModelMetadata(100)
    )
    registry, _, discovery = make_registry(tmp_path, models=(cached,))

    discovery.responses["p"] = inventory(models=(duplicate, duplicate))
    duplicate_report = registry.refresh()
    assert duplicate_report.stale_provider_ids == ("p",)
    assert {item.id for item in registry.view().models} == {cached.id}

    discovery.responses["p"] = inventory(models=(invalid_metadata,))
    metadata_report = registry.refresh()
    assert metadata_report.stale_provider_ids == ("p",)
    assert {item.id for item in registry.view().models} == {cached.id}


def test_control_character_inventory_timestamp_preserves_last_good_as_stale(tmp_path: Path) -> None:
    cached, untrusted_addition = model("cached"), model("untrusted-addition")
    registry, settings, discovery = make_registry(tmp_path, models=(cached,))
    malformed = dataclass_replace(
        inventory(models=(untrusted_addition,)), last_success_at="2026-10-01\n12:00:00"
    )
    discovery.responses["p"] = malformed

    report = registry.refresh()
    view = registry.view()

    assert report.stale_provider_ids == ("p",)
    assert report.error_codes == (("p", "invalid_inventory"),)
    assert view.models == (cached,)
    assert view.providers[0].status == "stale"
    persisted = settings.load().document
    assert persisted is not None
    assert persisted.models == (cached,)
    assert persisted.providers[0].status == "stale"


@pytest.mark.parametrize("change", ["addition", "metadata"])
def test_refresh_write_failure_does_not_publish_additions_or_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    cached = model("cached", context=100)
    registry, settings, discovery = make_registry(tmp_path, models=(cached,))
    registry.select(cached.id)
    before_view = registry.view()
    before_document = settings.load().document
    assert before_document is not None
    if change == "addition":
        returned = inventory(models=(cached, model("new")))
    else:
        returned = inventory(models=(model("cached", context=200),))
    discovery.responses["p"] = returned
    monkeypatch.setattr(
        settings, "write", lambda _document: (_ for _ in ()).throw(OSError("private write detail"))
    )

    report = registry.refresh()
    after_view = registry.view()

    assert not report.persisted
    assert after_view.models == before_view.models
    assert after_view.providers == before_view.providers
    assert after_view.revision == before_view.revision
    assert after_view.active_model_id == cached.id
    assert settings.load().document == before_document
    assert "private write detail" not in repr(report)


def test_refresh_persists_candidate_before_publishing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached, added = model("cached"), model("added")
    registry, settings, discovery = make_registry(tmp_path, models=(cached,))
    before = registry.view()
    discovery.responses["p"] = inventory(models=(cached, added))
    original_write = settings.write
    observed: list[tuple[str, ...]] = []

    def inspect_then_write(document: CatalogDocument) -> None:
        observed.append(tuple(item.id for item in registry.view().models))
        original_write(document)

    monkeypatch.setattr(settings, "write", inspect_then_write)

    report = registry.refresh()

    assert report.persisted
    assert observed == [(cached.id,)]
    assert registry.view().revision == before.revision + 1
    assert {item.id for item in registry.view().models} == {cached.id, added.id}


def test_refresh_merges_against_selection_made_while_fetch_is_blocked(tmp_path: Path) -> None:
    first, added = model("first"), model("added")
    registry, settings, discovery = make_registry(tmp_path, models=(first,))
    entered = threading.Event()
    release = threading.Event()

    class BlockingDiscovery(FakeDiscovery):
        def fetch(self, connection: FakeResolvedConnection) -> FakeInventory:
            entered.set()
            assert release.wait(2)
            return inventory(models=(first, added))

    discovery = BlockingDiscovery()
    registry = ModelRegistry({"p": config()}, settings, discovery, resolve)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(registry.refresh()))
    worker.start()
    assert entered.wait(2)
    registry.select(first.id)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result and isinstance(result[0], object)
    assert registry.view().active_model_id == first.id
    loaded = settings.load().document
    assert loaded is not None
    assert loaded.default_model == first.id
    assert {item.id for item in loaded.models} == {first.id, added.id}


def test_newer_completed_refresh_discards_older_late_result(tmp_path: Path) -> None:
    old, latest = model("old"), model("latest")
    registry, settings, _ = make_registry(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    class OrderedDiscovery:
        def fetch(self, connection: FakeResolvedConnection) -> FakeInventory:
            nonlocal calls
            with counter_lock:
                calls += 1
                number = calls
            if number == 1:
                first_entered.set()
                assert release_first.wait(2)
                return inventory(models=(old,))
            return inventory(models=(latest,))

    registry = ModelRegistry({"p": config()}, settings, OrderedDiscovery(), resolve)
    older_report: list[object] = []
    older = threading.Thread(target=lambda: older_report.append(registry.refresh()))
    older.start()
    assert first_entered.wait(2)
    newer_report = registry.refresh()
    release_first.set()
    older.join(2)

    assert not older.is_alive()
    assert newer_report.refreshed_provider_ids == ("p",)
    assert older_report and older_report[0].discarded_provider_ids == ("p",)  # type: ignore[union-attr]
    assert tuple(item.id for item in registry.view().models) == (latest.id,)


@pytest.mark.parametrize("remove", [True, False])
def test_changed_or_deleted_connection_discards_inflight_refresh_and_purges_inventory(
    tmp_path: Path, remove: bool
) -> None:
    cached, late = model("cached"), model("late")
    registry, settings, _ = make_registry(tmp_path, models=(cached,))
    entered, release = threading.Event(), threading.Event()

    class BlockingDiscovery:
        def fetch(self, connection: FakeResolvedConnection) -> FakeInventory:
            entered.set()
            assert release.wait(2)
            return inventory(models=(late,))

    registry = ModelRegistry({"p": config()}, settings, BlockingDiscovery(), resolve)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(registry.refresh()))
    worker.start()
    assert entered.wait(2)
    registry.replace_connections({} if remove else {"p": config(url="https://new.invalid")})
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result and result[0].discarded_provider_ids == ("p",)  # type: ignore[union-attr]
    assert registry.view().models == ()
    if remove:
        assert "p" not in {state.provider_id for state in registry.view().providers}
    else:
        assert registry.view().providers[0].provider_id == "p"
        assert registry.view().providers[0].status == "stale"


def test_pinned_run_survives_selection_and_inventory_removal_and_view_hides_connection_secrets(
    tmp_path: Path,
) -> None:
    first, second = model("first"), model("second")
    other_config = FakeProviderConfig("q", "openai", "env:OTHER_SECRET", "https://other.invalid")
    second = model("second", "q")
    registry, _, _ = make_registry(tmp_path, connections=(config(), other_config), models=(first, second))
    registry.select(first.id)
    pinned = registry.admit()
    registry.select(second.id)
    registry.replace_connections({"q": other_config})

    view = registry.view(pinned)
    assert pinned.model == first
    assert view.pinned_model == first
    assert view.active_model_id == second.id
    assert "fake-secret" not in repr(view)
    assert "env:OTHER_SECRET" not in repr(view)
    assert "other.invalid" not in repr(view)


def test_connection_change_while_resolving_prevents_retargeted_admission(tmp_path: Path) -> None:
    cached = model("cached")
    registry, _, _ = make_registry(tmp_path, models=(cached,))
    registry.select(cached.id)
    entered, release = threading.Event(), threading.Event()
    result: list[object] = []

    def blocking_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        entered.set()
        assert release.wait(2)
        return resolve(provider)

    registry = ModelRegistry({"p": config()}, ModelSettings(tmp_path / "models-config.toml"), FakeDiscovery(), blocking_resolver)
    registry.select(cached.id)
    worker = threading.Thread(target=lambda: _capture_admission(registry, result))
    worker.start()
    assert entered.wait(2)
    registry.replace_connections({"p": config(url="https://changed.invalid")})
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result and isinstance(result[0], ModelRegistryError)
    assert result[0].code == "connection_changed"  # type: ignore[union-attr]


def test_explicit_admission_rechecks_active_main_after_other_model_credential_resolution(
    tmp_path: Path,
) -> None:
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    other_config = config("q")
    registry, settings, _ = make_registry(
        tmp_path,
        connections=(config("p"), other_config),
        models=(main_model, explicit_model),
    )
    registry.select(main_model.id)
    entered, release = threading.Event(), threading.Event()
    resolved: list[str] = []
    result: list[object] = []
    discovery = FakeDiscovery()

    def blocking_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        resolved.append(provider.name)
        if provider.name == "q":
            entered.set()
            assert release.wait(2)
        return resolve(provider)

    registry = ModelRegistry(
        {"p": config("p"), "q": other_config},
        settings,
        discovery,
        blocking_resolver,
    )
    registry.select(main_model.id)
    worker = threading.Thread(
        target=lambda: _capture_admission(registry, result, explicit_model.id)
    )
    worker.start()
    assert entered.wait(2)
    registry.replace_connections({"q": other_config})
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert resolved == ["q"]
    assert discovery.calls == []
    assert len(result) == 1 and isinstance(result[0], ModelRegistryError)
    assert result[0].code == "no_active_selection"  # type: ignore[union-attr]
    assert registry.view().active_model_id is None


@pytest.mark.parametrize(
    "mismatch",
    [
        {"provider_id": "wrong-id"},
        {"adapter": "anthropic"},
        {"base_url": "https://wrong.invalid"},
        {"request_timeout": 99},
    ],
)
def test_refresh_rejects_mismatched_resolved_connection_before_discovery(
    tmp_path: Path, mismatch: dict[str, object]
) -> None:
    provider = ProviderConfig(
        "typed-account",
        "openai",
        api_key="fake-credential-reference",
        base_url="https://api.example.invalid/v1",
        request_timeout=20,
    )
    cached = ModelRecord("typed-account/cached", "typed-account", "cached", "Cached")
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(
        CatalogDocument(
            1,
            4,
            cached.id,
            (ProviderState("typed-account", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (cached,),
            (),
        )
    )
    expected = ResolvedProviderConnection(
        "typed-account", "openai", provider.base_url, provider.request_timeout, "fake-resolved-secret"
    )
    resolved = dataclass_replace(expected, **mismatch)

    class NoNetworkDiscovery:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def fetch(self, connection: object) -> FakeInventory:
            self.calls.append(connection)
            return inventory("typed-account", (model("new", "typed-account"),))

    discovery = NoNetworkDiscovery()
    registry = ModelRegistry(
        {provider.name: provider}, settings, discovery, lambda _config: resolved
    )

    report = registry.refresh()

    assert discovery.calls == []
    assert report.stale_provider_ids == ("typed-account",)
    assert report.error_codes == (("typed-account", "credential_resolution_failed"),)
    assert registry.view().models == (cached,)
    assert registry.view().providers[0].status == "stale"
    assert "fake-resolved-secret" not in repr(report)


def test_refresh_sanitizes_credential_resolver_failure_and_preserves_stale_cache(
    tmp_path: Path,
) -> None:
    cached = model("cached")
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(
        CatalogDocument(
            1,
            2,
            cached.id,
            (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (cached,),
            (),
        )
    )

    class NoNetworkDiscovery:
        def fetch(self, _connection: object) -> FakeInventory:
            raise AssertionError("discovery must not run after credential resolution fails")

    def failed_resolver(_config: FakeProviderConfig) -> FakeResolvedConnection:
        raise RuntimeError("PRIVATE_CREDENTIAL_SENTINEL")

    registry = ModelRegistry({"p": config()}, settings, NoNetworkDiscovery(), failed_resolver)

    report = registry.refresh()

    assert report.stale_provider_ids == ("p",)
    assert report.error_codes == (("p", "credential_resolution_failed"),)
    assert registry.view().models == (cached,)
    assert registry.view().providers[0].status == "stale"
    assert "PRIVATE_CREDENTIAL_SENTINEL" not in repr(report)


def _capture_admission(
    registry: ModelRegistry, result: list[object], selector: str | None = None
) -> None:
    try:
        result.append(registry.admit(selector))
    except ModelRegistryError as error:
        result.append(error)


def test_refresh_write_failure_removal_still_blocks_future_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = model("cached")
    registry, settings, discovery = make_registry(tmp_path, models=(cached,))
    registry.select(cached.id)
    before_document = settings.load().document
    assert before_document is not None
    discovery.responses["p"] = inventory()
    monkeypatch.setattr(settings, "write", lambda _document: (_ for _ in ()).throw(OSError("private")))

    report = registry.refresh()

    assert not report.persisted
    assert report.error_codes[-1] == ("catalog", "persistence_failed")
    assert registry.view().remembered_model_id == cached.id
    assert registry.view().active_model_id is None
    assert registry.view().models == ()
    assert registry.view().revision == before_document.revision
    assert settings.load().document == before_document
    with pytest.raises(ModelRegistryError) as error:
        registry.admit(cached.id)
    assert error.value.code == "no_active_selection"


def test_discovery_and_resolved_credentials_never_appear_in_public_views_or_errors(tmp_path: Path) -> None:
    current = model("safe")
    discovery = FakeDiscovery()
    registry, _, _ = make_registry(tmp_path, discovery=discovery)
    discovery.responses["p"] = inventory(models=(current,))
    registry.refresh()
    registry.select(current.id)

    snapshot = registry.admit()
    view = registry.view(snapshot)

    assert isinstance(snapshot, RunModel)
    assert "env:FAKE_KEY" not in repr(view)
    assert "provider.invalid" not in repr(view)
    assert "fake-secret" not in repr(view)
    assert "p/safe" in repr(view)
