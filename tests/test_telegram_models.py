"""RED-first tests for the registry-backed `/models` catalog (task 8.1).

Scope boundary (do not expand): `/models` becomes read-only + refresh when a
real `ModelRegistry` is injected as `iface.model_registry`. No selection
button, no canonical model id in any callback, no new public command, no
generic pagination framework. The legacy `iface.llm_client.list_models()` /
`set_model()` path stays exactly as-is when `iface.model_registry` is None.
Opaque <=64-byte selection tokens with expiry/revalidation/persist-before-ack
are a separate later task and are intentionally not covered here.

Planned (not yet implemented) symbols these tests exercise:
  - `TelegramInterface.model_registry` attribute (optional, default None)
  - `cmd_models` (telegram_commands.py) gains a registry-backed branch,
    calling `registry.refresh()` then `registry.view()`, rendering at most
    ~20 entries with fresh/stale, selection, capability (known-yes /
    known-no / unknown), HTML-escaped, capped
    at 4096 chars, with a single "Refresh" inline button
    (`callback_data="models_refresh"`) and no other buttons.
  - `cb_models_refresh` (telegram_callbacks.py), `@_require_cb_auth`,
    re-running `registry.refresh()` + `registry.view()` and editing the
    message in place.

Label conventions asserted below (fixed by this test file as the contract
for the implementation phase):
  - capability fields render "<field>: known-yes" / "known-no" / "unknown"
  - numeric fields render "<field>: <value>" when known (provider-reported
    display data, no source label), "<field>: unknown" when None
  - provider freshness renders the literal words "fresh" / "stale"
  - selection status renders one of "active" / "no selection" /
    "saved selection unavailable"
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_registry import (  # noqa: E402
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ProviderState,
)
from model_settings import ModelSettings  # noqa: E402
from telegram_commands import cmd_models  # noqa: E402


# ---------------------------------------------------------------------------
# Fake provider/connection/discovery plumbing (mirrors tests/test_model_registry.py)
# ---------------------------------------------------------------------------

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
    models: tuple
    last_success_at: str


class FakeDiscovery:
    """Discovery boundary double — no real network, matches real discovery contract."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[FakeResolvedConnection] = []

    def fetch(self, connection: FakeResolvedConnection):
        self.calls.append(connection)
        result = self.responses[connection.provider_id]
        if isinstance(result, BaseException):
            raise result
        return result


def _provider_config(provider_id: str, *, type_: str = "openai", url: str = "https://provider.invalid") -> FakeProviderConfig:
    return FakeProviderConfig(provider_id, type_, "env:FAKE_KEY", url)


def _resolve(provider: FakeProviderConfig) -> FakeResolvedConnection:
    return FakeResolvedConnection(provider.name, provider.type, provider.base_url)


def _model(
    upstream_id: str,
    provider_id: str,
    *,
    context_tokens: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    vision: bool | None = None,
    tool_calling: bool | None = None,
    reasoning: bool | None = None,
    display_name: str | None = None,
) -> ModelRecord:
    return ModelRecord(
        f"{provider_id}/{upstream_id}",
        provider_id,
        upstream_id,
        display_name or upstream_id,
        ModelMetadata(context_tokens, input_tokens, output_tokens, vision, tool_calling, reasoning),
    )


def _inventory(
    provider_id: str,
    models: tuple = (),
    *,
    adapter: str = "openai",
    last_success_at: str = "2026-10-01T12:00:00Z",
) -> FakeInventory:
    return FakeInventory(provider_id, adapter, tuple(models), last_success_at)


def _make_registry(
    tmp_path: Path,
    connections: tuple[FakeProviderConfig, ...],
    *,
    seed_document: CatalogDocument | None = None,
    resolver=None,
) -> tuple[ModelRegistry, ModelSettings, FakeDiscovery]:
    """Build a real `ModelRegistry`. `resolver` defaults to the always-succeeding
    `_resolve`; pass a custom one to exercise external credential-resolution
    failure via the registry's own public constructor, not private patching."""
    settings = ModelSettings(tmp_path / "models-config.toml")
    if seed_document is not None:
        settings.write(seed_document)
    discovery = FakeDiscovery()
    conn_map = {c.name: c for c in connections}
    registry = ModelRegistry(conn_map, settings, discovery, resolver or _resolve)
    return registry, settings, discovery


# ---------------------------------------------------------------------------
# Telegram update/iface fixtures
# ---------------------------------------------------------------------------

def _make_iface(*, model_registry=None, llm_client=None):
    from telegram_interface import TelegramInterface
    import time

    config = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
    iface._config = config
    iface.token = "fake:token"
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.agent_handler = MagicMock(return_value="done")
    iface.agent_reset_fn = None
    iface.agent_compress_fn = None
    iface.agent = None
    iface.scheduler = None
    iface.tool_registry = None
    iface.llm_client = llm_client
    iface._tool_index = None
    iface.skill_registry = None
    iface._usage_registry = None
    iface.mcp_manager = None
    iface._downloads_dir = "/tmp"
    iface._start_time = time.time()
    iface._pending_pairs = {}
    iface._agent_locks = {}
    iface._deferred_messages = {}
    iface._current_deferred_token = {}
    iface._verbose = False
    iface._app = None
    iface._loop = None
    iface.model_registry = model_registry
    from telegram_model_selection import SelectionTokenCache

    iface.model_selection_tokens = SelectionTokenCache()
    return iface


