"""Tests for the versioned managed model settings codec."""

from dataclasses import replace
import json
from pathlib import Path
from typing import Callable

import pytest

import model_settings
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
    SupplementRecord,
)
from model_settings import ModelSettings, ModelSettingsError
from model_settings import _document_from_mapping, _document_to_toml, _is_json_container


def sample_document() -> CatalogDocument:
    return CatalogDocument(
        schema_version=1,
        revision=3,
        default_model="router/vendor/model/revision",
        providers=(ProviderState("router", "openrouter", "2026-01-01T00:00:00Z", "stale", "rate_limited"),),
        models=(
            ModelRecord(
                "router/vendor/model/revision",
                "router",
                "vendor/model/revision",
                "Vendor Model",
                ModelMetadata(
                    100,
                    90,
                    10,
                    False,
                    None,
                    True,
                    (("context_tokens", "provider"), ("input_tokens", "provider"),
                     ("output_tokens", "provider"), ("reasoning", "models.dev"),
                     ("vision", "provider")),
                ),
            ),
        ),
        supplemental=(
            SupplementRecord(
                "openrouter",
                "vendor/model/revision",
                "2026-01-02T00:00:00Z",
                ModelMetadata(100, None, None, False, sources=(("context_tokens", "models.dev"), ("vision", "models.dev"))),
            ),
        ),
    )


def test_round_trip_preserves_metadata_unknown_false_and_exact_upstream_id(tmp_path: Path) -> None:
    settings = ModelSettings(tmp_path / "models-config.toml")
    document = sample_document()

    settings.write(document)

    result = settings.load()
    assert result.status == "valid"
    assert result.document == document
    assert result.document is not None
    assert result.document.models[0].upstream_id == "vendor/model/revision"
    assert result.document.models[0].metadata.vision is False
    assert result.document.models[0].metadata.tool_calling is None
    assert ("reasoning", "models.dev") in result.document.models[0].metadata.sources


def test_missing_file_does_not_invent_selection(tmp_path: Path) -> None:
    result = ModelSettings(tmp_path / "missing.toml").load()

    assert result.status == "missing"
    assert result.document is None


def test_unavailable_remembered_model_id_is_preserved(tmp_path: Path) -> None:
    document = CatalogDocument(1, 2, "deleted/old/model", (), (), ())
    settings = ModelSettings(tmp_path / "models.toml")

    settings.write(document)

    assert settings.load().document == document


@pytest.mark.parametrize(
    "content",
    [
        "schema_version = 1\nrevision = true\n",
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nbase_url = "https://secret.invalid"\n',
        'schema_version = 1\nrevision = 1\nextra = "raw response"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "{ \\"data\\": [ { \\"id\\": \\"m\\" } ] }"\n',
        'schema_version = 1\nrevision = 1\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "env:API_KEY"\n',
        'schema_version = 1\nrevision = 1\n[[models]]\nid = "p/model"\nprovider_id = "other"\nupstream_id = "model"\ndisplay_name = "Name"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\ncontext_tokens = 0\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\nvision = "false"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Bearer abcdefghijklmnop"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\nvision = true\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\nvision = false\n[models.sources]\nvision = "not-a-source"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\ncontext_tokens = -1\nsources.context_tokens = "provider"\n',
        'schema_version = 2\nrevision = 1\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\ncredential = "env:API_KEY"\n',
        'schema_version = 1\nrevision = 1\n[[supplemental]]\nadapter = "openai"\nupstream_id = "https://example.invalid/model"\nfetched_at = "2026-01-01T00:00:00Z"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Duplicate"\n',
        'schema_version = 1\nrevision = 1\ndefault_model = "not-canonical"\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\nlast_success_at = "2026-01-01T00:00:00Z"\n[[providers.p.models]]\n',
    ],
)
def test_invalid_or_unsafe_managed_data_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "models.toml"
    path.write_text(content, encoding="utf-8")
    settings = ModelSettings(path)

    result = settings.load()

    assert result.status == "corrupt"


def test_corrupt_input_is_preserved_exactly_before_any_write(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    original = b"schema_version = 1\nrevision = 1\nprivate = 'do not expose'\n"
    path.write_bytes(original)

    result = ModelSettings(path).load()

    assert result.status == "corrupt"
    assert result.preserved_path is not None
    assert Path(result.preserved_path).read_bytes() == original
    assert path.read_bytes() == original
    assert "do not expose" not in repr(result)


def test_preservation_failure_prevents_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "models.toml"
    original = b"invalid = true\n"
    path.write_bytes(original)
    monkeypatch.setattr(model_settings.shutil, "copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret detail")))

    with pytest.raises(ModelSettingsError) as error:
        ModelSettings(path).load()

    assert error.value.code == "preserve_failed"
    assert "secret detail" not in str(error.value)
    assert path.read_bytes() == original


