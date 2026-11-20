"""Task 9.3: opaque, scoped `/models` selection tokens end to end (ADR-0040, ADR-0045).

Spec ``telegram-command-surface``: candidate buttons use opaque callbacks of at most 64 bytes, reject
expired or unknown tokens, validate only the captured selector's shape and configured connection
(never cache membership), and persist the preference before publishing or reporting success.

Registries are built like ``main.py`` (``ModelCache`` + ``ModelPreference``); discovery and the
Telegram transport are the only substituted boundaries. The real ``cmd_models`` mints tokens and
the real ``cb_model_select`` redeems them.
"""

from __future__ import annotations

import asyncio
import logging
import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import telegram_callbacks
import telegram_model_selection
from model_registry import CatalogDocument, ModelRegistry, ProviderState
from model_settings import ModelCache, ModelPreference
from telegram_model_selection import CALLBACK_PREFIX, SelectionTokenCache
from tests.test_telegram_models import (
    FakeDiscovery,
    FakeResolvedConnection,
    _inventory,
    _make_callback_update,
    _make_iface,
    _mint_via_cmd_models,
    _model,
    _provider_config,
)

CANARY_KEY = "sk-canary-key-9-3-0123456789"
CANARY_URL = "https://canary-endpoint-9-3.invalid/v1"
REOPEN = "/models"


class World:
    """A split-wiring registry with canary credentials and its files."""

    def __init__(self, tmp_path: Path, models: tuple, saved: str | None = None) -> None:
        provider = _provider_config("pa", url=CANARY_URL)
        self.cache_path = tmp_path / "models-cache.json"
        self.pref_path = tmp_path / "model-selection.toml"
        cache = ModelCache(self.cache_path)
        self.preference = ModelPreference(self.pref_path)
        cache.write(CatalogDocument(
            1, 1, (ProviderState("pa", "openai", "2026-10-01T12:00:00Z", "fresh"),), models,
        ))
        if saved is not None:
            self.preference.write(saved)
        self.discovery = FakeDiscovery()
        self.discovery.responses["pa"] = _inventory("pa", models)

        def resolve(config):
            return FakeResolvedConnection(config.name, config.type, config.base_url, api_key=CANARY_KEY)

        self.registry = ModelRegistry({"pa": provider}, cache, self.discovery, resolve, preference=self.preference)
        self.registry.refresh()
        self.registry.restore_selection()
        self.iface = _make_iface(model_registry=self.registry)

    def mint(self, *, user_id: int = 42, chat_id: int = 1001) -> tuple[str, object]:
        """Run the real ``/models`` handler and return its text and keyboard."""
        return _mint_via_cmd_models(self.iface, user_id=user_id, chat_id=chat_id)

    def drop_from_cache(self, *remaining: str) -> None:
        """A later successful refresh that no longer lists some candidates."""
        self.discovery.responses["pa"] = _inventory("pa", tuple(_model(name, "pa") for name in remaining))
        self.registry.refresh()

    def press(self, data: str, *, user_id: int = 42, chat_id: int = 1001) -> str:
        """Redeem ``data`` through the real callback and return the edited message."""
        update, query = _make_callback_update(data, user_id=user_id, chat_id=chat_id)
        asyncio.run(telegram_callbacks.cb_model_select(self.iface, update, MagicMock()))
        return query.edit_message_text.await_args.args[0]

    def pref_bytes(self) -> bytes | None:
        return self.pref_path.read_bytes() if self.pref_path.exists() else None

    def state(self) -> tuple[str | None, bytes | None]:
        return self.registry.view().active_model_id, self.pref_bytes()


def token_for(markup: object, label: str) -> str:
    for row in markup.inline_keyboard:
        for button in row:
            if label in button.text:
                return button.callback_data
    raise AssertionError(f"No {label!r} button")


def models(*names: str) -> tuple:
    return tuple(_model(name, "pa") for name in names)


def test_valid_token_selects_candidate_removed_by_a_later_refresh(tmp_path: Path) -> None:
    """The cache is not a whitelist: a captured token still selects if the connection remains."""
    world = World(tmp_path, models("m1", "m2"), saved="pa/m1")
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    world.drop_from_cache("m1")
    assert [m.id for m in world.registry.view().models] == ["pa/m1"]

    message = world.press(token)

    assert "Switched to" in message and "pa/m2" in message
    assert world.registry.view().active_model_id == "pa/m2"
    assert world.pref_bytes() is not None and b"pa/m2" in world.pref_bytes()