def _make_command_update(user_id: int = 42):
    captured: dict = {}

    async def _reply(text, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = kwargs.get("reply_markup")
        return MagicMock()

    mock_message = MagicMock()
    mock_message.reply_text = AsyncMock(side_effect=_reply)

    mock_user = MagicMock()
    mock_user.id = user_id

    mock_chat = MagicMock()
    mock_chat.id = 1001

    update = MagicMock()
    update.effective_user = mock_user
    update.effective_chat = mock_chat
    update.effective_message = mock_message

    ctx = MagicMock()
    ctx.args = []
    return update, ctx, captured


def _make_callback_update(data: str, user_id: int = 42, chat_id: int = 1001):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = chat_id

    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    update.effective_chat = query.message.chat
    return update, query


def _get_cb_model_select():
    """Look up `telegram_callbacks.cb_model_select` without a module-level
    import, so a missing handler fails the calling test (RED), not test
    collection for the whole file."""
    import telegram_callbacks

    handler = getattr(telegram_callbacks, "cb_model_select", None)
    assert handler is not None, "cb_model_select is not implemented yet (task 8.2 next slice)"
    return handler


def _selection_buttons(markup) -> list[str]:
    """Non-refresh callback_data strings, in render order."""
    return [b for b in _flatten_buttons(markup) if b != "models_refresh"]


def _button_callback_data(markup, *, label_contains: str) -> str:
    """The callback_data of the one button whose visible label contains
    `label_contains`; fails loudly if zero or more than one match."""
    matches = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if label_contains in (btn.text or "")
    ]
    assert len(matches) == 1, f"expected exactly one button containing {label_contains!r}, found {matches!r}"
    return matches[0]


def _mint_via_cmd_models(iface, *, user_id: int = 42, chat_id: int = 1001):
    """Run the real, authorized `cmd_models` and return (text, reply_markup)."""
    update, ctx, captured = _make_command_update(user_id=user_id)
    update.effective_chat.id = chat_id
    asyncio.run(cmd_models(iface, update, ctx))
    return captured["text"], captured["reply_markup"]


_SHOWN_HEADER_RE = re.compile(r"\((\d+) shown of (\d+) total\)")


def _reported_shown_and_total(text: str) -> tuple[int, int]:
    match = _SHOWN_HEADER_RE.search(text)
    assert match, f"expected a '(N shown of M total)' header, found none in: {text[:200]!r}"
    return int(match.group(1)), int(match.group(2))


def _complete_entries(text: str, candidate_ids: list[str]) -> list[str]:
    """Return the subset of `candidate_ids` that render with all six fields,
    using each id's position (and the next *present* candidate's position)
    as the block boundary — so a cut mid-fields is caught, not hidden."""
    present = [cid for cid in candidate_ids if cid in text]
    complete: list[str] = []
    for pos, cid in enumerate(present):
        start = text.index(cid)
        end = text.index(present[pos + 1]) if pos + 1 < len(present) else len(text)
        block = text[start:end]
        fields = ("context_tokens", "input_tokens", "output_tokens", "vision", "tool_calling", "reasoning")
        if all(field in block for field in fields):
            complete.append(cid)
    return complete