def test_direct_write_preserves_corrupt_file_before_replacing(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    original = b"invalid = true\nraw bytes\n"
    path.write_bytes(original)

    ModelSettings(path).write(sample_document())

    assert path.read_bytes() != original
    assert (tmp_path / "models.toml.corrupt").read_bytes() == original


def test_write_after_failed_load_preservation_stays_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "models.toml"
    original = b"invalid = true\n"
    path.write_bytes(original)
    settings = ModelSettings(path)
    monkeypatch.setattr(model_settings.shutil, "copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")))
    with pytest.raises(ModelSettingsError, match="preserve failed"):
        settings.load()

    monkeypatch.undo()
    with pytest.raises(ModelSettingsError) as error:
        settings.write(sample_document())

    assert error.value.code == "preserve_failed"
    assert path.read_bytes() == original


def test_known_metadata_requires_provenance_in_serialized_and_loaded_documents() -> None:
    document = sample_document()
    model = document.models[0]
    missing_source = ModelRecord(
        model.id,
        model.provider_id,
        model.upstream_id,
        model.display_name,
        ModelMetadata(100, 90, 10, False, None, True, (("context_tokens", "provider"),)),
    )
    invalid = CatalogDocument(1, 3, document.default_model, document.providers, (missing_source,), document.supplemental)
    settings = ModelSettings(Path("unused.toml"))

    with pytest.raises(ModelSettingsError) as error:
        settings.write(invalid)
    assert error.value.code == "invalid_provenance"

    with pytest.raises(ModelSettingsError) as error:
        _document_from_mapping(
            {
                "schema_version": 1,
                "revision": 1,
                "providers": {"p": {"adapter": "openai", "status": "fresh"}},
                "models": [{"id": "p/model", "provider_id": "p", "upstream_id": "model", "display_name": "M", "vision": True}],
            }
        )
    assert error.value.code == "invalid_provenance"


def test_supplemental_metadata_has_intrinsic_models_dev_provenance(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        'schema_version = 1\nrevision = 1\n[[supplemental]]\nadapter = "openai"\nupstream_id = "model"\nfetched_at = "2026-01-01T00:00:00Z"\ncontext_tokens = 100\n',
        encoding="utf-8",
    )

    result = ModelSettings(path).load()

    assert result.status == "valid"
    assert result.document is not None
    assert result.document.supplemental[0].metadata.sources == (("context_tokens", "models.dev"),)


def test_json_like_upstream_id_remains_exact_and_is_not_restricted(tmp_path: Path) -> None:
    upstream = '{ "revision": 1 }'
    model_id = f"p/{upstream}"
    document = CatalogDocument(
        1,
        1,
        model_id,
        (ProviderState("p", "openai", None, "fresh"),),
        (ModelRecord(model_id, "p", upstream, "Ordinary model name"),),
        (),
    )

    settings = ModelSettings(tmp_path / "models.toml")
    settings.write(document)

    assert settings.load().document == document


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("context_tokens", True, "invalid_context_tokens"),
        ("input_tokens", 0, "invalid_input_tokens"),
        ("output_tokens", -1, "invalid_output_tokens"),
        ("vision", "true", "invalid_metadata"),
    ],
)
def test_metadata_scalar_types_and_positive_limits_have_specific_safe_errors(
    field: str, value: object, expected_code: str
) -> None:
    with pytest.raises(ModelSettingsError) as error:
        _document_from_mapping(
            {
                "schema_version": 1,
                "revision": 1,
                "providers": {"p": {"adapter": "openai", "status": "fresh"}},
                "models": [
                    {
                        "id": "p/model",
                        "provider_id": "p",
                        "upstream_id": "model",
                        "display_name": "Model",
                        field: value,
                        "sources": {field: "provider"},
                    }
                ],
            }
        )
    assert error.value.code == expected_code


def test_unsupported_schema_version_has_specific_safe_error() -> None:
    with pytest.raises(ModelSettingsError) as error:
        _document_from_mapping({"schema_version": 2, "revision": 1})
    assert error.value.code == "unsupported_version"


