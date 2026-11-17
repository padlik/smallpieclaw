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
                ),
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
    assert result.document.models[0].metadata.reasoning is True


def test_missing_file_does_not_invent_selection(tmp_path: Path) -> None:
    result = ModelSettings(tmp_path / "missing.toml").load()

    assert result.status == "missing"
    assert result.document is None


def test_unavailable_remembered_model_id_is_preserved(tmp_path: Path) -> None:
    document = CatalogDocument(1, 2, "deleted/old/model", (), ())
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
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "Name"\ncontext_tokens = -1\nsources.context_tokens = "provider"\n',
        'schema_version = 2\nrevision = 1\n',
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\ncredential = "env:API_KEY"\n',
        'schema_version = 1\nrevision = 1\nsupplemental = "not-a-list"\n',
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


def test_retired_supplement_and_provenance_keys_are_discarded_and_never_rewritten(tmp_path: Path) -> None:
    """The retired-file reader accepts old supplement/provenance rows but drops them.

    Provider-reported values survive; nothing about ``models.dev`` or per-field
    provenance is decoded into the document or written back on the next save.
    """
    path = tmp_path / "models.toml"
    path.write_text(
        'schema_version = 1\nrevision = 1\n[providers.p]\nadapter = "openai"\nstatus = "fresh"\n'
        '[[models]]\nid = "p/model"\nprovider_id = "p"\nupstream_id = "model"\ndisplay_name = "M"\n'
        'context_tokens = 100\n[models.sources]\ncontext_tokens = "provider"\n'
        '[[supplemental]]\nadapter = "openai"\nupstream_id = "model"\nfetched_at = "2026-01-01T00:00:00Z"\n'
        'vision = true\n[supplemental.sources]\nvision = "models.dev"\n',
        encoding="utf-8",
    )
    settings = ModelSettings(path)

    result = settings.load()

    assert result.status == "valid" and result.document is not None
    assert result.document.models[0].metadata == ModelMetadata(context_tokens=100)
    assert not hasattr(result.document, "supplemental")
    settings.write(result.document)
    rewritten = path.read_text(encoding="utf-8")
    assert "models.dev" not in rewritten and "supplemental" not in rewritten and "sources" not in rewritten
    assert 'context_tokens = 100' in rewritten


