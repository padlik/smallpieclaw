"""Behavior tests for graph model resolution independent of Main (task 12.15, ADR-0039)."""

from __future__ import annotations

import logging
import threading
from dataclasses import FrozenInstanceError, dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Callable

import pytest

from config_schema import ProviderConfig, ResolvedProviderConnection
from model_discovery import ProviderInventory
from model_registry import (
    CatalogDocument,
    CatalogView,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings

GRAPH_WORKER = "graph-resolution-test"
SEEDED_AT = "2026-09-01T00:00:00Z"
REFRESHED_AT = "2026-10-01T12:00:00Z"


def provider(provider_id: str, *, url: str | None = None) -> ProviderConfig:
    return ProviderConfig(
        provider_id,
        "openai",
        api_key=f"env:{provider_id.upper()}_KEY",
        base_url=url or f"https://{provider_id}.provider.invalid/v1",
        request_timeout=20,
    )


def resolved(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        config.name, config.type, config.base_url, config.request_timeout, f"resolved-secret-{config.name}"
    )


def record(upstream_id: str, provider_id: str, *, context: int = 100) -> ModelRecord:
    return ModelRecord(
        f"{provider_id}/{upstream_id}",
        provider_id,
        upstream_id,
        upstream_id,
        ModelMetadata(context, None, None, False, None, None),
    )


MAIN_RECORD = record("main", "p")
SIBLING_RECORD = record("sibling", "p")
GRAPH_RECORD = record("org/graph-model", "g")


def inventory(provider_id: str, models: tuple[ModelRecord, ...]) -> ProviderInventory:
    return ProviderInventory(provider_id, "openai", models, REFRESHED_AT)


class FakeDiscovery:
    """External provider discovery boundary returning real inventories."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[str] = []

    def fetch(self, connection: ResolvedProviderConnection) -> ProviderInventory:
        self.calls.append(connection.provider_id)
        result = self.responses[connection.provider_id]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


class CredentialBoundary:
    """External credential resolver; holds the graph worker's first call open."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.outcome: Callable[[ProviderConfig], ResolvedProviderConnection] = resolved
        self.entered = threading.Event()
        self.ready = threading.Event()
        self.release = threading.Event()
        self.release_waits: list[bool] = []

    def __call__(self, config: ProviderConfig) -> ResolvedProviderConnection:
        thread = threading.current_thread().name
        self.calls.append((thread, config.name))
        if thread == GRAPH_WORKER and not self.entered.is_set():
            self.entered.set()
            self.ready.set()
            self.release_waits.append(self.release.wait(5))
        return self.outcome(config)

    def providers(self, thread: str | None = None) -> list[str]:
        return [name for caller, name in self.calls if thread is None or caller == thread]


class ObservedSettings(ModelSettings):
    """Real managed settings that records each delegated write."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.writes: list[CatalogDocument] = []

    def write(self, document: CatalogDocument) -> None:
        self.writes.append(document)
        super().write(document)


@dataclass(frozen=True)
class Observed:
    view: CatalogView
    settings_bytes: bytes
    writes: int
    discovery_calls: int


@dataclass
class GraphFixture:
    registry: ModelRegistry
    settings: ObservedSettings
    discovery: FakeDiscovery
    boundary: CredentialBoundary

    def reset(self) -> None:
        self.settings.writes.clear()
        self.discovery.calls.clear()
        self.boundary.calls.clear()

    def observe(self) -> Observed:
        return Observed(
            self.registry.view(),
            self.settings.path.read_bytes(),
            len(self.settings.writes),
            len(self.discovery.calls),
        )

    def refresh_with(self, *, graph: object, main: tuple[ModelRecord, ...] = (MAIN_RECORD, SIBLING_RECORD)) -> None:
        self.discovery.responses["p"] = inventory("p", main)
        self.discovery.responses["g"] = graph
        self.registry.refresh()


def build(tmp_path: Path, *, remembered: str | None = None) -> GraphFixture:
    providers = (provider("g"), provider("p"))
    settings = ObservedSettings(tmp_path / "models-config.toml")
    settings.write(
        CatalogDocument(
            1,
            3,
            remembered,
            tuple(ProviderState(item.name, item.type, SEEDED_AT, "fresh") for item in providers),
            tuple(sorted((MAIN_RECORD, SIBLING_RECORD, GRAPH_RECORD), key=lambda item: item.id)),
        )
    )
    discovery = FakeDiscovery()
    boundary = CredentialBoundary()
    registry = ModelRegistry({item.name: item for item in providers}, settings, discovery, boundary)
    fixture = GraphFixture(registry, settings, discovery, boundary)
    fixture.reset()
    return fixture


_REMEMBERED = {"unselected": None, "remembered-missing": "p/gone", "active-main": MAIN_RECORD.id}
_STATUS = {
    "unselected": "no_selection",
    "remembered-missing": "saved_selection_unavailable",
    "active-main": "active",
}


def arrange(tmp_path: Path, main_state: str, freshness: str) -> GraphFixture:
    """Drive startup-like state through the public refresh and restore entries."""
    fixture = build(tmp_path, remembered=_REMEMBERED[main_state])
    graph = inventory("g", (GRAPH_RECORD,)) if freshness == "fresh" else OSError("fake provider outage")
    fixture.refresh_with(graph=graph)
    fixture.registry.restore_selection()
    fixture.reset()
    view = fixture.registry.view()
    assert view.selection_status == _STATUS[main_state]
    assert GRAPH_RECORD in view.models
    assert {state.provider_id: state.status for state in view.providers}["g"] == freshness
    return fixture


def resolve_graph_during(
    fixture: GraphFixture, selector: object, mutate: Callable[[], object]
) -> tuple[object, object]:
    """Run ``mutate`` while the graph worker's credential resolution is held open."""
    result: list[object] = []
    boundary = fixture.boundary

    def work() -> None:
        try:
            result.append(fixture.registry.resolve_graph_model(selector))  # type: ignore[arg-type]
        except Exception as error:
            result.append(error)
        finally:
            boundary.ready.set()

    worker = threading.Thread(target=work, name=GRAPH_WORKER)
    worker.start()
    try:
        assert boundary.ready.wait(5)
        if not boundary.entered.is_set():
            worker.join(5)
            if result and isinstance(result[0], BaseException):
                raise result[0]
            raise AssertionError("graph worker never reached credential resolution")
        mutation = mutate()
    finally:
        boundary.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert boundary.release_waits == [True]
    assert len(result) == 1
    return result[0], mutation


@pytest.mark.parametrize("freshness", ["fresh", "stale"])
@pytest.mark.parametrize("main_state", ["unselected", "remembered-missing", "active-main"])
def test_graph_selector_resolves_exact_record_regardless_of_main_state(
    tmp_path: Path, main_state: str, freshness: str
) -> None:
    fixture = arrange(tmp_path, main_state, freshness)
    before = fixture.observe()

    pin = fixture.registry.resolve_graph_model("g/org/graph-model")

    assert isinstance(pin, RunModel)
    assert pin.model == GRAPH_RECORD
    assert pin.model.upstream_id == "org/graph-model"
    assert pin.connection == resolved(provider("g"))
    assert (pin.catalog_revision, pin.selection_revision) == (before.view.revision, before.view.selection_revision)
    assert fixture.boundary.providers() == ["g"]
    assert fixture.observe() == before


def test_graph_pin_is_not_retargeted_by_later_main_change_or_target_removal(tmp_path: Path) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")

    pin = fixture.registry.resolve_graph_model(GRAPH_RECORD.id)
    fixture.registry.select(SIBLING_RECORD.id)
    fixture.refresh_with(graph=inventory("g", ()))

    assert GRAPH_RECORD not in fixture.registry.view().models
    assert pin.model == GRAPH_RECORD
    assert pin.connection == resolved(provider("g"))
    with pytest.raises(FrozenInstanceError):
        pin.model = SIBLING_RECORD  # type: ignore[misc]


@pytest.mark.parametrize("entry", ["admit", "admit_scheduled"])
@pytest.mark.parametrize("main_state", ["unselected", "remembered-missing"])
def test_graph_resolution_does_not_open_ordinary_admission(tmp_path: Path, entry: str, main_state: str) -> None:
    """Graph resolution never activates Main: implicit and inheriting scheduled admission stay closed."""
    fixture = arrange(tmp_path, main_state, "fresh")
    ordinary = (
        fixture.registry.admit if entry == "admit"
        else fixture.registry.admit_scheduled  # inheriting (absent selector) needs Main; explicit does not (8.1)
    )
    with pytest.raises(ModelRegistryError) as before:
        ordinary()
    assert before.value.code == "no_active_selection"
    assert fixture.boundary.providers() == []

    fixture.registry.resolve_graph_model(GRAPH_RECORD.id)

    with pytest.raises(ModelRegistryError) as after:
        ordinary()
    assert after.value.code == "no_active_selection"
    assert fixture.boundary.providers() == ["g"]


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(" g/org/graph-model", id="leading-space"),
        pytest.param("g/org/graph-model\n", id="trailing-newline"),
        pytest.param("G/org/graph-model", id="case-alias"),
        pytest.param("org/graph-model", id="upstream-alias"),
        pytest.param("g/org/unknown", id="unknown-canonical"),
        pytest.param("g/sk-graphselectorsecret999", id="secretish-unknown"),
    ],
)
def test_graph_selector_not_exactly_in_catalog_is_model_not_found(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, selector: str
) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    before = fixture.observe()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ModelRegistryError) as error:
        fixture.registry.resolve_graph_model(selector)

    assert error.value.code == "model_not_found"
    assert fixture.boundary.providers() == []
    assert fixture.observe() == before
    if selector.strip():
        assert selector.strip() not in str(error.value)
        assert selector.strip() not in caplog.text


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param(None, id="none"),
        pytest.param(True, id="bool"),
        pytest.param(0, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(b"g/org/graph-model", id="bytes"),
        pytest.param(["g/org/graph-model"], id="list"),
        pytest.param({"id": "g/org/graph-model"}, id="dict"),
    ],
)
def test_graph_selector_of_wrong_type_is_invalid_request(tmp_path: Path, selector: object) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    before = fixture.observe()

    with pytest.raises(ModelRegistryError) as error:
        fixture.registry.resolve_graph_model(selector)  # type: ignore[arg-type]

    assert error.value.code == "invalid_request"
    assert fixture.boundary.providers() == []
    assert fixture.observe() == before


