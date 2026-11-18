"""Behavior tests for the provider model catalog state owner."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from dataclasses import replace as dataclass_replace

import httpx
import pytest

from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
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
    return ModelRecord(
        f"{provider_id}/{upstream_id}",
        provider_id,
        upstream_id,
        display_name or upstream_id,
        ModelMetadata(context, None, None, vision, None, None),
    )


def inventory(
    provider_id: str = "p",
    models: tuple[ModelRecord, ...] = (),
    *,
    adapter: str = "openai",
) -> FakeInventory:
    return FakeInventory(provider_id, adapter, models, "2026-10-01T12:00:00Z")


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
            )
        )
    discovery = discovery or FakeDiscovery()
    conn_map = {item.name: item for item in connections}
    registry = ModelRegistry(conn_map, settings, discovery, resolve)
    return registry, settings, discovery


def resolve(provider: FakeProviderConfig) -> FakeResolvedConnection:
    return FakeResolvedConnection(provider.name, provider.type, provider.base_url)


@pytest.mark.parametrize("flag", [False, None], ids=["cached-false", "cached-unknown"])
@pytest.mark.parametrize("adapter", ["openai", "google"])
def test_false_cached_capabilities_do_not_block_image_or_native_request(
    tmp_path: Path, adapter: str, flag: bool | None
) -> None:
    """Cached false/unknown vision and tool flags are display only for a supported adapter.

    The pinned run model comes from the real registry admission; the real
    ``LLMClient`` sends over the ``httpx.MockTransport`` boundary. Exactly one
    provider call per request carries the genuine image or tool wire payload.
    """
    import base64
    from config_schema import AgentConfig
    from llm_client import LLMClient

    cached = ModelRecord(
        "p/m", "p", "m", "m",
        ModelMetadata(10_000, None, 1_000, flag, flag, None),
    )
    registry, _, _ = make_registry(
        tmp_path, connections=(FakeProviderConfig("p", adapter, "env:K", "https://provider.test"),),
        models=(cached,),
    )
    registry.select("p/m")
    pin = registry.admit("p/m")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if adapter == "google" and b'"tools"' not in request.content:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    client = LLMClient(
        pin, agent_config=AgentConfig(ctx_max_tokens=100_000, max_retries=0, retry_delay=0),
        http_transport=httpx.MockTransport(handler),
    )
    image = tmp_path / "pic.png"
    image.write_bytes(b"fake-image-bytes")
    tool = {"type": "function", "function": {"name": "probe", "description": "d",
                                              "parameters": {"type": "object", "properties": {}}}}
    try:
        assert client.chat([{"role": "user", "content": "look", "images": [str(image)]}]) == "ok"
        client.chat_with_tools([{"role": "user", "content": "use the tool"}], [tool])
    finally:
        client.close()

    assert len(seen) == 2  # one provider call per request, no model switch or retry
    encoded = base64.b64encode(b"fake-image-bytes").decode("ascii")
    assert encoded in seen[0].content.decode()
    assert b"probe" in seen[1].content
    assert {request.url.host for request in seen} == {"provider.test"}
    assert all(
        request.headers.get("authorization", "") == "Bearer fake-secret"
        if adapter == "openai" else request.headers.get("x-goog-api-key", "") == "fake-secret"
        for request in seen
    )
    assert registry.view().active_model_id == "p/m"


def test_missing_settings_do_not_infer_selection_and_explicit_admission_needs_no_main(tmp_path: Path) -> None:
    registry, _, _ = make_registry(tmp_path)

    view = registry.view()
    assert view.active_model_id is None
    assert view.remembered_model_id is None
    assert view.selection_status == "no_selection"
    assert registry.admit("p/explicit").model.upstream_id == "explicit"  # task 4.1: no Main, no cache needed
    assert registry.view().active_model_id is None and registry.view().models == ()
    with pytest.raises(ModelRegistryError) as error:
        registry.admit()
    assert error.value.code == "no_active_selection"  # only an implicit request needs Main
    assert registry.select("p/explicit").active_model_id == "p/explicit"  # the cache is not a whitelist (task 9.3)


@pytest.mark.parametrize(
    ("selector", "admit_code"),
    [
        ("Friendly Name", "malformed_selector"),
        ("P/other", "unknown_connection"),
        ("other", "malformed_selector"),
        (" p/other", "unknown_connection"),
    ],
    ids=["display-name", "case-variant", "upstream-only", "padded"],
)
def test_ordinary_selection_and_admission_use_exact_canonical_ids_only(
    tmp_path: Path, selector: str, admit_code: str,
) -> None:
    """Display names, case variants and non-canonical IDs are never selectors."""
    main, other = model("main"), model("other", display_name="Friendly Name")
    registry, _, _ = make_registry(tmp_path, models=(main, other))
    registry.select(main.id)

    with pytest.raises(ModelRegistryError) as error:
        registry.select(selector)
    assert error.value.code == admit_code  # selection and admission share the exact first-slash parse
    with pytest.raises(ModelRegistryError) as error:
        registry.admit(selector)
    assert error.value.code == admit_code
    assert registry.view().active_model_id == main.id
    assert registry.admit(other.id).model == other


def test_unavailable_saved_id_is_remembered_but_not_restored_or_admitted(tmp_path: Path) -> None:
    registry, _, discovery = make_registry(tmp_path, remembered="p/old/model")
    discovery.responses["p"] = inventory()

    registry.refresh()
    view = registry.restore_selection()

    assert view.remembered_model_id == "p/old/model"
    assert view.active_model_id is None
    assert view.selection_status == "saved_selection_unavailable"
    assert registry.admit("p/new").model.upstream_id == "new"  # explicit needs neither Main nor cache
    with pytest.raises(ModelRegistryError) as error:
        registry.admit()
    assert error.value.code == "no_active_selection"  # implicit use has no restored Main


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


def test_refresh_replaces_complete_inventory_and_persists_provider_values(tmp_path: Path) -> None:
    old, current = model("old"), model("vendor/model/rev")
    registry, _, discovery = make_registry(tmp_path, models=(old,))
    discovery.responses["p"] = inventory(models=(current,))

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
    assert loaded.models == (current,)


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


def test_valid_empty_inventory_clears_candidates_but_keeps_selection_and_rediscovery_does_not_change_it(
    tmp_path: Path,
) -> None:
    cached = model("cached")
    registry, _, discovery = make_registry(tmp_path, models=(cached,), remembered=cached.id)
    registry.restore_selection()
    discovery.responses["p"] = inventory()

    report = registry.refresh()

    assert report.refreshed_provider_ids == ("p",)
    assert registry.view().remembered_model_id == cached.id
    assert registry.view().active_model_id == cached.id and registry.view().models == ()
    discovery.responses["p"] = inventory(models=(cached,))
    registry.refresh()
    assert registry.view().active_model_id == cached.id


def test_malformed_or_duplicate_inventory_is_rejected_without_partial_merge(tmp_path: Path) -> None:
    cached = model("cached")
    duplicate = model("new")
    invalid_metadata = ModelRecord(
        "p/bad", "p", "bad", "Bad", ModelMetadata(context_tokens=-5)
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
def test_refresh_write_failure_publishes_discovery_in_memory_and_keeps_disk_and_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Task 3.4: the cache is advisory, so a failed cache write is reported but not rolled back."""
    cached = model("cached", context=100)
    registry, settings, discovery = make_registry(tmp_path, models=(cached,))
    registry.select(cached.id)
    before_view = registry.view()
    before_document = settings.load().document
    assert before_document is not None
    if change == "addition":
        returned = inventory(models=(cached, model("new")))
        expected = (cached, model("new"))
    else:
        returned = inventory(models=(model("cached", context=200),))
        expected = (model("cached", context=200),)
    discovery.responses["p"] = returned
    monkeypatch.setattr(
        settings, "write", lambda _document: (_ for _ in ()).throw(OSError("private write detail"))
    )

    report = registry.refresh()
    after_view = registry.view()

    assert not report.persisted
    assert after_view.models == expected
    assert after_view.revision == before_view.revision + 1
    assert after_view.active_model_id == cached.id
    assert after_view.remembered_model_id == before_view.remembered_model_id
    assert after_view.selection_revision == before_view.selection_revision
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


def test_pinned_run_survives_selection_and_inventory_removal_and_view_hides_connection_secrets(
    tmp_path: Path,
) -> None:
    first, second = model("first"), model("second")
    other_config = FakeProviderConfig("q", "openai", "env:OTHER_SECRET", "https://other.invalid")
    second = model("second", "q")
    registry, _, discovery = make_registry(tmp_path, connections=(config(), other_config), models=(first, second))
    registry.select(first.id)
    pinned = registry.admit()
    registry.select(second.id)
    discovery.responses["p"] = inventory("p", ())
    discovery.responses["q"] = inventory("q", (second,))
    report = registry.refresh()

    assert report.refreshed_provider_ids == ("p", "q")
    assert tuple(item.id for item in registry.view().models) == (second.id,)
    view = registry.view(pinned)
    assert pinned.model == first
    assert view.pinned_model == first
    assert view.active_model_id == second.id
    assert "fake-secret" not in repr(view)
    assert "env:OTHER_SECRET" not in repr(view)
    assert "other.invalid" not in repr(view)


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


def test_refresh_write_failure_removal_is_published_but_keeps_active_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert registry.view().active_model_id == cached.id  # a failed cache write never changes the active choice
    assert registry.view().models == ()
    assert registry.view().revision == before_document.revision + 1
    assert settings.load().document == before_document


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