def _flatten_buttons(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


_TG_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler"}


def _assert_valid_telegram_html(text: str) -> None:
    """Fail if `text` has unbalanced/unknown tags or stray bare '&'/'<'."""
    import html.parser

    stack: list[str] = []
    errors: list[str] = []

    class _Checker(html.parser.HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in _TG_ALLOWED_TAGS:
                errors.append(f"unexpected tag <{tag}>")
            stack.append(tag)

        def handle_endtag(self, tag):
            if not stack or stack.pop() != tag:
                errors.append(f"unbalanced closing tag </{tag}>")

    checker = _Checker(convert_charrefs=True)
    checker.feed(text)
    checker.close()
    assert not errors, f"invalid Telegram HTML: {errors}"
    assert not stack, f"unclosed tags: {stack}"


# ---------------------------------------------------------------------------
# Registry-backed /models — fresh/stale, capability, source, selection
# ---------------------------------------------------------------------------

class TestRegistryBackedCatalog:
    """Real `ModelRegistry` + fake discovery boundary — no mocked view objects."""

    def test_refresh_success_and_failure_distinguish_fresh_and_stale_providers(self, tmp_path: Path):
        pa = _provider_config("pa")
        pb = _provider_config("pb")
        seed = CatalogDocument(
            1, 1, None,
            (ProviderState("pb", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (_model("old-model", "pb", context_tokens=1000),),
        )
        registry, _, discovery = _make_registry(tmp_path, (pa, pb), seed_document=seed)
        discovery.responses["pa"] = _inventory("pa", (_model("new-model", "pa", context_tokens=2000),))
        discovery.responses["pb"] = RuntimeError("upstream unreachable")

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "pa/new-model" in text
        assert "fresh" in text
        assert "pb/old-model" in text, "last-good model for a now-stale provider must stay listed"
        assert "stale" in text

    def test_capability_distinguishes_known_yes_known_no_and_unknown(self, tmp_path: Path):
        p = _provider_config("p")
        m = _model("vision-model", "p", vision=True, tool_calling=False, reasoning=None)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "vision: known-yes" in text
        assert "tool_calling: known-no" in text
        assert "reasoning: unknown" in text

    def test_metadata_is_shown_as_advisory_values_without_provenance_labels(self, tmp_path: Path):
        p = _provider_config("p")
        m = _model("m1", "p", context_tokens=4096, vision=True)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "context_tokens: 4096" in text and "vision: known-yes" in text
        assert "source" not in text and "models.dev" not in text

    def test_unknown_numeric_field_is_shown_as_unknown_not_inferred(self, tmp_path: Path):
        p = _provider_config("p")
        m = _model("m1", "p", context_tokens=4096)  # input/output left None
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "input_tokens: unknown" in text
        assert "output_tokens: unknown" in text
        assert "4096 + " not in text and "+ 4096" not in text, "must not fabricate a derived budget"

    @pytest.mark.parametrize(
        "remembered,active_after_refresh,expected",
        [
            (None, False, "no selection"),
            ("p/gone", False, "saved selection unavailable"),
            ("p/m1", True, "active"),
        ],
    )
    def test_selection_status_states(self, tmp_path: Path, remembered, active_after_refresh, expected):
        p = _provider_config("p")
        m = _model("m1", "p", context_tokens=100)
        seed = CatalogDocument(1, 1, remembered, (), ())
        registry, _, discovery = _make_registry(tmp_path, (p,), seed_document=seed)
        discovery.responses["p"] = _inventory("p", (m,))
        registry.refresh()
        registry.restore_selection()
        if active_after_refresh:
            registry.select("p/m1")

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        assert expected in captured["text"]

    def test_entry_count_is_capped_near_twenty_with_overflow_noted(self, tmp_path: Path):
        p = _provider_config("p")
        models = tuple(_model(f"model-{i:02d}", "p", context_tokens=100) for i in range(25))
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", models)

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        shown = sum(1 for i in range(25) if f"p/model-{i:02d}" in text)
        assert shown == 20, "exactly ~20 ordinary entries must render, not fewer"
        assert "25" in text, "the real total must still be visible even when the list is capped"
        assert any(word in text for word in ("more", "overflow", "+5")), (
            "an explicit overflow note must accompany the cap"
        )

    def test_no_generic_pagination_buttons_appear(self, tmp_path: Path):
        p = _provider_config("p")
        models = tuple(_model(f"model-{i:02d}", "p", context_tokens=100) for i in range(25))
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", models)

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        buttons = _flatten_buttons(captured["reply_markup"])
        assert not any("page" in (b or "").lower() for b in buttons)
        assert "models_refresh" in buttons
        assert all(b == "models_refresh" or b.startswith("modelsel:") for b in buttons)

    def test_selection_buttons_reject_legacy_callback_shape_and_leak_no_canonical_id(self, tmp_path: Path):
        """Selection controls (task 8.2) must never reuse the legacy `model:<name>`
        callback shape and must never carry the raw canonical id as payload."""
        p = _provider_config("p")
        m = _model("m1", "p", context_tokens=100)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        buttons = _flatten_buttons(captured["reply_markup"])
        assert not any(b.startswith("model:") for b in buttons)
        assert not any("p/m1" in b for b in buttons)

    def test_selection_control_uses_opaque_token_not_canonical_identity_for_long_id_model(self, tmp_path: Path):
        """A selection control beyond `models_refresh` must exist for a shown
        model, be prefixed `modelsel:`, fit Telegram's 64-byte callback-data
        limit, be ASCII, and never serialize the provider id, upstream id, or
        full/truncated canonical id into the payload."""
        provider_id = "p" * 40
        upstream_id = "m" * 40
        p = _provider_config(provider_id)
        m = _model(upstream_id, provider_id, context_tokens=100)
        canonical_id = f"{provider_id}/{upstream_id}"
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses[provider_id] = _inventory(provider_id, (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        buttons = _flatten_buttons(captured["reply_markup"])
        assert "models_refresh" in buttons
        selection_buttons = [b for b in buttons if b != "models_refresh"]
        assert selection_buttons, "expected at least one selection control for the shown model"
        for b in selection_buttons:
            assert b.startswith("modelsel:"), f"unexpected selection callback shape: {b!r}"
            assert len(b.encode("utf-8")) <= 64
            assert b.isascii()
            token = b[len("modelsel:"):]
            assert token, "opaque token must not be empty"
            assert provider_id not in b
            assert upstream_id not in b
            assert canonical_id not in b
            assert not b.startswith(f"modelsel:{provider_id}")
            assert not b.startswith(f"modelsel:{upstream_id}")

    def test_malicious_display_name_is_escaped_and_message_fits_telegram_limit(self, tmp_path: Path):
        p = _provider_config("p")
        malicious = "<script>alert(1)</script>&\"'"
        m = _model("m1", "p", context_tokens=100, display_name=malicious)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "<script>" not in text
        assert html.escape(malicious) in text
        assert len(text) <= 4096
        _assert_valid_telegram_html(text)

    def test_long_display_name_and_canonical_id_stay_under_limit_with_valid_html(self, tmp_path: Path):
        p = _provider_config("p" * 40)
        long_name = ("Überlang Model Name <b>&</b> " * 20)[:600]
        long_upstream = "very-long-upstream-identifier-segment-" * 10
        m = _model(long_upstream, p.name, context_tokens=100, display_name=long_name)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses[p.name] = _inventory(p.name, (m,))

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert len(text) <= 4096
        _assert_valid_telegram_html(text)
        assert "<b>" not in text or text.count("<b>") == text.count("</b>")

    def test_safe_error_code_shown_without_raw_exception_or_secret_leak(self, tmp_path: Path):
        p = _provider_config("p")
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = RuntimeError("boom sk-FAKESECRETVALUE1234567890")

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "discovery_failed" in text
        assert "sk-FAKESECRETVALUE1234567890" not in text
        assert "boom" not in text

    def test_registry_backed_branch_never_calls_legacy_llm_client(self, tmp_path: Path):
        p = _provider_config("p")
        m = _model("m1", "p", context_tokens=100)
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (m,))

        legacy_client = MagicMock()
        legacy_client.list_models = MagicMock()
        legacy_client.set_model = MagicMock()
        iface = _make_iface(model_registry=registry, llm_client=legacy_client)
        update, ctx, _captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        legacy_client.list_models.assert_not_called()
        legacy_client.set_model.assert_not_called()

    def test_unauthorized_user_gets_no_catalog_and_registry_refresh_is_not_called(self, tmp_path: Path):
        p = _provider_config("p")
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (_model("m1", "p", context_tokens=100),))
        real_refresh = registry.refresh
        calls: list[int] = []

        def _spy_refresh():
            calls.append(1)
            return real_refresh()

        registry.refresh = _spy_refresh  # type: ignore[method-assign]

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update(user_id=999)  # not in allowed_ids
        asyncio.run(cmd_models(iface, update, ctx))

        assert calls == []
        assert "text" not in captured or "fresh" not in captured.get("text", "")


# ---------------------------------------------------------------------------
# Legacy (no registry injected) path stays untouched
# ---------------------------------------------------------------------------

class TestConstructorInjection:
    """Real `TelegramInterface.__init__` (no mocking) accepts `model_registry`."""

    def test_real_constructor_binds_injected_registry(self, tmp_path: Path):
        from telegram_interface import TelegramInterface

        p = _provider_config("p")
        registry, _, _discovery = _make_registry(tmp_path, (p,))
        config = {
            "telegram": {
                "bot_token": "fake:token",
                "security_mode": "allowlist",
                "allowed_user_ids": [42],
            }
        }
        iface = TelegramInterface(
            config,
            agent_handler=MagicMock(),
            downloads_dir=str(tmp_path),
            model_registry=registry,
        )
        assert iface.model_registry is registry

    def test_real_constructor_defaults_to_no_registry(self, tmp_path: Path):
        from telegram_interface import TelegramInterface

        config = {
            "telegram": {
                "bot_token": "fake:token",
                "security_mode": "allowlist",
                "allowed_user_ids": [42],
            }
        }
        iface = TelegramInterface(
            config,
            agent_handler=MagicMock(),
            downloads_dir=str(tmp_path),
        )
        assert iface.model_registry is None


class TestLegacyPathPreserved:
    """When `iface.model_registry` is None, behavior is exactly the pre-8.1 one."""

    def test_legacy_llm_client_path_renders_active_and_inactive_entries(self):
        legacy_client = MagicMock()
        legacy_client.list_models = MagicMock(
            return_value=[
                {"name": "gpt", "model": "gpt-4o", "active": True, "vision": True},
                {"name": "claude", "model": "claude-3", "active": False, "vision": False},
            ]
        )
        iface = _make_iface(model_registry=None, llm_client=legacy_client)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        legacy_client.list_models.assert_called_once()
        assert "LLM Models" in captured["text"]
        assert "gpt-4o" in captured["text"]
        assert "claude-3" in captured["text"]
        buttons = _flatten_buttons(captured["reply_markup"])
        assert buttons == ["model:claude"], "only the inactive entry gets a switch button"

    def test_legacy_switch_button_invokes_real_cb_model_switch_and_set_model(self):
        from telegram_callbacks import cb_model_switch

        legacy_client = MagicMock()
        legacy_client.set_model = MagicMock(return_value=True)
        legacy_client.llm_cfg = {"name": "claude"}
        iface = _make_iface(model_registry=None, llm_client=legacy_client)
        update, query = _make_callback_update("model:claude")

        asyncio.run(cb_model_switch(iface, update, MagicMock()))

        legacy_client.set_model.assert_called_once_with("claude")
        query.edit_message_text.assert_awaited_once()

    def test_no_registry_and_no_llm_client_reports_unavailable(self):
        iface = _make_iface(model_registry=None, llm_client=None)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        assert "not available" in captured["text"]


# ---------------------------------------------------------------------------
# Refresh callback (new, planned: telegram_callbacks.cb_models_refresh)
# ---------------------------------------------------------------------------

class TestRefreshCallback:
    """The inline 'Refresh' button re-runs the real registry refresh."""

    def test_refresh_callback_updates_listing_after_partial_failure(self, tmp_path: Path):
        from telegram_callbacks import cb_models_refresh

        pa = _provider_config("pa")
        pb = _provider_config("pb")
        registry, _, discovery = _make_registry(tmp_path, (pa, pb))
        discovery.responses["pa"] = _inventory("pa", (_model("ok-model", "pa", context_tokens=100),))
        discovery.responses["pb"] = RuntimeError("down")

        iface = _make_iface(model_registry=registry)
        update, query = _make_callback_update("models_refresh")
        asyncio.run(cb_models_refresh(iface, update, MagicMock()))

        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        text = query.edit_message_text.call_args.args[0]
        assert "pa/ok-model" in text
        assert "fresh" in text
        assert "stale" in text

    def test_refresh_callback_requires_auth_and_does_not_touch_registry(self, tmp_path: Path):
        from telegram_callbacks import cb_models_refresh

        p = _provider_config("p")
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (_model("m1", "p", context_tokens=100),))
        real_refresh = registry.refresh
        calls: list[int] = []

        def _spy_refresh():
            calls.append(1)
            return real_refresh()

        registry.refresh = _spy_refresh  # type: ignore[method-assign]

        iface = _make_iface(model_registry=registry)
        update, query = _make_callback_update("models_refresh", user_id=999)
        asyncio.run(cb_models_refresh(iface, update, MagicMock()))

        assert calls == []
        query.edit_message_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Oracle-flagged regressions: unsafe exception logging, discarded
# persistence outcome, and line-based truncation that can cut a model
# record in half. These lock in the real `model_registry.py` contract
# (never mocked) rather than a fabricated report.
# ---------------------------------------------------------------------------

class TestUnexpectedRefreshExceptionSafety:
    """An exception type the registry itself does not whitelist must never
    reach a log line or the UI in raw form — not even via exc_info/traceback."""

    def test_unexpected_refresh_exception_is_never_logged_or_shown_raw(self, tmp_path: Path, caplog):
        class _ExoticDiscoveryError(Exception):
            """Not OSError/ValueError/RuntimeError — escapes registry's own catches."""

        p = _provider_config("p")
        seed = CatalogDocument(
            1, 1, None,
            (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (_model("last-good", "p", context_tokens=100),),
        )
        registry, _, discovery = _make_registry(tmp_path, (p,), seed_document=seed)
        secret = "fake-secret-ABCDEF1234567890"
        discovery.responses["p"] = _ExoticDiscoveryError(f"boom {secret}")

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        with caplog.at_level(logging.WARNING):
            asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert "p/last-good" in text, "last known catalog must still render after an unexpected refresh error"
        assert secret not in text

        formatter = logging.Formatter()
        for record in caplog.records:
            rendered = formatter.format(record)
            assert secret not in rendered, "a fake credential must never reach any log line"
            assert "Traceback" not in rendered, "a raw exception traceback must never be logged here"


class TestRefreshPersistenceHonesty:
    """`RefreshReport.persisted=False` must never be silently treated as success."""

    @pytest.mark.parametrize("invoke", ["command", "callback"])
    def test_unpersisted_refresh_is_not_silently_treated_as_success(
        self, tmp_path: Path, invoke: str, monkeypatch: pytest.MonkeyPatch,
    ):
        from model_settings import ModelSettingsError

        p = _provider_config("p")
        seed = CatalogDocument(
            1, 1, None,
            (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (_model("seed-model", "p", context_tokens=100),),
        )
        registry, settings, discovery = _make_registry(tmp_path, (p,), seed_document=seed)
        discovery.responses["p"] = _inventory("p", (_model("new-model", "p", context_tokens=200),))

        settings_path = tmp_path / "models-config.toml"
        original_bytes = settings_path.read_bytes()
        monkeypatch.setattr(
            settings, "write",
            lambda _doc: (_ for _ in ()).throw(ModelSettingsError("write_failed")),
        )

        iface = _make_iface(model_registry=registry)
        if invoke == "command":
            update, ctx, captured = _make_command_update()
            asyncio.run(cmd_models(iface, update, ctx))
            text = captured["text"]
        else:
            from telegram_callbacks import cb_models_refresh

            update, query = _make_callback_update("models_refresh")
            asyncio.run(cb_models_refresh(iface, update, MagicMock()))
            text = query.edit_message_text.call_args.args[0]

        assert "p/new-model" in text, "successful discovery stays usable in memory when only the cache write fails"
        assert any(
            phrase in text.lower()
            for phrase in ("not saved", "not persisted", "unsaved", "failed to save", "could not save")
        ), "a failed-to-persist refresh must say so explicitly, not silently reuse the old 'fresh' label"
        assert "session" in text.lower() and "last known" not in text.lower()
        assert settings_path.read_bytes() == original_bytes, (
            "a failed write must never partially touch the on-disk settings file"
        )


class TestCompleteEntriesUnderMessageLimit:
    """Capping/truncation must never half-render a model record."""

    def test_twenty_fully_populated_models_render_complete_blocks_with_truthful_count(self, tmp_path: Path):
        p = _provider_config("p")
        models = tuple(
            _model(
                f"model-{i:02d}", "p",
                context_tokens=4096, input_tokens=2048, output_tokens=1024,
                vision=bool(i % 2), tool_calling=bool((i + 1) % 2), reasoning=bool(i % 3 == 0),
            )
            for i in range(25)
        )
        registry, _, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", models)

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert len(text) <= 4096
        _assert_valid_telegram_html(text)

        candidate_ids = [f"p/model-{i:02d}" for i in range(25)]
        complete = _complete_entries(text, candidate_ids)
        reported_shown, reported_total = _reported_shown_and_total(text)

        assert reported_total == 25
        assert reported_shown == 20, "ordinary fully-populated entries must actually reach the ~20 cap"
        assert len(complete) == reported_shown, (
            "the header's shown count must match the number of ACTUALLY complete entries, "
            f"not a pre-truncation estimate: header said {reported_shown}, found {len(complete)} complete"
        )
        assert str(25 - reported_shown) in text, "the explicit remainder count must be visible"

    def test_aggregate_pressure_multiple_providers_long_identifiers_no_partial_entries(self, tmp_path: Path):
        providers = tuple(_provider_config(f"prov{n}") for n in range(3))
        registry, _, discovery = _make_registry(tmp_path, providers)
        for n, prov in enumerate(providers):
            models = tuple(
                _model(
                    f"very-long-model-identifier-segment-{n}-{i:02d}", prov.name,
                    context_tokens=8192, input_tokens=4096, output_tokens=2048,
                    vision=True, tool_calling=False, reasoning=(i % 2 == 0),
                    display_name=(f"Long Display Name For Provider {n} Model {i:02d} " * 2).strip(),
                )
                for i in range(10)
            )
            discovery.responses[prov.name] = _inventory(prov.name, models)

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert len(text) <= 4096
        _assert_valid_telegram_html(text)
        for prov in providers:
            assert prov.name in text, f"provider {prov.name} must not be silently dropped"

        candidate_ids = [
            f"{prov.name}/very-long-model-identifier-segment-{n}-{i:02d}"
            for n, prov in enumerate(providers)
            for i in range(10)
        ]
        complete = _complete_entries(text, candidate_ids)
        reported_shown, reported_total = _reported_shown_and_total(text)

        assert reported_total == 30
        assert len(complete) >= 1, "at least one fully complete entry must survive the pressure case"
        assert len(complete) == reported_shown, (
            "with long labels the layout may truthfully show fewer than 20, but the header "
            f"count must match reality: header said {reported_shown}, found {len(complete)} complete"
        )
        assert str(30 - reported_shown) in text, "the explicit remainder must reflect the real shown count"


class TestProviderSectionBudgeted:
    """The provider section itself must be budgeted — it cannot be allowed
    to push the whole message over the limit unconditionally, and it must
    never silently drop providers without saying so."""

    def test_many_providers_with_long_ids_and_universal_credential_failure(self, tmp_path: Path):
        long_ids = [(f"provider-{n:02d}-" + "x" * 116) for n in range(25)]
        assert all(len(pid) == 128 for pid in long_ids)
        providers = tuple(_provider_config(pid) for pid in long_ids)

        resolver_calls: list[str] = []

        def _always_fail_resolve(provider):
            resolver_calls.append(provider.name)
            raise ValueError(f"credential lookup failed for {provider.name}")

        registry, _, discovery = _make_registry(tmp_path, providers, resolver=_always_fail_resolve)

        iface = _make_iface(model_registry=registry)
        update, ctx, captured = _make_command_update()
        asyncio.run(cmd_models(iface, update, ctx))

        text = captured["text"]
        assert len(resolver_calls) == 25, "every configured provider's resolver must be attempted"
        assert discovery.calls == [], "discovery must never run once credential resolution fails"

        assert len(text) <= 4096
        _assert_valid_telegram_html(text)
        assert "no selection" in text, "selection state must stay visible even under provider pressure"

        shown_providers = [pid for pid in long_ids if pid in text]
        reported_shown, reported_total = _reported_shown_and_total(text)
        assert reported_total == 25
        assert len(shown_providers) == reported_shown, (
            "the header's shown-provider count must match the number of full provider ids "
            f"actually visible: header said {reported_shown}, found {len(shown_providers)}"
        )
        assert str(25 - reported_shown) in text, "the omitted-provider count must be explicit"
        assert "fake" not in text.lower() and "credential lookup failed" not in text, (
            "the raw resolver failure message must never reach the UI"
        )


# ---------------------------------------------------------------------------
# Selection callback (new, planned: telegram_callbacks.cb_model_select) —
# task 8.2 second slice. `cb_model_select` does not exist yet; every test
# below looks it up via `_get_cb_model_select()` so a missing handler fails
# the individual test (RED), never test-file collection.
# ---------------------------------------------------------------------------

class TestModelSelectionCallback:
    """Real `ModelRegistry`/`ModelSettings`/`SelectionTokenCache` — no mocked
    `select`/`view`/cache internals anywhere in this class."""

    def test_select_callback_persists_before_any_ack_or_edit(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("provider-" + "a" * 50)
        target = _model("model-" + "z" * 50, p.name, context_tokens=100, display_name="Target-" + "z" * 50)
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses[p.name] = _inventory(p.name, (target,))

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-")

        events: list[str] = []
        original_write = settings.write

        def _recording_write(doc):
            original_write(doc)
            events.append("persisted")

        settings.write = _recording_write

        update, query = _make_callback_update(token)
        query.answer = AsyncMock(side_effect=lambda *a, **k: events.append("acked"))
        query.edit_message_text = AsyncMock(side_effect=lambda *a, **k: events.append("edited"))

        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert "persisted" in events, "a successful selection must actually call through to the real settings.write"
        assert "acked" in events and "edited" in events
        assert events.index("persisted") < events.index("acked"), "no ack before the selection is durably saved"
        assert events.index("persisted") < events.index("edited"), "no success edit before the selection is saved"

        assert registry.view().active_model_id == target.id
        loaded = settings.load()
        assert loaded.document is not None and loaded.document.default_model == target.id

    def test_select_callback_reports_failure_and_leaves_prior_selection_unchanged_when_persistence_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        cb_model_select = _get_cb_model_select()
        from model_settings import ModelSettingsError

        p = _provider_config("p")
        old = _model("old-model", "p", context_tokens=100, display_name="Old-Model")
        target = _model("target-model", "p", context_tokens=200, display_name="Target-Model")
        seed = CatalogDocument(
            1, 1, "p/old-model",
            (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (old,),
        )
        registry, settings, discovery = _make_registry(tmp_path, (p,), seed_document=seed)
        registry.restore_selection()
        discovery.responses["p"] = _inventory("p", (old, target))

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-Model")

        settings_path = tmp_path / "models-config.toml"
        original_bytes = settings_path.read_bytes()
        monkeypatch.setattr(
            settings, "write",
            lambda _doc: (_ for _ in ()).throw(ModelSettingsError("write_failed")),
        )

        update, query = _make_callback_update(token)
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id == "p/old-model"
        assert registry.view().remembered_model_id == "p/old-model"
        assert settings_path.read_bytes() == original_bytes, "a failed write must never touch the saved selection"
        query.edit_message_text.assert_awaited()
        text = query.edit_message_text.call_args.args[0]
        assert "✅" not in text and "switched" not in text.lower(), "a failed save must never read as success"

    def test_select_callback_succeeds_for_target_retained_through_refresh_failure(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        seed = CatalogDocument(
            1, 1, None,
            (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
            (target,),
        )
        registry, settings, discovery = _make_registry(tmp_path, (p,), seed_document=seed)
        discovery.responses["p"] = RuntimeError("provider down")

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)  # cmd_models's own refresh fails; target stays stale-but-listed
        token = _button_callback_data(markup, label_contains="Target-Model")

        update, query = _make_callback_update(token)
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id == target.id
        loaded = settings.load()
        assert loaded.document is not None and loaded.document.default_model == target.id

    def test_select_callback_ignores_catalog_revision_change_when_target_still_present(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-Model")

        registry.refresh()  # same inventory again: bumps catalog revision; target untouched

        update, query = _make_callback_update(token)
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id == target.id, (
            "a revision bump that leaves the target model untouched must not invalidate its selection token"
        )

    def test_select_callback_selects_target_removed_before_redemption(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("p")
        old = _model("old-model", "p", context_tokens=100, display_name="Old-Model")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (old, target))

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-Model")

        registry.select(old.id)
        discovery.responses["p"] = _inventory("p", (old,))  # a later successful refresh drops the target
        registry.refresh()

        settings_path = tmp_path / "models-config.toml"
        before_bytes = settings_path.read_bytes()

        update, query = _make_callback_update(token)
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id == target.id, "the cache is not a whitelist: the captured selector still selects"
        assert settings_path.read_bytes() != before_bytes, "the deliberate selection is persisted"
        query.edit_message_text.assert_awaited()
        text = query.edit_message_text.call_args.args[0]
        assert "Switched to" in text and target.id in text

    @pytest.mark.parametrize("case", ["expired_at_ttl", "unknown_token", "evicted_by_cap", "cache_restarted"])
    def test_select_callback_rejects_stale_or_unknown_tokens(self, tmp_path: Path, case: str):
        cb_model_select = _get_cb_model_select()
        from telegram_model_selection import SelectionTokenCache

        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        fake_now = [1_000.0]

        def clock() -> float:
            return fake_now[0]

        iface = _make_iface(model_registry=registry)
        iface.model_selection_tokens = SelectionTokenCache(
            clock=clock, max_entries=1 if case == "evicted_by_cap" else 128,
        )

        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-Model")

        if case == "expired_at_ttl":
            fake_now[0] += 300.0  # exactly at the approved 5-minute TTL boundary
        elif case == "unknown_token":
            body = token[len("modelsel:"):]
            flipped_char = "x" if body[-1] != "x" else "y"
            token = token[: -len(body)] + body[:-1] + flipped_char  # same shape, one deterministically different char
        elif case == "evicted_by_cap":
            _mint_via_cmd_models(iface)  # a second mint legitimately evicts the first (max_entries=1)
        elif case == "cache_restarted":
            iface.model_selection_tokens = SelectionTokenCache(clock=clock)  # brand-new process-memory cache

        settings_path = tmp_path / "models-config.toml"
        before_bytes = settings_path.read_bytes() if settings_path.exists() else b""

        update, query = _make_callback_update(token)
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id is None
        after_bytes = settings_path.read_bytes() if settings_path.exists() else b""
        assert after_bytes == before_bytes, f"case={case}: a rejected token must never write to disk"
        query.edit_message_text.assert_awaited()
        text = query.edit_message_text.call_args.args[0]
        assert "/models" in text

    def test_select_callback_rejects_redemption_by_different_user(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        iface = _make_iface(model_registry=registry)
        iface.allowed_ids = {42, 43}
        _text, markup = _mint_via_cmd_models(iface, user_id=42)
        token = _button_callback_data(markup, label_contains="Target-Model")

        update, query = _make_callback_update(token, user_id=43)  # allowlisted but did not mint this token
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id is None
        query.edit_message_text.assert_awaited()

    def test_select_callback_rejects_redemption_from_different_chat(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface, chat_id=1001)
        token = _button_callback_data(markup, label_contains="Target-Model")

        update, query = _make_callback_update(token, chat_id=1002)  # same user, different chat
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert registry.view().active_model_id is None
        query.edit_message_text.assert_awaited()

    def test_select_callback_rejects_unauthorized_user_before_registry_or_cache_io(self, tmp_path: Path):
        cb_model_select = _get_cb_model_select()
        from telegram_model_selection import SelectionTokenCache

        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        write_calls: list[int] = []
        original_write = settings.write

        def _recording_write(doc):
            write_calls.append(1)
            return original_write(doc)

        settings.write = _recording_write

        armed = [False]

        def _clock() -> float:
            if armed[0]:
                raise AssertionError("cache clock must not be consulted for an unauthorized caller")
            return 1_000.0

        iface = _make_iface(model_registry=registry)
        iface.model_selection_tokens = SelectionTokenCache(clock=_clock)

        _text, markup = _mint_via_cmd_models(iface)
        token = _button_callback_data(markup, label_contains="Target-Model")

        settings_path = tmp_path / "models-config.toml"
        before_bytes = settings_path.read_bytes() if settings_path.exists() else b""
        write_calls.clear()
        armed[0] = True

        update, query = _make_callback_update(token, user_id=999)  # not in allowed_ids
        asyncio.run(cb_model_select(iface, update, MagicMock()))

        assert write_calls == [], "an unauthorized caller must never trigger a settings write"
        after_bytes = settings_path.read_bytes() if settings_path.exists() else b""
        assert after_bytes == before_bytes
        assert registry.view().active_model_id is None

    def test_legacy_model_switch_callback_is_noop_when_registry_present(self, tmp_path: Path):
        from telegram_callbacks import cb_model_switch

        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        legacy_client = MagicMock()
        legacy_client.set_model = MagicMock(return_value=True)
        iface = _make_iface(model_registry=registry, llm_client=legacy_client)

        update, query = _make_callback_update(f"model:{target.id}")
        asyncio.run(cb_model_switch(iface, update, MagicMock()))

        legacy_client.set_model.assert_not_called()
        assert registry.view().active_model_id is None

    def test_refresh_callback_reissues_selection_controls_for_shown_models(self, tmp_path: Path):
        from telegram_callbacks import cb_models_refresh

        p = _provider_config("p")
        target = _model("target-model", "p", context_tokens=100, display_name="Target-Model")
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", (target,))

        iface = _make_iface(model_registry=registry)
        update, query = _make_callback_update("models_refresh")
        asyncio.run(cb_models_refresh(iface, update, MagicMock()))

        markup = query.edit_message_text.call_args.kwargs.get("reply_markup")
        selection = _selection_buttons(markup)
        assert selection, "cb_models_refresh must reissue selection controls, not leave only the refresh button"
        for cb in selection:
            assert cb.startswith("modelsel:")

        cb_model_select = getattr(__import__("telegram_callbacks"), "cb_model_select", None)
        assert cb_model_select is not None, "cb_model_select is not implemented yet (task 8.2 next slice)"
        select_token = _button_callback_data(markup, label_contains="Target-Model")
        select_update, select_query = _make_callback_update(select_token)
        asyncio.run(cb_model_select(iface, select_update, MagicMock()))
        assert registry.view().active_model_id == target.id, (
            "a control reissued by cb_models_refresh must redeem for the same user/chat it was minted for"
        )

    def test_selection_controls_match_shown_models_not_full_inventory(self, tmp_path: Path):
        """Regression lock: already expected to pass with the first slice."""
        p = _provider_config("p")
        models = tuple(_model(f"model-{i:02d}", "p", context_tokens=100) for i in range(25))
        registry, settings, discovery = _make_registry(tmp_path, (p,))
        discovery.responses["p"] = _inventory("p", models)

        iface = _make_iface(model_registry=registry)
        _text, markup = _mint_via_cmd_models(iface)

        selection = _selection_buttons(markup)
        assert len(selection) == 20, "exactly the shown (capped) models get a control, never the full inventory"
        for cb in selection:
            assert cb.startswith("modelsel:")
        assert "models_refresh" in _flatten_buttons(markup)