def _raise_private(config: ProviderConfig) -> ResolvedProviderConnection:
    raise RuntimeError("vault sk-privatecredential123 token: hunter2")


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_raise_private, id="resolver-raises"),
        pytest.param(lambda c: dataclass_replace(resolved(c), provider_id="p"), id="wrong-provider"),
        pytest.param(lambda c: dataclass_replace(resolved(c), adapter="anthropic"), id="wrong-adapter"),
        pytest.param(lambda c: dataclass_replace(resolved(c), base_url="https://wrong.invalid"), id="wrong-endpoint"),
        pytest.param(lambda c: dataclass_replace(resolved(c), request_timeout=99), id="wrong-timeout"),
    ],
)
def test_unusable_graph_credential_resolution_fails_typed_once(
    tmp_path: Path, outcome: Callable[[ProviderConfig], ResolvedProviderConnection]
) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    fixture.boundary.outcome = outcome
    before = fixture.observe()

    with pytest.raises(ModelRegistryError) as error:
        fixture.registry.resolve_graph_model(GRAPH_RECORD.id)

    assert error.value.code == "credential_resolution_failed"
    for detail in ("sk-privatecredential123", "hunter2", "resolved-secret", "provider.invalid"):
        assert detail not in str(error.value)
    assert fixture.boundary.providers() == ["g"]
    assert fixture.observe() == before