@pytest.mark.parametrize(
    ("raw_selector", "expected_id", "stale_explicit_provider"),
    [
        pytest.param(None, "p/main", False, id="none-uses-main"),
        pytest.param("p/not-known", "p/not-known", False, id="uncached-exact"),
        pytest.param("p/ not-known ", "p/ not-known ", False, id="upstream-whitespace-verbatim"),
        pytest.param("p/ ", "p/ ", False, id="blank-upstream-is-routed"),
        pytest.param("q/explicit", "q/explicit", False, id="known-fresh"),
        pytest.param("q/explicit", "q/explicit", True, id="known-stale-provider"),
    ],
)
def test_scheduled_admission_pins_saved_selector_exactly_or_inherits_main_with_one_resolution(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw_selector: object,
    expected_id: str,
    stale_explicit_provider: bool,
) -> None:
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    uncached = ModelRecord(expected_id, "p", expected_id[2:], expected_id[2:])
    expected_model = {"p/main": main_model, "q/explicit": explicit_model}.get(expected_id, uncached)
    _, settings, discovery = make_registry(
        tmp_path,
        connections=(config("p"), config("q")),
        models=(main_model, explicit_model),
    )
    caplog.set_level(logging.WARNING, logger="model_registry")
    observations: list[str] = []

    def observing_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        observations.append(provider.name)
        return resolve(provider)

    registry = ModelRegistry(
        {"p": config("p"), "q": config("q")}, settings, discovery, observing_resolver
    )
    registry.select(main_model.id)
    if stale_explicit_provider:
        discovery.responses["p"] = inventory("p", (main_model,))
        discovery.responses["q"] = RuntimeError("fake discovery failure")
        registry.refresh()
        states = {state.provider_id: state.status for state in registry.view().providers}
        assert states == {"p": "fresh", "q": "stale"}
        assert explicit_model in registry.view().models
    observations.clear()
    caplog.clear()
    before_view = registry.view()
    settings_path = tmp_path / "models-config.toml"
    before_bytes = settings_path.read_bytes()

    pin = registry.admit_scheduled(raw_selector)

    assert isinstance(pin, RunModel)
    assert pin.model == expected_model
    assert pin.connection.provider_id == expected_model.provider_id
    assert pin.catalog_revision == before_view.revision
    assert pin.selection_revision == before_view.selection_revision
    assert observations == [expected_model.provider_id]
    assert _registry_warning_count(caplog) == 0
    assert registry.view() == before_view
    assert settings_path.read_bytes() == before_bytes


@pytest.mark.parametrize(
    ("raw_selector", "code"),
    [
        pytest.param("", "malformed_selector", id="blank"),
        pytest.param(" \t ", "malformed_selector", id="whitespace"),
        pytest.param(False, "malformed_selector", id="bool"),
        pytest.param(0, "malformed_selector", id="int"),
        pytest.param(1.25, "malformed_selector", id="float"),
        pytest.param(["fake-content"], "malformed_selector", id="list"),
        pytest.param({"k": "fake-content"}, "malformed_selector", id="dict"),
        pytest.param(datetime(2026, 10, 1, 12, 0), "malformed_selector", id="datetime"),
        pytest.param("no-slash", "malformed_selector", id="no-slash"),
        pytest.param("p/", "malformed_selector", id="empty-upstream"),
        pytest.param("/m", "malformed_selector", id="empty-connection"),
        pytest.param("ghost/not-known", "unknown_connection", id="unknown-connection"),
        pytest.param(" q/explicit ", "unknown_connection", id="padded-connection"),
        pytest.param("Q/explicit", "unknown_connection", id="case-variant-connection"),
    ],
)
@pytest.mark.parametrize("main_state", ["active", "unselected"])
def test_scheduled_admission_fails_invalid_selector_with_its_own_code_and_never_substitutes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, main_state: str, raw_selector: object, code: str
) -> None:
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    _, settings, discovery = make_registry(
        tmp_path, connections=(config("p"), config("q")), models=(main_model, explicit_model)
    )
    calls: list[str] = []

    def counting_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        calls.append(provider.name)
        return resolve(provider)

    registry = ModelRegistry({"p": config("p"), "q": config("q")}, settings, discovery, counting_resolver)
    if main_state == "active":
        registry.select(main_model.id)
    caplog.set_level(logging.WARNING, logger="model_registry")
    caplog.clear()
    before_view = registry.view()
    settings_path = tmp_path / "models-config.toml"
    before_bytes = settings_path.read_bytes()
    selector_copy = copy.deepcopy(raw_selector)

    with pytest.raises(ModelRegistryError) as error:
        registry.admit_scheduled(raw_selector)

    assert error.value.code == code
    assert "fake-content" not in str(error.value) and "not-known" not in str(error.value)
    assert calls == [] and discovery.calls == []
    assert _registry_warning_count(caplog) == 0
    assert registry.view() == before_view
    assert settings_path.read_bytes() == before_bytes
    assert type(raw_selector) is type(selector_copy) and raw_selector == selector_copy


def _registry_warning_count(caplog: pytest.LogCaptureFixture) -> int:
    return sum(
        1
        for record in caplog.records
        if record.name == "model_registry" and record.levelno == logging.WARNING
    )


@pytest.mark.parametrize("main_state", ["unselected", "remembered-missing"])
def test_scheduled_admission_inherits_only_an_active_main_and_explicit_needs_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, main_state: str
) -> None:
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    _, settings, discovery = make_registry(
        tmp_path,
        connections=(config("p"), config("q")),
        models=(main_model, explicit_model),
        remembered="p/gone" if main_state == "remembered-missing" else None,
    )
    calls: list[str] = []

    def counting_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        calls.append(provider.name)
        return resolve(provider)

    registry = ModelRegistry({"p": config("p"), "q": config("q")}, settings, discovery, counting_resolver)
    if main_state == "remembered-missing":
        registry.restore_selection()
        assert registry.view().selection_status == "saved_selection_unavailable"
    else:
        assert registry.view().selection_status == "no_selection"
    caplog.set_level(logging.WARNING, logger="model_registry")
    caplog.clear()
    before_view = registry.view()
    settings_path = tmp_path / "models-config.toml"
    before_bytes = settings_path.read_bytes()

    with pytest.raises(ModelRegistryError) as error:
        registry.admit_scheduled(None)

    assert error.value.code == "no_active_selection"
    assert calls == [] and discovery.calls == []
    assert _registry_warning_count(caplog) == 0

    pin = registry.admit_scheduled("q/explicit")

    assert pin.model.id == "q/explicit" and calls == ["q"]
    assert discovery.calls == []
    assert _registry_warning_count(caplog) == 0
    assert registry.view() == before_view
    assert settings_path.read_bytes() == before_bytes


def test_scheduled_admission_without_saved_catalog_runs_explicit_and_fails_closed_for_inherited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings_path = tmp_path / "models-config.toml"
    calls: list[str] = []

    def counting_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        calls.append(provider.name)
        return resolve(provider)

    discovery = FakeDiscovery()
    registry = ModelRegistry(
        {"p": config("p"), "q": config("q")}, ModelSettings(settings_path), discovery, counting_resolver
    )
    caplog.set_level(logging.WARNING, logger="model_registry")
    caplog.clear()
    before_view = registry.view()

    with pytest.raises(ModelRegistryError) as error:
        registry.admit_scheduled()

    assert error.value.code == "no_active_selection"
    assert calls == []

    pin = registry.admit_scheduled("q/explicit")

    assert pin.model.id == "q/explicit" and calls == ["q"]
    assert before_view.models == ()
    assert before_view.selection_status == "no_selection"
    assert discovery.calls == []
    assert _registry_warning_count(caplog) == 0
    assert registry.view() == before_view
    assert not settings_path.exists()


@pytest.mark.parametrize(
    ("raw_selector", "failure", "expected_provider"),
    [
        pytest.param("q/explicit", "raises", "q", id="known-explicit-resolver-raises"),
        pytest.param(None, "raises", "p", id="inherited-main-resolver-raises"),
        pytest.param("q/explicit", "wrong-endpoint", "q", id="known-explicit-wrong-endpoint"),
    ],
)
def test_scheduled_admission_rejects_unusable_credential_resolution_without_fallback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw_selector: object,
    failure: str,
    expected_provider: str,
) -> None:
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    _, settings, discovery = make_registry(
        tmp_path,
        connections=(config("p"), config("q")),
        models=(main_model, explicit_model),
    )
    caplog.set_level(logging.WARNING, logger="model_registry")
    observations: list[str] = []

    def failing_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        observations.append(provider.name)
        if failure == "raises":
            raise RuntimeError("PRIVATE_CREDENTIAL_SENTINEL fake-secret")
        return FakeResolvedConnection(provider.name, provider.type, "https://wrong.invalid")

    registry = ModelRegistry({"p": config("p"), "q": config("q")}, settings, discovery, failing_resolver)
    registry.select(main_model.id)
    caplog.clear()
    before_view = registry.view()
    settings_path = tmp_path / "models-config.toml"
    before_bytes = settings_path.read_bytes()

    with pytest.raises(ModelRegistryError) as error:
        registry.admit_scheduled(raw_selector)

    assert error.value.code == "credential_resolution_failed"
    assert observations == [expected_provider]
    assert _registry_warning_count(caplog) == 0
    assert discovery.calls == []
    assert "PRIVATE_CREDENTIAL_SENTINEL" not in str(error.value)
    assert "fake-secret" not in str(error.value)
    assert "wrong.invalid" not in str(error.value)
    assert registry.view() == before_view
    assert settings_path.read_bytes() == before_bytes


_SCHEDULED_WORKER = "scheduled-admission-test"