def test_deep_json_display_name_load_is_corrupt_and_preserved_exactly(tmp_path: Path) -> None:
    candidate = replace(
        sample_document(),
        models=(replace(sample_document().models[0], display_name="[" * 2000 + "0" + "]" * 2000),),
    )
    original = _document_to_toml(candidate).encode("utf-8")
    path = tmp_path / "models.toml"
    path.write_bytes(original)

    result = ModelSettings(path).load()

    assert result.status == "corrupt"
    assert result.document is None
    assert result.preserved_path is not None
    assert result.preserved_path.read_bytes() == original
    assert path.read_bytes() == original


def test_deep_json_display_name_write_fails_before_touching_valid_settings(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    settings = ModelSettings(path)
    settings.write(sample_document())
    original = path.read_bytes()
    baseline = sample_document()
    candidate = replace(
        baseline,
        models=(replace(baseline.models[0], display_name="[" * 2000 + "0" + "]" * 2000),),
    )

    with pytest.raises(ModelSettingsError) as error:
        settings.write(candidate)

    assert error.value.code in {"unsafe_display_name", "unsafe_json_value"}
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", [RecursionError, ValueError])
def test_json_decoder_resource_limit_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    def fail_decode(_decoder: json.JSONDecoder, _value: str) -> tuple[object, int]:
        raise failure("decoder limit")

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", fail_decode)

    with pytest.raises(ModelSettingsError) as error:
        _is_json_container("[1]")

    assert error.value.code == "unsafe_json_value"
    assert "decoder limit" not in str(error.value)


def test_replace_failure_leaves_previous_settings_and_cleans_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "models.toml"
    path.write_text("schema_version = 1\nrevision = 1\n", encoding="utf-8")
    monkeypatch.setattr(model_settings.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("secret detail")))

    with pytest.raises(ModelSettingsError) as error:
        ModelSettings(path).write(sample_document())

    assert error.value.code == "write_failed"
    assert "secret detail" not in str(error.value)
    assert path.read_text(encoding="utf-8") == "schema_version = 1\nrevision = 1\n"
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_write_never_touches_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text("keep = true\n", encoding="utf-8")

    with pytest.raises(ModelSettingsError):
        ModelSettings(path).write(CatalogDocument(1, -1, None, (), (), ()))

    assert path.read_text(encoding="utf-8") == "keep = true\n"


# ---------------------------------------------------------------------------
# Read-only mode (task 12.17): saved-catalog consumers never repair or write
# ---------------------------------------------------------------------------

_READ_ONLY_LEAK = "sk-readonlyleak12345"


def _tree(root: Path) -> dict[str, bytes | None]:
    """Snapshot every entry below ``root`` with file bytes, without Path.read_bytes."""
    snapshot: dict[str, bytes | None] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_file():
            with entry.open("rb") as stream:
                snapshot[entry.relative_to(root).as_posix()] = stream.read()
        else:
            snapshot[entry.relative_to(root).as_posix()] = None
    return snapshot


def _refuse(name: str) -> Callable[..., None]:
    """Return a callable that fails the test if a forbidden write primitive runs."""
    def refused(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"read-only settings must not call {name}")
    return refused