@pytest.mark.parametrize(
    "mutation",
    ["graph-record-metadata-changed", "graph-record-removed"],
)
def test_graph_target_change_during_credential_resolution_fails_connection_changed(
    tmp_path: Path, mutation: str
) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    observed: list[Observed] = []

    def mutate() -> None:
        if mutation == "graph-record-metadata-changed":
            fixture.refresh_with(graph=inventory("g", (record("org/graph-model", "g", context=200),)))
        else:
            fixture.refresh_with(graph=inventory("g", ()))
        observed.append(fixture.observe())

    result, _ = resolve_graph_during(fixture, GRAPH_RECORD.id, mutate)

    assert isinstance(result, ModelRegistryError)
    assert result.code == "connection_changed"
    assert "resolved-secret" not in str(result)
    assert fixture.boundary.providers(GRAPH_WORKER) == ["g"]
    assert fixture.observe() == observed[0]


@pytest.mark.parametrize(
    ("mutation", "preselect"),
    [
        pytest.param("main-selected", False, id="main-selected"),
        pytest.param("main-switched", True, id="main-switched"),
        pytest.param("catalog-refreshed-same-graph-record", True, id="catalog-refreshed-same-graph-record"),
    ],
)
def test_unrelated_change_during_credential_resolution_keeps_graph_pin_with_current_provenance(
    tmp_path: Path, mutation: str, preselect: bool
) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    if preselect:
        fixture.registry.select(MAIN_RECORD.id)
        fixture.reset()
    before = fixture.observe()
    observed: list[Observed] = []

    def mutate() -> None:
        if mutation == "main-selected":
            fixture.registry.select(MAIN_RECORD.id)
        elif mutation == "main-switched":
            fixture.registry.select(SIBLING_RECORD.id)
        else:
            fixture.refresh_with(graph=inventory("g", (GRAPH_RECORD,)))
        observed.append(fixture.observe())

    pin, _ = resolve_graph_during(fixture, GRAPH_RECORD.id, mutate)

    current = observed[0].view
    assert (current.revision, current.selection_revision) != (before.view.revision, before.view.selection_revision)
    assert isinstance(pin, RunModel)
    assert pin.model == GRAPH_RECORD
    assert pin.connection == resolved(provider("g"))
    assert (pin.catalog_revision, pin.selection_revision) == (current.revision, current.selection_revision)
    assert fixture.boundary.providers(GRAPH_WORKER) == ["g"]
    assert fixture.observe() == observed[0]