class _ScheduledAdmissionGate:
    """Hold only the scheduled worker's q credential resolution open."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.ready = threading.Event()
        self.release = threading.Event()
        self.worker_calls: list[str] = []
        self.parent_calls: list[str] = []
        self.release_waits: list[bool] = []

    def resolve(self, provider: FakeProviderConfig) -> FakeResolvedConnection:
        if threading.current_thread().name != _SCHEDULED_WORKER:
            self.parent_calls.append(provider.name)
            return resolve(provider)
        self.worker_calls.append(provider.name)
        if provider.name == "q":
            self.entered.set()
            self.ready.set()
            self.release_waits.append(self.release.wait(2))
        return resolve(provider)


def _race_registry(
    tmp_path: Path, gate: _ScheduledAdmissionGate
) -> tuple[ModelRegistry, ModelSettings, FakeDiscovery]:
    _, settings, discovery = make_registry(
        tmp_path,
        connections=(config("p"), config("q")),
        models=(model("main", "p"), model("sibling", "p"), model("explicit", "q")),
    )
    registry = ModelRegistry({"p": config("p"), "q": config("q")}, settings, discovery, gate.resolve)
    registry.select("p/main")
    return registry, settings, discovery


def _race_scheduled_admission(
    registry: ModelRegistry,
    gate: _ScheduledAdmissionGate,
    selector: object,
    mutate: Callable[[], object],
) -> tuple[object, object]:
    """Run ``mutate`` while the worker's q credential resolution is held open."""
    result: list[object] = []

    def work() -> None:
        try:
            result.append(registry.admit_scheduled(selector))
        except Exception as error:
            result.append(error)
        finally:
            gate.ready.set()

    worker = threading.Thread(target=work, name=_SCHEDULED_WORKER)
    worker.start()
    try:
        assert gate.ready.wait(2)
        if not gate.entered.is_set():
            worker.join(2)
            if result and isinstance(result[0], BaseException):
                raise result[0]
            raise AssertionError("worker never reached q credential resolution")
        mutation = mutate()
    finally:
        gate.release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert gate.release_waits == [True]
    assert len(result) == 1
    return result[0], mutation


@pytest.mark.parametrize(
    ("mutation", "expected_parent_calls"),
    [
        pytest.param("select-other", [], id="main-selects-other"),
        pytest.param("reselect-main", [], id="main-reselected-same-id"),
        pytest.param("identical-refresh", ["p", "q"], id="catalog-refresh-same-entries"),
        pytest.param("explicit-record-change", ["p", "q"], id="explicit-record-refreshed"),
    ],
)
def test_scheduled_admission_is_not_invalidated_by_state_change_during_credential_resolution(
    tmp_path: Path, mutation: str, expected_parent_calls: list[str]
) -> None:
    """Task 4.1: no selection, catalog-revision or record recheck; the admission-time snapshot is pinned."""
    gate = _ScheduledAdmissionGate()
    registry, settings, discovery = _race_registry(tmp_path, gate)
    settings_path = settings.path
    observed: dict[str, object] = {}

    def mutate() -> object:
        if mutation == "select-other":
            outcome: object = registry.select("p/sibling")
        elif mutation == "reselect-main":
            outcome = registry.select("p/main")
        else:
            changed = mutation == "explicit-record-change"
            discovery.responses["p"] = inventory("p", (model("main", "p"), model("sibling", "p")))
            discovery.responses["q"] = inventory(
                "q", (model("explicit", "q", context=200 if changed else 100),)
            )
            outcome = registry.refresh()
            assert outcome.refreshed_provider_ids == ("p", "q")  # type: ignore[union-attr]
        observed["view"] = registry.view()
        observed["bytes"] = settings_path.read_bytes()
        observed["discovery_calls"] = len(discovery.calls)
        return outcome

    result, _ = _race_scheduled_admission(registry, gate, "q/explicit", mutate)

    assert isinstance(result, RunModel)
    assert (result.model.id, result.connection.provider_id) == ("q/explicit", "q")
    assert result.model.metadata.context_tokens == 100  # the record seen at admission, not the refreshed one
    assert "fake-secret" not in repr(result)
    assert gate.worker_calls == ["q"]
    assert gate.parent_calls == expected_parent_calls
    assert len(discovery.calls) == observed["discovery_calls"]
    assert registry.view() == observed["view"]
    assert settings_path.read_bytes() == observed["bytes"]


def test_scheduled_admission_ignores_document_published_despite_cache_write_failure_during_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _ScheduledAdmissionGate()
    registry, settings, discovery = _race_registry(tmp_path, gate)
    settings_path = settings.path
    main_model, explicit_model = model("main", "p"), model("explicit", "q")
    before_view = registry.view()
    before_document = registry._document
    before_bytes = settings_path.read_bytes()
    original_replace = os.replace
    observed: dict[str, object] = {}

    def replace_except_settings(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if Path(dst) == settings_path:  # type: ignore[arg-type]
            raise OSError("fake settings replace failure")
        original_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    def mutate() -> object:
        discovery.responses["p"] = inventory("p", (main_model,))
        discovery.responses["q"] = inventory("q", (explicit_model,))
        monkeypatch.setattr(os, "replace", replace_except_settings)
        report = registry.refresh()
        observed["view"] = registry.view()
        observed["document"] = registry._document
        observed["discovery_calls"] = len(discovery.calls)
        return report

    result, report = _race_scheduled_admission(registry, gate, "q/explicit", mutate)

    assert report.persisted is False  # type: ignore[union-attr]
    assert report.error_codes[-1] == ("catalog", "persistence_failed")  # type: ignore[union-attr]
    mutated_view = observed["view"]
    assert mutated_view.revision == before_view.revision + 1  # type: ignore[union-attr]
    assert mutated_view.selection_revision == before_view.selection_revision  # type: ignore[union-attr]
    assert mutated_view.active_model_id == main_model.id  # type: ignore[union-attr]
    assert mutated_view.models == (main_model, explicit_model)  # type: ignore[union-attr]
    assert observed["document"] is not before_document
    assert gate.parent_calls == ["p", "q"]
    assert isinstance(result, RunModel) and result.model == explicit_model
    assert "fake-secret" not in repr(result)
    assert gate.worker_calls == ["q"]
    assert len(discovery.calls) == observed["discovery_calls"]
    assert registry.view() == mutated_view
    assert settings_path.read_bytes() == before_bytes


def test_connection_map_is_fixed_without_live_replacement(tmp_path: Path) -> None:
    """Task 3.1: the registry's chat connections are fixed for the process lifetime.

    There is no live replacement/hot-reload entry point, and mutating the
    mapping a caller passed in never retargets refresh or admission.
    """
    cached = model("cached")
    discovery = FakeDiscovery()
    discovery.responses["p"] = FakeInventory("p", "openai", (cached,), "2026-10-01T12:00:00Z")
    resolved: list[FakeProviderConfig] = []

    def recording_resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        resolved.append(provider)
        return resolve(provider)

    source = {"p": config(url="https://original.invalid")}
    registry = ModelRegistry(source, ModelSettings(tmp_path / "models-config.toml"), discovery, recording_resolver)

    assert not hasattr(registry, "replace_connections")
    source["p"] = config(url="https://moved.invalid")
    source["q"] = config("q", url="https://late.invalid")

    report = registry.refresh()
    registry.restore_selection()
    registry.select(cached.id)
    pinned = registry.admit()

    assert report.refreshed_provider_ids == ("p",)
    assert [c.base_url for c in discovery.calls] == ["https://original.invalid"]
    assert [p.base_url for p in resolved] == ["https://original.invalid", "https://original.invalid"]
    assert pinned.connection.base_url == "https://original.invalid"
    assert [state.provider_id for state in registry.view().providers] == ["p"]


# ---------------------------------------------------------------------------
# Durable selection preference transaction (task 3.3; ADR-0040)
# ---------------------------------------------------------------------------


def _make_registry(
    tmp_path: Path, models: tuple[ModelRecord, ...], *, catalog_default: str | None = None,
) -> tuple[ModelRegistry, Path, Path, FakeDiscovery]:
    """Build a real registry over a real catalog file and a real preference file."""
    import model_settings
    from model_settings import ModelPreference

    catalog = tmp_path / "models-config.toml"
    ModelSettings(catalog).write(CatalogDocument(
        1, 2, catalog_default,
        (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),), models,
    ))
    selection = tmp_path / "config" / "model-selection.toml"
    discovery = FakeDiscovery()
    registry = ModelRegistry(
        {"p": config()}, ModelSettings(catalog), discovery, resolve,
        preference=ModelPreference(selection),
    )
    assert model_settings  # module used by callers for OS-boundary patching
    return registry, catalog, selection, discovery


def test_deliberate_selection_is_durable_before_publication_and_failure_keeps_prior_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import model_settings

    first, second = model("first"), model("second")
    registry, catalog, selection, _ = _make_registry(tmp_path, (first, second))
    catalog_bytes = catalog.read_bytes()
    registry.select(first.id)
    assert selection.read_bytes() == b'model = "p/first"\n'
    assert catalog.read_bytes() == catalog_bytes  # selection never rewrites the catalog

    real_replace = os.replace
    at_swap: list[tuple[str | None, bytes]] = []

    def observing_replace(source: str, destination: object) -> None:
        at_swap.append((registry.view().active_model_id, Path(destination).read_bytes()))
        real_replace(source, destination)

    monkeypatch.setattr(model_settings.os, "replace", observing_replace)
    view = registry.select(second.id)
    assert at_swap == [(first.id, b'model = "p/first"\n')]
    assert view.active_model_id == second.id and view.remembered_model_id == second.id
    assert selection.read_bytes() == b'model = "p/second"\n'

    def failing_replace(*_args: object) -> None:
        raise OSError("private detail")

    revision_before_failure = registry.view().selection_revision
    monkeypatch.setattr(model_settings.os, "replace", failing_replace)
    with pytest.raises(ModelRegistryError) as error:
        registry.select(first.id)
    assert error.value.code == "persistence_failed" and "private detail" not in str(error.value)
    view = registry.view()
    assert (view.active_model_id, view.remembered_model_id) == (second.id, second.id)
    assert view.selection_revision == revision_before_failure
    assert selection.read_bytes() == b'model = "p/second"\n'
    assert sorted(item.name for item in selection.parent.iterdir()) == [selection.name]
    assert catalog.read_bytes() == catalog_bytes