def test_json_like_upstream_id_remains_exact_and_is_not_restricted(tmp_path: Path) -> None:
    upstream = '{ "revision": 1 }'
    model_id = f"p/{upstream}"
    document = CatalogDocument(
        1,
        1,
        model_id,
        (ProviderState("p", "openai", None, "fresh"),),
        (ModelRecord(model_id, "p", upstream, "Ordinary model name"),),
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
        ModelSettings(path).write(CatalogDocument(1, -1, None, (), ()))

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
            pytest.param(True, True, CatalogDocument(1, -1, None, (), ()), id="invalid-document"),
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


# ---------------------------------------------------------------------------
# Durable selection preference (task 3.3; provider-model-catalog, ADR-0040)
# ---------------------------------------------------------------------------

_PREF_CANARY_KEY = "sk-prefcanary0123456789"
_PREF_CANARY_URL = "https://pref-canary.invalid/v1"


def _failing(name: str) -> Callable[..., None]:
    """Return an OS-boundary stand-in that fails with a canary detail."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"{name} {_PREF_CANARY_KEY}")

    return fail


@pytest.mark.parametrize("stage", ["fsync", "replace"])
@pytest.mark.parametrize("prior", [b'model = "p/first"\n', None], ids=["existing", "absent"])
def test_preference_write_before_publish_keeps_prior_bytes_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, prior: bytes | None
) -> None:
    """A failed fsync/replace keeps prior bytes (or absence) and leaves no temp file."""
    from model_settings import ModelPreference

    path = tmp_path / "model-selection.toml"
    if prior is not None:
        path.write_bytes(prior)
    monkeypatch.setattr(model_settings.os, stage, _failing(stage))

    with pytest.raises(ModelSettingsError) as error:
        ModelPreference(path).write("p/second")

    assert error.value.code == "write_failed"
    assert _PREF_CANARY_KEY not in str(error.value) and error.value.__cause__ is None
    if prior is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == prior
    assert [item.name for item in tmp_path.iterdir()] == ([path.name] if prior else [])


def test_preference_wire_is_exactly_one_selector_without_credentials(tmp_path: Path) -> None:
    """Only `model = "<conn>/<upstream>"` is written; unsafe selectors never touch disk."""
    from model_settings import ModelPreference

    path = tmp_path / "model-selection.toml"
    ModelPreference(path).write("router/vendor/model:free")
    text = path.read_text(encoding="utf-8")
    assert text == 'model = "router/vendor/model:free"\n'
    assert (path.stat().st_mode & 0o777) == 0o600
    loaded = ModelPreference(path).load()
    assert (loaded.status, loaded.model) == ("valid", "router/vendor/model:free")

    for unsafe in (f"p/{_PREF_CANARY_KEY}", f"p/{_PREF_CANARY_URL}", "p/sec:KEY_NAME", "p/env:KEY", "nosep", "p/"):
        with pytest.raises(ModelSettingsError) as error:
            ModelPreference(path).write(unsafe)
        assert _PREF_CANARY_KEY not in str(error.value)
        assert path.read_text(encoding="utf-8") == text
    for extra in (f'model = "p/m"\napi_key = "{_PREF_CANARY_KEY}"\n', f'model = "p/m"\nbase_url = "{_PREF_CANARY_URL}"\n'):
        path.write_text(extra, encoding="utf-8")
        assert ModelPreference(path).load().status == "corrupt"


@pytest.mark.parametrize(
    "corrupt",
    [b"model = \n", b"\xff\xfe", b'model = 3\n', b'model = "p/sk-prefcanary0123456789"\n'],
    ids=["toml", "utf8", "type", "secretish"],
)
def test_deliberate_select_replaces_corrupt_preference_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bytes
) -> None:
    """Load never rewrites/quarantines corrupt bytes; a deliberate write replaces them atomically."""
    from model_settings import ModelPreference

    path = tmp_path / "model-selection.toml"
    path.write_bytes(corrupt)
    preference = ModelPreference(path)
    for _ in range(2):
        result = preference.load()
        assert result.status == "corrupt" and result.model is None
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    assert path.read_bytes() == corrupt

    real_replace = model_settings.os.replace
    seen: list[bytes] = []

    def observing_replace(source: str, destination: object) -> None:
        seen.append(Path(destination).read_bytes())  # destination untouched until the swap
        real_replace(source, destination)

    monkeypatch.setattr(model_settings.os, "replace", observing_replace)
    preference.write("p/fixed")

    assert seen == [corrupt]
    assert path.read_bytes() == b'model = "p/fixed"\n'
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    assert preference.load().model == "p/fixed"


# ---------------------------------------------------------------------------
# Disposable advisory discovery cache (task 3.4; provider-model-catalog, ADR-0040)
# ---------------------------------------------------------------------------

_CACHE_CANARY_KEY = "sk-cachecanary0123456789"
_CACHE_CANARY_URL = "https://cache-canary.invalid/v1"
_CACHE_CANARY_REF = "sec:CACHE_CANARY_KEY"


def build() -> CatalogDocument:
    """A catalog carrying a default selection the cache must drop."""
    return CatalogDocument(
        schema_version=1,
        revision=3,
        default_model="router/vendor/model/revision",
        providers=(ProviderState("router", "openrouter", "2026-01-01T00:00:00Z", "stale", "rate_limited"),),
        models=(
            ModelRecord(
                "router/vendor/model/revision", "router", "vendor/model/revision", "Vendor Model",
                ModelMetadata(
                    100, 90, 10, False, None, True,
                ),
            ),
        ),
    )


_CACHE_WIRE = {
    "schema_version": 1,
    "providers": {
        "router": {
            "adapter": "openrouter",
            "last_success_at": "2026-01-01T00:00:00Z",
            "sanitized_last_error_code": "rate_limited",
            "status": "stale",
        }
    },
    "models": [
        {
            "id": "router/vendor/model/revision",
            "provider_id": "router",
            "upstream_id": "vendor/model/revision",
            "display_name": "Vendor Model",
            "context_tokens": 100,
            "input_tokens": 90,
            "output_tokens": 10,
            "vision": False,
            "reasoning": True,
        }
    ],
}


def test_cache_wire_is_exactly_the_whitelisted_provider_display_data(tmp_path: Path) -> None:
    """Only ids, per-provider status and provider-reported display data reach the cache file."""
    from model_settings import ModelCache

    path = tmp_path / "cache" / "models-cache.json"
    ModelCache(path).write(build())

    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == _CACHE_WIRE
    # Selection, supplement and provenance never ride in the disposable cache.
    parsed = json.loads(text)
    assert set(parsed) == {"schema_version", "providers", "models"}
    for forbidden in ('"default_model"', '"revision"', '"supplemental"', '"sources"', "models.dev"):
        assert forbidden not in text
    assert (path.stat().st_mode & 0o777) == 0o600
    assert [item.name for item in path.parent.iterdir()] == [path.name]


def test_cache_round_trip_restores_provider_candidates_without_selection(tmp_path: Path) -> None:
    from model_settings import ModelCache

    cache = ModelCache(tmp_path / "models-cache.json")
    assert cache.load().status == "missing" and cache.load().document is None
    cache.write(build())

    loaded = cache.load()

    assert loaded.status == "valid" and loaded.document is not None
    document = loaded.document
    assert document.default_model is None
    assert document.providers == build().providers
    (record,) = document.models
    original = build().models[0]
    assert (record.id, record.provider_id, record.upstream_id, record.display_name) == (
        original.id, original.provider_id, original.upstream_id, original.display_name,
    )
    assert (record.metadata.context_tokens, record.metadata.input_tokens, record.metadata.output_tokens) == (100, 90, 10)
    assert record.metadata.vision is False and record.metadata.tool_calling is None
    assert record.metadata.reasoning is True  # every cached value is provider-reported display data
    # A loaded cache document is itself acceptable to a later cache write.
    cache.write(document)
    assert json.loads((tmp_path / "models-cache.json").read_text(encoding="utf-8")) == _CACHE_WIRE


@pytest.mark.parametrize("field", ["display_name", "upstream_id"])
@pytest.mark.parametrize(
    "unsafe", [_CACHE_CANARY_KEY, _CACHE_CANARY_URL, _CACHE_CANARY_REF, "api_key: " + _CACHE_CANARY_KEY]
)
def test_cache_write_refuses_key_reference_or_endpoint_before_touching_disk(
    tmp_path: Path, field: str, unsafe: str
) -> None:
    from model_settings import ModelCache

    path = tmp_path / "models-cache.json"
    ModelCache(path).write(build())
    prior = path.read_bytes()
    document = build()
    record = document.models[0]
    if field == "display_name":
        bad = replace(record, display_name=unsafe)
    else:
        bad = replace(record, id=f"router/{unsafe}", upstream_id=unsafe)

    with pytest.raises(ModelSettingsError) as error:
        ModelCache(path).write(replace(document, models=(bad,)))

    assert _CACHE_CANARY_KEY not in str(error.value) and "cache-canary" not in str(error.value)
    assert path.read_bytes() == prior
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def _wire_with(**changes: object) -> bytes:
    wire = json.loads(json.dumps(_CACHE_WIRE))
    wire.update(changes)
    return json.dumps(wire).encode("utf-8")


def _model_wire(**changes: object) -> dict:
    return {**_CACHE_WIRE["models"][0], **changes}  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(b"{not json", id="json"),
        pytest.param(b"\xff\xfe\x00", id="utf8"),
        pytest.param(b"[]", id="shape"),
        pytest.param(b"[" * 100000, id="deep"),
        pytest.param(_wire_with(schema_version=2), id="version"),
        pytest.param(_wire_with(schema_version=True), id="version-bool"),
        pytest.param(_wire_with(api_key=_CACHE_CANARY_KEY), id="extra-key"),
        pytest.param(_wire_with(base_url=_CACHE_CANARY_URL), id="endpoint-key"),
        pytest.param(_wire_with(default_model="router/vendor/model/revision"), id="selection-in-cache"),
        pytest.param(_wire_with(models=[_model_wire(display_name=_CACHE_CANARY_URL)]), id="endpoint-value"),
        pytest.param(_wire_with(models=[_model_wire(display_name=_CACHE_CANARY_REF)]), id="reference-value"),
        pytest.param(_wire_with(models=[_model_wire(sources={"vision": "models.dev"})]), id="provenance"),
        pytest.param(_wire_with(models=[_model_wire(context_tokens=True)]), id="bool-int"),
        pytest.param(_wire_with(models=[_model_wire(context_tokens=0)]), id="nonpositive"),
        pytest.param(_wire_with(models=[_model_wire(provider_id="ghost")]), id="unknown-provider"),
        pytest.param(_wire_with(models=[_model_wire(), _model_wire()]), id="duplicate"),
        pytest.param(b'{"schema_version": 1, "schema_version": 1}', id="duplicate-key"),
        pytest.param(b'{"schema_version": NaN}', id="nan"),
    ],
)
def test_corrupt_cache_is_absent_unchanged_and_never_echoed(tmp_path: Path, corrupt: bytes) -> None:
    from model_settings import ModelCache

    path = tmp_path / "models-cache.json"
    path.write_bytes(corrupt)
    cache = ModelCache(path)

    for _ in range(2):
        result = cache.load()
        assert result.status == "corrupt" and result.document is None
        assert _CACHE_CANARY_KEY not in repr(result)

    assert path.read_bytes() == corrupt  # no rewrite, no quarantine copy
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def test_unreadable_cache_is_corrupt_not_an_exception(tmp_path: Path) -> None:
    from model_settings import ModelCache

    path = tmp_path / "models-cache.json"
    path.mkdir()  # a read failure other than "missing"

    result = ModelCache(path).load()

    assert result.status == "corrupt" and result.document is None
    assert path.is_dir() and list(path.iterdir()) == []


@pytest.mark.parametrize("stage", ["fsync", "replace"])
@pytest.mark.parametrize("prior", [b'{"schema_version": 1}\n', None], ids=["existing", "absent"])
def test_cache_write_failure_keeps_prior_bytes_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, prior: bytes | None
) -> None:
    from model_settings import ModelCache

    path = tmp_path / "models-cache.json"
    if prior is not None:
        path.write_bytes(prior)
    monkeypatch.setattr(model_settings.os, stage, _failing(stage))

    with pytest.raises(ModelSettingsError) as error:
        ModelCache(path).write(build())

    assert error.value.code == "write_failed"
    assert _PREF_CANARY_KEY not in str(error.value) and error.value.__cause__ is None
    if prior is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == prior
    assert [item.name for item in tmp_path.iterdir()] == ([path.name] if prior else [])


def test_cache_write_replaces_corrupt_bytes_atomically_without_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from model_settings import ModelCache

    path = tmp_path / "models-cache.json"
    path.write_bytes(b"{garbage")
    real_replace = model_settings.os.replace
    seen: list[bytes] = []

    def observing_replace(source: str, destination: object) -> None:
        seen.append(Path(destination).read_bytes())
        real_replace(source, destination)

    monkeypatch.setattr(model_settings.os, "replace", observing_replace)
    ModelCache(path).write(build())

    assert seen == [b"{garbage"]
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHE_WIRE
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