def test_graph_credential_resolution_runs_outside_registry_lock(tmp_path: Path) -> None:
    fixture = arrange(tmp_path, "unselected", "fresh")
    selections: list[object] = []
    selector_thread = threading.Thread(
        target=lambda: selections.append(fixture.registry.select(MAIN_RECORD.id)), name="main-select"
    )

    def mutate() -> bool:
        selector_thread.start()
        selector_thread.join(2)
        finished_while_blocked = not selector_thread.is_alive() and not fixture.boundary.release.is_set()
        return finished_while_blocked

    try:
        pin, finished_while_blocked = resolve_graph_during(fixture, GRAPH_RECORD.id, mutate)
    finally:
        if selector_thread.is_alive():
            selector_thread.join(5)

    assert not selector_thread.is_alive()
    assert finished_while_blocked is True
    assert len(selections) == 1
    assert isinstance(pin, RunModel)
    assert pin.model == GRAPH_RECORD
    assert fixture.registry.view().active_model_id == MAIN_RECORD.id


def test_repeated_graph_resolution_leaves_main_and_managed_settings_untouched(tmp_path: Path) -> None:
    fixture = build(tmp_path, remembered=MAIN_RECORD.id)
    before = fixture.observe()
    assert before.view.selection_status == "saved_selection_unavailable"
    assert before.view.active_model_id is None

    pins = [fixture.registry.resolve_graph_model(GRAPH_RECORD.id) for _ in range(3)]

    assert [pin.model for pin in pins] == [GRAPH_RECORD] * 3
    assert fixture.observe() == before
    assert fixture.settings.writes == []
    assert fixture.discovery.calls == []
    assert fixture.boundary.providers() == ["g", "g", "g"]
    assert fixture.registry.restore_selection().active_model_id == MAIN_RECORD.id