def test_preference_is_the_only_selection_source_and_sink(tmp_path: Path) -> None:
    """The combined catalog's ``default_model`` is neither read nor written for selection."""
    first, second = model("first"), model("second")
    registry, catalog, selection, discovery = _make_registry(
        tmp_path, (first, second), catalog_default=second.id,
    )
    view = registry.restore_selection()
    # Task 3.5: the in-memory ordinal-first choice, never the catalog default (second).
    assert view.remembered_model_id is None and view.active_model_id == first.id
    assert not selection.exists()  # no automatic preference write

    from model_settings import ModelPreference

    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text('model = "p/second"\n', encoding="utf-8")
    registry = ModelRegistry(
        {"p": config()}, ModelSettings(catalog), discovery, resolve,
        preference=ModelPreference(selection),
    )
    discovery.responses["p"] = FakeInventory("p", "openai", (first, second), "2026-10-01T12:00:00Z")
    registry.refresh()
    view = registry.restore_selection()
    assert (view.remembered_model_id, view.active_model_id) == (second.id, second.id)
    assert selection.read_bytes() == b'model = "p/second"\n'
    assert ModelSettings(catalog).load().document.default_model == second.id


def test_corrupt_preference_is_untouched_by_startup_and_refresh_until_deliberate_select(
    tmp_path: Path,
) -> None:
    first = model("first")
    from model_settings import ModelPreference

    selection = tmp_path / "config" / "model-selection.toml"
    selection.parent.mkdir(parents=True)
    corrupt = b'model = "p/first"\napi_key = "sk-corruptcanary0123456"\n'
    selection.write_bytes(corrupt)
    catalog = tmp_path / "models-config.toml"
    ModelSettings(catalog).write(CatalogDocument(
        1, 2, None, (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),), (first,),
    ))
    discovery = FakeDiscovery()
    discovery.responses["p"] = FakeInventory("p", "openai", (first,), "2026-10-01T12:00:00Z")
    registry = ModelRegistry(
        {"p": config()}, ModelSettings(catalog), discovery, resolve,
        preference=ModelPreference(selection),
    )
    registry.refresh()
    view = registry.restore_selection()
    assert view.active_model_id is None and view.remembered_model_id is None
    assert selection.read_bytes() == corrupt
    assert sorted(item.name for item in selection.parent.iterdir()) == [selection.name]

    view = registry.select(first.id)
    assert view.active_model_id == first.id
    assert selection.read_bytes() == b'model = "p/first"\n'
    assert sorted(item.name for item in selection.parent.iterdir()) == [selection.name]


# ---------------------------------------------------------------------------
# Disposable advisory cache (task 3.4; provider-model-catalog, ADR-0040)
# ---------------------------------------------------------------------------

_CACHE_CANARY = "sk-registrycanary0123456789"


def _registry(
    tmp_path: Path, models: tuple[ModelRecord, ...] = (), *, preference_text: str | None = None,
    cache_bytes: bytes | None = None,
) -> tuple[ModelRegistry, Path, Path, FakeDiscovery]:
    """A real registry over a real ModelCache file and a real ModelPreference file."""
    from model_settings import ModelCache, ModelPreference

    cache_path = tmp_path / "cache" / "models-cache.json"
    selection = tmp_path / "config" / "model-selection.toml"
    if models:
        ModelCache(cache_path).write(CatalogDocument(
            1, 0, None, (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),), models,
        ))
    if cache_bytes is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(cache_bytes)
    if preference_text is not None:
        selection.parent.mkdir(parents=True, exist_ok=True)
        selection.write_text(preference_text, encoding="utf-8")
    discovery = FakeDiscovery()
    registry = ModelRegistry(
        {"p": config()}, ModelCache(cache_path), discovery, resolve, preference=ModelPreference(selection),
    )
    return registry, cache_path, selection, discovery


