"""`/models` catalog presentation on the split cache + preference wiring (task 9.1, ADR-0040).

Spec ``telegram-command-surface``: *`/models` lists, refreshes, and selects discovered models*.
Registries are built exactly like ``main.py`` builds them (``ModelCache`` + ``ModelPreference``),
discovery and the Telegram transport are the only substituted boundaries; the real ``cmd_models``
and ``cb_models_refresh`` handlers render. 9.2 (text select) and 9.3 (callbacks) are not covered.
"""

from __future__ import annotations

import asyncio
import html
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_callbacks  # noqa: E402
from model_registry import CatalogDocument, ModelRegistry, ProviderState  # noqa: E402
from model_settings import ModelCache, ModelPreference  # noqa: E402
from telegram_commands import cmd_models  # noqa: E402
from telegram_formatter import TELEGRAM_RENDERED_LIMIT, format_models_catalog  # noqa: E402
from tests.test_telegram_models import (  # noqa: E402
    FakeDiscovery,
    _inventory,
    _make_callback_update,
    _make_command_update,
    _make_iface,
    _model,
    _provider_config,
    _resolve,
)

GUIDANCE = "No models are cached yet. Tap 🔄 Refresh, then choose one with its button."
ADVISORY = "(advisory, provider-reported)"
SECRET = "sk-file-content-secret-9-1"


class Env:
    """A real split-wiring registry plus the files it reads and the discovery double."""

    def __init__(self, tmp_path: Path) -> None:
        self.cache_path = tmp_path / "models-cache.json"
        self.pref_path = tmp_path / "model-selection.toml"
        self.discovery = FakeDiscovery()
        self.registry: ModelRegistry | None = None


def build(
    tmp_path: Path, connections: tuple = (), *, cache_models: tuple = (), saved: str | None = None,
    corrupt_pref: bool = False, corrupt_cache: bool = False,
) -> Env:
    """Seed files, construct like main.py, then refresh + restore the selection like startup."""
    env = Env(tmp_path)
    connections = connections or (_provider_config("pa"),)
    cache = ModelCache(env.cache_path)
    preference = ModelPreference(env.pref_path)
    if cache_models:
        providers = tuple(ProviderState(c.name, "openai", "2026-10-01T12:00:00Z", "fresh") for c in connections)
        cache.write(CatalogDocument(1, 1, None, providers, cache_models))
    if corrupt_cache:
        env.cache_path.write_text(f"{{not json {SECRET}", encoding="utf-8")
    if saved is not None:
        preference.write(saved)
    if corrupt_pref:
        env.pref_path.write_text(f"garbage = [ {SECRET}", encoding="utf-8")
    env.registry = ModelRegistry(
        {c.name: c for c in connections}, cache, env.discovery, _resolve, preference=preference,
    )
    for conn in connections:  # discovery answers with the same cached candidates
        mine = tuple(m for m in cache_models if m.provider_id == conn.name)
        env.discovery.responses[conn.name] = _inventory(conn.name, mine)
    env.registry.refresh()
    env.registry.restore_selection()
    return env


def run_models(env: Env) -> tuple[str, object]:
    """Real ``cmd_models``; returns the sent text and keyboard."""
    iface = _make_iface(model_registry=env.registry)
    update, ctx, captured = _make_command_update()
    asyncio.run(cmd_models(iface, update, ctx))
    return captured["text"], captured["reply_markup"]


def pa_models(*names: str, **kw) -> tuple:
    return tuple(_model(n, "pa", **kw) for n in names)


# ---------------------------------------------------------------------------
# Selection: saved vs unsaved automatic, uncached, none
# ---------------------------------------------------------------------------


def test_saved_active_selection_is_labelled_saved(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), saved="pa/m1")

    text, _ = run_models(env)

    assert "Selection: active (saved)" in text and "Active: <code>pa/m1</code>" in text


def test_unsaved_automatic_active_selection_is_labelled_and_never_saved(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1", "m2"))

    text, _ = run_models(env)

    assert "Selection: active (automatic, unsaved)" in text and "Active: <code>pa/m1</code>" in text
    assert not env.pref_path.exists(), "an automatic choice must never create the preference"


def test_uncached_saved_active_selector_is_still_active_and_not_unavailable(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), saved="pa/retired")

    text, _ = run_models(env)

    assert "Selection: active (saved)" in text and "Active: <code>pa/retired</code>" in text
    assert "unavailable" not in text.lower()


def test_empty_cache_without_selection_says_no_selection(tmp_path: Path) -> None:
    env = build(tmp_path)

    text, _ = run_models(env)

    assert "Selection: no selection" in text and "Active:" not in text and "Saved:" not in text


def test_saved_selector_that_is_not_active_is_labelled_saved_not_remembered(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), saved="gone/m9")  # connection not configured

    text, _ = run_models(env)

    assert "Selection: saved selection unavailable" in text
    assert "Saved: <code>gone/m9</code>" in text and "Remembered" not in text


