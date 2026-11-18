"""HTTP-capture tests for provider inventory discovery and metadata merging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest

from model_discovery import ModelDiscovery, ModelDiscoveryError
from model_registry import CatalogDocument, ProviderState
from model_settings import ModelSettings


@dataclass(frozen=True)
class Connection:
    """Minimal frozen resolved-connection fixture without importing config schema."""

    provider_id: str = "account"
    adapter: str = "openai"
    base_url: str | None = None
    request_timeout: float = 3.0
    api_key: str | None = "test-secret"


def _discovery(
    handler,
    *,
    provider_headers=None,
    provider_cookies=None,
    provider_params=None,
    provider_auth=None,
):
    """Build a mocked provider client and a deterministic clock."""
    provider = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=provider_headers,
        cookies=provider_cookies,
        params=provider_params,
        auth=provider_auth,
    )
    timestamp = datetime(2026, 10, 13, tzinfo=timezone.utc)
    return ModelDiscovery(provider, lambda: timestamp)


def _write_catalog(inventory, path) -> None:
    """Prove normalized inventory records pass the unchanged managed codec."""
    document = CatalogDocument(
        providers=(ProviderState(inventory.provider_id, inventory.adapter, inventory.last_success_at, "fresh"),),
        models=inventory.models,
    )
    settings = ModelSettings(path / "models.toml")
    settings.write(document)
    assert settings.load().document == document
    persisted = settings.path.read_text(encoding="utf-8")
    assert "api_key" not in persisted
    assert "test-secret" not in persisted
    assert "https://" not in persisted


def test_openai_exact_ids_and_unsupported_metadata_remain_unknown() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/models"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.extensions["timeout"]["read"] == 3.0
        return httpx.Response(200, json={"data": [{"id": "team/model:v2", "name": "Display"}]})

    inventory = _discovery(handler).fetch(Connection())
    assert len(requests) == 1
    model = inventory.models[0]
    assert (model.id, model.upstream_id, model.display_name) == (
        "account/team/model:v2", "team/model:v2", "Display"
    )
    assert model.metadata.context_tokens is None
    assert model.metadata.vision is None
    assert inventory.last_success_at == "2026-10-13T00:00:00+00:00"


def test_anthropic_cursor_pages_and_nullable_limits() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.headers.get("x-api-key")))
        if len(seen) == 1:
            return httpx.Response(200, json={"data": [{"id": "m/a"}], "has_more": True, "last_id": "cursor"})
        return httpx.Response(200, json={"data": [{"id": "m/b", "max_input_tokens": 10,
                                                         "max_tokens": None,
                                                         "capabilities": {"image_input": {"supported": False}}}],
                                          "has_more": False, "last_id": "m/b"})

    inventory = _discovery(handler).fetch(Connection(adapter="anthropic", api_key="anthropic-key"))
    assert seen == [
        ("/v1/models", {}, "anthropic-key"),
        ("/v1/models", {"after_id": "cursor"}, "anthropic-key"),
    ]
    assert [model.id for model in inventory.models] == ["account/m/a", "account/m/b"]
    assert inventory.models[1].metadata.input_tokens == 10
    assert inventory.models[1].metadata.output_tokens is None
    assert inventory.models[1].metadata.vision is False


@pytest.mark.parametrize(
    ("supported", "expected"),
    [(None, None), (False, False), (True, True)],
)
def test_anthropic_image_supported_is_tristate(supported, expected) -> None:
    inventory = _discovery(
        lambda _: httpx.Response(200, json={"data": [{"id": "vision-model", "capabilities": {
            "image_input": {"supported": supported}
        }}], "has_more": False})
    ).fetch(Connection(adapter="anthropic"))
    assert inventory.models[0].metadata.vision is expected


@pytest.mark.parametrize("supported", [1, "true", [], {}])
def test_anthropic_image_supported_rejects_non_boolean_values(supported) -> None:
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json={"data": [{"id": "m", "capabilities": {
            "image_input": {"supported": supported}
        }}], "has_more": False})).fetch(Connection(adapter="anthropic"))
    assert error.value.code == "invalid_response"


def test_google_pages_use_exact_base_model_id_not_resource_name() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return httpx.Response(200, json={"models": [{"name": "models/gemini-latest", "baseModelId": "gemini-1.5"}],
                                             "nextPageToken": "next"})
        return httpx.Response(200, json={"models": [{"name": "models/next", "baseModelId": "exact/second",
                                                        "inputTokenLimit": 100, "outputTokenLimit": 20,
                                                        "supportedGenerationMethods": ["generateContent"]}]})

    inventory = _discovery(handler).fetch(Connection(adapter="google", api_key="google-key"))
    assert seen == [{}, {"pageToken": "next"}]
    assert [m.upstream_id for m in inventory.models] == ["gemini-1.5", "exact/second"]
    assert inventory.models[1].metadata.input_tokens == 100
    assert inventory.models[1].metadata.context_tokens is None
    assert [(item.canonical_id, item.resource_name, item.generation_methods) for item in inventory.descriptors] == [
        ("account/gemini-1.5", "models/gemini-latest", None),
        ("account/exact/second", "models/next", ("generateContent",)),
    ]
    assert inventory.descriptors[1].canonical_id == inventory.models[1].id


def test_openrouter_uses_same_origin_next_link_and_false_capability() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url, request.headers.get("authorization")))
        if len(seen) == 1:
            return httpx.Response(200, json={"data": [{"id": "x"}],
                                             "links": {"next": "/api/v1/models?offset=1&limit=2"}})
        return httpx.Response(200, json={"data": [{"id": "x/second", "context_length": 90,
                                                    "architecture": {"input_modalities": ["text"]},
                                                    "top_provider": {"max_completion_tokens": None}}]})

    inventory = _discovery(handler).fetch(Connection(adapter="openrouter"))
    assert [request[0].path for request in seen] == ["/api/v1/models", "/api/v1/models"]
    assert dict(seen[1][0].params) == {"offset": "1", "limit": "2"}
    assert inventory.models[1].metadata.context_tokens == 90
    assert inventory.models[1].metadata.vision is False
    assert inventory.models[1].metadata.output_tokens is None


def test_empty_inventory_is_valid_and_invalid_response_is_safe() -> None:
    inventory = _discovery(lambda _: httpx.Response(200, json={"data": []})).fetch(Connection())
    assert inventory.models == ()
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json={"data": "bad"})).fetch(Connection())
    assert error.value.code == "invalid_response"
    assert "test-secret" not in str(error.value)


def test_foreign_pagination_is_rejected_before_credentialed_request() -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "x"}],
                                         "links": {"next": "https://attacker.example/steal"}})

    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(handler).fetch(Connection(adapter="openrouter"))
    assert error.value.code == "unsafe_pagination_url"
    assert [request.url.host for request in captured] == ["openrouter.ai"]


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        (None, None),
        ([], ()),
        (["generateContent", "countTokens"], ("generateContent", "countTokens")),
    ],
)
def test_google_descriptor_preserves_methods_state_and_exact_resource_identity(methods, expected) -> None:
    row = {"name": "models/resource-name", "baseModelId": "exact/upstream-id"}
    if methods is not None:
        row["supportedGenerationMethods"] = methods
    inventory = _discovery(lambda _: httpx.Response(200, json={"models": [row]})).fetch(
        Connection(adapter="google")
    )
    descriptor = inventory.descriptors[0]
    assert descriptor.canonical_id == inventory.models[0].id == "account/exact/upstream-id"
    assert descriptor.resource_name == "models/resource-name"
    assert descriptor.resource_name != inventory.models[0].upstream_id
    assert descriptor.generation_methods == expected
    if isinstance(methods, list):
        methods.append("mutated-after-fetch")
        assert descriptor.generation_methods == expected


@pytest.mark.parametrize("methods", [None, "generateContent", 3, {"method": "generateContent"}, ["ok", None]])
def test_google_invalid_generation_methods_fail_complete_inventory_safely(methods) -> None:
    payload = {"models": [{"name": "models/resource", "baseModelId": "upstream",
                           "supportedGenerationMethods": methods}]}
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter="google"))
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("row", [
    {"name": "models/resource"},
    {"name": "", "baseModelId": "upstream"},
    {"name": 4, "baseModelId": "upstream"},
    {"name": "models/resource", "baseModelId": ""},
])
def test_google_invalid_resource_or_inference_identity_fails_inventory(row) -> None:
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json={"models": [row]})).fetch(
            Connection(adapter="google")
        )
    assert error.value.code == "invalid_response"


def test_google_descriptor_collections_are_immutable_and_inventory_copies_inputs() -> None:
    methods = ["generateContent"]
    from model_discovery import GoogleModelDescriptor, ProviderInventory

    descriptor = GoogleModelDescriptor("account/upstream", "models/resource", methods)
    methods.append("mutated")
    descriptors = [descriptor]
    inventory = ProviderInventory("account", "google", (), "stamp", descriptors)
    descriptors.clear()
    assert descriptor.generation_methods == ("generateContent",)
    assert inventory.descriptors == (descriptor,)
    with pytest.raises(AttributeError):
        descriptor.generation_methods.append("mutated")


@pytest.mark.parametrize("url", ["http://localhost:11434", "https://192.168.0.4", "https://other.example"])
def test_ollama_rejects_non_official_origin_before_request(url: str) -> None:
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: pytest.fail("provider request must not occur")).fetch(
            Connection(adapter="ollama", base_url=url)
        )
    assert error.value.code == "invalid_endpoint"


def test_ollama_cloud_path_auth_and_no_duplicate_api_segment() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"models": [{"name": "tag:latest"}]})

    inventory = _discovery(handler).fetch(Connection(adapter="ollama", base_url="https://ollama.com/api",
                                                      api_key="cloud-key"))
    assert len(seen) == 1
    assert seen[0].url == "https://ollama.com/api/tags"
    assert seen[0].headers["authorization"] == "Bearer cloud-key"
    assert inventory.models[0].id == "account/tag:latest"


@pytest.mark.parametrize(
    ("adapter", "base_url", "path", "payload", "expected_auth"),
    [
        ("openai", "https://api.example.test/account/v1", "/account/v1/models",
         {"data": [{"id": "m"}]}, {"authorization": "Bearer key-openai"}),
        ("anthropic", "https://api.example.test/account/v1", "/account/v1/models",
         {"data": [{"id": "m"}], "has_more": False},
         {"x-api-key": "key-anthropic", "anthropic-version": "2023-06-01"}),
        ("google", "https://api.example.test/account/v1beta", "/account/v1beta/models",
         {"models": [{"name": "models/r", "baseModelId": "m"}]},
         {"x-goog-api-key": "key-google"}),
        ("openrouter", "https://api.example.test/account/api/v1", "/account/api/v1/models",
         {"data": [{"id": "m"}]}, {"authorization": "Bearer key-openrouter"}),
        ("ollama", "https://ollama.com/api", "/api/tags",
         {"models": [{"name": "m"}]}, {"authorization": "Bearer key-ollama"}),
    ],
)
def test_provider_requests_use_only_connection_auth_and_clean_endpoint(
    adapter, base_url, path, payload, expected_auth
) -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    key = f"key-{adapter}"
    connection = Connection(adapter=adapter, base_url=base_url, api_key=key)
    inventory = _discovery(
        handler,
        provider_headers={"Authorization": "Basic default-auth", "X-Private": "fake-provider-header"},
        provider_cookies={"session": "fake-provider-cookie"},
        provider_params={"account": "fake-private-account", "api_key": "fake-param-key"},
        provider_auth=("client-user", "client-password"),
    ).fetch(connection)
    request = captured[0]
    assert request.url.path == path
    assert request.url.query == b""
    assert request.content == b""
    assert "cookie" not in request.headers
    assert "x-private" not in request.headers
    assert "fake-provider" not in str(request.headers)
    assert {key: value for key, value in request.headers.items() if key in expected_auth} == expected_auth
    assert set(request.headers) == {"accept", "host", *expected_auth}
    assert inventory.adapter == adapter


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("openai", {"data": [{"id": "same"}, {"id": "same"}]}),
        ("anthropic", {"data": [{"id": "same"}, {"id": "same"}], "has_more": False}),
        ("google", {"models": [
            {"name": "models/one", "baseModelId": "same"},
            {"name": "models/two", "baseModelId": "same"},
        ]}),
        ("openrouter", {"data": [{"id": "same"}, {"id": "same"}]}),
        ("ollama", {"models": [{"name": "same"}, {"name": "same"}]}),
    ],
)
def test_duplicate_canonical_models_in_page_fail_before_inventory(adapter, payload) -> None:
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter=adapter))
    assert error.value.code == "duplicate_model"


@pytest.mark.parametrize(
    ("adapter", "first", "second"),
    [
        ("anthropic", {"data": [{"id": "same"}], "has_more": True, "last_id": "cursor"},
         {"data": [{"id": "same"}], "has_more": False}),
        ("google", {"models": [{"name": "models/one", "baseModelId": "same"}], "nextPageToken": "next"},
         {"models": [{"name": "models/two", "baseModelId": "same"}]}),
        ("openrouter", {"data": [{"id": "same"}], "links": {"next": "/api/v1/models?offset=1"}},
         {"data": [{"id": "same"}]}),
    ],
)
def test_duplicate_canonical_models_across_pages_fail(adapter, first, second) -> None:
    pages = iter((first, second))
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=next(pages))).fetch(Connection(adapter=adapter))
    assert error.value.code == "duplicate_model"


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("openai", {"data": [{"id": "model-a", "name": "A"}]}),
        ("anthropic", {"data": [{"id": "model-a", "name": "A"}], "has_more": False}),
        ("google", {"models": [{"name": "models/model-a", "baseModelId": "model-a",
                                  "supportedGenerationMethods": []}]}),
        ("openrouter", {"data": [{"id": "model-a"}]}),
        ("ollama", {"models": [{"name": "model-a"}]}),
    ],
)
def test_successful_inventory_is_accepted_by_managed_codec(adapter, payload, tmp_path) -> None:
    inventory = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter=adapter))
    _write_catalog(inventory, tmp_path)


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("openai", {"data": [{"id": "sec:PRIVATE_KEY"}]}),
        ("openai", {"data": [{"id": "https://fake-credential.example/model"}]}),
        ("openai", {"data": [{"id": "model", "name": '{"error":"raw response fake"}'}]}),
        ("openai", {"data": [{"id": "model\nvalue"}]}),
        ("google", {"models": [{"name": "https://fake-credential.example/resource",
                                  "baseModelId": "model"}]}),
        ("google", {"models": [{"name": "models/resource", "baseModelId": "model",
                                  "supportedGenerationMethods": ["sec:PRIVATE_KEY"]}]}),
        ("google", {"models": [{"name": "models/resource", "baseModelId": "model\nvalue"}]}),
    ],
)
def test_unsafe_provider_strings_fail_with_safe_error_and_repr(adapter, payload) -> None:
    secret = "fake-credential"
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter=adapter))
    assert error.value.code == "invalid_response"
    assert secret not in str(error.value)
    assert str(payload) not in str(error.value)
    assert str(payload) not in repr(error.value)


def test_foreign_provider_redirect_is_not_followed() -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/collect"})

    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(handler).fetch(Connection())
    assert error.value.code == "provider_request_failed"
    assert [request.url.host for request in captured] == ["api.openai.com"]


def test_pagination_cycle_fails_without_partial_inventory() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "x"}], "has_more": True, "last_id": "cursor"})

    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(handler).fetch(Connection(adapter="anthropic"))
    assert error.value.code == "pagination_cycle"


_FIRST_PAGES = {
    "anthropic": {"data": [{"id": "fresh-a"}], "has_more": True, "last_id": "cursor"},
    "google": {"models": [{"name": "models/fresh-a", "baseModelId": "fresh-a"}], "nextPageToken": "next"},
    "openrouter": {"data": [{"id": "fresh-a"}], "links": {"next": "/api/v1/models?offset=1"}},
}
_BAD_SECOND_PAGES = {
    "anthropic": {"data": "not-a-list", "has_more": False},
    "google": {"models": "not-a-list"},
    "openrouter": {"data": "not-a-list"},
}


@pytest.mark.parametrize("adapter", ["anthropic", "google", "openrouter"])
@pytest.mark.parametrize("failure", ["http-503", "malformed"])
def test_second_page_failure_keeps_last_good_inventory_stale_without_partial_merge(
    adapter, failure, tmp_path
) -> None:
    """A refresh ending on an incomplete page never replaces the prior inventory."""
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_registry import ModelMetadata, ModelRecord, ModelRegistry

    cached = ModelRecord(
        "account/cached", "account", "cached", "Cached",
        ModelMetadata(100, None, None, None, None, None),
    )
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(CatalogDocument(
        1, 2, None, (ProviderState("account", adapter, "2026-09-01T00:00:00Z", "fresh"),),
        (cached,),
    ))
    pages: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request)
        if len(pages) == 1:
            return httpx.Response(200, json=_FIRST_PAGES[adapter])
        if failure == "http-503":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_BAD_SECOND_PAGES[adapter])

    def resolve(config: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(
            config.name, config.type, config.base_url, config.request_timeout, "test-secret"
        )

    registry = ModelRegistry(
        {"account": ProviderConfig("account", adapter, "env:KEY")},
        settings, _discovery(handler), resolve,
    )
    report = registry.refresh()

    assert len(pages) == 2, report
    view = registry.view()
    assert view.models == (cached,)
    assert view.providers[0].status == "stale"
    assert report.refreshed_provider_ids == ()
    assert [code for _provider, code in report.error_codes] == [view.providers[0].sanitized_last_error_code]
    assert settings.load().document.models == (cached,)
    assert "fresh-a" not in settings.path.read_text(encoding="utf-8")


def test_cloud_ollama_empty_api_key_requires_authentication_before_any_request() -> None:
    from config_schema import ResolvedProviderConnection

    provider_requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(200, json={"models": [{"name": "m"}]})

    connection = ResolvedProviderConnection("cloud", "ollama", "https://ollama.com", 3.0, "")
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(provider).fetch(connection)
    assert error.value.code == "authentication_required"
    assert provider_requests == []


@pytest.mark.parametrize("adapter", ["openai", "anthropic", "google", "openrouter", "ollama"])
def test_no_supplement_network_call_occurs(adapter: str) -> None:
    """Discovery contacts only the owning provider endpoint: no public supplement service.

    The transport is the real ``httpx.MockTransport`` boundary shared by the one
    discovery client; every request URL is checked against an allowlist and any
    other destination (``models.dev`` included) is a recorded canary failure.
    """
    allowed = {
        "openai": "https://api.openai.com/v1/models",
        "anthropic": "https://api.anthropic.com/v1/models",
        "google": "https://generativelanguage.googleapis.com/v1beta/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
        "ollama": "https://ollama.com/api/tags",
    }[adapter]
    payloads = {
        "openai": {"data": [{"id": "m"}]},
        "anthropic": {"data": [{"id": "m"}], "has_more": False},
        "google": {"models": [{"name": "models/m", "baseModelId": "m"}]},
        "openrouter": {"data": [{"id": "m"}]},
        "ollama": {"models": [{"name": "m"}]},
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) != allowed:
            return httpx.Response(200, json={"canary": "unexpected destination"})
        return httpx.Response(200, json=payloads[adapter])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    timestamp = datetime(2026, 10, 13, tzinfo=timezone.utc)
    discovery = ModelDiscovery(client, clock=lambda: timestamp)

    inventory = discovery.fetch(Connection(adapter=adapter))

    assert seen == [allowed]
    assert [model.upstream_id for model in inventory.models] == ["m"]
    assert not hasattr(inventory, "supplemental")
    assert "models.dev" not in repr(inventory)


def _registry_for(adapter, handler, tmp_path):
    """Build a real registry over the real discovery and a MockTransport provider."""
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_registry import ModelRegistry

    def resolve(config: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(
            config.name, config.type, config.base_url, config.request_timeout, "test-secret"
        )

    settings = ModelSettings(tmp_path / "models-config.toml")
    return ModelRegistry(
        {"account": ProviderConfig("account", adapter, "env:KEY")},
        settings, _discovery(handler), resolve,
    ), settings


@pytest.mark.parametrize("padded", ["Padded Name ", " Padded Name", "\tPadded Name\n"])
def test_presentation_whitespace_does_not_invalidate_openrouter_inventory(padded, tmp_path) -> None:
    """A padded display name is trimmed; the whole inventory is still committed fresh."""
    payload = {"data": [
        {"id": "vendor/good", "name": "Good"},
        {"id": "vendor/padded", "name": padded},
        {"id": "vendor/last", "name": "Last"},
    ]}
    registry, settings = _registry_for(
        "openrouter", lambda _: httpx.Response(200, json=payload), tmp_path
    )
    assert registry.view().models == ()

    report = registry.refresh()

    assert report.error_codes == ()
    assert report.refreshed_provider_ids == ("account",)
    view = registry.view()
    assert view.providers[0].status == "fresh"
    by_id = {model.id: model for model in view.models}
    assert set(by_id) == {"account/vendor/good", "account/vendor/padded", "account/vendor/last"}
    assert by_id["account/vendor/padded"].upstream_id == "vendor/padded"
    assert by_id["account/vendor/padded"].display_name == "Padded Name"
    assert {model.id for model in settings.load().document.models} == set(by_id)
    # An initially empty inventory is now usable for selection.
    assert registry.select("account/vendor/padded").models


def test_blank_display_name_falls_back_to_exact_upstream_id() -> None:
    payload = {"data": [{"id": "vendor/blank", "name": "  \t "}]}
    inventory = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="openrouter")
    )
    assert inventory.models[0].display_name == "vendor/blank"
    assert inventory.models[0].upstream_id == "vendor/blank"


@pytest.mark.parametrize("model_id", [" padded", "padded ", "pad\nded", "https://x.example/m"])
def test_identity_whitespace_and_unsafe_ids_still_reject_inventory(model_id) -> None:
    payload = {"data": [{"id": "ok", "name": "Ok"}, {"id": model_id, "name": "Bad"}]}
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter="openrouter"))
    assert error.value.code == "invalid_response"


def test_padded_display_reflecting_credential_still_fails_closed(tmp_path) -> None:
    payload = {"data": [{"id": "vendor/m", "name": " prefix test-secret "}]}
    registry, settings = _registry_for(
        "openrouter", lambda _: httpx.Response(200, json=payload), tmp_path
    )
    report = registry.refresh()
    assert report.refreshed_provider_ids == ()
    assert registry.view().models == ()
    assert not settings.path.exists() or "test-secret" not in settings.path.read_text(encoding="utf-8")


_FRESH_PAGE = {
    "anthropic": lambda n: {"data": [{"id": f"m{n}"}], "has_more": True, "last_id": f"c{n}"},
    "google": lambda n: {"models": [{"name": f"models/m{n}", "baseModelId": f"m{n}"}],
                         "nextPageToken": f"t{n}"},
    "openrouter": lambda n: {"data": [{"id": f"m{n}"}], "links": {"next": f"/api/v1/models?offset={n}"}},
}
_LAST_PAGE = {
    "anthropic": lambda n: {"data": [{"id": f"m{n}"}], "has_more": False},
    "google": lambda n: {"models": [{"name": f"models/m{n}", "baseModelId": f"m{n}"}]},
    "openrouter": lambda n: {"data": [{"id": f"m{n}"}]},
}


@pytest.mark.parametrize("adapter", ["anthropic", "google", "openrouter"])
def test_endless_fresh_continuations_stop_at_page_cap(adapter, monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_MAX_PAGES", 3, raising=False)
    count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count > 3:  # overshoot is recorded (not swallowed) and ends an unbounded loop
            return httpx.Response(503, text="overshoot")
        return httpx.Response(200, json=_FRESH_PAGE[adapter](count))

    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(handler).fetch(Connection(adapter=adapter))
    assert count == 3
    assert error.value.code == "pagination_limit"


@pytest.mark.parametrize("adapter", ["anthropic", "google", "openrouter"])
def test_finishing_exactly_on_last_allowed_page_succeeds(adapter, monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_MAX_PAGES", 3, raising=False)
    count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        page = _LAST_PAGE if count == 3 else _FRESH_PAGE
        return httpx.Response(200, json=page[adapter](count))

    inventory = _discovery(handler).fetch(Connection(adapter=adapter))
    assert count == 3
    assert [model.upstream_id for model in inventory.models] == ["m1", "m2", "m3"]


def test_page_cap_failure_keeps_last_good_stale_without_partial_commit(monkeypatch, tmp_path) -> None:
    import model_discovery
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_registry import ModelMetadata, ModelRecord, ModelRegistry

    monkeypatch.setattr(model_discovery, "_MAX_PAGES", 2, raising=False)
    cached = ModelRecord("account/cached", "account", "cached", "Cached",
                         ModelMetadata(100, None, None, None, None, None))
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(CatalogDocument(
        1, 2, None, (ProviderState("account", "anthropic", "2026-09-01T00:00:00Z", "fresh"),),
        (cached,),
    ))
    count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count > 2:  # overshoot is recorded (not swallowed) and ends an unbounded loop
            return httpx.Response(503, text="overshoot")
        return httpx.Response(200, json=_FRESH_PAGE["anthropic"](count))

    def resolve(config: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(
            config.name, config.type, config.base_url, config.request_timeout, "test-secret"
        )

    registry = ModelRegistry(
        {"account": ProviderConfig("account", "anthropic", "env:KEY")},
        settings, _discovery(handler), resolve,
    )
    report = registry.refresh()

    assert count == 2
    view = registry.view()
    assert view.models == (cached,)
    assert view.providers[0].status == "stale"
    assert report.refreshed_provider_ids == ()
    assert [code for _p, code in report.error_codes] == [view.providers[0].sanitized_last_error_code]
    assert settings.load().document.models == (cached,)
    assert "m1" not in settings.path.read_text(encoding="utf-8")


def test_page_cap_is_per_connection_not_shared(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_MAX_PAGES", 2, raising=False)
    discovery = _discovery(lambda _: httpx.Response(
        200, json={"data": [{"id": "m"}], "has_more": False}))
    for _ in range(3):
        assert discovery.fetch(Connection(adapter="anthropic")).models


# --- low-issue wave: Google displayName is the friendly presentation field (5) ---


def _google_row(**extra):
    return {"name": "models/gemini-latest", "baseModelId": "gemini-1.5", **extra}


def test_google_display_name_is_trimmed_presentation_while_identity_stays_exact() -> None:
    payload = {"models": [_google_row(displayName="  Gemini 1.5 Pro \n")]}
    inventory = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="google", api_key="google-key")
    )
    record = inventory.models[0]
    assert (record.id, record.upstream_id, record.display_name) == (
        "account/gemini-1.5", "gemini-1.5", "Gemini 1.5 Pro",
    )
    assert inventory.descriptors[0].resource_name == "models/gemini-latest"


def test_google_blank_display_name_falls_back_to_exact_upstream_id() -> None:
    payload = {"models": [_google_row(displayName="   ")]}
    record = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="google", api_key="google-key")
    ).models[0]
    assert record.display_name == "gemini-1.5" and record.upstream_id == "gemini-1.5"


def test_google_older_display_name_key_is_still_honoured_without_display_name() -> None:
    payload = {"models": [_google_row(display_name="Older Fixture")]}
    record = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="google", api_key="google-key")
    ).models[0]
    assert record.display_name == "Older Fixture"


@pytest.mark.parametrize("value", ["https://x.example/m", 7, "line\nbreak"])
def test_google_unsafe_or_non_string_display_name_still_fails_the_inventory(value) -> None:
    payload = {"models": [_google_row(displayName=value)]}
    discovery = _discovery(lambda _: httpx.Response(200, json=payload))
    if value == 7:
        # A JSON number is unusable presentation, not unsafe: exact identity fallback.
        record = discovery.fetch(Connection(adapter="google", api_key="google-key")).models[0]
        assert (record.upstream_id, record.display_name) == ("gemini-1.5", "gemini-1.5")
        return
    with pytest.raises(ModelDiscoveryError) as error:
        discovery.fetch(Connection(adapter="google", api_key="google-key"))
    assert error.value.code == "invalid_response"


def test_google_display_name_reflecting_the_credential_still_fails_closed(tmp_path) -> None:
    payload = {"models": [_google_row(displayName=" see test-secret ")]}
    registry, _ = _registry_for("google", lambda _: httpx.Response(200, json=payload), tmp_path)
    report = registry.refresh()
    assert report.refreshed_provider_ids == () and registry.view().models == ()


# --- reliability wave: cooperative per-connection elapsed discovery budget ---


def test_fresh_continuation_expires_before_next_request_with_no_partial_inventory(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    tick = [0.0]
    requests = []

    def handler(request):
        requests.append(request)
        tick[0] = 120.0
        return httpx.Response(200, json={"data": [{"id": "m1"}], "has_more": True, "last_id": "m1"})

    discovery = _discovery(handler)
    monkeypatch.setattr(discovery, "_monotonic", lambda: tick[0], raising=False)
    with pytest.raises(ModelDiscoveryError) as error:
        discovery.fetch(Connection(adapter="anthropic"))
    assert error.value.code == "discovery_timeout"
    assert len(requests) == 1


def test_single_final_page_finished_at_budget_boundary_is_rejected(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    tick = [0.0]

    def handler(_request):
        tick[0] = 120.0
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    discovery = _discovery(handler)
    monkeypatch.setattr(discovery, "_monotonic", lambda: tick[0], raising=False)
    with pytest.raises(ModelDiscoveryError) as error:
        discovery.fetch(Connection())
    assert error.value.code == "discovery_timeout"


def test_slow_stream_is_closed_when_budget_expires_between_chunks(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    tick = [0.0]
    chunks = []
    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            chunks.append(1)
            yield b'{"data":'
            tick[0] = 120.0
            chunks.append(2)
            yield b'[{"id":"m"}]}'
            chunks.append(3)

        def close(self):
            closed.append(True)

    discovery = _discovery(lambda _: httpx.Response(200, stream=Stream()))
    monkeypatch.setattr(discovery, "_monotonic", lambda: tick[0], raising=False)
    with pytest.raises(ModelDiscoveryError) as error:
        discovery.fetch(Connection())
    assert error.value.code == "discovery_timeout"
    assert closed == [True] and chunks == [1, 2]


def test_remaining_budget_restricts_each_transport_timeout_phase(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    tick = [0.0]
    captured = []

    def clock():
        current = tick[0]
        tick[0] = 117.0
        return current

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    discovery = _discovery(handler)
    monkeypatch.setattr(discovery, "_monotonic", clock, raising=False)
    inventory = discovery.fetch(Connection(request_timeout=15.0))
    assert inventory.models[0].id == "account/m"
    assert all(0 < value <= 3.0 for value in captured[0].values())


def test_each_provider_fetch_gets_a_new_budget(monkeypatch) -> None:
    import model_discovery

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    tick = [0.0]
    calls = []

    def handler(_request):
        calls.append(tick[0])
        tick[0] += 90.0
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    discovery = _discovery(handler)
    monkeypatch.setattr(discovery, "_monotonic", lambda: tick[0], raising=False)
    assert discovery.fetch(Connection()).models
    assert discovery.fetch(Connection()).models
    assert calls == [0.0, 90.0]


def test_deadline_failure_leaves_registry_last_good_stale_and_cache_unmodified(monkeypatch, tmp_path) -> None:
    import model_discovery
    from config_schema import ProviderConfig, ResolvedProviderConnection
    from model_registry import ModelMetadata, ModelRecord, ModelRegistry

    monkeypatch.setattr(model_discovery, "_DISCOVERY_BUDGET_S", 120, raising=False)
    cached = ModelRecord("account/cached", "account", "cached", "Cached",
                         ModelMetadata(100, None, None, None, None, None))
    settings = ModelSettings(tmp_path / "models.toml")
    settings.write(CatalogDocument(1, 2, None,
        (ProviderState("account", "openai", "2026-09-01T00:00:00Z", "fresh"),), (cached,)))
    tick = [0.0]

    def handler(_request):
        tick[0] = 120.0
        return httpx.Response(200, json={"data": [{"id": "new"}]})

    discovery = _discovery(handler)
    monkeypatch.setattr(discovery, "_monotonic", lambda: tick[0], raising=False)

    def resolve(config: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(config.name, config.type, config.base_url, config.request_timeout, "test-secret")

    registry = ModelRegistry({"account": ProviderConfig("account", "openai", "env:KEY")}, settings, discovery, resolve)
    report = registry.refresh()
    assert report.refreshed_provider_ids == () and registry.view().models == (cached,)
    assert registry.view().providers[0].status == "stale"
    assert settings.load().document.models == (cached,)
    assert b"new" not in settings.path.read_bytes()


# --- input-handling wave: unusable (non-string) presentation never invalidates identity ---

_UNUSABLE = [None, True, 12, 1.5, ["a", 1], {"k": "v"}]
_UNUSABLE_IDS = ["null", "bool", "int", "float", "list", "object"]


@pytest.mark.parametrize("unusable", _UNUSABLE, ids=_UNUSABLE_IDS)
def test_unusable_presentation_json_value_falls_back_to_exact_upstream_id(unusable, tmp_path) -> None:
    payload = {"data": [{"id": "vendor/good", "name": "Good"}, {"id": "vendor/odd", "name": unusable}]}
    registry, settings = _registry_for("openrouter", lambda _: httpx.Response(200, json=payload), tmp_path)
    assert registry.view().models == ()

    report = registry.refresh()

    assert report.error_codes == () and report.refreshed_provider_ids == ("account",)
    view = registry.view()
    assert view.providers[0].status == "fresh"
    by_id = {m.id: m for m in view.models}
    assert sorted(by_id) == ["account/vendor/good", "account/vendor/odd"]
    assert by_id["account/vendor/odd"].display_name == "vendor/odd"
    assert by_id["account/vendor/odd"].upstream_id == "vendor/odd"
    assert by_id["account/vendor/good"].display_name == "Good"
    assert {m.id for m in settings.load().document.models} == set(by_id)
    assert registry.select("account/vendor/good").active_model_id == "account/vendor/good"


def test_google_present_null_preferred_field_uses_upstream_id_not_the_legacy_field() -> None:
    payload = {"models": [_google_row(displayName=None, display_name="Legacy")]}
    record = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="google", api_key="google-key")
    ).models[0]
    assert record.display_name == "gemini-1.5"


def test_google_missing_preferred_field_still_uses_the_legacy_field() -> None:
    payload = {"models": [_google_row(display_name="Legacy")]}
    record = _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
        Connection(adapter="google", api_key="google-key")
    ).models[0]
    assert record.display_name == "Legacy"


_SECRET_CASES = {
    "list-leaf": ["ok", "x test-secret x"],
    "nested-list": [[["deep test-secret"]]],
    "object-value": {"k": "test-secret"},
    "object-key": {"test-secret": 1},
    "nested-object": {"a": {"b": ["test-secret"]}},
}


@pytest.mark.parametrize("value", list(_SECRET_CASES.values()), ids=list(_SECRET_CASES))
@pytest.mark.parametrize("adapter", ["openrouter", "google"])
def test_credential_inside_discarded_presentation_value_fails_the_whole_refresh(
    adapter, value, tmp_path, caplog
) -> None:
    if adapter == "google":
        payload = {"models": [_google_row(displayName=value)]}
    else:
        payload = {"data": [{"id": "vendor/a", "name": value}]}
    registry, settings = _registry_for(adapter, lambda _: httpx.Response(200, json=payload), tmp_path)

    report = registry.refresh()

    assert report.refreshed_provider_ids == () and registry.view().models == ()
    assert not settings.path.exists() or settings.load().document.models == ()
    assert "test-secret" not in caplog.text and "test-secret" not in repr(report)
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
            Connection(adapter=adapter, api_key="test-secret")
        )
    assert error.value.code == "invalid_inventory" and "test-secret" not in str(error.value)


@pytest.mark.parametrize("name", [None, 7, ["x"], ""])
def test_ollama_identity_name_that_is_not_a_nonempty_string_still_fails(name) -> None:
    payload = {"models": [{"name": name}]}
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(
            Connection(adapter="ollama", api_key="k")
        )
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("identity", [None, 5, "", " "])
def test_malformed_identity_still_fails_even_with_unusable_presentation(identity) -> None:
    payload = {"data": [{"id": identity, "name": None}]}
    with pytest.raises(ModelDiscoveryError):
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter="openrouter"))


def test_unknown_python_object_presentation_is_rejected_without_stringifying_it() -> None:
    import model_discovery

    class Boom:
        def __str__(self) -> str:
            raise AssertionError("str called")

        __repr__ = __str__
        __format__ = lambda self, spec: (_ for _ in ()).throw(AssertionError("format called"))  # noqa: E731

    with pytest.raises(ModelDiscoveryError) as error:
        model_discovery._normalize_model("account", "openrouter", {"id": "m", "name": Boom()})
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(lambda: [[]] * 0 or __import__("functools").reduce(lambda acc, _: [acc], range(60), []), id="too-deep"),
        pytest.param(lambda: list(range(20_000)), id="too-many-nodes"),
    ],
)
def test_presentation_walk_exhaustion_fails_closed(value) -> None:
    payload = {"data": [{"id": "m", "name": value()}]}
    with pytest.raises(ModelDiscoveryError) as error:
        _discovery(lambda _: httpx.Response(200, json=payload)).fetch(Connection(adapter="openrouter"))
    assert error.value.code == "invalid_response"


def test_presentation_walk_checks_the_cooperative_deadline(monkeypatch) -> None:
    def run(width: int, expire_after: int | None):
        payload = {"data": [{"id": "m", "name": ["x"] * width}]}
        calls = [0]

        def clock() -> float:
            calls[0] += 1
            return 1e9 if expire_after is not None and calls[0] > expire_after else 0.0

        discovery = _discovery(lambda _: httpx.Response(200, json=payload))
        monkeypatch.setattr(discovery, "_monotonic", clock)
        discovery.fetch(Connection(adapter="openrouter"))
        return calls[0]

    base = run(1, None)
    with pytest.raises(ModelDiscoveryError) as error:
        run(500, base + 5)
    assert error.value.code == "discovery_timeout"


# --- input-handling review fix: whole raw candidate guard and lazy bounded walk ---

_CRED_PAYLOADS = {
    "google-resource-name-display-null": ("google", {"models": [{"baseModelId": "safe-model", "name": "models/test-secret", "displayName": None}]}),
    "google-ignored-legacy-field": ("google", {"models": [{"baseModelId": "safe-model", "name": "models/safe", "displayName": "Safe", "display_name": "x test-secret"}]}),
    "string-display-other-field-value": ("openrouter", {"data": [{"id": "vendor/a", "name": "Fine", "extra": {"k": ["test-secret"]}}]}),
    "string-display-other-field-key": ("openrouter", {"data": [{"id": "vendor/a", "name": "Fine", "architecture": {"test-secret": 1}}]}),
}


@pytest.mark.parametrize("case", list(_CRED_PAYLOADS))
def test_credential_anywhere_in_a_raw_candidate_row_fails_the_whole_refresh(case, tmp_path, caplog) -> None:
    adapter, payload = _CRED_PAYLOADS[case]
    registry, settings = _registry_for(adapter, lambda _: httpx.Response(200, json=payload), tmp_path)

    report = registry.refresh()

    assert report.refreshed_provider_ids == () and registry.view().models == ()
    assert not settings.path.exists() or settings.load().document.models == ()
    assert "test-secret" not in caplog.text and "test-secret" not in repr(report)
    discovery = _discovery(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ModelDiscoveryError) as error:
        discovery.fetch(Connection(adapter=adapter, api_key="test-secret"))
    assert error.value.code == "invalid_inventory" and "test-secret" not in str(error.value)


@pytest.mark.parametrize("shape", ["list", "dict"])
def test_wide_container_walk_stops_at_the_node_limit_without_expanding_children(shape, monkeypatch) -> None:
    """Pending work stays O(depth): the walk allocates nothing proportional to the width."""
    import tracemalloc

    import model_discovery

    monkeypatch.setattr(model_discovery, "_MAX_PRESENTATION_NODES", 3)
    wide = list(range(200_000)) if shape == "list" else {f"k{i}": i for i in range(100_000)}
    ticks = [0]
    tracemalloc.start()
    try:
        with pytest.raises(ModelDiscoveryError) as error:
            model_discovery._reject_credential_in_json(
                wide, "test-secret", lambda: ticks.__setitem__(0, ticks[0] + 1)
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.code == "invalid_response"
    assert ticks[0] <= 4 and peak < 50_000  # eager expansion of the children would take MBs