def test_cache_write_failure_keeps_discovery_in_memory_and_selection_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import model_settings

    first, added = model("first"), model("added")
    registry, cache_path, selection, discovery = _registry(
        tmp_path, (first,), preference_text='model = "p/first"\n'
    )
    registry.refresh()
    registry.restore_selection()
    cache_before, preference_before = cache_path.read_bytes(), selection.read_bytes()
    view_before = registry.view()
    discovery.responses["p"] = FakeInventory("p", "openai", (first, added), "2026-10-01T12:00:00Z")
    real_replace = os.replace

    def fail_cache_replace(src: object, dst: object, *a: object, **k: object) -> None:
        if Path(dst) == cache_path:  # type: ignore[arg-type]
            raise OSError(f"disk {_CACHE_CANARY}")
        real_replace(src, dst, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(model_settings.os, "replace", fail_cache_replace)

    report = registry.refresh()
    view = registry.view()

    assert report.persisted is False
    assert report.error_codes[-1] == ("catalog", "persistence_failed")
    assert _CACHE_CANARY not in repr(report)
    assert {m.id for m in view.models} == {first.id, added.id}  # usable discovery is not rolled back
    assert view.active_model_id == first.id == view_before.active_model_id
    assert view.remembered_model_id == view_before.remembered_model_id
    assert view.selection_revision == view_before.selection_revision
    assert cache_path.read_bytes() == cache_before and selection.read_bytes() == preference_before
    assert sorted(p.name for p in cache_path.parent.iterdir()) == [cache_path.name]
    assert sorted(p.name for p in selection.parent.iterdir()) == [selection.name]


@pytest.mark.parametrize(
    "corrupt", [b"{broken", b"\xff\xfe", b'{"schema_version": 1, "api_key": "sk-registrycanary0123456789"}'],
    ids=["json", "utf8", "extra-key"],
)
def test_corrupt_cache_is_absent_with_bounded_notice_and_not_rewritten(
    tmp_path: Path, corrupt: bytes
) -> None:
    registry, cache_path, selection, discovery = _registry(
        tmp_path, preference_text='model = "p/first"\n', cache_bytes=corrupt
    )

    assert registry.view().models == ()
    notice = registry.cache_notice
    assert notice is not None and 0 < len(notice) <= 300
    assert _CACHE_CANARY not in notice and str(tmp_path) not in notice
    assert "/models" in notice  # actionable: names the recovery path
    assert cache_path.read_bytes() == corrupt and selection.read_bytes() == b'model = "p/first"\n'
    assert [p.name for p in cache_path.parent.iterdir()] == [cache_path.name]

    discovery.responses["p"] = FakeInventory("p", "openai", (model("first"),), "2026-10-01T12:00:00Z")
    registry.refresh()  # a successful refresh deliberately replaces the disposable file
    assert json.loads(cache_path.read_text(encoding="utf-8"))["models"][0]["id"] == "p/first"
    assert [p.name for p in cache_path.parent.iterdir()] == [cache_path.name]


def test_missing_or_valid_cache_has_no_notice(tmp_path: Path) -> None:
    assert _registry(tmp_path)[0].cache_notice is None
    assert _registry(tmp_path, (model("first"),))[0].cache_notice is None


def test_cache_write_failure_removal_keeps_active_selector_and_preference_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validated removals are published, but a failed cache write never changes the active choice."""
    import model_settings

    first, other = model("first"), model("other")
    registry, cache_path, selection, discovery = _registry(
        tmp_path, (first, other), preference_text='model = "p/first"\n'
    )
    registry.refresh()
    registry.restore_selection()
    before = registry.view()
    assert before.active_model_id == first.id
    cache_before, preference_before = cache_path.read_bytes(), selection.read_bytes()
    discovery.responses["p"] = FakeInventory("p", "openai", (other,), "2026-10-01T12:00:00Z")
    real_replace = os.replace

    def fail_cache_replace(src: object, dst: object, *a: object, **k: object) -> None:
        if Path(dst) == cache_path:  # type: ignore[arg-type]
            raise OSError("disk")
        real_replace(src, dst, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(model_settings.os, "replace", fail_cache_replace)

    report = registry.refresh()
    view = registry.view()

    assert report.persisted is False
    assert tuple(m.id for m in view.models) == (other.id,)  # removal published; no stale candidate re-inserted
    assert view.active_model_id == first.id  # the honest, uncached selector, not a fake candidate
    assert view.remembered_model_id == first.id and view.selection_status == "active"
    assert view.selection_revision == before.selection_revision
    assert cache_path.read_bytes() == cache_before and selection.read_bytes() == preference_before
    # Task 4.1: the uncached active selector and an explicit use of it are sent as saved (no fallback).
    assert registry.admit().model.id == first.id and registry.admit(first.id).model.upstream_id == "first"


@pytest.mark.parametrize("channel", ["display_name", "id"])
def test_provider_reflected_resolved_credential_never_reaches_inventory_or_cache(
    tmp_path: Path, channel: str
) -> None:
    """Real ModelDiscovery over httpx.MockTransport: an opaque reflected credential is rejected."""
    import httpx

    from model_discovery import ModelDiscovery

    canary = "opaqueCredentialCanary987654321"
    row = {"id": "safe", "display_name": "Safe"}
    row["id" if channel == "id" else "display_name"] = f"x-{canary}" if channel == "id" else canary
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [row]})

    def resolver(provider: FakeProviderConfig) -> FakeResolvedConnection:
        return FakeResolvedConnection(provider.name, provider.type, provider.base_url, api_key=canary)

    from model_settings import ModelCache, ModelPreference

    cache_path = tmp_path / "cache" / "models-cache.json"
    discovery = ModelDiscovery(httpx.Client(transport=httpx.MockTransport(handler)))
    registry = ModelRegistry(
        {"p": config()}, ModelCache(cache_path), discovery, resolver,
        preference=ModelPreference(tmp_path / "config" / "model-selection.toml"),
    )

    report = registry.refresh()

    assert seen == [f"Bearer {canary}"]  # the credential really was used for the request
    assert report.stale_provider_ids == ("p",) and report.refreshed_provider_ids == ()
    assert report.error_codes == (("p", "invalid_inventory"),)
    assert canary not in repr(report) and canary not in repr(registry.view())
    assert registry.view().models == ()
    assert not cache_path.exists() or canary not in cache_path.read_text(encoding="utf-8")
    assert canary not in str(registry.view().providers)


# ---------------------------------------------------------------------------
# Startup selection (task 3.5; provider-model-catalog, ADR-0040)
# ---------------------------------------------------------------------------

_PREF_CANARY = "sk-prefcanary0123456789"


def _build_world(
    tmp_path: Path,
    *,
    cached: tuple[ModelRecord, ...] = (),
    connections: tuple[str, ...] = ("p",),
    preference: bytes | None = None,
    preference_is_directory: bool = False,
) -> tuple[ModelRegistry, Path, Path, FakeDiscovery]:
    """Real registry over a real cache file and a real preference path (no fakes of owned code)."""
    from model_settings import ModelCache, ModelPreference

    cache_path = tmp_path / "cache" / "models-cache.json"
    selection = tmp_path / "config" / "model-selection.toml"
    if cached:
        providers = tuple(sorted({m.provider_id for m in cached}))
        ModelCache(cache_path).write(CatalogDocument(
            1, 0, None,
            tuple(ProviderState(pid, "openai", "2026-09-01T00:00:00Z", "fresh") for pid in providers),
            cached,
        ))
    selection.parent.mkdir(parents=True, exist_ok=True)
    if preference is not None:
        selection.write_bytes(preference)
    if preference_is_directory:
        selection.mkdir()  # a real OS read failure (IsADirectoryError), no permission mocking
    discovery = FakeDiscovery()
    registry = ModelRegistry(
        {pid: config(pid) for pid in connections}, ModelCache(cache_path), discovery, resolve,
        preference=ModelPreference(selection),
    )
    return registry, cache_path, selection, discovery


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("cached", [(), (model("other"),)], ids=["uncached", "other-cached"])
def test_saved_configured_selector_is_active_even_when_not_cached(
    tmp_path: Path, cached: tuple[ModelRecord, ...]
) -> None:
    registry, _, selection, discovery = _build_world(
        tmp_path, cached=cached, preference=b'model = "p/not-cached"\n'
    )
    before = _tree(tmp_path)

    view = registry.restore_selection()

    assert view.active_model_id == "p/not-cached" and view.remembered_model_id == "p/not-cached"
    assert view.selection_status == "active"
    assert registry.selection_notice is None
    assert discovery.calls == [] and _tree(tmp_path) == before  # no write, no fallback, no discovery


@pytest.mark.parametrize(
    "raw",
    [
        b"model = 7\n",
        b'model = "no-separator"\n',
        b'model = "p/"\n',
        b'model = "bad id/x"\n',
        b'model = ""\n',
        b"model = [broken\n",
        b"\xff\xfe\x00",
        b'model = "p/first"\napi_key = "' + _PREF_CANARY.encode() + b'"\n',
        b"",
    ],
    ids=["wrong-type", "no-separator", "empty-upstream", "bad-connection", "blank", "toml", "utf8", "extra-key", "empty-file"],
)
def test_invalid_saved_selection_is_unset_with_notice_and_never_replaced(
    tmp_path: Path, raw: bytes
) -> None:
    registry, _, selection, discovery = _build_world(tmp_path, cached=(model("first"),), preference=raw)
    before = _tree(tmp_path)

    view = registry.restore_selection()

    assert view.active_model_id is None and view.selection_status == "no_selection"
    assert registry.selection_notice is not None
    notice = registry.selection_notice
    assert notice is not None and 0 < len(notice) <= 300
    assert _PREF_CANARY not in notice and str(tmp_path) not in notice and "p/first" not in notice
    assert "/models" in notice  # actionable recovery path
    assert discovery.calls == [] and _tree(tmp_path) == before  # bytes preserved, nothing quarantined


def test_unreadable_saved_selection_is_unset_not_treated_as_missing(tmp_path: Path) -> None:
    registry, _, selection, discovery = _build_world(
        tmp_path, cached=(model("first"),), preference_is_directory=True
    )

    view = registry.restore_selection()

    assert view.active_model_id is None  # not the missing-file automatic first-candidate choice
    assert registry.selection_notice is not None
    assert selection.is_dir() and discovery.calls == []


def test_saved_selector_naming_a_removed_connection_is_unset_with_notice(tmp_path: Path) -> None:
    registry, cache_path, selection, discovery = _build_world(
        tmp_path, cached=(model("first"), model("m", "gone")), connections=("p",),
        preference=b'model = "gone/m"\n',
    )
    before = _tree(tmp_path)

    view = registry.restore_selection()

    assert view.active_model_id is None and view.selection_status == "saved_selection_unavailable"
    assert view.remembered_model_id == "gone/m"
    assert registry.selection_notice is not None
    assert registry.selection_notice is not None and "gone" not in registry.selection_notice
    assert discovery.calls == [] and _tree(tmp_path) == before


def test_without_preference_first_ordinal_canonical_id_is_active_in_memory_only(tmp_path: Path) -> None:
    """Ordinal (code point) order of full IDs: not insertion, provider default, display name or locale."""
    cached = (
        model("model-9", "alpha", display_name="A first by display"),
        model("Model-1", "alpha"),
        model("m", "Zeta", display_name="zzz"),
        model("a", "alpha2"),
    )
    registry, cache_path, selection, discovery = _build_world(
        tmp_path, cached=cached, connections=("alpha", "Zeta", "alpha2"),
    )
    before = _tree(tmp_path)

    view = registry.restore_selection()

    assert sorted(m.id for m in cached)[0] == "Zeta/m"
    assert view.active_model_id == "Zeta/m" and view.selection_status == "active"
    assert view.remembered_model_id is None  # a choice made in memory is not a saved selection
    assert not selection.exists() and discovery.calls == [] and _tree(tmp_path) == before
    assert registry.selection_notice is None


def test_unsaved_automatic_choice_is_rederived_at_restart_not_remembered(tmp_path: Path) -> None:
    from model_settings import ModelCache

    registry, cache_path, selection, _ = _build_world(tmp_path, cached=(model("b"), model("c")))
    assert registry.restore_selection().active_model_id == "p/b"
    assert not selection.exists()
    # The cache changes between runs; the former automatic choice is not sticky.
    ModelCache(cache_path).write(CatalogDocument(
        1, 0, None, (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
        (model("a"), model("c")),
    ))
    restarted, _, _, _ = _build_world(tmp_path)
    assert restarted.restore_selection().active_model_id == "p/a"
    assert not selection.exists()


def test_no_preference_and_empty_cache_leaves_selection_unset_without_files(tmp_path: Path) -> None:
    registry, cache_path, selection, discovery = _build_world(tmp_path)

    view = registry.restore_selection()

    assert view.active_model_id is None and view.selection_status == "no_selection"
    assert not cache_path.exists() and not selection.exists() and discovery.calls == []
    assert registry.selection_notice is None


def test_cached_candidates_of_removed_connections_are_filtered_before_the_automatic_choice(
    tmp_path: Path,
) -> None:
    stale = (model("aaa", "gone"), model("zzz", "p"))
    registry, cache_path, selection, discovery = _build_world(tmp_path, cached=stale, connections=("p",))
    cache_before = cache_path.read_bytes()

    view = registry.restore_selection()

    assert [m.id for m in view.models] == ["p/zzz"]
    assert [s.provider_id for s in view.providers] == ["p"]
    assert view.active_model_id == "p/zzz"  # not the filtered-out, ordinal-first stale id
    assert cache_path.read_bytes() == cache_before and not selection.exists()  # no cache mutation


def test_only_stale_removed_connection_candidates_leave_selection_unset(tmp_path: Path) -> None:
    registry, _, _, _ = _build_world(tmp_path, cached=(model("aaa", "gone"),), connections=("p",))
    assert registry.restore_selection().active_model_id is None


def test_automatic_choice_comes_from_the_initially_loaded_cache_not_the_refresh(tmp_path: Path) -> None:
    """Task 3.5 correction: refresh may replace the catalog, but the startup choice stays the cached minimum."""
    registry, cache_path, selection, discovery = _build_world(tmp_path, cached=(model("a"), model("m")))
    discovery.responses["p"] = FakeInventory("p", "openai", (model("z"),), "2026-10-01T12:00:00Z")

    report = registry.refresh()
    view = registry.restore_selection()

    assert report.refreshed_provider_ids == ("p",)
    assert [m.id for m in view.models] == ["p/z"]  # the discovered replacement is published as-is
    assert view.active_model_id == "p/a" and view.remembered_model_id is None  # uncached, memory only
    assert not selection.exists()


def test_valid_empty_discovery_does_not_wipe_the_initial_automatic_choice(tmp_path: Path) -> None:
    registry, _, selection, discovery = _build_world(tmp_path, cached=(model("a"),))
    discovery.responses["p"] = FakeInventory("p", "openai", (), "2026-10-01T12:00:00Z")

    registry.refresh()
    view = registry.restore_selection()

    assert view.models == () and view.active_model_id == "p/a" and not selection.exists()


def test_initial_choice_ignores_removed_connections_and_uses_ordinal_not_insertion(tmp_path: Path) -> None:
    registry, _, _, discovery = _build_world(
        tmp_path, cached=(model("aaa", "gone"), model("b"), model("B")), connections=("p",),
    )
    discovery.responses["p"] = FakeInventory("p", "openai", (model("0"),), "2026-10-01T12:00:00Z")
    registry.refresh()
    assert registry.restore_selection().active_model_id == "p/B"


def test_empty_initial_cache_derives_the_choice_from_the_refreshed_candidates(tmp_path: Path) -> None:
    registry, _, selection, discovery = _build_world(tmp_path)
    discovery.responses["p"] = FakeInventory("p", "openai", (model("y"), model("x")), "2026-10-01T12:00:00Z")

    registry.refresh()
    view = registry.restore_selection()

    assert view.active_model_id == "p/x" and view.remembered_model_id is None and not selection.exists()


# --- Task 3.6: first-start discovery runs once, only when nothing is known -------------------------


def _real_world(
    tmp_path: Path,
    behaviors: dict[str, Callable[[httpx.Request], httpx.Response]],
    *,
    cached: tuple[ModelRecord, ...] = (),
    cache_bytes: bytes | None = None,
    preference: bytes | None = None,
    preference_is_directory: bool = False,
):
    """Real registry, cache, preference and ModelDiscovery over httpx.MockTransport (HTTP is the only fake)."""
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_discovery import ModelDiscovery
    from model_settings import ModelCache, ModelPreference

    cache_path = tmp_path / "cache" / "models-cache.json"
    selection = tmp_path / "config" / "model-selection.toml"
    if cached:
        providers = tuple(sorted({m.provider_id for m in cached}))
        ModelCache(cache_path).write(CatalogDocument(
            1, 0, None,
            tuple(ProviderState(pid, "anthropic", "2026-09-01T00:00:00Z", "fresh") for pid in providers),
            cached,
        ))
    if cache_bytes is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(cache_bytes)
    selection.parent.mkdir(parents=True, exist_ok=True)
    if preference is not None:
        selection.write_bytes(preference)
    if preference_is_directory:
        selection.mkdir()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return behaviors[request.url.host.split(".")[0]](request)

    discovery = ModelDiscovery(
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connections = {pid: ProviderConfig(pid, "anthropic", "env:K", f"https://{pid}.example") for pid in behaviors}

    def resolve_real(item: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(item.name, item.type, item.base_url, item.request_timeout, "canary-key-0123")

    registry = ModelRegistry(
        connections, ModelCache(cache_path), discovery, resolve_real, preference=ModelPreference(selection),
    )
    return registry, cache_path, selection, requests


def _paged(first: str, second: str) -> Callable[[httpx.Request], httpx.Response]:
    """Two-page Anthropic-shaped inventory: one logical discovery pass is two HTTP requests."""

    def behave(request: httpx.Request) -> httpx.Response:
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": first}], "has_more": True, "last_id": "c1"})
        return httpx.Response(200, json={"data": [{"id": second}], "has_more": False})

    return behave


def _unavailable(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="unavailable")


_PREF_OK = b'model = "p/saved"\n'
_STARTUP_CASES = {
    "empty": dict(),
    "corrupt-cache": dict(cache_bytes=b"{not json"),
    "only-removed-connection-cached": dict(cached=(model("gone", "removed"),)),
    "cached-candidate": dict(cached=(model("kept"),)),
    "valid-pref-uncached": dict(preference=_PREF_OK),
    "valid-pref-cached": dict(preference=_PREF_OK, cached=(model("kept"),)),
    "corrupt-pref": dict(preference=b"{{garbage"),
    "unreadable-pref": dict(preference_is_directory=True),
    "unconfigured-pref": dict(preference=b'model = "removed/x"\n'),
}
_DISCOVERS = {"empty", "corrupt-cache", "only-removed-connection-cached"}


@pytest.mark.parametrize("case", list(_STARTUP_CASES))
def test_startup_discovers_exactly_one_logical_pass_only_with_no_preference_and_no_usable_cache(
    tmp_path: Path, case: str
) -> None:
    import main as main_mod

    registry, cache_path, selection, requests = _real_world(
        tmp_path, {"p": _paged("a", "b")}, **_STARTUP_CASES[case]
    )
    pref_before = selection.read_bytes() if selection.is_file() else None
    cache_before = cache_path.read_bytes() if cache_path.is_file() else None

    main_mod._refresh_model_catalog(registry, cache_path)

    if case in _DISCOVERS:
        assert len(requests) == 2  # one logical pass: both pages of the one connection, no retry
        assert [m.id for m in registry.view().models] == ["p/a", "p/b"]  # pages publish together
        assert registry.view().active_model_id == "p/a" and registry.view().remembered_model_id is None
        assert (selection.read_bytes() if selection.is_file() else None) == pref_before
    else:
        assert requests == []
        assert (cache_path.read_bytes() if cache_path.is_file() else None) == cache_before
    assert (selection.read_bytes() if selection.is_file() else None) == pref_before


@pytest.mark.parametrize("failing", [("p", "q"), ("q",)], ids=["all-fail", "partial-fail"])
def test_first_start_failures_are_one_pass_without_retry_and_do_not_block_startup(
    tmp_path: Path, failing: tuple[str, ...]
) -> None:
    import main as main_mod

    behaviors = {pid: (_unavailable if pid in failing else _paged("a", "b")) for pid in ("p", "q")}
    registry, cache_path, selection, requests = _real_world(tmp_path, behaviors)

    main_mod._refresh_model_catalog(registry, cache_path)

    hosts = sorted(r.url.host for r in requests)
    assert hosts == (["p.example", "q.example"] if len(failing) == 2 else ["p.example", "p.example", "q.example"])
    view = registry.view()
    assert {s.provider_id: s.status for s in view.providers} == {
        pid: ("stale" if pid in failing else "fresh") for pid in ("p", "q")
    }
    assert [m.id for m in view.models] == ([] if len(failing) == 2 else ["p/a", "p/b"])
    assert view.active_model_id == (None if len(failing) == 2 else "p/a")
    assert not selection.exists() or selection.read_bytes() == b""
    assert "canary-key-0123" not in repr(view)


def test_second_page_failure_during_refresh_keeps_whole_old_inventory_and_selection(tmp_path: Path) -> None:
    cached = (model("old1"), model("old2"))
    registry, cache_path, selection, requests = _real_world(
        tmp_path,
        {"p": lambda r: httpx.Response(200, json={"data": [{"id": "new"}], "has_more": True, "last_id": "c1"})
         if "after_id" not in r.url.params else httpx.Response(200, json={"data": "bad", "has_more": False})},
        cached=cached, preference=_PREF_OK,
    )
    registry.restore_selection()
    cache_before = cache_path.read_bytes()

    report = registry.refresh()

    assert len(requests) == 2 and report.refreshed_provider_ids == ()
    assert registry.view().models == cached and registry.view().providers[0].status == "stale"
    assert registry.view().remembered_model_id == "p/saved" and selection.read_bytes() == _PREF_OK
    assert "new" not in cache_path.read_text(encoding="utf-8") and cache_path.read_bytes() != b""
    assert cache_before  # stale metadata persisted, but no page of the failed fetch


def test_valid_empty_refresh_clears_inventory_but_never_selection_pin_or_preference(tmp_path: Path) -> None:
    saved = model("saved")
    registry, cache_path, selection, _ = _real_world(
        tmp_path, {"p": lambda _r: httpx.Response(200, json={"data": [], "has_more": False})},
        cached=(saved,), preference=_PREF_OK,
    )
    registry.restore_selection()
    pin = registry.admit()
    before = registry.view()

    report = registry.refresh()
    after = registry.view()

    assert report.refreshed_provider_ids == ("p",) and after.models == ()
    assert after.active_model_id == before.active_model_id == "p/saved"
    assert after.remembered_model_id == "p/saved"
    assert after.selection_revision == before.selection_revision and after.selection_status == "active"
    assert selection.read_bytes() == _PREF_OK
    assert pin.model.id == "p/saved" and pin.connection.api_key == "canary-key-0123"
    assert "saved" not in cache_path.read_text(encoding="utf-8")


def test_failed_first_start_leaves_empty_state_that_a_deliberate_refresh_and_select_recover(tmp_path: Path) -> None:
    import main as main_mod

    state = {"up": False}

    def behave(request: httpx.Request) -> httpx.Response:
        return _paged("a", "b")(request) if state["up"] else _unavailable(request)

    registry, cache_path, selection, requests = _real_world(tmp_path, {"p": behave})

    main_mod._refresh_model_catalog(registry, cache_path)

    assert len(requests) == 1 and registry.view().active_model_id is None
    with pytest.raises(ModelRegistryError) as error:
        registry.admit()
    assert error.value.code == "no_active_selection"  # implicit use refuses; /models guides
    assert registry.admit("p/a").model.upstream_id == "a"  # explicit use needs neither Main nor cache
    with pytest.raises(ModelRegistryError) as error:
        registry.select("p")
    assert error.value.code == "malformed_selector"  # selection validates shape and connection, never the empty cache
    assert registry.view().active_model_id is None and not selection.exists()
    state["up"] = True
    registry.refresh()  # the user's explicit /models
    assert registry.select("p/a").active_model_id == "p/a"
    assert selection.read_bytes() == b'model = "p/a"\n'


# --- Task 4.1: exact first-slash selectors need neither Main nor cache membership -----------------

_CANARY = "canary-key-4-1-0123"


class _ExplicitWorld:
    """Real registry, preference file and cache file; only credential-free HTTP is faked."""

    def __init__(self, tmp_path: Path, connections: dict[str, str]) -> None:
        from config_schema import ProviderConfig, ResolvedProviderConnection
        from model_settings import ModelCache, ModelPreference

        self.cache_path = tmp_path / "cache" / "models-cache.json"
        self.selection = tmp_path / "config" / "model-selection.toml"
        self.selection.parent.mkdir(parents=True, exist_ok=True)
        self.resolved: list[str] = []
        self.discovery = FakeDiscovery()
        self.configs = {
            name: ProviderConfig(name, adapter, "env:K", f"https://{name.lower()}.test", 30)
            for name, adapter in connections.items()
        }

        def resolve_real(item: ProviderConfig) -> ResolvedProviderConnection:
            self.resolved.append(item.name)
            return ResolvedProviderConnection(item.name, item.type, item.base_url, item.request_timeout, _CANARY)

        self.resolver = resolve_real
        self.registry = ModelRegistry(
            self.configs, ModelCache(self.cache_path), self.discovery, self.resolver,
            preference=ModelPreference(self.selection),
        )

    def assert_nothing_persisted(self) -> None:
        assert not self.cache_path.exists() and not self.selection.exists()
        assert self.registry.view().models == ()


@pytest.mark.parametrize(
    ("selector", "upstream"),
    [
        ("p/m", "m"),
        ("p/org/sub/Model:Tag", "org/sub/Model:Tag"),
        ("p/ padded model ", " padded model "),
        ("p/   ", "   "),
        ("p//lead-slash", "/lead-slash"),
        ("p/trail/", "trail/"),
        ("Mixed-Case.1/GPT X", "GPT X"),
    ],
)
def test_explicit_selector_splits_at_first_slash_and_keeps_upstream_verbatim(
    tmp_path: Path, selector: str, upstream: str
) -> None:
    """Uncached explicit selectors work with no Main and no cache, and persist nothing."""
    world = _ExplicitWorld(tmp_path, {"p": "openai", "Mixed-Case.1": "openai"})

    pin = world.registry.admit(selector)

    assert (pin.model.provider_id, pin.model.upstream_id, pin.model.id) == (
        selector.split("/", 1)[0], upstream, selector,
    )
    assert pin.connection.provider_id == selector.split("/", 1)[0]
    assert world.resolved == [pin.model.provider_id]  # one credential resolution
    assert pin.model.metadata == ModelMetadata()  # nothing is claimed as provider-reported
    world.assert_nothing_persisted()
    assert world.registry.view().active_model_id is None
    assert world.discovery.calls == []


@pytest.mark.parametrize(
    ("selector", "code"),
    [
        ("", "malformed_selector"),
        ("p", "malformed_selector"),
        ("p/", "malformed_selector"),
        ("/m", "malformed_selector"),
        ("/", "malformed_selector"),
        (" p/m", "unknown_connection"),
        ("p /m", "unknown_connection"),
        ("p\t/m", "unknown_connection"),
        ("P/m", "unknown_connection"),
        ("other/m", "unknown_connection"),
        ("p/m".upper(), "unknown_connection"),
        (7, "malformed_selector"),
    ],
)
def test_explicit_selector_rejects_bad_shape_or_connection_locally_without_normalizing(
    tmp_path: Path, selector: object, code: str
) -> None:
    world = _ExplicitWorld(tmp_path, {"p": "openai"})

    with pytest.raises(ModelRegistryError) as error:
        world.registry.admit(selector)  # type: ignore[arg-type]

    assert error.value.code == code
    assert world.resolved == []  # rejected before credential resolution
    world.assert_nothing_persisted()
    if isinstance(selector, str) and len(selector) > 1:
        assert selector not in str(error.value)  # the message is actionable but never echoes the input
    assert _CANARY not in str(error.value)


def test_explicit_selector_is_independent_of_corrupt_preference_and_never_touches_it(tmp_path: Path) -> None:
    world = _ExplicitWorld(tmp_path, {"p": "openai"})
    world.selection.write_bytes(b"{{garbage")
    from model_settings import ModelCache, ModelPreference

    registry = ModelRegistry(
        world.configs, ModelCache(world.cache_path), world.discovery, world.resolver,
        preference=ModelPreference(world.selection),
    )
    registry.restore_selection()

    pin = registry.admit("p/never-discovered")

    assert pin.model.upstream_id == "never-discovered"
    assert world.selection.read_bytes() == b"{{garbage"
    assert not world.cache_path.exists() and registry.view().active_model_id is None
    with pytest.raises(ModelRegistryError) as error:
        registry.admit()  # implicit use still has nothing to inherit
    assert error.value.code == "no_active_selection"


def test_implicit_admission_uses_saved_active_main_even_when_uncached(tmp_path: Path) -> None:
    world = _ExplicitWorld(tmp_path, {"p": "openai"})
    world.selection.write_bytes(b'model = "p/saved/with space"\n')
    from model_settings import ModelCache, ModelPreference

    registry = ModelRegistry(
        world.configs, ModelCache(world.cache_path), world.discovery, world.resolver,
        preference=ModelPreference(world.selection),
    )
    assert registry.restore_selection().active_model_id == "p/saved/with space"

    pin = registry.admit()

    assert (pin.model.id, pin.model.upstream_id) == ("p/saved/with space", "saved/with space")
    assert world.resolved == ["p"] and not world.cache_path.exists()


def test_explicit_cached_selector_keeps_provider_reported_record(tmp_path: Path) -> None:
    cached = model("known", display_name="Known One")
    registry, _, _ = make_registry(tmp_path, models=(cached,))

    assert registry.admit("p/known").model == cached


def test_refresh_and_main_change_during_credential_resolution_do_not_reject_explicit_selector(
    tmp_path: Path,
) -> None:
    """No catalog or selection revision recheck: the pin keeps the admitted target."""
    first, second = model("first"), model("second")
    registry, _, discovery = make_registry(tmp_path, models=(first, second))
    registry.select(first.id)
    discovery.responses["p"] = inventory()  # a refresh would empty the catalog
    state = {"armed": True}

    def disturbing(provider: FakeProviderConfig) -> FakeResolvedConnection:
        if state["armed"]:
            state["armed"] = False
            registry.select(second.id)
            registry.refresh()
        return resolve(provider)

    registry._credential_resolver = disturbing  # type: ignore[attr-defined]
    pin = registry.admit("p/first")

    assert pin.model.id == "p/first" and pin.model.upstream_id == "first"
    assert registry.view().active_model_id == second.id and registry.view().models == ()


def test_scheduled_uncached_explicit_selector_is_used_verbatim_with_main(tmp_path: Path) -> None:
    main = model("main")
    registry, _, _ = make_registry(tmp_path, models=(main,))
    registry.select(main.id)

    pin = registry.admit_scheduled("p/not cached/at all")

    assert (pin.model.id, pin.model.upstream_id) == ("p/not cached/at all", "not cached/at all")


def _wire(adapter: str, seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if adapter == "google":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    return handler


@pytest.mark.parametrize("adapter", ["openai", "openrouter", "google"])
def test_uncached_explicit_upstream_is_sent_once_verbatim_to_its_own_connection(
    tmp_path: Path, adapter: str
) -> None:
    """Real registry + real LLMClient: exact provider/key/endpoint/upstream reach the wire once."""
    from config_schema import AgentConfig
    from llm_client import LLMClient
    from urllib.parse import quote

    upstream = "Org/Sub-Model v1.5 (Preview)"
    world = _ExplicitWorld(tmp_path, {"Prod-1": adapter, "other": "openai"})
    pin = world.registry.admit(f"Prod-1/{upstream}")
    seen: list[httpx.Request] = []
    client = LLMClient(
        pin, agent_config=AgentConfig(ctx_max_tokens=100_000, max_retries=3, retry_delay=0),
        http_transport=httpx.MockTransport(_wire(adapter, seen)),
    )
    try:
        assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
    finally:
        client.close()

    assert len(seen) == 1 and seen[0].url.host == "prod-1.test"
    if adapter == "google":
        assert quote(upstream, safe="") in str(seen[0].url) and seen[0].headers["x-goog-api-key"] == _CANARY
    else:
        assert json.loads(seen[0].content)["model"] == upstream
        assert seen[0].headers["authorization"] == f"Bearer {_CANARY}"
    assert world.resolved == ["Prod-1"]
    world.assert_nothing_persisted()


@pytest.mark.parametrize("status", [404, 400])
def test_provider_rejection_of_unknown_upstream_is_one_permanent_failure_without_fallback(
    tmp_path: Path, status: int
) -> None:
    from config_schema import AgentConfig
    from llm_client import LLMClient, LLMPermanentError

    world = _ExplicitWorld(tmp_path, {"p": "openai", "q": "openai"})
    world.selection.write_bytes(b'model = "q/main-model"\n')
    seen: list[httpx.Request] = []

    def reject(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": f"echo {_CANARY}", "code": "model_not_found"}})

    pin = world.registry.admit("p/does-not-exist")
    client = LLMClient(
        pin, agent_config=AgentConfig(ctx_max_tokens=100_000, max_retries=3, retry_delay=0),
        http_transport=httpx.MockTransport(reject),
    )
    try:
        with pytest.raises(LLMPermanentError) as error:
            client.chat([{"role": "user", "content": "hi"}])
    finally:
        client.close()

    assert len(seen) == 1 and json.loads(seen[0].content)["model"] == "does-not-exist"
    assert seen[0].url.host == "p.test"  # no other model or connection was tried
    assert _CANARY not in str(error.value) and _CANARY not in repr(error.value)
    assert world.selection.read_bytes() == b'model = "q/main-model"\n'


def test_explicit_unknown_upstream_on_anthropic_is_admitted_registry_side(tmp_path: Path) -> None:
    """Admission is adapter-neutral; Anthropic's required output cap is task 6.x request budgeting."""
    world = _ExplicitWorld(tmp_path, {"claude": "anthropic"})

    pin = world.registry.admit("claude/claude-future-9")

    assert (pin.connection.adapter, pin.model.upstream_id) == ("anthropic", "claude-future-9")


# --- user-approved 6.1/6.3 slice under task 4.1: Anthropic sends the configured required cap -----------

def _anthropic_ok(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1},
        })

    return handler