class TestReadOnlyModelSettings:
    """``ModelSettings(path, read_only=True)`` reads one snapshot and never writes."""

    def test_valid_settings_decode_without_filesystem_changes(self, tmp_path: Path) -> None:
        """A valid file yields its decoded document and leaves the directory untouched."""
        path = tmp_path / "models.toml"
        ModelSettings(path).write(sample_document())
        before = _tree(tmp_path)

        result = ModelSettings(path, read_only=True).load()

        assert result.status == "valid"
        assert result.document == sample_document()
        assert result.preserved_path is None
        assert _tree(tmp_path) == before
        with pytest.raises(TypeError):
            ModelSettings(path, True)  # type: ignore[misc]

    @pytest.mark.parametrize("parent_exists", [True, False], ids=["file-missing", "parent-missing"])
    def test_missing_settings_create_nothing(self, tmp_path: Path, parent_exists: bool) -> None:
        """Missing settings return no document and create neither file nor parent."""
        parent = tmp_path if parent_exists else tmp_path / "absent"
        path = parent / "models.toml"

        result = ModelSettings(path, read_only=True).load()

        assert (result.status, result.document, result.preserved_path) == ("missing", None, None)
        assert _tree(tmp_path) == {}

    @pytest.mark.parametrize(
        "content",
        [
            f"schema_version = 1\nrevision = [\n# {_READ_ONLY_LEAK}\n".encode(),
            f'schema_version = 2\nrevision = 1\nnote = "{_READ_ONLY_LEAK}"\n'.encode(),
            b"\xff\xfe not utf-8",
        ],
        ids=["toml-syntax", "invalid-schema", "invalid-utf8"],
    )
    def test_corrupt_settings_are_reported_without_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
    ) -> None:
        """Corrupt bytes stay in place with no sibling backup; default mode still preserves."""
        path = tmp_path / "models.toml"
        path.write_bytes(content)
        with monkeypatch.context() as patched:
            patched.setattr(model_settings.shutil, "copy2", _refuse("shutil.copy2"))
            result = ModelSettings(path, read_only=True).load()

        assert (result.status, result.document, result.preserved_path) == ("corrupt", None, None)
        assert _tree(tmp_path) == {"models.toml": content}
        assert _READ_ONLY_LEAK not in repr(result)

        default = ModelSettings(path).load()
        assert default.status == "corrupt"
        assert default.preserved_path is not None
        assert default.preserved_path.read_bytes() == content

    def test_first_read_is_one_stable_snapshot_and_new_instances_reread(self, tmp_path: Path) -> None:
        """Repeated read-only loads reuse the first snapshot; defaults and new instances reread."""
        path = tmp_path / "models.toml"
        original = sample_document()
        replacement = replace(original, revision=original.revision + 1, default_model=None)
        writer = ModelSettings(path)
        writer.write(original)
        reader = ModelSettings(path, read_only=True)
        default_reader = ModelSettings(path)
        assert reader.load().document == original
        assert default_reader.load().document == original

        writer.write(replacement)

        assert reader.load().document == original
        assert default_reader.load().document == replacement
        assert ModelSettings(path, read_only=True).load().document == replacement

    def test_missing_snapshot_stays_missing_after_external_creation(self, tmp_path: Path) -> None:
        """A first missing result is the instance's snapshot even if the file appears later."""
        path = tmp_path / "models.toml"
        reader = ModelSettings(path, read_only=True)
        assert reader.load().status == "missing"

        ModelSettings(path).write(sample_document())

        assert reader.load().status == "missing"
        assert ModelSettings(path, read_only=True).load().document == sample_document()

    @pytest.mark.parametrize(
        ("existing", "parent_exists", "document"),
        [
            pytest.param(True, True, sample_document(), id="existing-valid-document"),
            pytest.param(False, False, sample_document(), id="missing-parent"),
            pytest.param(True, True, CatalogDocument(1, -1, None, (), (), ()), id="invalid-document"),
        ],
    )
    def test_write_is_refused_before_any_validation_or_filesystem_effect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        existing: bool,
        parent_exists: bool,
        document: CatalogDocument,
    ) -> None:
        """Read-only write raises ``read_only`` first and touches no file, temp or parent."""
        path = (tmp_path if parent_exists else tmp_path / "absent") / "models.toml"
        if existing:
            ModelSettings(path).write(replace(sample_document(), revision=9))
        before = _tree(tmp_path)
        settings = ModelSettings(path, read_only=True)
        with monkeypatch.context() as patched:
            patched.setattr(model_settings.tempfile, "mkstemp", _refuse("tempfile.mkstemp"))
            patched.setattr(model_settings.shutil, "copy2", _refuse("shutil.copy2"))
            with pytest.raises(ModelSettingsError) as error:
                settings.write(document)

        assert error.value.code == "read_only"
        assert _tree(tmp_path) == before
        expected = "valid" if existing else "missing"
        assert settings.load().status == expected

    def test_read_failure_is_safe_and_performs_no_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OS read error becomes ``read_failed`` with no raw detail and no filesystem change."""
        path = tmp_path / "models.toml"
        ModelSettings(path).write(sample_document())
        before = _tree(tmp_path)
        real_read_bytes = Path.read_bytes

        def denied(self: Path) -> bytes:
            if self == path:
                raise PermissionError(13, f"Permission denied {_READ_ONLY_LEAK}")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", denied)
        with pytest.raises(ModelSettingsError) as error:
            ModelSettings(path, read_only=True).load()

        assert error.value.code == "read_failed"
        assert _READ_ONLY_LEAK not in str(error.value)
        assert error.value.__cause__ is None
        assert _tree(tmp_path) == before
