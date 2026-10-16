"""Tests for immutable model catalog value records."""

from dataclasses import FrozenInstanceError

import pytest

from model_registry import (
    CatalogDocument,
    MetadataField,
    MetadataSource,
    ModelMetadata,
    ModelRecord,
    ProviderState,
    RunModel,
    SupplementRecord,
)


def test_records_are_frozen_and_nested_metadata_is_immutable() -> None:
    original = [("vision", "provider")]
    metadata = ModelMetadata(sources=original)
    original.append(("reasoning", "models.dev"))
    record = ModelRecord("router/vendor/model", "router", "vendor/model", "Name", metadata)
    document = CatalogDocument(1, 4, record.id, (ProviderState("router", "openrouter", None, "fresh", None),), (record,), ())

    assert metadata.sources == (("vision", "provider"),)
    with pytest.raises(FrozenInstanceError):
        record.display_name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        metadata.sources += (("reasoning", "models.dev"),)  # type: ignore[misc]
    assert document.models == (record,)


def test_metadata_literals_and_unknown_defaults() -> None:
    metadata = ModelMetadata()

    assert metadata.context_tokens is None
    assert metadata.vision is None
    assert metadata.sources == ()
    assert set(MetadataField.__args__) == {  # type: ignore[attr-defined]
        "context_tokens", "input_tokens", "output_tokens", "vision", "tool_calling", "reasoning"
    }
    assert set(MetadataSource.__args__) == {"provider", "models.dev"}  # type: ignore[attr-defined]


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


def test_supplement_record_is_frozen() -> None:
    supplement = SupplementRecord("openai", "model", "2026-01-01T00:00:00Z", ModelMetadata())

    with pytest.raises(FrozenInstanceError):
        supplement.adapter = "changed"  # type: ignore[misc]