@pytest.mark.parametrize("cap", [None, 777], ids=["default-1024", "configured-777"])
def test_uncached_unknown_anthropic_upstream_is_sent_once_with_the_configured_required_cap(
    tmp_path: Path, cap: int | None
) -> None:
    """Real registry -> real LLMClient over MockTransport: no Main, no cache, no discovered metadata."""
    from config_schema import AgentConfig
    from llm_client import LLMClient

    upstream = "Claude/Future 9 (beta)"
    world = _ExplicitWorld(tmp_path, {"Anth-1": "anthropic"})
    pin = world.registry.admit(f"Anth-1/{upstream}")
    assert pin.model.metadata == ModelMetadata()
    seen: list[httpx.Request] = []
    kwargs = {} if cap is None else {"required_output_cap_tokens": cap}
    client = LLMClient(
        pin, agent_config=AgentConfig(max_retries=3, retry_delay=0, **kwargs),
        http_transport=httpx.MockTransport(_anthropic_ok(seen)),
    )
    try:  # Anthropic has no native-tool wire here; ordinary chat is its only request mode
        assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert client.chat([{"role": "system", "content": "be brief"}, {"role": "user", "content": "again"}]) == "ok"
    finally:
        client.close()

    assert len(seen) == 2 and {r.url.host for r in seen} == {"anth-1.test"}
    for request in seen:
        body = json.loads(request.content)
        assert body["model"] == upstream and body["max_tokens"] == (cap or 1024)
        assert "temperature" not in body and "top_p" not in body
        assert request.headers["x-api-key"] == _CANARY
    world.assert_nothing_persisted()


