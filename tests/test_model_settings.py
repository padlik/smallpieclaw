"""Tests for the model cache/preference codecs and the shared catalog validators."""

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
from model_settings import ModelCache, ModelSettingsError
from model_settings import _document_from_mapping, _is_json_container


def sample_document() -> CatalogDocument:
    return CatalogDocument(
        schema_version=1,
        revision=3,
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


def _model_row(**changes: object) -> dict:
    row = {"id": "p/model", "provider_id": "p", "upstream_id": "model", "display_name": "Name"}
    row.update(changes)
    return row


def _wire_document(models: list, providers: object = None) -> dict:
    return {
        "schema_version": 1,
        "providers": {"p": {"adapter": "openai", "status": "fresh"}} if providers is None else providers,
        "models": models,
    }


@pytest.mark.parametrize(
    "data",
    [
        _wire_document([_model_row(display_name='{ "data": [ { "id": "m" } ] }')]),
        _wire_document([_model_row(display_name="env:API_KEY")]),
        _wire_document([_model_row(provider_id="other")]),
        _wire_document([_model_row(context_tokens=0)]),
        _wire_document([_model_row(vision="false")]),
        _wire_document([_model_row(display_name="Bearer abcdefghijklmnop")]),
        _wire_document([_model_row(context_tokens=-1)]),
        {"schema_version": 2},
        _wire_document([], {"p": {"adapter": "openai", "status": "fresh", "credential": "env:API_KEY"}}),
        _wire_document([_model_row(), _model_row(display_name="Duplicate")]),
        _wire_document([], {"p": {"adapter": "openai", "base_url": "https://secret.invalid", "status": "fresh"}}),
    ],
    ids=[
        "json-display-name", "credential-ref-display-name", "unknown-provider", "zero-limit", "string-bool",
        "bearer-display-name", "negative-limit", "unsupported-version", "provider-credential-field",
        "duplicate-model", "provider-endpoint-field",
    ],
)
def test_invalid_or_unsafe_cache_data_is_corrupt_and_left_untouched(tmp_path: Path, data: dict) -> None:
    path = tmp_path / "models-cache.json"
    raw = json.dumps(data).encode()
    path.write_bytes(raw)

    assert ModelCache(path).load().status == "corrupt"
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


def test_json_like_upstream_id_remains_exact_and_is_not_restricted(tmp_path: Path) -> None:
    upstream = '{ "revision": 1 }'
    model_id = f"p/{upstream}"
    document = CatalogDocument(
        1,
        0,
        (ProviderState("p", "openai", None, "fresh"),),
        (ModelRecord(model_id, "p", upstream, "Ordinary model name"),),
    )

    cache = ModelCache(tmp_path / "models-cache.json")
    cache.write(document)

    assert cache.load().document == document


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
                "models": [_model_row(display_name="Model", **{field: value})],
            }
        )
    assert error.value.code == expected_code


@pytest.mark.parametrize("supplemental", [[], "not-a-list", [{"anything": 1}]])
def test_top_level_supplemental_is_an_unknown_field(supplemental: object) -> None:
    """The retired public-supplement key is no longer accepted by the shared decoder."""
    with pytest.raises(ModelSettingsError) as error:
        _document_from_mapping({"schema_version": 1, "revision": 0, "supplemental": supplemental})
    assert error.value.code == "unknown_field"


def test_valid_mapping_without_supplemental_still_decodes() -> None:
    document = _document_from_mapping(
        {
            "schema_version": 1,
            "revision": 0,
            "providers": {"p": {"adapter": "openai", "status": "fresh"}},
            "models": [_model_row()],
        }
    )
    assert [m.id for m in document.models] == ["p/model"]


def test_unsupported_schema_version_has_specific_safe_error() -> None:
    with pytest.raises(ModelSettingsError) as error:
        _document_from_mapping({"schema_version": 2, "revision": 1})
    assert error.value.code == "unsupported_version"


def test_deep_json_display_name_write_fails_before_touching_valid_cache(tmp_path: Path) -> None:
    path = tmp_path / "models-cache.json"
    cache = ModelCache(path)
    cache.write(sample_document())
    original = path.read_bytes()
    baseline = sample_document()
    candidate = replace(
        baseline,
        models=(replace(baseline.models[0], display_name="[" * 2000 + "0" + "]" * 2000),),
    )

    with pytest.raises(ModelSettingsError) as error:
        cache.write(candidate)

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


def test_invalid_write_never_touches_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "models-cache.json"
    path.write_text("keep = true\n", encoding="utf-8")

    with pytest.raises(ModelSettingsError):
        ModelCache(path).write(CatalogDocument(1, -1, (), ()))

    assert path.read_text(encoding="utf-8") == "keep = true\n"


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
    """A catalog document for the cache wire."""
    return CatalogDocument(
        schema_version=1,
        revision=3,
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
    assert not hasattr(document, "default_model")  # the cache never carries a selection
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
