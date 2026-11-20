"""Tests for immutable model catalog value records."""

from dataclasses import FrozenInstanceError, fields

import pytest

from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
    RunModel,
)


def test_records_are_frozen_and_nested_metadata_is_immutable() -> None:
    metadata = ModelMetadata(vision=True)
    record = ModelRecord("router/vendor/model", "router", "vendor/model", "Name", metadata)
    document = CatalogDocument(1, 4, (ProviderState("router", "openrouter", None, "fresh", None),), (record,))

    with pytest.raises(FrozenInstanceError):
        record.display_name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        metadata.vision = False  # type: ignore[misc]
    assert document.models == (record,)


def test_metadata_fields_are_the_six_provider_reported_values_with_unknown_defaults() -> None:
    metadata = ModelMetadata()

    assert metadata.context_tokens is None
    assert metadata.vision is None
    assert [item.name for item in fields(ModelMetadata)] == [
        "context_tokens", "input_tokens", "output_tokens", "vision", "tool_calling", "reasoning"
    ]
    assert all(getattr(metadata, item.name) is None for item in fields(ModelMetadata))


def test_canonical_identity_splits_only_first_slash() -> None:
    model = ModelRecord("router/vendor/model/revision", "router", "vendor/model/revision", "Name")

    assert model.provider_id == model.id.split("/", 1)[0]
    assert model.upstream_id == model.id.split("/", 1)[1]


def test_run_model_repr_never_includes_resolved_connection_secrets() -> None:
    secret = "never-print-this-key"
    connection = type("Resolved", (), {"api_key": secret, "base_url": "https://example.invalid"})()
    model = ModelRecord("p/model", "p", "model", "Name")
    run = RunModel(model, connection, 7, 2)

    assert secret not in repr(run)
    assert "example.invalid" not in repr(run)