def test_unknown_anthropic_upstream_rejection_is_one_permanent_failure_without_fallback(tmp_path: Path) -> None:
    from config_schema import AgentConfig
    from llm_client import LLMClient, LLMPermanentError

    world = _ExplicitWorld(tmp_path, {"anth": "anthropic", "other": "anthropic"})
    world.selection.write_bytes(b'model = "other/main-model"\n')
    seen: list[httpx.Request] = []

    def reject(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"type": "error", "error": {
            "type": "not_found_error", "message": f"no model {_CANARY}"}})

    client = LLMClient(
        world.registry.admit("anth/does/not exist"), agent_config=AgentConfig(max_retries=3, retry_delay=0),
        http_transport=httpx.MockTransport(reject),
    )
    try:
        with pytest.raises(LLMPermanentError) as error:
            client.chat([{"role": "user", "content": "hi"}])
    finally:
        client.close()

    assert len(seen) == 1 and json.loads(seen[0].content)["model"] == "does/not exist"
    assert seen[0].url.host == "anth.test" and _CANARY not in repr(error.value)
    assert world.selection.read_bytes() == b'model = "other/main-model"\n' and not world.cache_path.exists()
    assert world.discovery.calls == []


def test_graph_fixed_wire_cap_is_independent_of_the_configured_required_cap(tmp_path: Path) -> None:
    from config_schema import AgentConfig
    from llm_client import LLMClient

    world = _ExplicitWorld(tmp_path, {"anth": "anthropic"})
    seen: list[httpx.Request] = []
    client = LLMClient.for_graph_extraction(
        world.registry.admit("anth/graph-model"), agent_config=AgentConfig(required_output_cap_tokens=5000),
        http_transport=httpx.MockTransport(_anthropic_ok(seen)),
    )
    try:
        client.chat([{"role": "user", "content": "extract"}])
    finally:
        client.close()

    assert json.loads(seen[0].content)["max_tokens"] == 1024  # the graph policy cap, not 5000