def test_removed_candidate_selection_never_touches_the_cache_or_calls_discovery(tmp_path: Path) -> None:
    """A button press is local: no provider I/O, no cache rewrite, no membership repair."""
    world = World(tmp_path, models("m1", "m2"))
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    world.drop_from_cache("m1")
    cache_before, calls_before = world.cache_path.read_bytes(), len(world.discovery.calls)

    assert "Switched to" in world.press(token)

    assert world.registry.view().active_model_id == "pa/m2"
    assert world.cache_path.read_bytes() == cache_before
    assert len(world.discovery.calls) == calls_before


def test_valid_token_is_not_consumed_by_redemption(tmp_path: Path) -> None:
    """The same unexpired token keeps working, for example after another selection."""
    world = World(tmp_path, models("m1", "m2"))
    _, keyboard = world.mint()
    first, second = token_for(keyboard, "m1"), token_for(keyboard, "m2")

    world.press(second)
    world.press(first)
    assert "Switched to" in world.press(second)

    assert world.registry.view().active_model_id == "pa/m2"


@pytest.mark.parametrize("selector", ["gone/m1", "pa/", "pa", "/m1", ""], ids=["unconfigured", "empty-upstream", "no-slash", "empty-connection", "empty"])
def test_token_for_unconfigured_or_malformed_selector_is_refused_without_change(tmp_path: Path, selector: str) -> None:
    """Shape and configured connection are still required; refusal changes no byte or state."""
    world = World(tmp_path, models("m1"), saved="pa/m1")
    token = world.iface.model_selection_tokens.issue(selector, user_id=42, chat_id=1001)
    before = world.state()

    message = world.press(token)

    assert REOPEN in message and "Switched to" not in message
    assert world.state() == before


@pytest.mark.parametrize("case", ["expired", "unknown", "evicted", "restarted", "foreign-prefix", "oversized"])
def test_expired_or_unknown_tokens_reopen_with_zero_change_and_no_service_call(tmp_path: Path, case: str) -> None:
    """Dead tokens never select, write or reach the registry or any provider."""
    now = [100.0]
    world = World(tmp_path, models("m1", "m2"), saved="pa/m1")
    world.iface.model_selection_tokens = SelectionTokenCache(
        ttl_seconds=60.0, max_entries=1 if case == "evicted" else 128, clock=lambda: now[0],
    )
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    if case == "expired":
        now[0] += 60.0
    elif case == "unknown":
        token = token[:-1] + ("x" if token[-1] != "x" else "y")
    elif case == "evicted":
        world.mint()
    elif case == "restarted":
        world.iface.model_selection_tokens = SelectionTokenCache(clock=lambda: now[0])
    elif case == "foreign-prefix":
        token = "other:" + token[len(CALLBACK_PREFIX):]
    else:
        token = token + "A" * 80
    before, calls = world.state(), len(world.discovery.calls)
    select_calls: list[str] = []
    original = world.registry.select
    world.registry.select = lambda selector: select_calls.append(selector) or original(selector)

    message = world.press(token)

    assert REOPEN in message and "Switched to" not in message
    assert world.state() == before and select_calls == [] and len(world.discovery.calls) == calls


def test_token_is_valid_one_tick_before_expiry(tmp_path: Path) -> None:
    """The deterministic boundary: age below the TTL selects, age at the TTL does not."""
    now = [100.0]
    world = World(tmp_path, models("m1", "m2"))
    world.iface.model_selection_tokens = SelectionTokenCache(ttl_seconds=60.0, clock=lambda: now[0])
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")

    now[0] += 59.999

    assert "Switched to" in world.press(token)


@pytest.mark.parametrize("wrong", [{"user_id": 43}, {"chat_id": 2002}, {"user_id": 43, "chat_id": 2002}], ids=["user", "chat", "both"])
def test_wrong_scope_attempts_change_nothing_and_do_not_burn_the_holders_token(tmp_path: Path, wrong: dict) -> None:
    """Independent user and chat scopes: a mismatch is refused and the real holder still selects."""
    world = World(tmp_path, models("m1", "m2"))
    world.iface.allowed_ids = {42, 43}
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    before = world.state()

    refused = world.press(token, **wrong)

    assert REOPEN in refused and world.state() == before
    assert "Switched to" in world.press(token)
    assert world.registry.view().active_model_id == "pa/m2"


