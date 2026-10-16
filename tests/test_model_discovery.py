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
    public_handler=None,
    public_headers=None,
    *,
    provider_headers=None,
    provider_cookies=None,
    provider_params=None,
    provider_auth=None,
    public_cookies=None,
    public_params=None,
    public_auth=None,
):
    """Build independent mocked provider/public clients and a deterministic clock."""
    provider = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=provider_headers,
        cookies=provider_cookies,
        params=provider_params,
        auth=provider_auth,
    )
    public = httpx.Client(
        transport=httpx.MockTransport(public_handler or _empty_public),
        headers=public_headers,
        cookies=public_cookies,
        params=public_params,
        auth=public_auth,
    )
    timestamp = datetime(2026, 10, 13, tzinfo=timezone.utc)
    return ModelDiscovery(provider, public, lambda: timestamp)


def _empty_public(request: httpx.Request) -> httpx.Response:
    assert request.url == "https://models.dev/api.json"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.content == b""
    return httpx.Response(200, json={})


def _write_catalog(inventory, path) -> None:
    """Prove normalized inventory records pass the unchanged managed codec."""
    document = CatalogDocument(
        providers=(ProviderState(inventory.provider_id, inventory.adapter, inventory.last_success_at, "fresh"),),
        models=inventory.models,
        supplemental=inventory.supplemental,
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


def test_models_dev_exact_match_fills_unknown_fields_only_and_is_credential_free(tmp_path) -> None:
    captured = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "known", "architecture": {"input_modalities": ["text"]}}, {"id": "absent"}
        ]})

    def public(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url == "https://models.dev/api.json"
        assert request.url.query == b""
        assert request.content == b""
        assert dict(request.headers) == {"accept": "application/json", "host": "models.dev"}
        return httpx.Response(200, json={"openrouter": {"models": {
            "known": {"id": "known", "limit": {"context": 100, "output": 10},
                      "modalities": {"input": ["text", "image"]}, "tool_call": False, "reasoning": True},
            "absent": {"id": "other", "limit": {"context": 1}},
            "fuzzy-alias": {"id": "known", "limit": {"context": 2}},
        }}})

    inventory = _discovery(handler, public, public_headers={"Authorization": "private-secret",
                                                            "x-api-key": "private-secret",
                                                            "X-Account-Id": "fake-private-account",
                                                            "X-Private": "fake-private-header"},
                         public_cookies={"session": "fake-private-cookie"},
                         public_params={"account": "fake-private-account", "api_key": "fake-param-key"},
                         public_auth=("fake-user", "fake-password")).fetch(Connection(adapter="openrouter"))
    assert len(captured) == 1
    assert len(inventory.supplemental) == 1
    metadata = inventory.models[0].metadata
    assert metadata.context_tokens == 100
    assert metadata.output_tokens == 10
    assert metadata.vision is False  # provider value overrides public true
    assert metadata.tool_calling is False
    assert metadata.reasoning is True
    assert dict(metadata.sources) == {
        "context_tokens": "models.dev", "output_tokens": "models.dev", "vision": "provider",
        "tool_calling": "models.dev", "reasoning": "models.dev",
    }
    assert [model.upstream_id for model in inventory.models] == ["known", "absent"]
    _write_catalog(inventory, tmp_path)


def test_supplement_failure_does_not_invalidate_provider_inventory() -> None:
    inventory = _discovery(lambda _: httpx.Response(200, json={"data": [{"id": "ok"}]}),
                           lambda _: httpx.Response(403)).fetch(Connection())
    assert [model.upstream_id for model in inventory.models] == ["ok"]
    assert inventory.supplemental == ()


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
    inventory = ProviderInventory("account", "google", (), (), "stamp", descriptors)
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