def test_required_cap_from_parsed_config_reaches_the_anthropic_wire(tmp_path: Path) -> None:
    """Config path: ``parse_config`` -> ``cfg.agent`` (what main/controller/graph pass on) -> wire."""
    from config_schema import parse_config
    from llm_client import LLMClient

    cfg = parse_config({
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1], "security_mode": "allowlist"},
        "agent": {"required_output_cap_tokens": 333},
        "providers": {"anth": {"type": "anthropic", "api_key": "k", "base_url": "https://anth.test"}},
        "embeddings": {"provider": "anth", "model": "e"},
        "paths": {},
    })
    world = _ExplicitWorld(tmp_path, {"anth": "anthropic"})
    seen: list[httpx.Request] = []
    client = LLMClient(
        world.registry.admit("anth/m"), agent_config=cfg.agent, http_transport=httpx.MockTransport(_anthropic_ok(seen)),
    )
    try:
        client.chat([{"role": "user", "content": "hi"}])
    finally:
        client.close()

    assert json.loads(seen[0].content)["max_tokens"] == 333


# --- low-issue wave: selector-validation category (6) and repair clears stale notice (7) ---


@pytest.mark.parametrize("selector", ["p/bad\nid", "p/https://host.invalid/m", "p/sec:value"])
def test_selector_rejected_by_preference_validation_is_malformed_not_persistence_failure(
    tmp_path: Path, selector: str
) -> None:
    registry, _, selection, discovery = _build_world(
        tmp_path, cached=(model("first"),), preference=b'model = "p/first"\n'
    )
    registry.restore_selection()
    before_tree, before_view = _tree(tmp_path), registry.view()

    with pytest.raises(ModelRegistryError) as error:
        registry.select(selector)

    assert error.value.code == "malformed_selector"
    assert selector not in str(error.value) and "bad" not in str(error.value)
    assert registry.view() == before_view and _tree(tmp_path) == before_tree
    assert discovery.calls == []


def test_real_preference_write_failure_is_still_persistence_failed(tmp_path: Path) -> None:
    registry, _, selection, _ = _build_world(
        tmp_path, cached=(model("first"),), preference_is_directory=True
    )
    registry.restore_selection()

    with pytest.raises(ModelRegistryError) as error:
        registry.select("p/first")

    assert error.value.code == "persistence_failed"
    assert selection.is_dir() and registry.view().active_model_id is None


@pytest.mark.parametrize(
    "raw",
    [b"model = 7\n", b'model = "gone/m"\n'],
    ids=["corrupt", "unconfigured-connection"],
)
def test_successful_deliberate_select_repairs_selection_and_clears_stale_notice(
    tmp_path: Path, raw: bytes
) -> None:
    registry, _, selection, discovery = _build_world(
        tmp_path, cached=(model("first"),), preference=raw
    )
    registry.restore_selection()
    assert registry.selection_notice is not None and registry.view().active_model_id is None

    view = registry.select("p/first")

    assert selection.read_bytes() == b'model = "p/first"\n'
    assert registry.selection_notice is None
    assert view.active_model_id == "p/first" and view.selection_status == "active"
    assert registry.needs_first_start_discovery is False
    assert registry.view().active_model_id == "p/first"
    restarted, _, _, _ = _build_world(tmp_path)
    assert restarted.selection_notice is None and restarted.restore_selection().active_model_id == "p/first"
    assert discovery.calls == []


def test_failed_repair_write_keeps_notice_state_and_preference_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import model_settings

    registry, _, selection, _ = _build_world(
        tmp_path, cached=(model("first"),), preference=b"model = 7\n"
    )
    registry.restore_selection()
    notice, before_view = registry.selection_notice, registry.view()

    def failing_replace(*_args: object) -> None:
        raise OSError("private detail")

    monkeypatch.setattr(model_settings.os, "replace", failing_replace)
    with pytest.raises(ModelRegistryError):
        registry.select("p/first")

    assert registry.selection_notice == notice is not None
    assert registry.view() == before_view
    assert selection.read_bytes() == b"model = 7\n"


def test_refresh_alone_never_clears_the_corrupt_preference_notice_or_saves_a_choice(
    tmp_path: Path,
) -> None:
    registry, _, selection, discovery = _build_world(
        tmp_path, cached=(model("first"),), preference=b"model = 7\n"
    )
    discovery.responses["p"] = inventory("p", (model("first"), model("second")))
    registry.restore_selection()

    report = registry.refresh()

    assert report.refreshed_provider_ids == ("p",)
    assert registry.selection_notice is not None
    assert registry.view().active_model_id is None
    assert selection.read_bytes() == b"model = 7\n"


def test_explicit_admission_without_main_still_works_with_corrupt_preference_and_never_touches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, _, selection, _ = _build_world(
        tmp_path, cached=(model("first"),), preference=b"model = 7\n"
    )
    monkeypatch.setenv("FAKE_KEY", "k")
    registry.restore_selection()

    run = registry.admit("p/not-cached")

    assert run.model.id == "p/not-cached"
    assert registry.selection_notice is not None
    assert selection.read_bytes() == b"model = 7\n"