def test_long_unicode_multi_slash_identity_is_never_callback_data_and_still_selects_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long canonical id with slashes and non-ASCII text rides only server-side.

    Tokens are minted by a deterministic generator of valid ``token_urlsafe(16)``-shaped values that
    all begin with ``org``: a legitimate random token may contain any such word, so opacity is
    asserted structurally and by full identity, never by banning short substrings.
    """
    counter = iter(range(10**6))
    monkeypatch.setattr(
        telegram_model_selection, "secrets",
        SimpleNamespace(token_urlsafe=lambda _n: f"org{next(counter):019d}"),
    )
    upstream = "org/ünï-模型/" + "x" * 150
    world = World(tmp_path, (_model(upstream, "pa"), _model("m1", "pa")))
    _, keyboard = world.mint()
    datas = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert all(len(data.encode("utf-8")) <= 64 for data in datas)
    canonical = f"pa/{upstream}"
    assert "models_refresh" in datas
    tokens = [data for data in datas if data != "models_refresh"]
    assert len(tokens) == 2
    for data in datas:
        assert data.isascii() and canonical not in data and upstream not in data and "pa/" not in data
        assert CANARY_KEY not in data and CANARY_URL not in data
    for data in tokens:
        assert data.startswith(CALLBACK_PREFIX)
        body = data.removeprefix(CALLBACK_PREFIX)
        assert body and set(body) <= set(string.ascii_letters + string.digits + "-_")  # opaque token shape only
    assert all(data.removeprefix(CALLBACK_PREFIX).startswith("org") for data in tokens)  # a legal "org" token is accepted
    target = next(
        button.callback_data for row in keyboard.inline_keyboard for button in row if "x" * 20 in button.text
    )
    world.drop_from_cache("m1")  # the long candidate leaves the cache before the press

    assert "Switched to" in world.press(target)
    assert world.registry.view().active_model_id == f"pa/{upstream}"
    assert world.pref_bytes() == f'model = "pa/{upstream}"\n'.encode()


def test_credentials_and_endpoints_never_reach_view_callbacks_acknowledgement_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolved key and base URL stay out of the list, tokens, success message and log records."""
    world = World(tmp_path, models("m1", "m2"))
    with caplog.at_level(logging.DEBUG):
        text, keyboard = world.mint()
        datas = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        world.drop_from_cache("m1")
        success = world.press(token_for(keyboard, "m2"))
        refused = world.press(world.iface.model_selection_tokens.issue("gone/m", user_id=42, chat_id=1001))
    assert "Switched to" in success and "Switched to" not in refused
    surfaces = [text, success, refused, *datas, caplog.text, world.pref_path.read_text()]
    for surface in surfaces:
        assert CANARY_KEY not in surface and CANARY_URL not in surface and "canary-endpoint" not in surface
        assert "env:FAKE_KEY" not in surface


def test_selection_persists_before_active_publication_and_success(tmp_path: Path) -> None:
    """At write time the old choice is still active; success is reported only afterwards."""
    world = World(tmp_path, models("m1", "m2"), saved="pa/m1")
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    original = world.preference.write
    seen: list[tuple[str | None, bytes | None]] = []

    def write(selector: str) -> None:
        seen.append(world.state())
        original(selector)
        seen.append(world.state())

    world.preference.write = write
    update, query = _make_callback_update(token)
    asyncio.run(telegram_callbacks.cb_model_select(world.iface, update, MagicMock()))

    assert seen[0] == ("pa/m1", b'model = "pa/m1"\n')
    assert seen[1][0] == "pa/m1" and b"pa/m2" in seen[1][1]
    assert world.registry.view().active_model_id == "pa/m2"
    assert "Switched to" in query.edit_message_text.await_args.args[0]


def test_failed_save_keeps_valid_prior_bytes_and_active_selection_without_success(tmp_path: Path) -> None:
    """A failed durable write is reported neutrally: no success text, no detail, nothing changed."""
    world = World(tmp_path, models("m1", "m2"), saved="pa/m1")
    _, keyboard = world.mint()
    token = token_for(keyboard, "m2")
    before = world.state()

    def fail(_selector: str) -> None:
        raise OSError(f"disk full {CANARY_KEY} {CANARY_URL}")

    world.preference.write = fail
    message = world.press(token)

    assert world.state() == before
    assert "Switched to" not in message and "✅" not in message
    assert CANARY_KEY not in message and CANARY_URL not in message and "disk full" not in message