# ---------------------------------------------------------------------------
# Advisory label in every layout, empty-cache guidance
# ---------------------------------------------------------------------------


def test_advisory_label_is_in_the_verbose_layout(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1", "m2", context_tokens=1000))

    text, _ = run_models(env)

    assert f"<b>Models</b> {ADVISORY} (2 shown of 2 total)" in text
    assert "context_tokens: 1000" in text


def test_advisory_label_survives_the_compact_layout(tmp_path: Path) -> None:
    models = tuple(_model(f"model-{i:02d}", "pa", context_tokens=1000 + i, vision=True, display_name="N" * 120) for i in range(20))
    env = build(tmp_path, cache_models=models, saved="pa/model-00")

    text, _ = run_models(env)

    from telegram_formatter import _COMPACT_LEGEND

    assert _COMPACT_LEGEND in text, "fixture must force the compact layout"
    assert ADVISORY in text and len(text) <= TELEGRAM_RENDERED_LIMIT


def test_empty_cache_shows_refresh_and_direct_select_guidance(tmp_path: Path) -> None:
    env = build(tmp_path)

    text, keyboard = run_models(env)

    assert f"<b>Models</b> {ADVISORY} (none discovered)\n{GUIDANCE}" in text
    assert keyboard.inline_keyboard[0][0].text == "🔄 Refresh"


def test_guidance_still_shows_beside_an_uncached_saved_active_selector(tmp_path: Path) -> None:
    env = build(tmp_path, saved="pa/new")

    text, _ = run_models(env)

    assert GUIDANCE in text and "Active: <code>pa/new</code>" in text


def test_guidance_is_absent_when_models_are_cached(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"))

    text, _ = run_models(env)

    assert "No models are cached yet" not in text


# ---------------------------------------------------------------------------
# Registry notices: content-free, escaped, ordered
# ---------------------------------------------------------------------------


def test_corrupt_preference_notice_is_shown_without_file_content(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), corrupt_pref=True)

    text, _ = run_models(env)

    notice = env.registry.selection_notice
    assert notice and f"⚠️ <i>{html.escape(notice)}</i>" in text
    assert "Selection: no selection" in text and SECRET not in text
    assert "garbage" not in text and str(env.pref_path) not in text


def test_unreadable_cache_notice_is_shown_without_file_content(tmp_path: Path) -> None:
    env = build(tmp_path, corrupt_cache=True)

    text, _ = run_models(env)

    notice = env.registry.cache_notice
    assert notice and f"⚠️ <i>{html.escape(notice)}</i>" in text
    assert SECRET not in text and str(env.cache_path) not in text and GUIDANCE in text


def test_unconfigured_saved_connection_notice_is_shown(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), saved="gone/m9")

    text, _ = run_models(env)

    assert html.escape(env.registry.selection_notice) in text


def test_no_notice_lines_when_files_are_healthy(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), saved="pa/m1")

    text, _ = run_models(env)

    assert env.registry.cache_notice is None and env.registry.selection_notice is None
    assert "⚠️" not in text


def test_notices_follow_the_refresh_outcome_selection_first_then_cache(tmp_path: Path) -> None:
    env = build(tmp_path, corrupt_cache=True, corrupt_pref=True)
    env.registry._settings.write = _raise_oserror  # type: ignore[method-assign]

    text, _ = run_models(env)

    not_saved = text.index("Refresh was not saved")
    selection = text.index(html.escape(env.registry.selection_notice))
    cache = text.index(html.escape(env.registry.cache_notice))
    assert not_saved < selection < cache < text.index("Selection: ")


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError(f"disk full at /secret/{SECRET}")


