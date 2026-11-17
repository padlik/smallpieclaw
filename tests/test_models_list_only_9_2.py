"""Task 9.2 list-only Telegram model selection regressions (ADR-0045)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import telegram_callbacks
from telegram_commands import cmd_models
from tests.test_models_catalog_9_1 import SECRET, build, pa_models, run_models
from tests.test_telegram_models import (
    _inventory, _make_callback_update, _make_command_update, _make_iface, _mint_via_cmd_models,
)

GUIDANCE = "No models are cached yet. Tap 🔄 Refresh, then choose one with its button."


def _button(markup: object, label: str) -> str:
    """Return a real catalog button's opaque callback token."""
    for row in markup.inline_keyboard:
        for button in row:
            if label in button.text:
                return button.callback_data
    raise AssertionError(f"No {label!r} button")


def _select(iface: object, token: str) -> str:
    """Invoke the real selection callback with its original scoped token."""
    update, query = _make_callback_update(token)
    asyncio.run(telegram_callbacks.cb_model_select(iface, update, MagicMock()))
    return query.edit_message_text.await_args.args[0]


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"saved": "pa/new"}, {"corrupt_pref": True}, {"corrupt_cache": True}],
    ids=["empty", "uncached-saved", "corrupt-preference", "unreadable-cache"],
)
def test_empty_catalog_offers_refresh_without_a_typed_route(tmp_path: Path, kwargs: dict) -> None:
    """All empty-cache states direct operators to Refresh and then a button."""
    env = build(tmp_path, **kwargs)
    text, markup = run_models(env)
    assert GUIDANCE in text
    assert "/models select" not in text
    assert [button.text for row in markup.inline_keyboard for button in row] == ["🔄 Refresh"]


def test_readme_directs_selection_to_buttons() -> None:
    """User-facing documentation must not advertise an unimplemented route."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "/models select" not in readme
    assert "choose a model with its button once candidates appear" in " ".join(readme.split())


@pytest.mark.parametrize("saved", [None, "pa/m1"], ids=["automatic", "saved"])
def test_typed_models_arguments_leave_selection_untouched(tmp_path: Path, saved: str | None) -> None:
    """Typed arguments merely list models, never persisting a deliberate selection."""
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), saved=saved)
    expected, _ = run_models(env)
    before = (
        env.registry.view().active_model_id,
        env.pref_path.read_bytes() if env.pref_path.exists() else None,
    )
    iface = _make_iface(model_registry=env.registry)
    for args in (["select", "pa/m2"], ["select", "gone/m3"], ["select", "pa/"]):
        update, ctx, captured = _make_command_update()
        ctx.args = args
        asyncio.run(cmd_models(iface, update, ctx))
        assert captured["text"] == expected
    assert (
        env.registry.view().active_model_id,
        env.pref_path.read_bytes() if env.pref_path.exists() else None,
    ) == before


def test_corrupt_preference_button_persists_before_publishing_active_selection(tmp_path: Path) -> None:
    """A real candidate button replaces corrupt bytes before the active state changes."""
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), corrupt_pref=True)
    iface = _make_iface(model_registry=env.registry, llm_client=None)
    _, keyboard = _mint_via_cmd_models(iface)
    token = _button(keyboard, "m2")
    original_write = env.registry._preference.write
    at_write: list[tuple[str | None, bytes]] = []

    def write(selector: str) -> None:
        original_write(selector)
        at_write.append((env.registry.view().active_model_id, env.pref_path.read_bytes()))

    env.registry._preference.write = write
    message = _select(iface, token)
    assert len(at_write) == 1 and at_write[0][0] is None and b"pa/m2" in at_write[0][1]
    assert env.registry.view().active_model_id == "pa/m2"
    assert "Switched to" in message
    assert "saved model selection is invalid" not in run_models(env)[0]


def test_failed_button_save_preserves_corrupt_bytes_and_selection(tmp_path: Path) -> None:
    """A failed write cannot publish success, expose IO details or alter selection."""
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), corrupt_pref=True)
    iface = _make_iface(model_registry=env.registry)
    _, keyboard = _mint_via_cmd_models(iface)
    before = env.pref_path.read_bytes()

    def fail(_selector: str) -> None:
        raise OSError(f"disk full /secret/{SECRET}")

    env.registry._preference.write = fail
    message = _select(iface, _button(keyboard, "m2"))
    assert "Switched to" not in message and SECRET not in message and "disk full" not in message
    assert env.pref_path.read_bytes() == before and env.registry.view().active_model_id is None
    assert "saved model selection is invalid" in run_models(env)[0]


def test_refresh_then_button_repairs_corrupt_preference_without_main(tmp_path: Path) -> None:
    """Refresh is cache-only; a subsequent candidate button performs the repair."""
    env = build(tmp_path, corrupt_pref=True)
    before = env.pref_path.read_bytes()
    text, keyboard = run_models(env)
    assert GUIDANCE in text and SECRET not in text
    assert [button.text for row in keyboard.inline_keyboard for button in row] == ["🔄 Refresh"]
    env.discovery.responses["pa"] = _inventory("pa", pa_models("m1"))
    iface = _make_iface(model_registry=env.registry, llm_client=None)
    update, query = _make_callback_update("models_refresh")
    asyncio.run(telegram_callbacks.cb_models_refresh(iface, update, MagicMock()))
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert env.pref_path.read_bytes() == before and env.registry.view().active_model_id is None
    assert "Switched to" in _select(iface, _button(markup, "m1"))
    assert env.registry.view().active_model_id == "pa/m1"
    assert "saved model selection is invalid" not in run_models(env)[0]


def test_failed_refresh_does_not_repair_corrupt_preference(tmp_path: Path) -> None:
    """Provider failure cannot make Refresh write a preference or select a model."""
    env = build(tmp_path, corrupt_pref=True)
    before = env.pref_path.read_bytes()
    env.discovery.responses["pa"] = RuntimeError(f"failure: {SECRET}")
    iface = _make_iface(model_registry=env.registry)
    update, query = _make_callback_update("models_refresh")
    asyncio.run(telegram_callbacks.cb_models_refresh(iface, update, MagicMock()))
    message = query.edit_message_text.await_args.args[0]
    assert GUIDANCE in message and SECRET not in message
    assert env.pref_path.read_bytes() == before and env.registry.view().active_model_id is None