def test_formatter_kwargs_are_optional_and_escaped(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"))
    view = env.registry.view()

    assert "⚠️" not in format_models_catalog(view)
    text = format_models_catalog(view, cache_notice="c<&>", selection_notice="s<&>")
    assert "⚠️ <i>s&lt;&amp;&gt;</i>" in text and "⚠️ <i>c&lt;&amp;&gt;</i>" in text
    assert "c<&>" not in text and "s<&>" not in text


# ---------------------------------------------------------------------------
# Refresh touches the cache only (real handlers)
# ---------------------------------------------------------------------------


def _state(env: Env) -> tuple:
    view = env.registry.view()
    return (
        view.active_model_id, view.remembered_model_id,
        env.pref_path.read_bytes() if env.pref_path.exists() else None,
    )


@pytest.mark.parametrize("saved", [None, "pa/m1", "pa/retired"], ids=["automatic", "saved", "uncached-saved"])
def test_the_models_command_and_refresh_callback_leave_selection_and_preference_alone(
    tmp_path: Path, saved: str | None,
) -> None:
    env = build(tmp_path, cache_models=pa_models("m1", "m2"), saved=saved)
    before = _state(env)
    iface = _make_iface(model_registry=env.registry)
    update, ctx, captured = _make_command_update()
    asyncio.run(cmd_models(iface, update, ctx))
    cb_update, query = _make_callback_update("models_refresh")

    asyncio.run(telegram_callbacks.cb_models_refresh(iface, cb_update, ctx))

    assert _state(env) == before
    edited = query.edit_message_text.await_args.args[0]
    assert ("active (automatic, unsaved)" if saved is None else "active (saved)") in edited
    assert ADVISORY in edited


def test_a_refresh_that_cannot_persist_keeps_the_selection_and_leaks_nothing(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), saved="pa/m1")
    before = _state(env)
    env.registry._settings.write = _raise_oserror  # type: ignore[method-assign]

    text, _ = run_models(env)

    assert "Refresh was not saved" in text and "Selection: active (saved)" in text
    assert SECRET not in text and "/secret/" not in text and "disk full" not in text
    assert _state(env) == before


# ---------------------------------------------------------------------------
# Worst-case size: critical lines survive within Telegram's limit
# ---------------------------------------------------------------------------


def test_worst_case_message_fits_and_keeps_the_critical_lines(tmp_path: Path) -> None:
    long_id = "<é世界>" * 30
    models = tuple(
        _model(f"{long_id}-{i}", "pa", context_tokens=1000 + i, vision=True, display_name=f"Name & <{i}> {long_id}")
        for i in range(25)
    )
    env = build(tmp_path, cache_models=models, saved=f"pa/{long_id}", corrupt_cache=True)

    text, _ = run_models(env)

    assert len(text) <= TELEGRAM_RENDERED_LIMIT
    assert html.escape(env.registry.cache_notice) in text
    assert "Active: <code>" in text and ADVISORY in text and "&lt;" in text
    assert "<é世界>" not in text


def test_worst_case_empty_cache_with_both_notices_keeps_the_guidance(tmp_path: Path) -> None:
    env = build(tmp_path, corrupt_cache=True, corrupt_pref=True)

    text, _ = run_models(env)

    assert len(text) <= TELEGRAM_RENDERED_LIMIT and GUIDANCE in text
    assert html.escape(env.registry.cache_notice) in text and html.escape(env.registry.selection_notice) in text


# ---------------------------------------------------------------------------
# Oracle P2A: the standing selection notice follows the resolved state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["corrupt", "unconfigured"])
def test_selecting_a_cached_model_recovers_and_clears_the_selection_notice(tmp_path: Path, bad: str) -> None:
    """After the real select route replaces an unusable preference, /models is consistent: saved and active."""
    if bad == "corrupt":
        env = build(tmp_path, cache_models=pa_models("m1", "m2"), corrupt_pref=True)
    else:
        env = build(tmp_path, cache_models=pa_models("m1", "m2"), saved="gone/m9")
    notice = env.registry.selection_notice
    assert notice and html.escape(notice) in run_models(env)[0]

    env.registry.select("pa/m1")
    text, _ = run_models(env)

    assert "Selection: active (saved)" in text and "Active: <code>pa/m1</code>" in text
    assert html.escape(notice) not in text and "⚠️" not in text
    assert "pa/m1" in env.pref_path.read_text(encoding="utf-8") and SECRET not in env.pref_path.read_text(encoding="utf-8")


def test_recovery_does_not_hide_a_still_present_cache_notice(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), corrupt_pref=True, corrupt_cache=True)
    env.registry.select("pa/m1")

    text, _ = run_models(env)

    assert html.escape(env.registry.cache_notice) in text and html.escape(env.registry.selection_notice) not in text


def test_a_failed_save_keeps_the_notice_no_selection_and_the_old_preference_bytes(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), corrupt_pref=True)
    before = _state(env)
    env.registry._preference.write = _raise_oserror  # type: ignore[method-assign]

    with pytest.raises(Exception) as failure:
        env.registry.select("pa/m1")
    text, _ = run_models(env)

    assert getattr(failure.value, "code", None) == "persistence_failed"
    assert html.escape(env.registry.selection_notice) in text and "Selection: no selection" in text
    assert "Active:" not in text and _state(env) == before


def test_refresh_callback_keeps_an_unresolved_notice(tmp_path: Path) -> None:
    env = build(tmp_path, cache_models=pa_models("m1"), corrupt_pref=True)
    iface = _make_iface(model_registry=env.registry)
    update, ctx, _captured = _make_command_update()
    cb_update, query = _make_callback_update("models_refresh")

    asyncio.run(telegram_callbacks.cb_models_refresh(iface, cb_update, ctx))

    assert html.escape(env.registry.selection_notice) in query.edit_message_text.await_args.args[0]
