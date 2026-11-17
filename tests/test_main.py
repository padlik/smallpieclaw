"""Tests for main.py — CLI arg parsing, XDG dir creation, and startup flow helpers.

``TestRootStartupComposition`` drives the actual ``main._run`` through
``tests/root_startup_harness.py`` (see its docstring for the real versus
replaced boundaries). Graph memory stays disabled; graph pin invocation and
caller cutover belong to task 12.25, scheduler closure to 7.3.
"""

from __future__ import annotations

import asyncio
import builtins
import concurrent.futures
import dataclasses
import html
import io
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog

import agent_controller
import graph_memory
import main as main_mod
import sqlite_log
import token_usage
from embedding_client import EmbeddingClient
from llm_client import LLMClient
from model_registry import CatalogView, ModelRegistry, ModelRegistryError, RunModel
from model_settings import ModelCache, ModelPreference
from prompt_builder import format_model_catalog_section
from react_loop import ReactContext
from scheduler import Scheduler
from token_usage import TokenUsageRegistry
from xdg import xdg_paths

from tests.root_startup_harness import (
    ACME_HOST,
    DISCOVERED,
    EMBED_HOST,
    GEM_HOST,
    GONE_ID,
    INVALID_SAVED,
    UNCONFIGURED_ID,
    remembered_for,
    GRAPH_HOST,
    GRAPH_ID,
    GRAPH_UPSTREAM,
    MAIN_ID,
    OTHER_ID,
    SECRETS,
    RootHarness,
    restore_structlog_state,
)
from tests.test_scheduler_load_containment import _UNPARSEABLE, _write_config


class TestAgentNameArg:
    def test_absent_agent_name_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py"])
        with pytest.raises(SystemExit) as exc:
            main_mod._parse_args()
        assert exc.value.code != 0

    def test_present_agent_name_parsed(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "myagent"])
        args = main_mod._parse_args()
        assert args.agent_name == "myagent"


class TestCreateXdgDirs:
    def test_first_launch_creates_all_dirs(self, tmp_xdg):
        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        for d in (paths.config_home, paths.data_home, paths.state_home,
                  paths.cache_home, paths.logs_dir, paths.skills_dir, paths.runtime_dir):
            assert d.is_dir()

    def test_second_launch_is_idempotent(self, tmp_xdg):
        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        main_mod._create_xdg_dirs(paths)  # must not raise
        assert paths.state_home.is_dir()

    def test_runtime_dir_parent_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-such-parent" / "run"))
        paths = xdg_paths("agent")
        with pytest.raises(FileNotFoundError):
            main_mod._create_xdg_dirs(paths)

    def test_state_home_permissions_owner_only(self, tmp_xdg):
        import stat

        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        mode = stat.S_IMODE(paths.state_home.stat().st_mode)
        assert mode == 0o700


class TestConfigMissingExit:
    def test_missing_config_exits_with_path_in_message(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "agent"])
        paths = xdg_paths("agent")
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        assert str(paths.config_file) in str(exc.value.code)


class TestWarnRelativePaths:
    def test_relative_path_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "./foo"}})
        assert any("looks like a relative path" in r.message for r in caplog.records)

    def test_tilde_path_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "~/Documents"}})
        assert not any("looks like a relative path" in r.message for r in caplog.records)

    def test_absolute_path_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "/opt/workspace"}})
        assert not any("looks like a relative path" in r.message for r in caplog.records)

    def test_nested_dict_scanned(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"agent": {"nested": {"value": "./relative"}}})
        assert any("agent.nested.value" in r.message for r in caplog.records)


class TestDownloadsDirDerivation:
    def test_default_workspace_gives_documents_downloads(self, tmp_xdg, monkeypatch):
        import os

        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "agent"])
        # Recreate the derivation logic exactly as main() computes it, given a
        # config with no explicit workspace_dir.
        cfg = {"paths": {}}
        workspace_dir = os.path.abspath(os.path.expanduser(
            cfg.get("paths", {}).get("workspace_dir", "~/Documents/agent_workspace")
        ))
        downloads_dir = os.path.join(workspace_dir, "downloads")
        assert workspace_dir == os.path.expanduser("~/Documents/agent_workspace")
        assert downloads_dir == os.path.join(os.path.expanduser("~/Documents/agent_workspace"), "downloads")

    def test_custom_workspace_gives_custom_downloads(self, tmp_path):
        import os

        cfg = {"paths": {"workspace_dir": str(tmp_path / "myworkspace")}}
        workspace_dir = os.path.abspath(os.path.expanduser(
            cfg.get("paths", {}).get("workspace_dir", "~/Documents/agent_workspace")
        ))
        downloads_dir = os.path.join(workspace_dir, "downloads")
        assert workspace_dir == str(tmp_path / "myworkspace")
        assert downloads_dir == str(tmp_path / "myworkspace" / "downloads")


class TestStartupLockOrdering:
    def test_lock_precedes_config_read_and_holds_through_run(self, tmp_xdg, monkeypatch):
        """The daemon holds the shared lock before reading config and through _run.

        A migration that restores originals while a starting daemon is past its
        config read would serve stale candidate configuration, so the lock must
        be acquired strictly before the first config read and released only
        after the startup path (_run) has finished.
        """
        events = []

        class RecordingLock:
            def __init__(self, path):
                self._path = path

            def __enter__(self):
                events.append("lock")
                return self

            def __exit__(self, *args):
                events.append("unlock")

        def record_config_read(*args, **kwargs):
            events.append("config-read")
            return {"paths": {}}, object()

        def record_run(**kwargs):
            events.append("run")

        monkeypatch.setattr(main_mod, "_PidFileLock", RecordingLock)
        monkeypatch.setattr(main_mod, "load_config", record_config_read)
        monkeypatch.setattr(main_mod, "_run", record_run)
        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "agent"])

        config = xdg_paths("agent").config_file
        config.parent.mkdir(parents=True)
        config.write_text("placeholder = true\n")

        main_mod.main()

        assert events.index("lock") < events.index("config-read") < events.index("run")
        assert events.index("run") < events.index("unlock")


class TestCheckMigration:
    def test_auto_triggers_when_config_toml_alongside_main_and_no_sentinel(self, tmp_xdg, monkeypatch):
        calls = []
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: False)
        monkeypatch.setattr(main_mod.Path, "exists", lambda self: True)
        monkeypatch.setattr(main_mod.migrate, "main", lambda name, source: calls.append((name, source)) or ["did something"])

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert calls

    def test_skips_when_sentinel_exists(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: True)
        called = []
        monkeypatch.setattr(main_mod.migrate, "main", lambda *a, **k: called.append(1))

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert not called

    def test_skips_when_no_config_toml_present(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: False)
        called = []
        monkeypatch.setattr(main_mod.migrate, "main", lambda *a, **k: called.append(1))

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert not called


# ---------------------------------------------------------------------------
# Actual root composition (tasks 7.1 / 12.11 root wiring, graph disabled)
# ---------------------------------------------------------------------------

@pytest.fixture
def root_harness(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Yield a root harness; close production leftovers only after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch)
    yield harness
    harness.release_leftovers()


class _ReactBoundary:
    """Replaces only ``agent_controller.react_loop``; records each run's context."""

    def __init__(
        self, on_call: Callable[[int, SimpleNamespace], None] | None = None,
    ) -> None:
        """Record calls; ``on_call(index, call)`` runs inside each loop invocation."""
        self.on_call = on_call
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self,
        ctx: ReactContext,
        goal: str,
        progress_callback: Callable[[str], None] | None = None,
        images: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        """Capture the run's context, pin, catalog view and event; return an answer."""
        call = SimpleNamespace(
            ctx=ctx, goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None),
            view=ctx.model_catalog_view, event=ctx.cancel_event,
        )
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(len(self.calls), call)
        return f"answer-{len(self.calls)}"


def _request(root: RootHarness, text: str, trace: str) -> tuple[str | None, Exception | None]:
    """Send one ordinary request through the Telegram-facing agent handler."""
    try:
        return root.agent_handler(1, text, None, trace_id=trace), None
    except Exception as exc:  # noqa: BLE001 — the rejection surface is asserted by callers
        return None, exc


def _assert_no_secrets(text: str) -> None:
    """Assert that no synthetic credential value appears in *text*."""
    for value in SECRETS.values():
        assert value not in text


def _spy_admissions(
    monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry,
) -> dict[str, list[object]]:
    """Delegating instance spies on the registry's two admission entry points."""
    seen: dict[str, list[object]] = {"admit": [], "admit_scheduled": []}
    for name in seen:
        original = getattr(registry, name)

        def spy(
            selector: object = None,
            *,
            _name: str = name,
            _original: Callable[[object], RunModel] = original,
        ) -> RunModel:
            """Record the selector, then delegate to the real admission method."""
            seen[_name].append(selector)
            return _original(selector)

        monkeypatch.setattr(registry, name, spy)
    return seen


class _StartupFault(BaseException):
    """Abrupt external fault (e.g. an operator interrupt) raised at a lifecycle boundary."""


def _track_client_releases(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Count outermost ``close``/``__exit__`` releases per ``httpx.Client``.

    A release nested inside another release of the same call stack is not
    counted again, so the count reflects owner releases, not httpx internals.
    """
    releases: dict[int, int] = {}
    depth = [0]
    for name in ("close", "__exit__"):
        original = getattr(httpx.Client, name)

        def release(
            client: httpx.Client, *args: Any, _original: Callable[..., Any] = original, **kwargs: Any,
        ) -> Any:
            """Record an owner release, then delegate to the real method."""
            if depth[0] == 0:
                releases[id(client)] = releases.get(id(client), 0) + 1
            depth[0] += 1
            try:
                return _original(client, *args, **kwargs)
            finally:
                depth[0] -= 1

        monkeypatch.setattr(httpx.Client, name, release)
    return releases


class _InlineExecutor(concurrent.futures.ThreadPoolExecutor):
    """Default-executor stand-in that runs work inline: no worker thread starts."""

    def submit(self, fn, /, *args, **kwargs):
        """Run *fn* now and return its completed future."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — delivered through the future
            future.set_exception(exc)
        return future


def _run_on_inline_loop(coro: Any) -> Any:
    """Run *coro* on a fresh loop whose executor is inline (closed without threads)."""
    loop = asyncio.new_event_loop()
    loop.set_default_executor(_InlineExecutor())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _TelegramMessage:
    """Telegram message boundary: records replies (as child messages) and edits."""

    def __init__(self, text: str = "", reply_markup: Any = None) -> None:
        """Hold the text and markup this message was sent with."""
        self.text, self.reply_markup = text, reply_markup
        self.replies: list[_TelegramMessage] = []
        self.edits: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> _TelegramMessage:
        """Record a reply and return it as a new message."""
        child = _TelegramMessage(text, kwargs.get("reply_markup"))
        self.replies.append(child)
        return child

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        """Record an in-place edit."""
        del kwargs
        self.edits.append(text)


class _CallbackQuery:
    """Telegram callback-query boundary: records acknowledgments and edits in order."""

    def __init__(self, data: str, *, user_id: int, chat_id: int) -> None:
        """Bind the pressed button's data and its user/chat principals."""
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.events: list[str] = []
        self.edits: list[str] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        """Record the acknowledgment."""
        del args, kwargs
        self.events.append("answer")

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        """Record the in-place edit."""
        del kwargs
        self.events.append("edit")
        self.edits.append(text)


def _message_update(message: _TelegramMessage, *, user_id: int = 1, chat_id: int = 1) -> SimpleNamespace:
    """Return an authorized text/command update carrying *message*."""
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        callback_query=None,
    )


def _select_token(markup: Any, label: str) -> str:
    """Return the callback data of the one inline button whose label contains *label*."""
    matches = [
        button.callback_data for row in markup.inline_keyboard for button in row
        if label in (button.text or "")
    ]
    assert len(matches) == 1, matches
    return matches[0]


class TestRootStartupComposition:
    """Drive the real ``main._run``; only external lifecycle boundaries are replaced."""

    def test_startup_refreshes_every_provider_through_all_pages_then_restores_main(
        self, root_harness: RootHarness,
    ) -> None:
        """First start (no preference, no cache) discovers every provider once, then restores Main."""
        root_harness.start(saved_default=None, write_cache=False)
        root = root_harness
        events = root.events

        # One registry refresh (the reusable /models path) covers every provider.
        assert events.count("refresh:start") == 1 and events.count("restore") == 1
        start, end = events.index("refresh:start"), events.index("refresh:end")
        assert sorted(e for e in events[start:end] if e.startswith("fetch:")) == [
            "fetch:acme", "fetch:gem",
        ]
        assert not [e for e in events[:start] + events[end:] if e.startswith("fetch:")]
        # Remembered selection is restored only after the whole refresh, before
        # Telegram or the scheduler are started.
        restore = events.index("restore")
        assert end < restore < events.index("telegram:init")
        assert restore < events.index("scheduler:start")
        assert events.index("telegram:run") > restore

        # Every supported page of every provider was requested with its own credential.
        acme, gem = root.requests_to(ACME_HOST), root.requests_to(GEM_HOST)
        assert [dict(r.url.params) for r in acme] == [{}, {"after_id": "claude-a"}]
        assert [dict(r.url.params) for r in gem] == [{}, {"pageToken": "tok-2"}]
        assert {r.headers["x-api-key"] for r in acme} == {SECRETS["KEY_ACME"]}
        assert {r.headers["x-goog-api-key"] for r in gem} == {SECRETS["KEY_GEM"]}
        assert root.unexpected == []

        report = root.refresh_reports[0]
        assert sorted(report.refreshed_provider_ids) == ["acme", "gem"]
        assert report.stale_provider_ids == () and report.persisted is True

        view = root.registry.view()
        assert view.active_model_id == MAIN_ID and view.selection_status == "active"
        assert tuple(m.id for m in view.models) == DISCOVERED
        assert {p.provider_id: p.status for p in view.providers} == {"acme": "fresh", "gem": "fresh"}
        saved = ModelCache(root.paths.models_cache_file).load()
        assert saved.status == "valid" and saved.document.default_model is None
        assert not root.paths.model_selection_file.exists()  # the automatic choice is never saved
        assert view.remembered_model_id is None
        assert tuple(m.id for m in saved.document.models) == DISCOVERED

        # No standing inference client; graph memory stays disabled (12.25 scope).
        assert root.llm_clients_at_hold == 0 and root.llm_clients == []
        assert root.main_controller.llm is None
        assert root.main_controller._graph_memory is None

    def test_discovery_cache_lands_at_cache_path_and_legacy_catalog_stays_untouched(
        self, root_harness: RootHarness,
    ) -> None:
        """Task 3.4: the root writes the disposable cache to ``models_cache_file`` only."""
        legacy = b'schema_version = 1\nrevision = 9\n# legacy-canary sk-legacycanary0123456789\n'
        root_harness.start(saved_default=None, write_cache=False, legacy_settings=legacy)
        root = root_harness

        paths = root.paths
        assert paths.models_cache_file.parent == paths.cache_home
        cached = json.loads(paths.models_cache_file.read_text(encoding="utf-8"))
        assert set(cached) == {"schema_version", "providers", "models"}
        assert tuple(m["id"] for m in cached["models"]) == DISCOVERED
        assert {p["status"] for p in cached["providers"].values()} == {"fresh"}
        text = paths.models_cache_file.read_text(encoding="utf-8")
        for secret in SECRETS.values():
            assert secret not in text
        assert "https://" not in text and "sec:" not in text and "env:" not in text
        assert ModelCache(paths.models_cache_file).load().status == "valid"
        # The retired combined file is neither read, adopted, rewritten nor backed up.
        assert paths.models_config_file.read_bytes() == legacy
        assert sorted(p.name for p in paths.models_config_file.parent.iterdir() if "models-config" in p.name) == [
            "models-config.toml"
        ]
        # Discovery never writes the deliberate preference.
        assert not paths.model_selection_file.exists()
        assert root.registry.view().active_model_id == MAIN_ID

    @pytest.mark.parametrize("saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["absent", "removed"])
    def test_missing_main_keeps_commands_available_without_default_or_model_work(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        saved_default: str | None,
    ) -> None:
        """Without a valid Main, commands stay available but no model work is admitted."""
        from agent_runtime import RuntimeProfile

        boundary = _ReactBoundary()
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Attempt ordinary and sub-agent model work while startup is held."""
            creates: list[tuple[tuple[object, ...], dict[str, object]]] = []
            monkeypatch.setattr(root.runtime, "create", lambda *a, **k: creates.append((a, k)))
            outcome["handler"] = _request(root, "do ordinary work", "r-0000a001")
            codes: list[str] = []
            for profile, selector in (
                (RuntimeProfile.ON_DEMAND_SUBAGENT, None),
                (RuntimeProfile.PLAN_STEP_AGENT, None),  # implicit; explicit needs no Main (task 4.1)
                (RuntimeProfile.SCHEDULED_AGENT, None),  # inheriting; explicit needs no Main (task 8.1)
            ):
                with pytest.raises(ModelRegistryError) as info:
                    root.builtin._sub_agent_factory(model=selector, runtime_profile=profile)
                codes.append(info.value.code)
            outcome["codes"] = codes
            outcome["creates"] = list(creates)
            # A saved explicit scheduled selector is pinned exactly with no Main.
            root.builtin._sub_agent_factory(model=OTHER_ID, runtime_profile=RuntimeProfile.SCHEDULED_AGENT)
            outcome["explicit_scheduled_creates"] = creates[len(outcome["creates"]):]

        root_harness.start(saved_default=saved_default, hold=hold)
        root = root_harness

        # Telegram commands stay available: startup completed and polling ran.
        assert "telegram:init" in root.events and "telegram:run" in root.events
        assert "scheduler:start" in root.events and root.main_controller is not None
        # Ordinary model work is rejected without choosing an arbitrary default.
        result, exc = outcome["handler"]
        if exc is not None:
            assert isinstance(exc, ModelRegistryError) and exc.code == "no_active_selection", repr(exc)
        else:
            assert isinstance(result, str) and result.strip()
        assert boundary.calls == []
        assert outcome["codes"] == ["no_active_selection"] * 3
        assert outcome["creates"] == []
        ((create_args, _create_kwargs),) = outcome["explicit_scheduled_creates"]
        assert create_args[0] is RuntimeProfile.SCHEDULED_AGENT
        assert create_args[1].run_model.model.id == OTHER_ID
        assert root.llm_clients == [] and root.main_controller.llm is None
        view = root.registry.view()
        assert view.active_model_id is None
        assert view.remembered_model_id == remembered_for(saved_default)
        assert view.selection_status == (
            "no_selection" if remembered_for(saved_default) is None else "saved_selection_unavailable"
        )
        saved = ModelCache(root.paths.models_cache_file).load().document
        assert saved.default_model is None
        preference = ModelPreference(root.paths.model_selection_file).load()
        assert preference.model == remembered_for(saved_default)
        assert preference.status == ("corrupt" if saved_default == INVALID_SAVED else "valid")
        # An unusable saved selection means no startup discovery: the stale cache is untouched.
        assert tuple(m.id for m in saved.models) == tuple(sorted((MAIN_ID, OTHER_ID)))
        assert root.refresh_reports == []
        assert root.unexpected == []

    def test_first_start_all_provider_failure_keeps_commands_and_refuses_work_locally(
        self, tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task 3.6 regression guard: no preference + no cache + every provider down.

        Through the real ``main._run``: exactly one logical discovery pass per configured
        provider (one failed page each, no retry), the bot still reaches command handling,
        a real non-model command works, and an implicit task fails locally with /models
        guidance and no inference HTTP. An explicit selector is admitted at the registry (task 4.1)
        without Main or cache; full root wiring of explicit targets is task 4.2.
        """
        import telegram_interface
        from telegram_commands import cmd_jobs

        boundary = _ReactBoundary()
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        root_harness = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)  # real Scheduler for the real interface
        root_harness.serve_provider_chat(failing_discovery_hosts=(ACME_HOST, GEM_HOST))
        outcome: dict[str, Any] = {}

        def hold(started: RootHarness) -> None:
            """Drive a real non-model command and an implicit task, then an explicit-admission attempt."""
            iface = telegram_interface.TelegramInterface(*started.telegram.args, **started.telegram.kwargs)
            iface.agent = started.main_controller
            iface._prompt_registry = started.telegram._prompt_registry
            chat = _TelegramMessage()
            outcome["chat"] = chat
            outcome["requests_before"] = len(started.requests)
            ctx = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))

            async def drive() -> None:
                """``/jobs`` (no model needed), then one ordinary request through the real handler."""
                await cmd_jobs(iface, _message_update(chat), ctx)
                outcome["replies_after_jobs"] = len(chat.replies)
                await iface._run_agent_task(_message_update(chat), ctx, "ordinary request")
                await asyncio.gather(*list(iface._flush_tasks))

            _run_on_inline_loop(drive())
            outcome["explicit_pin"] = started.registry.admit(OTHER_ID).model.id
            outcome["requests_after"] = len(started.requests)

        try:
            root_harness.start(saved_default=None, write_cache=False, hold=hold)
            root = root_harness

            # Exactly one logical pass per configured provider: one (failed) page each, no retry.
            discovery = [(r.url.host, r.url.path) for r in root.requests if r.method == "GET"]
            assert sorted(discovery) == [(ACME_HOST, "/v1/models"), (GEM_HOST, "/v1beta/models")]
            (report,) = root.refresh_reports
            assert report.refreshed_provider_ids == () and sorted(report.stale_provider_ids) == ["acme", "gem"]
            view = root.registry.view()
            assert view.models == () and view.active_model_id is None and view.selection_status == "no_selection"
            assert not root.paths.model_selection_file.exists()

            # Startup reached command handling; a real non-model command answered.
            assert "telegram:init" in root.events and "telegram:run" in root.events
            chat: _TelegramMessage = outcome["chat"]
            assert outcome["replies_after_jobs"] >= 1
            # The implicit task failed locally with guidance, with no inference or any new HTTP.
            texts = [m.text for m in chat.replies] + [edit for m in chat.replies for edit in m.edits]
            assert any("No model is selected" in t and "/models" in t for t in texts), texts
            assert outcome["requests_after"] == outcome["requests_before"]
            assert not [r for r in root.requests if r.method == "POST" and r.url.host in (ACME_HOST, GEM_HOST)]
            assert boundary.calls == [] and root.llm_clients == []
            # Task 4.1: an explicit selector needs neither Main nor cache; admission makes no HTTP call.
            assert outcome["explicit_pin"] == OTHER_ID and outcome["requests_after"] == outcome["requests_before"]
            assert root.registry.view().models == () and root.registry.view().active_model_id is None
            assert root.unexpected == []
        finally:
            root_harness.release_leftovers()

    def test_first_start_discovery_makes_no_supplement_call_and_unknown_vision_does_not_block_images(
        self, root_harness: RootHarness, tmp_path: Path,
    ) -> None:
        """Real root: first-start discovery contacts only providers, and an image request
        for a model whose discovered vision flag is false goes out on its pinned route.

        The harness router is the only network boundary; a ``models.dev`` request is an
        unexpected destination. Main's discovered row reports ``vision`` unsupported, which
        is display metadata: the image reaches the one provider call unchanged.
        """
        root_harness.serve_provider_chat()
        served = root_harness._route
        chat_bodies: list[dict[str, Any]] = []

        def route(request: httpx.Request) -> httpx.Response:
            """Report vision unsupported for Main's row; answer Main's chat with a finish."""
            if request.method == "GET" and request.url.host == ACME_HOST and request.url.path == "/v1/models":
                response = served(request)
                if response.status_code == 200:
                    payload = json.loads(response.content)
                    for row in payload["data"]:
                        row["capabilities"] = {"image_input": {"supported": False}}
                    return httpx.Response(200, json=payload)
                return response
            if request.method == "POST" and request.url.host == ACME_HOST and request.url.path == "/v1/messages":
                request.read()
                root_harness.requests.append(request)
                chat_bodies.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": json.dumps({"action": "finish", "result": "saw it"})}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                })
            return served(request)

        root_harness.monkeypatch.setattr(root_harness, "_route", route)
        image = tmp_path / "picture.png"
        image.write_bytes(b"fake-image-bytes")
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Send one ordinary image-bearing request on Main."""
            outcome["view"] = root.registry.view()
            cards: list[str] = []

            def progress(message: str) -> None:
                """Record an LLM error card and answer it with Cancel (no operator exists)."""
                if message.startswith("__LLM_ERROR__:"):
                    cards.append(message)
                    root.main_controller.resume_llm_error(message.split(":", 2)[1], "cancel")

            outcome["reply"] = root.agent_handler(
                1, "what is this?", progress, [str(image)], trace_id="r-0000e0a1",
            )
            outcome["cards"] = cards

        root_harness.start(saved_default=None, write_cache=False, hold=hold)

        assert root_harness.requests_to("models.dev") == []
        assert root_harness.unexpected == []
        assert outcome["view"].active_model_id == MAIN_ID
        cached = {m.id: m for m in ModelCache(root_harness.paths.models_cache_file).load().document.models}
        assert cached[MAIN_ID].metadata.vision is False
        assert outcome["cards"] == []
        assert outcome["reply"] == "saw it"
        # The request itself is the first provider call; later bodies are post-run summaries.
        # Every call stays on Main's pinned upstream (no model switch, no fallback).
        assert {body["model"] for body in chat_bodies} == {"claude-a"}
        carrying_images = [
            body for body in chat_bodies
            if any(
                part.get("type") == "image"
                for message in body["messages"] if isinstance(message["content"], list)
                for part in message["content"]
            )
        ]
        assert carrying_images == [chat_bodies[0]]

    def test_exhausted_request_keeps_commands_and_next_task_available(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exhausted transient request ends only its own run on the real loop.

        The root config has ``max_retries = 0``: the first request's single
        outbound call fails with 503, the operator answers the retry card with
        Cancel through the controller's public handler, and the run ends. A
        later independent request on the same controller and Main succeeds.
        """
        root_harness.serve_provider_chat()
        served = root_harness._route
        outage = {"on": True}

        def route(request: httpx.Request) -> httpx.Response:
            """Fail Main's chat while the outage lasts; answer with a finish afterwards."""
            if request.method == "POST" and request.url.host == ACME_HOST and request.url.path == "/v1/messages":
                request.read()
                root_harness.requests.append(request)
                if outage["on"]:
                    return httpx.Response(503, json={"error": {"message": "provider outage"}})
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": json.dumps(
                        {"action": "finish", "result": "recovered later"}
                    )}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                })
            return served(request)

        monkeypatch.setattr(root_harness, "_route", route)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Exhaust one request, then run an independent task and a command query."""
            cards: list[str] = []

            def progress(message: str) -> None:
                if message.startswith("__LLM_ERROR__:"):
                    token = message.split(":", 2)[1]
                    cards.append(token)
                    root.main_controller.resume_llm_error(token, "cancel")

            try:
                outcome["first"] = (
                    root.agent_handler(1, "exhaust me", progress, trace_id="r-0000e001"), None,
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["first"] = (None, exc)
            outcome["cards"] = cards
            outcome["chat_after_first"] = len(
                [r for r in root.requests_to(ACME_HOST) if r.url.path == "/v1/messages"]
            )
            outage["on"] = False
            outcome["second"] = _request(root, "independent task", "r-0000e002")
            outcome["view"] = root.registry.view()

        root_harness.start(saved_default=None, write_cache=False, hold=hold)
        root = root_harness

        first, first_exc = outcome["first"]
        assert first_exc is None and first.startswith("❌"), (first, first_exc)
        assert len(outcome["cards"]) == 1
        assert outcome["chat_after_first"] == 1, "max_retries=0 allows exactly one call"
        assert outcome["second"] == ("recovered later", None)
        assert outcome["view"].active_model_id == MAIN_ID
        assert "telegram:run" in root.events and "scheduler:stop" in root.events
        assert root.unexpected == []

    def test_main_request_uses_run_local_pin_and_fresh_catalog_view(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each request admits its own pin and gets a fresh catalog view."""
        pins_after_select: list[RunModel] = []

        def on_call(index: int, call: SimpleNamespace) -> None:
            """Change Main during the first run and record that run's pin."""
            if index == 1:
                # Explicit Main change mid-run must not retarget the current pin.
                root_harness.registry.select(OTHER_ID)
                pins_after_select.append(call.ctx.llm._run_model)

        boundary = _ReactBoundary(on_call=on_call)
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Send two ordinary requests while startup is held."""
            outcome["admissions"] = _spy_admissions(monkeypatch, root.registry)
            outcome["first"] = _request(root, "first request", "r-0000b001")
            outcome["second"] = _request(root, "second request", "r-0000b002")

        root_harness.start(saved_default=None, write_cache=False, hold=hold)
        root = root_harness

        assert outcome["first"] == ("answer-1", None)
        assert outcome["second"] == ("answer-2", None)
        assert outcome["admissions"] == {"admit": [None, None], "admit_scheduled": []}
        controller = root.main_controller
        assert controller.llm is None  # no LLMClient(cfg); clients are run-local
        first, second = boundary.calls
        for call in (first, second):
            assert isinstance(call.llm, LLMClient) and isinstance(call.pin, RunModel)
            assert call.llm._cancel_event is call.event
            assert call.llm._closed is True  # run-owned client closed by the controller
        assert first.pin.model.id == MAIN_ID
        assert pins_after_select == [first.pin]
        assert second.pin.model.id == OTHER_ID and second.llm is not first.llm
        assert root.llm_clients == [first.llm, second.llm]

        # Catalog presentation is rebuilt per request, never a startup snapshot.
        for call, expected in ((first, MAIN_ID), (second, OTHER_ID)):
            view = call.view
            assert isinstance(view, CatalogView)
            assert view.active_model_id == expected and view.pinned_model.id == call.pin.model.id
            assert tuple(m.id for m in view.models) == DISCOVERED
            section = format_model_catalog_section(view)
            assert f"Pinned execution model for this run: {expected}" in section
            assert f"Active selection for future runs (status active): {expected}" in section
            _assert_no_secrets(section)
            _assert_no_secrets(repr(view))
        assert second.view is not first.view
        assert second.view.selection_revision > first.view.selection_revision
        assert root.unexpected == []

    def test_main_request_presents_active_main_distinct_from_admitted_pin(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Main changes after admission: the run keeps pin A while its view shows active B."""
        boundary = _ReactBoundary()
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        original_build = agent_controller.AgentRuntime.build_react_context
        built: list[str] = []

        def build_spy(
            controller: agent_controller.AgentController,
            trace_id: str,
            *,
            cancel_event: threading.Event | None = None,
        ) -> ReactContext:
            """Select B after pin A was admitted, then delegate the real assembly."""
            root_harness.registry.select(OTHER_ID)
            built.append(trace_id)
            return original_build(controller, trace_id, cancel_event=cancel_event)

        monkeypatch.setattr(
            agent_controller.AgentRuntime, "build_react_context", staticmethod(build_spy),
        )
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Send one ordinary request while startup is held."""
            outcome["admissions"] = _spy_admissions(monkeypatch, root.registry)
            outcome["result"] = _request(root, "pinned request", "r-0000c001")

        root_harness.start(saved_default=MAIN_ID, hold=hold)
        root = root_harness

        assert outcome["result"] == ("answer-1", None)
        assert built == ["r-0000c001"]
        assert outcome["admissions"] == {"admit": [None], "admit_scheduled": []}
        (call,) = boundary.calls
        assert isinstance(call.llm, LLMClient) and isinstance(call.pin, RunModel)
        assert call.pin.model.id == MAIN_ID
        assert root.llm_clients == [call.llm]
        view = call.view
        assert isinstance(view, CatalogView)
        assert view.active_model_id == OTHER_ID and view.selection_status == "active"
        assert view.pinned_model is not None and view.pinned_model.id == MAIN_ID
        section = format_model_catalog_section(view)
        assert f"Pinned execution model for this run: {MAIN_ID}" in section
        assert f"Active selection for future runs (status active): {OTHER_ID}" in section
        _assert_no_secrets(section)
        _assert_no_secrets(repr(view))
        assert root.registry.view().active_model_id == OTHER_ID
        assert root.unexpected == []

    def test_root_embedding_feeds_index_and_results_without_inference_client(
        self, root_harness: RootHarness,
    ) -> None:
        """One root embedding client serves indexing and results; children are separate."""
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Build two child embedding clients from the runtime factory and close them."""
            embedding = root.root_embedding
            factory = root.runtime_kwargs["embedding_factory"]
            events = (threading.Event(), threading.Event())
            children = [factory(event) for event in events]
            try:
                outcome["children"] = [
                    (child is not embedding, child._cancel_event is event,
                     child._http is not embedding._http, child.available)
                    for child, event in zip(children, events)
                ]
                outcome["distinct"] = children[0] is not children[1]
            finally:
                for child in children:
                    child.close()

        root_harness.start(saved_default=MAIN_ID, hold=hold)
        root = root_harness
        embedding = root.root_embedding
        controller = root.main_controller

        assert isinstance(embedding, EmbeddingClient) and embedding.available
        assert root.llm_clients_at_hold == 0  # no inference client solely for indexing
        assert len(root.tool_indexes) == 1
        index = root.tool_indexes[0]
        assert index.llm is embedding and controller.tool_index is index
        assert controller.results.llm is embedding
        assert root.builtin._results is controller.results
        # Startup indexing used only the explicit embedding credential and endpoint.
        embed_requests = root.requests_to(EMBED_HOST)
        assert embed_requests
        assert {r.headers["authorization"] for r in embed_requests} == {
            f"Bearer {SECRETS['EMBED_KEY']}"
        }
        for request in embed_requests:
            _assert_no_secrets(request.headers.get("x-api-key", ""))
            _assert_no_secrets(request.headers.get("x-goog-api-key", ""))
        # Child embedding factory: separate clients bound to their own events.
        assert outcome["children"] == [(True, True, True, True)] * 2
        assert outcome["distinct"] is True
        assert root.unexpected == []

    def test_edited_provider_connection_takes_effect_only_after_restart(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task 3.1: the root's chat connection map is fixed until the process restarts.

        While running, the operator edits ``[providers.acme]`` (endpoint and
        credential reference) in ``config.toml`` and in-process code tries to
        replace the parsed connection. Refresh and admission still use the
        original endpoint and credential. A restarted root reads the edit.
        """
        import dataclasses

        moved_host = "anthropic.moved.root.test"
        moved_canary = "sk-moved-canary-task31-000999"
        monkeypatch.setenv("KEY_ACME_MOVED", moved_canary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Edit the config file, try to replace the live connection, then refresh and admit."""
            text = root.paths.config_file.read_text()
            edited = text.replace(f'base_url = "https://{ACME_HOST}"', f'base_url = "https://{moved_host}"')
            edited = edited.replace('api_key = "env:KEY_ACME"', 'api_key = "env:KEY_ACME_MOVED"')
            assert edited != text
            root.paths.config_file.write_text(edited)
            providers = root.app_cfg.providers
            try:
                providers["acme"] = dataclasses.replace(  # type: ignore[index]
                    providers["acme"], base_url=f"https://{moved_host}", api_key="env:KEY_ACME_MOVED",
                )
            except TypeError:
                outcome["mutation_rejected"] = True
            else:
                outcome["mutation_rejected"] = False
            outcome["mark"] = len(root.requests)
            outcome["report"] = root.registry.refresh()
            pin = root.registry.admit()
            outcome["pin"] = (pin.connection.base_url, pin.connection.api_key == SECRETS["KEY_ACME"])

        root_harness.start(saved_default=None, write_cache=False, hold=hold)
        root = root_harness
        during = root.requests[outcome["mark"]:]
        assert [r.url.host for r in during if r.url.path == "/v1/models"].count(ACME_HOST) == 2
        assert root.requests_to(moved_host) == []
        assert all(moved_canary not in str(r.headers) for r in root.requests)
        assert {r.headers["x-api-key"] for r in during if r.url.host == ACME_HOST} == {SECRETS["KEY_ACME"]}
        assert "acme" in outcome["report"].refreshed_provider_ids
        assert outcome["pin"] == (f"https://{ACME_HOST}", True)
        assert outcome["mutation_rejected"] is True
        assert root.app_cfg.providers["acme"].base_url == f"https://{ACME_HOST}"

        # Restart: a new process parses the edited file and uses the new connection.
        root.hold = None
        cfg, app_cfg = main_mod.load_config(
            root.paths.config_file, vault_file=str(root.paths.secrets_file), agent_name="rootagent",
        )
        assert app_cfg.providers["acme"].base_url == f"https://{moved_host}"
        # A restart with a populated cache would not discover; drop it to make this a first start.
        root.paths.models_cache_file.unlink()
        mark = len(root.requests)
        main_mod._run(
            cfg=cfg, app_cfg=app_cfg, paths=root.paths,
            downloads_dir=str(root.workspace / "downloads"), tmp_dir=str(root.tmp_dir),
            workspace_dir=str(root.workspace), agent_name="rootagent",
        )
        restarted = [r for r in root.requests[mark:] if r.url.path == "/v1/models"]
        assert {r.url.host for r in restarted} == {moved_host}
        assert {r.headers["x-api-key"] for r in restarted} == {moved_canary}

    def test_root_owned_clients_close_on_normal_exit(self, root_harness: RootHarness) -> None:
        """Root-owned discovery and embedding clients are closed when ``_run`` returns."""
        root_harness.start(saved_default=MAIN_ID)
        root = root_harness
        assert root.discoveries, "root never constructed ModelDiscovery"
        for discovery in root.discoveries:
            assert discovery._client.is_closed
        assert root.root_embedding._http.is_closed
        assert root.open_clients_at_exit == []
        assert "scheduler:stop" in root.events

    def test_root_owned_clients_close_on_exception_exit(self, root_harness: RootHarness) -> None:
        """Root-owned discovery and embedding clients are closed when ``_run`` raises."""

        def hold(_root: RootHarness) -> None:
            """Fail while startup is held."""
            raise RuntimeError("hold boom")

        with pytest.raises(RuntimeError, match="hold boom"):
            root_harness.start(saved_default=MAIN_ID, hold=hold)
        root = root_harness
        assert root.discoveries, "root never constructed ModelDiscovery"
        for discovery in root.discoveries:
            assert discovery._client.is_closed
        assert root.root_embedding._http.is_closed
        assert root.open_clients_at_exit == []
        assert "scheduler:stop" in root.events

    def test_early_model_setup_failure_releases_each_allocated_model_resource_once(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fault inside the startup refresh closes every allocated root model resource
        exactly once, before any fixture hygiene."""
        releases = _track_client_releases(monkeypatch)
        original_route = root_harness._route

        def interrupting_route(request: httpx.Request) -> httpx.Response:
            """Interrupt startup at the first provider discovery request."""
            if request.url.host in (ACME_HOST, GEM_HOST):
                raise _StartupFault("interrupted during startup refresh")
            return original_route(request)

        monkeypatch.setattr(root_harness, "_route", interrupting_route)

        with pytest.raises(_StartupFault):
            root_harness.start(saved_default=None, write_cache=False)
        root = root_harness

        # Ownership is judged now, before the fixture's release_leftovers hygiene.
        allocated = list(root.http_clients)
        (embedding,) = root.embedding_clients
        assert embedding._http in allocated
        assert len(allocated) == 2  # root embedding + the one discovery client
        (discovery,) = root.discoveries
        assert discovery._client in allocated
        assert len(root.registries) == 1
        assert "refresh:start" in root.events and "refresh:end" not in root.events
        assert root.open_clients_at_exit == []
        assert {id(c): releases.get(id(c), 0) for c in allocated} == {id(c): 1 for c in allocated}
        assert root.llm_clients == []
        assert "telegram:init" not in root.events and "scheduler:start" not in root.events

    def test_models_selection_callback_recovers_ordinary_request_on_same_root_registry(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real Telegram routes on the root registry: refused request, ``/models``
        listing, opaque-token selection, then an ordinary request on that pin."""
        import telegram_interface
        from telegram_callbacks import cb_model_select
        from telegram_commands import cmd_models

        upstream: list[httpx.Request] = []
        original_route = root_harness._route

        def route(request: httpx.Request) -> httpx.Response:
            """Serve the pinned Anthropic chat endpoint; delegate everything else."""
            if request.method == "POST" and request.url.host == ACME_HOST and request.url.path == "/v1/messages":
                request.read()
                root_harness.requests.append(request)
                upstream.append(request)
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": "pong"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                })
            return original_route(request)

        monkeypatch.setattr(root_harness, "_route", route)
        outcome: dict[str, Any] = {}

        def on_call(_index: int, call: SimpleNamespace) -> None:
            """Send one real upstream request through the run's pinned client."""
            outcome["chat_reply"] = call.llm.chat([{"role": "user", "content": "ping"}])

        boundary = _ReactBoundary(on_call=on_call)
        monkeypatch.setattr(agent_controller, "react_loop", boundary)

        def hold(root: RootHarness) -> None:
            """Build the real interface from the captured kwargs and drive its routes."""
            root.scheduler.set_result_notice_sink = lambda _sink: None  # scheduler boundary
            iface = telegram_interface.TelegramInterface(*root.telegram.args, **root.telegram.kwargs)
            iface.agent = root.main_controller
            iface._prompt_registry = root.telegram._prompt_registry
            outcome["iface"] = iface
            chat = _TelegramMessage()
            outcome["chat"] = chat
            ctx = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))

            async def drive() -> None:
                """Refused request, /models, Select callback, then the recovered request."""
                await iface._run_agent_task(_message_update(chat), ctx, "before selection")
                outcome["clients_before_select"] = len(root.llm_clients)
                await cmd_models(iface, _message_update(chat), ctx)
                listing = next(m for m in chat.replies if "Model Catalog" in m.text)
                query = _CallbackQuery(
                    _select_token(listing.reply_markup, "claude-a"), user_id=1, chat_id=1,
                )
                outcome["query"] = query
                await cb_model_select(iface, SimpleNamespace(
                    callback_query=query, effective_user=query.from_user,
                    effective_chat=query.message.chat,
                ), ctx)
                outcome["active_after_select"] = root.registry.view().active_model_id
                await iface._run_agent_task(_message_update(chat), ctx, "after selection")
                await asyncio.gather(*list(iface._flush_tasks))

            _run_on_inline_loop(drive())

        root_harness.start(saved_default=INVALID_SAVED, hold=hold)
        root = root_harness

        # The interface was wired from the actual startup kwargs: same registry,
        # controller and handler as the root, and no standing chat client.
        iface = outcome["iface"]
        assert iface.model_registry is root.registry
        assert root.factory_binding()[1] is root.registry
        assert iface.agent is root.main_controller and iface.agent_handler is root.agent_handler
        assert iface.llm_client is None and root.main_controller.llm is None

        chat: _TelegramMessage = outcome["chat"]
        statuses = [m for m in chat.replies if m.text == "🔄 Processing…"]
        assert len(statuses) == 2
        # Before selection: refused, no client built, no model work admitted.
        assert outcome["clients_before_select"] == 0
        # /models listing came from the same registry and carried opaque Select controls.
        (listing,) = [m for m in chat.replies if "Model Catalog" in m.text]
        assert MAIN_ID in listing.text
        _assert_no_secrets(listing.text)
        query: _CallbackQuery = outcome["query"]
        assert query.data.startswith("modelsel:") and MAIN_ID not in query.data
        assert query.events == ["answer", "edit"]
        assert "Switched" in query.edits[-1] and MAIN_ID in query.edits[-1]
        assert outcome["active_after_select"] == MAIN_ID
        # The deliberate /models choice is durable only in the XDG preference file.
        assert root.paths.model_selection_file.read_bytes() == f'model = "{MAIN_ID}"\n'.encode()
        saved = ModelCache(root.paths.models_cache_file).load().document
        assert saved.default_model is None

        # After selection: one run on the explicitly selected pin, one upstream request.
        (call,) = boundary.calls
        assert isinstance(call.llm, LLMClient) and isinstance(call.pin, RunModel)
        assert call.pin.model.id == MAIN_ID and call.pin.connection.provider_id == "acme"
        assert call.pin.selection_revision == root.registry.view().selection_revision
        assert call.view.active_model_id == MAIN_ID
        assert root.llm_clients == [call.llm] and call.llm._closed is True
        assert outcome["chat_reply"] == "pong"
        (sent,) = upstream
        assert sent.headers["x-api-key"] == SECRETS["KEY_ACME"]
        assert json.loads(sent.content)["model"] == "claude-a"
        # No fallback: the only model POST went to the pinned provider.
        assert [r for r in root.requests if r.method == "POST" and r.url.host != EMBED_HOST] == [sent]
        assert any(m.text == "answer-1" for m in chat.replies)
        assert "Done" in statuses[1].edits[-1]
        prompts = {r.text: r.status for r in root.telegram._prompt_registry.list_recent(10)}
        assert prompts == {"before selection": "failed", "after selection": "done"}
        assert root.unexpected == []


class TestRootStartupGraphMemory:
    """Graph memory startup through the real ``main._run`` composition path."""

    @staticmethod
    def _assert_prerequisite_rejection(
        root: RootHarness, caplog: pytest.LogCaptureFixture, message_fragment: str,
    ) -> None:
        """Prove configuration rejection precedes helper and scalar dimension work."""
        assert root.graph_extraction_calls == []
        assert "graph:build_extraction_call" not in root.events
        assert not any(text == "test" for _client, text in root.embedding_scalar_calls)
        warnings = [
            record.getMessage() for record in caplog.records
            if record.name == main_mod.__name__ and record.levelno == logging.WARNING
            and record.getMessage().startswith(
                "Graph memory initialisation failed (continuing without it):"
            )
        ]
        assert any(message_fragment in message for message in warnings), warnings
        for message in warnings:
            assert all(secret not in message for secret in SECRETS.values())

    def test_graph_enabled_constructs_store_and_writer_with_resolved_pin(
        self, root_harness: RootHarness,
    ) -> None:
        """Explicit graph extraction builds store/writer independently of Main and the catalog."""
        root_harness.start(saved_default=None, graph_enabled=True, write_cache=False)
        root = root_harness

        # Provider refresh completed before any graph construction.
        refresh_end = root.events.index("refresh:end")
        assert root.events.index("graph:build_extraction_call") > refresh_end
        assert root.events.index("graph:store:init") > refresh_end

        # Exactly one extraction helper call with the resolved RunModel + AgentConfig.
        assert len(root.graph_extraction_calls) == 1
        args, kwargs = root.graph_extraction_calls[0]
        assert len(args) == 2
        pin, agent_config = args
        assert isinstance(pin, RunModel)
        assert pin.model.id == GRAPH_ID
        assert pin.model.upstream_id == "graph-model"
        assert pin.connection.provider_id == "graph-extraction"
        assert pin.connection.adapter == "openai"
        assert pin.connection.base_url == f"https://{GRAPH_HOST}"
        assert pin.connection.api_key == SECRETS["GRAPH_KEY"]
        assert agent_config is root.app_cfg.agent
        assert kwargs.get("caller_tag", "graph-extract") == "graph-extract"

        # Store and writer constructed once and wired into both controller and executor.
        assert len(root.graph_store_instances) == 1
        assert len(root.graph_writer_instances) == 1
        assert root.main_controller._graph_memory is root.graph_store
        assert root.main_controller._graph_memory_writer is root.graph_writer
        assert root.builtin._graph_memory is root.graph_store
        assert root.builtin._graph_memory_writer is root.graph_writer

        # Telegram and indexing completed despite no active Main.
        assert "telegram:init" in root.events
        assert "telegram:run" in root.events
        assert len(root.tool_indexes) == 1
        assert root.tool_indexes[0].llm is root.root_embedding
        probes = [client for client in root.embedding_clients_at_hold if client is not root.root_embedding]
        # Graph validation is pure: no temporary embedding probe client is ever built.
        assert probes == []
        assert root.unexpected == []

    @pytest.mark.parametrize("saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["no_selection", "main_gone"])
    def test_graph_enabled_works_without_active_main(
        self, root_harness: RootHarness, saved_default: str | None,
    ) -> None:
        """Graph memory is resolved from the catalog, not from Main admission."""
        root_harness.start(saved_default=saved_default, graph_enabled=True)
        root = root_harness

        assert len(root.graph_store_instances) == 1
        assert len(root.graph_writer_instances) == 1
        assert root.main_controller._graph_memory is not None
        assert root.main_controller._graph_memory_writer is not None
        # No ordinary Main admission happened.
        assert root.registry.view().active_model_id is None
        probes = [client for client in root.embedding_clients_at_hold if client is not root.root_embedding]
        # Graph validation is pure: no temporary embedding probe client is ever built.
        assert probes == []
        assert "telegram:run" in root.events
        assert root.unexpected == []

    def test_graph_disabled_when_extraction_field_missing(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing extraction field disables graph alone; Telegram/indexing survive."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_missing_model(workspace: Path, *, graph_enabled: bool = False) -> str:
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            return text.replace(f'extraction_model = "{GRAPH_UPSTREAM}"\n', "")

        monkeypatch.setattr(rsh, "_config_text", config_with_missing_model)

        root_harness.start(saved_default=INVALID_SAVED, graph_enabled=True)
        root = root_harness

        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        assert "graph:writer:init" not in root.events
        self._assert_prerequisite_rejection(root, caplog, "graph_memory.extraction_model")
        assert "telegram:run" in root.events
        assert len(root.tool_indexes) == 1
        assert root.unexpected == []

    def test_graph_disabled_when_embeddings_unavailable(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unavailable embeddings disable graph alone; indexing degrades gracefully."""

        def failing_embed(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(503, text="embedding service unavailable")

        # Override the embed route so the embedding client reports unavailable.
        original_route = root_harness._route

        def route_with_failing_embed(request: httpx.Request) -> httpx.Response:
            if request.url.host == EMBED_HOST:
                return failing_embed(request)
            return original_route(request)

        monkeypatch.setattr(root_harness, "_route", route_with_failing_embed)

        root_harness.start(saved_default=INVALID_SAVED, graph_enabled=True)
        root = root_harness

        # Graph disabled before store construction.
        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        # Telegram still alive; tool index may be degraded but not crashed.
        assert "telegram:run" in root.events
        assert root.unexpected == []

    def test_graph_starts_with_local_context_below_the_fixed_extraction_cap(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph extraction has no fit gate: a tiny internal context window (512) leaves graph enabled."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_small_context(workspace: Path, *, graph_enabled: bool = False) -> str:
            """Set the generated agent context without bypassing real startup."""
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            return text.replace("[agent]\n", "[agent]\ncompaction_threshold_tokens = 512\n")

        monkeypatch.setattr(rsh, "_config_text", config_with_small_context)

        # ``agent.ctx_max_tokens`` can no longer be written in TOML. Delegate to the REAL
        # ``main.load_config`` (real TOML parse + validation, threshold 512 asserted below)
        # and swap only the typed agent section of its result to inject the retained
        # internal attribute (removed in task 6.2), so the effective tiny window is 512.
        real_load_config = main_mod.load_config
        injected: list[int] = []

        def load_config_with_tiny_internal_context(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], Any]:
            """Return the real ``(cfg, app_cfg)`` with only ``agent.ctx_max_tokens`` replaced."""
            cfg, app_cfg = real_load_config(*args, **kwargs)
            assert app_cfg.agent.ctx_max_tokens == 90_000  # real parse never sets it
            agent = dataclasses.replace(app_cfg.agent, ctx_max_tokens=512)
            injected.append(agent.ctx_max_tokens)
            return cfg, dataclasses.replace(app_cfg, agent=agent)

        monkeypatch.setattr(main_mod, "load_config", load_config_with_tiny_internal_context)

        root_harness.start(saved_default=INVALID_SAVED, graph_enabled=True)
        root = root_harness

        assert injected == [512]
        assert root.app_cfg.agent.compaction_threshold_tokens == 512
        assert root.app_cfg.agent.ctx_max_tokens == 512
        assert len(root.graph_store_instances) == 1
        assert len(root.graph_writer_instances) == 1
        assert "graph:store:init" in root.events
        assert "telegram:run" in root.events
        assert root.unexpected == []

    def test_graph_disabled_when_extraction_credentials_unresolvable(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unresolvable extraction credential reference disables graph alone, with no fallback."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_missing_graph_key(workspace: Path, *, graph_enabled: bool = False) -> str:
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            if graph_enabled:
                text = text.replace('extraction_api_key = "env:GRAPH_KEY"', 'extraction_api_key = "env:MISSING_GRAPH_KEY"')
            return text

        monkeypatch.setattr(rsh, "_config_text", config_with_missing_graph_key)
        monkeypatch.delenv("MISSING_GRAPH_KEY", raising=False)

        root_harness.start(saved_default=INVALID_SAVED, graph_enabled=True)
        root = root_harness

        self._assert_prerequisite_rejection(root, caplog, "graph_memory.extraction_api_key")
        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        assert "telegram:run" in root.events
        assert len(root.tool_indexes) == 1
        assert root.unexpected == []

    def test_graph_disabled_when_embedding_configuration_unavailable(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An explicit unsupported embedding config rejects graph before dimension work."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_unknown_embeddings(workspace: Path, *, graph_enabled: bool = False) -> str:
            """Exercise real parsing of an explicit unknown embedding provider."""
            return original_config_text(workspace, graph_enabled=graph_enabled).replace(
                '[embeddings]\nprovider = "openai"',
                '[embeddings]\nprovider = "unsupported-embedding-provider"',
            )

        monkeypatch.setattr(rsh, "_config_text", config_with_unknown_embeddings)
        root_harness.start(saved_default=INVALID_SAVED, graph_enabled=True)
        root = root_harness

        assert 'provider = "unsupported-embedding-provider"' in root.paths.config_file.read_text()
        assert root.app_cfg.embeddings.unavailable_reason == (
            "embeddings.provider must name a supported adapter type"
        )
        self._assert_prerequisite_rejection(root, caplog, "graph memory requires embeddings")
        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        assert "graph:writer:init" not in root.events
        assert "telegram:run" in root.events
        assert len(root.tool_indexes) == 1
        assert root.tool_indexes[0].llm is root.root_embedding
        assert not root.root_embedding.available
        assert root.requests_to(EMBED_HOST) == []
        assert root.unexpected == []


# ---------------------------------------------------------------------------
# Bounded graph shutdown through the actual root (task 12.26)
#
# Real GraphMemoryWriter thread, real extraction helper/pin/per-call client,
# real root embedding and real logging/SQLite on temp XDG paths. Only HTTP,
# the graph database and the stop join timeout (blocked cases) are substituted.
# GraphMemoryStopError is asserted by runtime type name (it does not exist yet).
# ---------------------------------------------------------------------------

_STOP_TIMEOUT_MESSAGE = (
    "Graph memory worker did not terminate before the shutdown deadline; "
    "dependent resources retained."
)
_STOP_FAILED_MESSAGE = (
    "Graph memory shutdown failed; worker termination is unconfirmed and "
    "dependent resources retained."
)
_GRAPH_TEXT = "Alice uses Python for research and shutdown notes. " * 3
_GRAPH_EXTRACTION = json.dumps({
    "entities": [
        {"name": "Alice", "entity_type": "person"},
        {"name": "Python", "entity_type": "tool"},
    ],
    "facts": [{
        "source": "Alice", "target": "Python", "relation_type": "USES",
        "fact": "Alice uses Python.",
    }],
})
_GRAPH_USAGE = {"prompt_tokens": 11, "completion_tokens": 5}
_EXTRACTION_POST = f"POST {GRAPH_HOST}/v1/chat/completions"
_EMBED_POST = f"POST {EMBED_HOST}/v1/embeddings"
_WORKER_WRITES = [("entity", False), ("entity", False), ("relation", False), ("episode", False)]
_ENTITY_VECTORS = {"alice": [0.25, 0.75], "python": [0.25, 0.75]}
_WORKER_LOG_LINE = "GraphMemory writer: extracted 2 entities, 1 facts from 1 messages"


@pytest.fixture
def graph_root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Yield a root with the real graph writer and logging; hygiene runs after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch, real_graph_writer=True, real_logging=True)
    yield harness
    harness.finish_graph_hygiene()  # idempotent backstop for the in-test finally


def _serve_graph_worker(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, *, block_host: str | None = None,
) -> SimpleNamespace:
    """Serve valid extraction at the HTTP boundary; optionally hold the worker at one host.

    Only requests issued on the real ``graph-memory-writer`` thread are recorded
    and may be held; startup and test-thread requests pass straight through.
    """
    barrier = SimpleNamespace(
        entered=threading.Event(), release=threading.Event(), worker_requests=[],
    )
    root.graph_barriers.append(barrier.release)
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Record/hold worker requests, serve extraction, delegate the rest."""
        host, path = request.url.host, request.url.path
        if threading.current_thread().name == "graph-memory-writer":
            barrier.worker_requests.append(f"{request.method} {host}{path}")
            if host == block_host:
                barrier.entered.set()
                if not barrier.release.wait(timeout=30):
                    raise AssertionError("graph worker barrier was never released")
        if request.method == "POST" and host == GRAPH_HOST and path == "/v1/chat/completions":
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": _GRAPH_EXTRACTION}}], "usage": _GRAPH_USAGE,
            })
        return original(request)

    monkeypatch.setattr(root, "_route", route)
    return barrier


def _graph_extraction_clients(root: RootHarness) -> list[LLMClient]:
    """Per-call graph extraction clients the real helper built."""
    return [c for c in root.llm_clients if getattr(c, "_graph_policy", None) is not None]


def _log_rows(root: RootHarness) -> list[dict[str, Any]]:
    """Flush the active SQLite writer (if any) and read every stored log row."""
    writer = sqlite_log._active_writer
    if writer is not None:
        assert writer.flush(timeout=5.0), "SQLite log writer did not flush"
    conn = sqlite_log.connect_store(str(root.paths.log_store))
    try:
        return [dict(row) for row in conn.execute("SELECT level, logger, msg, extra FROM events ORDER BY id")]
    finally:
        conn.close()


def _assert_logging_retained(root: RootHarness, probe: str) -> list[dict[str, Any]]:
    """The production SQLite sink is still active and stores a new record."""
    writer = root.log_writer
    assert writer is not None and sqlite_log._active_writer is writer, "log store was shut down"
    assert writer._thread is not None and writer._thread.is_alive() and not writer.disabled
    logging.getLogger("tests.graph_shutdown").warning(probe)
    rows = _log_rows(root)
    assert probe in [row["msg"] for row in rows]
    return rows


def _assert_failed_shutdown_retained(root: RootHarness, probe: str) -> list[dict[str, Any]]:
    """Shared live-worker retention evidence, observed at ``_run`` exit before hygiene."""
    writer, store, embedding = root.graph_writer, root.graph_store, root.root_embedding
    assert writer._thread.is_alive()
    assert root.graph_alive_after_stop == [True]
    events = root.events
    assert events.count("graph:writer:stop:raised") == 1
    for absent in (
        "graph:writer:stop:returned", "graph:store:close", "log_store:shutdown", "embedding:close:root",
    ):
        assert absent not in events, absent
    assert store.closed is False
    assert embedding._http.is_closed is False
    assert embedding.embed("retained embedding probe") == [0.25, 0.75]
    assert root.discoveries, "root never constructed ModelDiscovery"
    for discovery in root.discoveries:
        assert discovery._client.is_closed
    rows = _assert_logging_retained(root, probe)
    assert "Agent stopped." not in [row["msg"] for row in rows]
    return rows


def _graph_log(root: RootHarness) -> str:
    """Text of the real isolated graph-memory component log."""
    return Path(root.paths.graph_memory_log).read_text(encoding="utf-8")


class TestRootGraphShutdown:
    """Actual ``main._run`` graph shutdown with the real writer and logging (task 12.26)."""

    def test_completed_drain_closes_dependencies_only_after_worker_termination(
        self, graph_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shutdown flushes buffered work through the real worker; store, log store and
        root embedding close only after termination, and success is reported."""
        root = graph_root
        barrier = _serve_graph_worker(root, monkeypatch)
        seen: dict[str, Any] = {}

        def hold(started: RootHarness) -> None:
            """Buffer one message; only shutdown may flush it."""
            started.graph_writer.enqueue(_GRAPH_TEXT, user_id="graph-user", source="chat")
            seen["pending"] = started.graph_writer.get_stats()["pending_depth"]
            seen["worker_requests"] = list(barrier.worker_requests)

        try:
            root.start(saved_default=INVALID_SAVED, graph_enabled=True, hold=hold)
            writer, store = root.graph_writer, root.graph_store

            assert seen == {"pending": 1, "worker_requests": []}
            assert len(root.graph_stop_calls) == 1
            requested, used = root.graph_stop_calls[0]
            assert used == requested
            assert root.graph_stop_errors == []
            assert root.graph_alive_after_stop == [False]
            assert not writer._thread.is_alive()
            events = root.events
            ordered = [events.index(name) for name in (
                "graph:extraction_client:close", "graph:writer:stop:returned",
                "graph:store:close", "log_store:shutdown", "embedding:close:root",
            )]
            assert ordered == sorted(ordered), events
            assert events.count("graph:store:close") == events.count("embedding:close:root") == 1
            (client,) = _graph_extraction_clients(root)
            assert client._closed is True
            assert client._run_model is root.graph_extraction_calls[0][0][0]
            assert barrier.worker_requests == [_EXTRACTION_POST, _EMBED_POST, _EMBED_POST]
            assert store.preloaded == [_ENTITY_VECTORS]
            assert [(kind, closed) for kind, _args, closed in store.writes] == _WORKER_WRITES
            assert store.writes[-1][1] == (_GRAPH_TEXT, "graph-user", "chat", [0.25, 0.75])
            assert store.closed is True
            assert root.root_embedding._http.is_closed
            assert root.open_clients_at_exit == []
            assert root.usage_registry is not None
            assert root.usage_registry.get_today() == {GRAPH_ID: {"prompt": 11, "completion": 5, "total": 16}}
            assert sqlite_log._active_writer is None
            assert root.log_writer is not None and root.log_writer._thread is not None
            assert not root.log_writer._thread.is_alive()
            messages = [row["msg"] or "" for row in _log_rows(root)]
            assert messages.count("Agent stopped.") == 1
            assert not any(
                _STOP_TIMEOUT_MESSAGE in message or _STOP_FAILED_MESSAGE in message for message in messages
            )
            assert _WORKER_LOG_LINE in _graph_log(root)
            assert root.unexpected == []
        finally:
            root.finish_graph_hygiene()

    def test_blocked_extraction_fails_shutdown_and_retains_live_worker_dependencies(
        self, graph_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker blocked in extraction HTTP makes the real finite stop fail: the root
        propagates the safe error, retains store/embedding/logging and reports no success."""
        root = graph_root
        barrier = _serve_graph_worker(root, monkeypatch, block_host=GRAPH_HOST)
        root.graph_stop_timeout = 0.05
        seen: dict[str, Any] = {}

        def hold(started: RootHarness) -> None:
            """Hand the worker one batch and wait until it is held in extraction."""
            writer = started.graph_writer
            writer.enqueue(_GRAPH_TEXT, user_id="graph-user", source="chat")
            writer.flush()
            assert barrier.entered.wait(timeout=10), "graph worker never reached the extraction barrier"
            seen["held"] = True

        try:
            with pytest.raises(RuntimeError, match=re.escape(_STOP_TIMEOUT_MESSAGE)) as caught:
                root.start(saved_default=INVALID_SAVED, graph_enabled=True, hold=hold)

            # Observed at _run exit with the worker still blocked, before any hygiene.
            assert seen == {"held": True}
            assert type(caught.value).__name__ == "GraphMemoryStopError"
            assert str(caught.value) == _STOP_TIMEOUT_MESSAGE
            assert [used for _requested, used in root.graph_stop_calls] == [0.05]
            assert root.graph_stop_errors == [caught.value]
            writer, store, embedding = root.graph_writer, root.graph_store, root.root_embedding
            rows = _assert_failed_shutdown_retained(root, "retained log probe: extraction")
            assert any(_STOP_TIMEOUT_MESSAGE in (row["msg"] or "") for row in rows)
            (client,) = _graph_extraction_clients(root)
            assert client._closed is False and client._http.is_closed is False
            assert client._run_model is root.graph_extraction_calls[0][0][0]
            assert {id(c) for c in root.open_clients_at_exit} == {id(embedding._http), id(client._http)}
            assert barrier.worker_requests == [_EXTRACTION_POST]
            assert store.writes == []

            # Release: the worker finishes its batch on the still-open dependencies.
            barrier.release.set()
            writer._thread.join(timeout=10)
            assert not writer._thread.is_alive()
            assert client._closed is True
            assert store.closed is False and embedding._http.is_closed is False
            assert store.preloaded == [_ENTITY_VECTORS]
            assert [(kind, closed) for kind, _args, closed in store.writes] == _WORKER_WRITES
            assert store.writes[-1][1] == (_GRAPH_TEXT, "graph-user", "chat", [0.25, 0.75])
            assert root.usage_registry is not None
            assert root.usage_registry.get_today() == {GRAPH_ID: {"prompt": 11, "completion": 5, "total": 16}}
            assert _WORKER_LOG_LINE in _graph_log(root)
            assert root.unexpected == []
        finally:
            root.finish_graph_hygiene()

    def test_blocked_embedding_after_extraction_retains_root_embedding_and_store(
        self, graph_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker blocked in root-embedding HTTP after its extraction client closed still
        fails shutdown and keeps the root embedding, store and logging usable."""
        root = graph_root
        barrier = _serve_graph_worker(root, monkeypatch, block_host=EMBED_HOST)
        root.graph_stop_timeout = 0.05
        seen: dict[str, Any] = {}

        def hold(started: RootHarness) -> None:
            """Hand the worker one batch and wait until it is held in entity embedding."""
            writer = started.graph_writer
            writer.enqueue(_GRAPH_TEXT, user_id="graph-user", source="chat")
            writer.flush()
            assert barrier.entered.wait(timeout=10), "graph worker never reached the embedding barrier"
            seen["extraction_closed"] = [client._closed for client in _graph_extraction_clients(started)]

        try:
            with pytest.raises(RuntimeError, match=re.escape(_STOP_TIMEOUT_MESSAGE)) as caught:
                root.start(saved_default=INVALID_SAVED, graph_enabled=True, hold=hold)

            # Observed at _run exit with the worker still blocked, before any hygiene.
            assert seen == {"extraction_closed": [True]}
            assert type(caught.value).__name__ == "GraphMemoryStopError"
            assert str(caught.value) == _STOP_TIMEOUT_MESSAGE
            assert [used for _requested, used in root.graph_stop_calls] == [0.05]
            assert root.graph_stop_errors == [caught.value]
            writer, store, embedding = root.graph_writer, root.graph_store, root.root_embedding
            rows = _assert_failed_shutdown_retained(root, "retained log probe: embedding")
            assert any(_STOP_TIMEOUT_MESSAGE in (row["msg"] or "") for row in rows)
            assert [id(c) for c in root.open_clients_at_exit] == [id(embedding._http)]
            assert barrier.worker_requests == [_EXTRACTION_POST, _EMBED_POST]
            assert store.preloaded == [] and store.writes == []

            # Release: the worker embeds and writes through the retained root embedding/store.
            barrier.release.set()
            writer._thread.join(timeout=10)
            assert not writer._thread.is_alive()
            assert store.closed is False and embedding._http.is_closed is False
            assert barrier.worker_requests == [_EXTRACTION_POST, _EMBED_POST, _EMBED_POST]
            assert store.preloaded == [_ENTITY_VECTORS]
            assert [(kind, closed) for kind, _args, closed in store.writes] == _WORKER_WRITES
            assert store.writes[-1][1] == (_GRAPH_TEXT, "graph-user", "chat", [0.25, 0.75])
            assert _WORKER_LOG_LINE in _graph_log(root)
            assert root.unexpected == []
        finally:
            root.finish_graph_hygiene()

    def test_unexpected_stop_failure_is_wrapped_and_retains_dependencies(
        self, graph_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected exception inside the real stop is wrapped in the safe error with
        its cause chained but never logged; dependencies stay retained, no success."""
        root = graph_root
        barrier = _serve_graph_worker(root, monkeypatch, block_host=GRAPH_HOST)
        cause_text = "synthetic stop fault sk-unlogged-cause-998877"
        fault = OSError(cause_text)
        tripped: list[object] = []

        def hold(started: RootHarness) -> None:
            """Hold the worker in extraction, then make the shutdown sentinel put fail once."""
            writer = started.graph_writer
            writer.enqueue(_GRAPH_TEXT, user_id="graph-user", source="chat")
            writer.flush()
            assert barrier.entered.wait(timeout=10), "graph worker never reached the extraction barrier"
            real_put = writer._queue.put

            def put(item: object, *args: Any, **kwargs: Any) -> None:
                """External fault: the first shutdown sentinel cannot be queued."""
                if item is graph_memory.GraphMemoryWriter._SENTINEL and not tripped:
                    tripped.append(item)
                    raise fault
                real_put(item, *args, **kwargs)

            monkeypatch.setattr(writer._queue, "put", put)

        try:
            with pytest.raises(RuntimeError, match=re.escape(_STOP_FAILED_MESSAGE)) as caught:
                root.start(saved_default=INVALID_SAVED, graph_enabled=True, hold=hold)

            # Observed at _run exit with the worker still blocked, before any hygiene.
            assert len(tripped) == 1
            assert type(caught.value).__name__ == "GraphMemoryStopError"
            assert str(caught.value) == _STOP_FAILED_MESSAGE
            assert caught.value.__cause__ is fault
            assert root.graph_stop_errors == [fault]
            embedding = root.root_embedding
            rows = _assert_failed_shutdown_retained(root, "retained log probe: fault")
            assert any(_STOP_FAILED_MESSAGE in (row["msg"] or "") for row in rows)
            assert cause_text not in json.dumps(rows)
            assert cause_text not in Path(root.paths.log_file).read_text(encoding="utf-8")
            assert cause_text not in _graph_log(root)
            (client,) = _graph_extraction_clients(root)
            assert client._closed is False
            assert {id(c) for c in root.open_clients_at_exit} == {id(embedding._http), id(client._http)}
            assert root.graph_store.writes == []
            assert root.unexpected == []
        finally:
            # Hygiene: production never queued its sentinel, so the helper ends the worker.
            root.finish_graph_hygiene()


class TestRealLoggingHarnessIsolation:
    """Opt-in real logging leaves preconfigured suite logging exactly as it was (hygiene)."""

    def test_real_logging_root_restores_preconfigured_suite_logging(
        self, tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prior handlers, the prior real SQLite writer and structlog config/context are
        detached (not closed/stopped) during the real root and reinstated exactly."""
        root_logger, named = logging.getLogger(), logging.getLogger("graph_memory")
        # This probe's own baseline, restored on exit independently of the harness.
        base_root_handlers, base_root_level = root_logger.handlers[:], root_logger.level
        base_named_handlers, base_named_propagate, base_named_level = (
            named.handlers[:], named.propagate, named.level,
        )
        base_structlog = (
            structlog.get_config(), structlog.is_configured(), structlog.contextvars.get_contextvars(),
        )
        with sqlite_log._active_writer_lock:
            base_writer = sqlite_log._active_writer
        prior_stream, prior_named_stream = io.StringIO(), io.StringIO()
        prior_handler = logging.StreamHandler(prior_stream)
        prior_named_handler = logging.StreamHandler(prior_named_stream)
        prior_db = tmp_xdg / "prior-suite-logs.sqlite"
        prior_writer = sqlite_log.SqliteLogWriter(str(prior_db))
        prior_processors = [structlog.contextvars.merge_contextvars, structlog.processors.KeyValueRenderer()]
        harness: RootHarness | None = None
        try:
            # Preconfigured suite logging: extra handlers, a real active writer, structlog state.
            root_logger.addHandler(prior_handler)
            named.addHandler(prior_named_handler)
            named.propagate = False
            named.setLevel(logging.DEBUG)
            prior_writer.start()
            with sqlite_log._active_writer_lock:
                sqlite_log._active_writer = prior_writer
            structlog.configure(processors=prior_processors, cache_logger_on_first_use=False)
            structlog.contextvars.bind_contextvars(suite_probe="kept")
            expected_root = (root_logger.handlers[:], root_logger.level)
            expected_named = (named.handlers[:], named.propagate, named.level)
            expected_config = structlog.get_config()
            expected_context = structlog.contextvars.get_contextvars()

            harness = RootHarness(tmp_xdg, monkeypatch, real_logging=True)
            harness.start(saved_default=MAIN_ID)

            # Production used only the test-owned sinks while the prior ones were detached.
            assert harness.log_writer is not None and harness.log_writer is not prior_writer
            assert "log_store:shutdown" in harness.events
            assert prior_handler not in root_logger.handlers
            assert prior_named_handler not in named.handlers
            assert prior_writer._thread is not None and prior_writer._thread.is_alive()
            assert "Agent stopped." not in prior_stream.getvalue()
            owned = sqlite_log.connect_store(str(harness.paths.log_store))
            try:
                owned_messages = [row["msg"] for row in owned.execute("SELECT msg FROM events")]
            finally:
                owned.close()
            assert "Agent stopped." in owned_messages

            harness.finish_graph_hygiene()

            assert (root_logger.handlers, root_logger.level) == expected_root
            assert (named.handlers, named.propagate, named.level) == expected_named
            assert structlog.get_config() == expected_config
            assert structlog.get_config()["processors"] is prior_processors
            assert structlog.is_configured() is True
            assert structlog.contextvars.get_contextvars() == expected_context
            with sqlite_log._active_writer_lock:
                assert sqlite_log._active_writer is prior_writer
            assert prior_writer._thread.is_alive() and not prior_writer.disabled
            assert harness.log_writer._thread is not None and not harness.log_writer._thread.is_alive()
            # The reinstated prior sinks are still open and usable.
            logging.getLogger("tests.suite_probe").warning("prior root handler still usable")
            assert "prior root handler still usable" in prior_stream.getvalue()
            named.debug("prior named handler still usable")
            assert "prior named handler still usable" in prior_named_stream.getvalue()
            prior_writer.enqueue({
                "ts": datetime.now(timezone.utc).isoformat(), "level": "info",
                "msg": "prior writer still usable",
            })
            assert prior_writer.flush(timeout=5.0)
            prior = sqlite_log.connect_store(str(prior_db))
            try:
                prior_messages = [row["msg"] for row in prior.execute("SELECT msg FROM events")]
            finally:
                prior.close()
            assert "prior writer still usable" in prior_messages
        finally:
            try:
                if harness is not None:
                    harness.finish_graph_hygiene()
            finally:
                root_logger.removeHandler(prior_handler)
                named.removeHandler(prior_named_handler)
                prior_handler.close()
                prior_named_handler.close()
                for handler in base_root_handlers:
                    if handler not in root_logger.handlers:
                        root_logger.addHandler(handler)
                root_logger.setLevel(base_root_level)
                for handler in base_named_handlers:
                    if handler not in named.handlers:
                        named.addHandler(handler)
                named.propagate = base_named_propagate
                named.setLevel(base_named_level)
                with sqlite_log._active_writer_lock:
                    sqlite_log._active_writer = base_writer
                prior_writer.stop(timeout=5.0)
                restore_structlog_state(*base_structlog)


class TestRootDiagnosticMainGate:
    """Task 4.1: a diagnostic run's explicit selector needs neither Main nor cache membership.

    Implicit and scheduled refusal without Main stays in
    ``test_missing_main_keeps_commands_available_without_default_or_model_work``.
    """

    @pytest.mark.parametrize("saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["absent", "removed"])
    def test_diagnostic_profile_admits_valid_explicit_selector_without_active_main(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        saved_default: str | None,
    ) -> None:
        """A valid explicit OTHER_ID is admitted once, resolving one credential and pinning
        its route; Main, the preference and the cache stay unchanged and no request is sent."""
        from agent_runtime import RuntimeProfile

        usage = TokenUsageRegistry()
        # Before start: the real runtime captures the shared registry at construction.
        monkeypatch.setattr(token_usage, "_registry", usage)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Call the installed root factory for a diagnostic run after startup."""
            runtime, registry, factory = root.factory_binding()
            outcome["binding"] = (
                runtime is root.runtime, registry is root.registry,
                root.builtin._sub_agent_factory is factory,
            )
            outcome["view_before"] = root.registry.view()
            outcome["settings_before"] = root.paths.models_cache_file.read_bytes()
            marks = (len(root.requests), len(root.llm_clients), len(root.http_clients))
            outcome["admissions"] = _spy_admissions(monkeypatch, root.registry)
            resolved: list[str] = []
            resolve = root.registry._credential_resolver

            def resolve_spy(config: Any) -> Any:
                """Record a credential resolution, then delegate."""
                resolved.append(config.name)
                return resolve(config)

            monkeypatch.setattr(root.registry, "_credential_resolver", resolve_spy)
            creates: list[tuple[object, object]] = []
            create = root.runtime.create

            def create_spy(profile: object, options: object, **kwargs: Any) -> object:
                """Record a runtime construction request, then delegate."""
                creates.append((profile, options))
                return create(profile, options, **kwargs)

            monkeypatch.setattr(root.runtime, "create", create_spy)
            runner = factory(model=OTHER_ID, runtime_profile=RuntimeProfile.DIAGNOSTIC_AGENT)
            outcome["runner_model"] = runner.model_id
            runner.close()  # the run never starts; release its owned clients
            outcome["resolved"], outcome["creates"] = resolved, creates
            requests, llm_clients, http_clients = marks
            outcome["new_requests"] = root.requests[requests:]
            outcome["new_llm_clients"] = root.llm_clients[llm_clients:]
            outcome["new_http_clients"] = root.http_clients[http_clients:]
            outcome["view_after"] = root.registry.view()
            outcome["settings_after"] = root.paths.models_cache_file.read_bytes()

        root_harness.start(saved_default=saved_default, hold=hold)
        root = root_harness

        assert outcome["binding"] == (True, True, True)
        before = outcome["view_before"]
        assert before.active_model_id is None and before.remembered_model_id == remembered_for(saved_default)
        assert OTHER_ID in {m.id for m in before.models}
        assert outcome["runner_model"] == OTHER_ID
        assert outcome["admissions"] == {"admit": [OTHER_ID], "admit_scheduled": []}
        assert outcome["resolved"] == [OTHER_ID.split("/", 1)[0]] and len(outcome["creates"]) == 1
        assert outcome["new_requests"] == []
        assert usage.get_today() == {}
        assert outcome["view_after"] == before
        assert outcome["settings_after"] == outcome["settings_before"]
        assert root.registry.view().active_model_id is None
        assert root.unexpected == [] and root.open_clients_at_exit == []


class TestRootGraphStartupContainment:
    """Task 12.24: graph startup after a partly failed refresh, and independent
    containment of scheduler and graph failures with real bot commands."""

    def test_graph_uses_explicit_config_without_catalog_or_startup_discovery(
        self, root_harness: RootHarness,
    ) -> None:
        """Task 5.2: graph extraction builds its pin from its own explicit settings: the catalog has
        no graph record or provider, nothing is discovered for it, and Main is only the in-memory
        first cached candidate."""
        root_harness.serve_provider_chat(failing_discovery_hosts=(GRAPH_HOST,))
        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        assert root.refresh_reports == [] and not [e for e in root.events if e.startswith("fetch:")]
        assert not [r for r in root.requests if r.url.path.endswith("/models")]
        view = root.registry.view()
        assert {p.provider_id: p.status for p in view.providers} == {"acme": "stale", "gem": "stale"}
        assert GRAPH_ID not in {m.id for m in view.models}

        # Graph pin is resolved after the selection restore (no rediscovery) and before store, writer and polling.
        ev = root.events
        assert ev.index("restore") < ev.index("graph:build_extraction_call") < ev.index("graph:store:init")
        assert ev.index("graph:store:init") < ev.index("graph:writer:init") < ev.index("telegram:run")
        ((args, kwargs),) = root.graph_extraction_calls
        pin, agent_config = args
        assert isinstance(pin, RunModel) and pin.model.id == GRAPH_ID
        assert pin.model.upstream_id == "graph-model"
        assert pin.connection.provider_id == "graph-extraction" and pin.connection.adapter == "openai"
        assert pin.connection.base_url == f"https://{GRAPH_HOST}"
        assert pin.connection.api_key == SECRETS["GRAPH_KEY"]
        assert agent_config is root.app_cfg.agent
        assert kwargs.get("caller_tag", "graph-extract") == "graph-extract"

        # Actual root wiring of the constructed store and writer.
        assert len(root.graph_store_instances) == 1 and len(root.graph_writer_instances) == 1
        assert root.main_controller._graph_memory is root.graph_store
        assert root.main_controller._graph_memory_writer is root.graph_writer
        assert root.builtin._graph_memory is root.graph_store
        assert root.builtin._graph_memory_writer is root.graph_writer
        assert root.telegram._graph_memory_store is root.graph_store
        assert root.telegram._graph_memory_writer is root.graph_writer

        # Main is the in-memory first candidate: never remembered or persisted, no Main chat client, no inference.
        assert view.active_model_id == MAIN_ID and view.remembered_model_id is None
        assert not root.paths.model_selection_file.exists()
        saved = ModelCache(root.paths.models_cache_file).load().document
        assert saved.default_model is None and GRAPH_ID not in {m.id for m in saved.models}
        assert root.llm_clients == [] and root.main_controller.llm is None
        assert [r for r in root.requests if r.method == "POST" and r.url.host != EMBED_HOST] == []
        assert root.unexpected == [] and root.open_clients_at_exit == []

    @pytest.mark.parametrize("scheduler_state", ["unparseable", "unreadable"])
    def test_invalid_scheduler_and_graph_extraction_fail_independently_while_commands_work(
        self, tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        scheduler_state: str,
    ) -> None:
        """An unparseable/unreadable scheduler file and a graph config missing its extraction model each fail
        alone: the real Scheduler is contained, graph is disabled, and the real bot
        ``/models`` and ``/jobs`` commands answer from the same root."""
        import telegram_interface
        from telegram_commands import cmd_jobs, cmd_models
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_bad_selector(workspace: Path, *, graph_enabled: bool = False) -> str:
            """Real config parsing with the graph extraction model removed."""
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            return text.replace(f'extraction_model = "{GRAPH_UPSTREAM}"\n', "")

        monkeypatch.setattr(rsh, "_config_text", config_with_bad_selector)
        root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
        try:
            main_mod._create_xdg_dirs(root.paths)
            scheduler_path = root.paths.scheduler_config
            # Genuine whole-file load failures (existing load-containment fixture).
            _write_config(scheduler_path, scheduler_state, "")
            outcome: dict[str, Any] = {}

            def hold(started: RootHarness) -> None:
                """Drive real bot commands on an interface built from the root's inputs."""
                iface = telegram_interface.TelegramInterface(
                    *started.telegram.args, **started.telegram.kwargs,
                )
                iface.agent = started.main_controller
                iface._prompt_registry = started.telegram._prompt_registry
                outcome["iface"] = iface
                outcome["notices_at_hold"] = list(started.notices)
                chat = _TelegramMessage()
                outcome["chat"] = chat
                ctx = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))

                async def drive() -> None:
                    """``/models`` then ``/jobs`` through the actual command handlers."""
                    await cmd_models(iface, _message_update(chat), ctx)
                    await cmd_jobs(iface, _message_update(chat), ctx)

                _run_on_inline_loop(drive())
                outcome["clients_at_hold"] = len(started.llm_clients)

            root.start(saved_default=INVALID_SAVED, graph_enabled=True, hold=hold)

            # The real Scheduler was built and contained its whole-file failure.
            scheduler = root.scheduler
            assert isinstance(scheduler, Scheduler)
            assert scheduler._load_result.status == scheduler_state
            assert scheduler.list_jobs() == []
            notice = scheduler.load_notice()
            assert notice is not None and str(scheduler_path) in notice and scheduler_state in notice
            assert outcome["notices_at_hold"] == [f"⚠️ {html.escape(notice)}"]
            if scheduler_state == "unparseable":
                assert scheduler_path.read_text() == _UNPARSEABLE
            else:
                assert scheduler_path.is_dir()
            scheduler_warnings = [
                r.getMessage() for r in caplog.records
                if r.name == "scheduler" and r.levelno == logging.WARNING
                and r.getMessage().startswith(("Could not parse scheduler file", "Could not read scheduler file"))
            ]
            assert len(scheduler_warnings) == 1, scheduler_warnings

            # Graph prerequisite failure disabled graph alone, before helper/store/dimension work.
            TestRootStartupGraphMemory._assert_prerequisite_rejection(
                root, caplog, "graph_memory.extraction_model must be configured explicitly",
            )
            assert root.graph_store_instances == [] and root.graph_writer_instances == []
            assert "graph:store:init" not in root.events and "graph:writer:init" not in root.events
            assert root.main_controller._graph_memory is None
            assert root.main_controller._graph_memory_writer is None
            assert root.builtin._graph_memory is None and root.builtin._graph_memory_writer is None
            assert root.telegram._graph_memory_store is None
            assert root.telegram._graph_memory_writer is None

            # Real commands answered from the same root registry and scheduler.
            iface = outcome["iface"]
            assert iface.model_registry is root.registry and iface.scheduler is scheduler
            assert scheduler._result_notice_sink == iface.enqueue_scheduled_notice
            chat: _TelegramMessage = outcome["chat"]
            (listing,) = [m for m in chat.replies if "Model Catalog" in m.text]
            assert MAIN_ID in listing.text and OTHER_ID in listing.text
            _assert_no_secrets(listing.text)
            (jobs_reply,) = [m for m in chat.replies if m is not listing]
            assert html.escape(notice) in jobs_reply.text

            # Indexing and the explicit embeddings stayed usable.
            assert len(root.tool_indexes) == 1
            assert root.tool_indexes[0].llm is root.root_embedding and root.root_embedding.available
            embed_requests = root.requests_to(EMBED_HOST)
            assert embed_requests and {r.headers["authorization"] for r in embed_requests} == {
                f"Bearer {SECRETS['EMBED_KEY']}"
            }

            # Main stays inactive: no fallback, no default, no chat client, no inference.
            view = root.registry.view()
            assert view.active_model_id is None and view.remembered_model_id is None
            saved = ModelCache(root.paths.models_cache_file).load().document
            assert saved.default_model is None
            assert outcome["clients_at_hold"] == 0 and root.llm_clients == []
            assert root.main_controller.llm is None
            assert [r for r in root.requests if r.method == "POST" and r.url.host != EMBED_HOST] == []

            # Root-owned resources were released by production before fixture hygiene.
            assert root.discoveries
            for discovery in root.discoveries:
                assert discovery._client.is_closed
            assert root.root_embedding._http.is_closed
            assert root.open_clients_at_exit == []
            assert root.unexpected == []
        finally:
            root.release_leftovers()


# Exact corrupt inputs of tests/test_model_settings.py component cases, with a fake leak marker.
_SETTINGS_LEAK = "sk-settingsleak12345"
_STARTUP_SETTINGS: dict[str, bytes | None] = {
    "absent": None,
    "corrupt-toml": f'{{"schema_version": 1, "models": [\n"{_SETTINGS_LEAK}"'.encode(),
    "invalid-schema": f'{{"schema_version": 2, "note": "{_SETTINGS_LEAK}"}}'.encode(),
}


class TestRootManagedSettingsStartup:
    """Task 3.6: genuinely absent or corrupt managed settings through the actual startup."""

    @pytest.mark.parametrize(
        ("state", "graph_enabled"),
        [
            pytest.param("absent", False, id="absent"),
            pytest.param("corrupt-toml", False, id="corrupt-toml"),
            pytest.param("invalid-schema", False, id="invalid-schema"),
            pytest.param("corrupt-toml", True, id="corrupt-toml-graph"),
        ],
    )
    def test_missing_or_corrupt_settings_keep_commands_and_gate_model_work(
        self, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, state: str, graph_enabled: bool,
    ) -> None:
        """Corrupt input is preserved before the first production overwrite; startup
        persists a fresh catalog; with no preference the first candidate is the in-memory Main
        (task 3.5) and is never saved; commands answer; graph resolves in the same run."""
        import telegram_interface
        from telegram_commands import cmd_models
        from tests import root_startup_harness as rsh

        raw = _STARTUP_SETTINGS[state]
        boundary = _ReactBoundary()
        monkeypatch.setattr(agent_controller, "react_loop", boundary)

        class _SetupWriter:
            """Replaces only the harness's setup write: places absent or corrupt bytes."""

            def __init__(self, path: Path) -> None:
                """Bind the managed settings path the harness would have written."""
                self.path = Path(path)

            def write(self, _document: object) -> None:
                """Leave the file absent or write the raw corrupt input."""
                if raw is None:
                    assert not self.path.exists()
                else:
                    self.path.write_bytes(raw)

        monkeypatch.setattr(rsh, "ModelCache", _SetupWriter)
        root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
        settings_path = root.paths.models_cache_file

        def backups() -> dict[str, bytes]:
            """Every ``.corrupt*`` sibling of the managed file and its bytes."""
            return {
                p.name: p.read_bytes()
                for p in sorted(settings_path.parent.glob(f"{settings_path.name}.corrupt*"))
            }

        # Delegating observers on the real ModelSettings read and the real FS replace.
        loads: list[SimpleNamespace] = []
        real_load = ModelCache.load

        def load_spy(settings: ModelCache) -> Any:
            """Record the input state around the real load, then return its result."""
            seen = SimpleNamespace(
                path=settings.path, events=list(root.events),
                before=settings.path.read_bytes() if settings.path.exists() else None,
            )
            result = real_load(settings)
            seen.status = result.status
            seen.after = settings.path.read_bytes() if settings.path.exists() else None
            loads.append(seen)
            return result

        replaces: list[SimpleNamespace] = []
        real_replace = os.replace

        def replace_spy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            """At each managed-file replacement, record what is on disk right before it."""
            if Path(dst) == settings_path:
                replaces.append(SimpleNamespace(
                    target=settings_path.read_bytes() if settings_path.exists() else None,
                    backups=backups(), incoming=Path(src).read_bytes(), events=list(root.events),
                ))
            real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(ModelCache, "load", load_spy)
        monkeypatch.setattr(os, "replace", replace_spy)
        outcome: dict[str, Any] = {}

        def hold(started: RootHarness) -> None:
            """Drive the real ``/models`` command, an ordinary request and a factory call."""
            iface = telegram_interface.TelegramInterface(*started.telegram.args, **started.telegram.kwargs)
            iface.agent = started.main_controller
            iface._prompt_registry = started.telegram._prompt_registry
            outcome["iface"] = iface
            chat = _TelegramMessage()
            outcome["chat"] = chat
            ctx = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))
            resolved: list[str] = []
            marks: list[int] = []

            def mark_ordinary_phase() -> None:
                """After the real /models (which legitimately refreshes the catalog),
                install the ordinary-work observers and take the I/O marks."""
                outcome["view_after_models"] = started.registry.view()
                outcome["replaces_after_models"] = len(replaces)
                outcome["admissions"] = _spy_admissions(monkeypatch, started.registry)
                resolve = started.registry._credential_resolver

                def resolve_spy(config: Any) -> Any:
                    """Record a credential resolution, then delegate."""
                    resolved.append(config.name)
                    return resolve(config)

                monkeypatch.setattr(started.registry, "_credential_resolver", resolve_spy)
                marks.extend((len(started.requests), len(started.llm_clients), len(started.http_clients)))

            outcome["resolved"] = resolved

            async def drive() -> None:
                """``/models``, then one ordinary request through the actual handler."""
                await cmd_models(iface, _message_update(chat), ctx)
                mark_ordinary_phase()
                await iface._run_agent_task(_message_update(chat), ctx, "ordinary request")
                await asyncio.gather(*list(iface._flush_tasks))

            _run_on_inline_loop(drive())
            requests, llm_clients, http_clients = marks
            outcome["new_requests"] = started.requests[requests:]
            outcome["new_llm"] = started.llm_clients[llm_clients:]
            outcome["new_http"] = started.http_clients[http_clients:]
            outcome["view"] = started.registry.view()

        try:
            root.start(saved_default=None, graph_enabled=graph_enabled, hold=hold)

            # Startup input: the real registry load saw the genuine absent/corrupt file first.
            first = loads[0]
            assert first.path == settings_path and "refresh:start" not in first.events
            if raw is None:
                assert (first.before, first.status) == (None, "missing")
            else:
                # Disposable cache: treated as absent, bytes untouched, never quarantined.
                assert first.before == raw and first.status == "corrupt" and first.after == raw

            # Two production refreshes write the managed file: startup
            # (main._refresh_model_catalog) and the real /models command
            # (telegram_commands._render_models_catalog -> registry.refresh).
            assert len(replaces) == 2 and outcome["replaces_after_models"] == 2
            write, command_write = replaces
            # Temporal proof at the FIRST production overwrite (inside the startup refresh).
            assert write.events.count("refresh:start") == 1
            assert "refresh:end" not in write.events and "telegram:run" not in write.events
            if raw is None:
                assert write.target is None and write.backups == {}
            else:
                assert write.target == raw and write.backups == {}
            # The command refresh overwrites only the startup payload; backups untouched.
            assert "telegram:run" in command_write.events
            assert command_write.events.count("refresh:start") == 2
            assert command_write.target == write.incoming
            assert command_write.backups == write.backups

            # The first (startup) payload, parsed by the real ModelSettings off the
            # production path, is a valid fresh catalog with no Main/default.
            expected = DISCOVERED  # graph extraction is never a catalog record (task 5.2)
            startup_copy = tmp_path / "phase-check" / "startup-payload.json"
            startup_copy.parent.mkdir()
            startup_copy.write_bytes(write.incoming)
            startup = ModelCache(startup_copy).load()
            assert startup.status == "valid" and startup.document.default_model is None
            assert tuple(m.id for m in startup.document.models) == expected
            assert {p.status for p in startup.document.providers} == {"fresh"}

            # Final managed document = the LAST production payload: valid, fresh,
            # one revision after startup, still no Main/default.
            assert settings_path.read_bytes() == command_write.incoming
            final = ModelCache(settings_path).load()
            assert final.status == "valid" and final.document.default_model is None
            assert final.document.models == startup.document.models
            assert tuple(m.id for m in final.document.models) == expected
            assert {p.status for p in final.document.providers} == {"fresh"}
            assert backups() == {}
            assert _SETTINGS_LEAK not in settings_path.read_text(encoding="utf-8")

            # Main is the in-memory first candidate (task 3.5): active, never remembered or saved.
            for view in (outcome["view_after_models"], outcome["view"], root.registry.view()):
                assert view.active_model_id == MAIN_ID and view.remembered_model_id is None
                assert view.selection_status == "active"
            assert not root.paths.model_selection_file.exists()
            assert root.main_controller.llm is None

            # Actual commands answer from the same root registry.
            iface = outcome["iface"]
            assert iface.model_registry is root.registry
            chat: _TelegramMessage = outcome["chat"]
            (listing,) = [m for m in chat.replies if "Model Catalog" in m.text]
            assert MAIN_ID in listing.text and OTHER_ID in listing.text
            # Ordinary work runs once on the in-memory Main pin; no other provider is touched.
            assert [m.text for m in chat.replies].count("🔄 Processing…") == 1
            prompts = {r.text: r.status for r in root.telegram._prompt_registry.list_recent(10)}
            assert prompts == {"ordinary request": "done"}
            (call,) = boundary.calls
            assert call.pin.model.id == MAIN_ID and call.pin.connection.provider_id == "acme"
            assert outcome["admissions"] == {"admit": [None], "admit_scheduled": []}
            assert outcome["resolved"] == ["acme"]
            assert outcome["new_requests"] == []

            # No input bytes leak anywhere observable.
            messages = [r.getMessage() for r in caplog.records]
            observable = "\n".join(messages + root.notices + [m.text for m in chat.replies] + [
                edit for m in chat.replies for edit in m.edits
            ])
            assert _SETTINGS_LEAK not in observable
            _assert_no_secrets(observable)

            # Graph carve-out: resolved after the complete refresh in the same run.
            if graph_enabled:
                refresh_end = root.events.index("refresh:end")
                build = root.events.index("graph:build_extraction_call")
                # Settled once at startup; neither /models nor the ordinary gate re-pins.
                assert refresh_end < build < root.events.index("telegram:run")
                ((args, _kwargs),) = root.graph_extraction_calls
                pin = args[0]
                assert isinstance(pin, RunModel) and pin.model.id == GRAPH_ID
                assert pin.model.upstream_id == "graph-model" and pin.connection.provider_id == "graph-extraction"
                assert pin.connection.api_key == SECRETS["GRAPH_KEY"]
                assert root.main_controller._graph_memory is root.graph_store is not None
                assert root.main_controller._graph_memory_writer is root.graph_writer is not None
                assert root.builtin._graph_memory is root.graph_store
            else:
                assert root.graph_store_instances == [] and root.graph_extraction_calls == []

            # Root-owned resources were released by production before hygiene.
            assert root.discoveries
            for discovery in root.discoveries:
                assert discovery._client.is_closed
            assert root.root_embedding._http.is_closed
            assert root.open_clients_at_exit == []
            assert root.unexpected == []
        finally:
            root.release_leftovers()


_DISCOVERY_HOSTS = (ACME_HOST, GEM_HOST)
_ROOT_PREF_CANARY = "sk-rootprefcanary0123456789"


class TestRootStartupSelection:
    """Task 3.5: the real ``main._run`` selects at startup from the saved preference or memory."""

    @staticmethod
    def _discovery_calls(root: RootHarness) -> list[str]:
        return [e for e in root.events if e.startswith("fetch:")] + [
            f"http:{r.url.host}" for r in root.requests if r.url.host in _DISCOVERY_HOSTS
        ]

    @pytest.mark.parametrize("with_cache", [True, False], ids=["cached", "empty-cache"])
    @pytest.mark.parametrize(
        "variant",
        ["wrong-type", "toml", "extra-key", "unreadable", "removed-connection"],
    )
    def test_invalid_or_unreadable_saved_selection_is_unset_without_discovery_or_writes(
        self, root_harness: RootHarness, caplog: pytest.LogCaptureFixture, variant: str, with_cache: bool,
    ) -> None:
        raw = {
            "wrong-type": b"model = 7\n",
            "toml": b"model = [broken\n",
            "extra-key": f'model = "{MAIN_ID}"\napi_key = "{_ROOT_PREF_CANARY}"\n'.encode(),
            "unreadable": None,
            "removed-connection": b'model = "oldconn/m"\n',
        }[variant]
        with caplog.at_level(logging.WARNING):
            root_harness.start(
                saved_default=None, preference_bytes=raw,
                preference_unreadable=variant == "unreadable", write_cache=with_cache,
            )
        root = root_harness
        cache = root.paths.models_cache_file
        pref = root.paths.model_selection_file

        assert "telegram:run" in root.events  # commands stay available
        view = root.registry.view()
        assert view.active_model_id is None  # no automatic choice, even with a known cache
        assert self._discovery_calls(root) == [] and root.refresh_reports == []
        assert root.unexpected == []
        if raw is None:
            assert pref.is_dir()
        else:
            assert pref.read_bytes() == raw  # preserved exactly, never quarantined or replaced
        assert sorted(p.name for p in pref.parent.iterdir() if "model-selection" in p.name) == [pref.name]
        assert cache.exists() is with_cache
        if with_cache:
            assert tuple(m.id for m in ModelCache(cache).load().document.models) == tuple(
                sorted((MAIN_ID, OTHER_ID))
            )  # the stale cache is not refreshed
        messages = [r.getMessage() for r in caplog.records]
        notice = [m for m in messages if "saved model selection" in m]
        assert len(notice) == 1 and "/models" in notice[0]
        assert _ROOT_PREF_CANARY not in "\n".join(messages)

    def test_saved_configured_uncached_selector_stays_active_and_unmodified(
        self, root_harness: RootHarness,
    ) -> None:
        root_harness.start(saved_default=GONE_ID, write_cache=False)
        root = root_harness
        view = root.registry.view()
        assert view.active_model_id == GONE_ID and view.selection_status == "active"
        assert view.remembered_model_id == GONE_ID
        assert GONE_ID not in {m.id for m in view.models}  # active independent of cache membership
        assert root.paths.model_selection_file.read_bytes() == f'model = "{GONE_ID}"\n'.encode()

    def test_without_preference_first_ordinal_candidate_is_active_in_memory_and_rederived(
        self, root_harness: RootHarness,
    ) -> None:
        root_harness.start(saved_default=None, removed_connection="oldconn")
        root = root_harness
        view = root.registry.view()
        assert view.active_model_id == MAIN_ID == min(DISCOVERED)  # not the removed "oldconn/aaa-first"
        assert view.remembered_model_id is None
        assert "oldconn/aaa-first" not in {m.id for m in view.models}
        assert not root.paths.model_selection_file.exists()
        assert all(p.provider_id != "oldconn" for p in view.providers)


class TestRootInitialCacheChoice:
    """Task 3.5 correction: the automatic choice is the minimum of the INITIAL cache, not of the refresh."""

    def test_cached_minimum_is_active_without_any_startup_discovery(
        self, root_harness: RootHarness,
    ) -> None:
        cached = "acme/aaa-cached"
        root_harness.start(saved_default=None, removed_connection="oldconn", extra_cached=(cached,))
        root = root_harness
        view = root.registry.view()
        assert view.active_model_id == cached and view.remembered_model_id is None
        assert view.selection_status == "active"
        assert cached in {m.id for m in view.models} and "oldconn/aaa-first" not in {m.id for m in view.models}
        assert not root.paths.model_selection_file.exists()
        assert root.refresh_reports == [] and not [r for r in root.requests if r.url.path.endswith("/models")]

    def test_valid_empty_refresh_after_first_start_keeps_the_choice_and_preference(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = root_harness._route
        outcome: dict[str, Any] = {}

        def route(request: httpx.Request) -> httpx.Response:
            if outcome.get("empty") and request.method == "GET" and request.url.path == "/v1/models" \
                    and request.url.host == ACME_HOST:
                request.read()
                root_harness.requests.append(request)
                return httpx.Response(200, json={"data": [], "has_more": False})
            if outcome.get("empty") and request.method == "GET" and request.url.path == "/v1beta/models" \
                    and request.url.host == GEM_HOST:
                request.read()
                root_harness.requests.append(request)
                return httpx.Response(200, json={"models": []})
            return original(request)

        monkeypatch.setattr(root_harness, "_route", route)

        def hold(root: RootHarness) -> None:
            """The deliberate /models-style refresh after a first-start discovery returns nothing."""
            root.registry.select(OTHER_ID)
            outcome["before"] = root.registry.view()
            outcome["pref"] = root.paths.model_selection_file.read_bytes()
            outcome["empty"] = True
            root.registry.refresh()

        root_harness.start(saved_default=None, write_cache=False, hold=hold)
        root = root_harness
        view, before = root.registry.view(), outcome["before"]
        assert view.models == ()  # the empty inventory is really published
        assert view.active_model_id == before.active_model_id == OTHER_ID
        assert view.remembered_model_id == OTHER_ID and view.selection_revision == before.selection_revision
        assert root.paths.model_selection_file.read_bytes() == outcome["pref"]

    def test_empty_initial_cache_uses_the_refreshed_candidates(self, root_harness: RootHarness) -> None:
        root_harness.start(saved_default=None, write_cache=False)
        root = root_harness
        assert root.registry.view().active_model_id == min(DISCOVERED)
        assert not root.paths.model_selection_file.exists()


# ---------------------------------------------------------------------------
# Task 10.3: the retired combined models-config.toml is only existence-checked
# ---------------------------------------------------------------------------

_RETIRED_CANARY = "sk-RETIRED0123456789canary"
_RETIRED_CONTENTS: dict[str, bytes] = {
    "valid-catalog": b"schema_version = 1\nrevision = 3\n# " + _RETIRED_CANARY.encode() + b"\n",
    "malformed": b"schema_version = [\n# " + _RETIRED_CANARY.encode() + b"\n",
    "secret-like": b'api_key = "' + _RETIRED_CANARY.encode() + b'"\n[[models]]\nname = "x"\n',
    "empty": b"",
    "invalid-utf8": b"\xff\xfe\x00" + _RETIRED_CANARY.encode(),
}
_CONTACT_TARGETS: tuple[tuple[Any, str], ...] = (
    (builtins, "open"), (io, "open"),
    (os, "open"), (os, "rename"), (os, "replace"), (os, "remove"), (os, "unlink"),
    (os, "rmdir"), (os, "truncate"), (os, "chmod"), (os, "utime"), (os, "link"), (os, "symlink"),
    (shutil, "copy"), (shutil, "copy2"), (shutil, "copyfile"), (shutil, "move"), (shutil, "rmtree"),
    (Path, "open"), (Path, "read_bytes"), (Path, "read_text"), (Path, "write_bytes"),
    (Path, "write_text"), (Path, "rename"), (Path, "replace"), (Path, "unlink"),
    (Path, "touch"), (Path, "chmod"), (Path, "rmdir"),
)


class _ContactTraps(list):
    """Recorded contacts (a list of labels); ``armed`` gates recording after startup."""

    armed: bool = True


def _install_contact_traps(monkeypatch: pytest.MonkeyPatch, *targets: Path) -> _ContactTraps:
    """Record (and refuse) any open/read/write/rename/delete of *targets* during startup.

    Contacts are recorded as well as raised because production may swallow
    exceptions. ``stat``/``lstat``/``exists`` are intentionally not trapped:
    they are the permitted existence probe. Call ``disarm()`` before the test
    inspects or restores the files itself.
    """
    names = {os.fspath(t) for t in targets} | {os.path.realpath(t) for t in targets}
    contacts = _ContactTraps()

    def matches(value: object) -> bool:
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value) in names
        return False

    for owner, attr in _CONTACT_TARGETS:
        real = getattr(owner, attr)

        def make(real: Callable[..., Any], label: str) -> Callable[..., Any]:
            def trapped(*args: Any, **kwargs: Any) -> Any:
                if contacts.armed and (any(matches(a) for a in args[:2]) or any(matches(v) for v in kwargs.values())):
                    contacts.append(label)
                    raise AssertionError(f"retired file contact: {label}")
                return real(*args, **kwargs)
            return trapped

        monkeypatch.setattr(owner, attr, make(real, f"{getattr(owner, '__name__', owner)}.{attr}"))
    return contacts


def _lstat_fingerprint(path: Path) -> tuple[int, ...]:
    """Return the metadata that any read-modify, rename or chmod would change."""
    st = os.lstat(path)
    return (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_uid)


class TestRetiredCombinedCatalogFile:
    """Real ``main._run`` startup: only an existence probe and one /models notice."""

    @staticmethod
    def _spy_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        messages: list[str] = []
        real = main_mod.logger.warning

        def spy(msg: object, *args: object, **kwargs: Any) -> None:
            messages.append(str(msg) % args if args else str(msg))
            real(msg, *args, **kwargs)

        monkeypatch.setattr(main_mod.logger, "warning", spy)
        return messages

    @staticmethod
    def _forbid_settings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        import model_settings

        constructed: list[str] = []

        def tripwire(self: object, path: object, *a: object, **k: object) -> None:
            constructed.append(os.fspath(path))  # type: ignore[arg-type]
            raise AssertionError("ModelSettings must not be constructed at runtime")

        monkeypatch.setattr(model_settings.ModelSettings, "__init__", tripwire)
        return constructed

    @staticmethod
    def _prepare(root: RootHarness) -> Path:
        retired = root.paths.models_config_file
        retired.parent.mkdir(parents=True, exist_ok=True)
        return retired

    def _assert_one_notice(self, messages: list[str], retired: Path) -> None:
        mine = [m for m in messages if str(retired) in m]
        assert len(mine) == 1, messages
        assert "/models" in mine[0] and "models-config.toml" in mine[0]
        assert all(_RETIRED_CANARY not in m for m in messages)

    @pytest.mark.parametrize("kind", sorted(_RETIRED_CONTENTS))
    def test_existing_file_gets_one_path_naming_notice_and_is_never_touched(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch, kind: str,
    ) -> None:
        retired = self._prepare(root_harness)
        retired.write_bytes(_RETIRED_CONTENTS[kind])
        before, fingerprint = retired.read_bytes(), _lstat_fingerprint(retired)
        siblings = sorted(p.name for p in retired.parent.iterdir())
        contacts = _install_contact_traps(monkeypatch, retired)
        constructed = self._forbid_settings(monkeypatch)
        messages = self._spy_warnings(monkeypatch)

        root_harness.start(saved_default=None, write_cache=False)

        assert contacts == [] and constructed == []
        self._assert_one_notice(messages, retired)
        contacts.armed = False
        assert retired.read_bytes() == before and _lstat_fingerprint(retired) == fingerprint
        assert [n for n in sorted(p.name for p in retired.parent.iterdir()) if "models-config" in n] == [
            "models-config.toml"
        ]
        assert set(siblings) <= {p.name for p in retired.parent.iterdir()}
        # The notice never derives or saves a selection.
        assert not root_harness.paths.model_selection_file.exists()
        assert root_harness.registry.view().active_model_id == min(DISCOVERED)

    def test_absent_file_emits_no_notice_and_is_not_created(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        retired = root_harness.paths.models_config_file
        contacts = _install_contact_traps(monkeypatch, retired)
        messages = self._spy_warnings(monkeypatch)

        root_harness.start(saved_default=None, write_cache=False)

        assert contacts == []
        assert not any("models-config" in m for m in messages)
        assert not retired.exists()

    def test_unreadable_mode_file_is_noticed_without_any_open(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        retired = self._prepare(root_harness)
        retired.write_bytes(_RETIRED_CONTENTS["secret-like"])
        retired.chmod(0)
        fingerprint = _lstat_fingerprint(retired)
        contacts = _install_contact_traps(monkeypatch, retired)
        try:
            messages = self._spy_warnings(monkeypatch)
            root_harness.start(saved_default=None, write_cache=False)
            assert contacts == []
            contacts.armed = False
            self._assert_one_notice(messages, retired)
            assert _lstat_fingerprint(retired) == fingerprint
        finally:
            contacts.armed = False
            retired.chmod(0o600)

    def test_directory_at_the_retired_path_is_noticed_and_left_alone(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        retired = self._prepare(root_harness)
        retired.mkdir()
        contacts = _install_contact_traps(monkeypatch, retired)
        messages = self._spy_warnings(monkeypatch)
        root_harness.start(saved_default=None, write_cache=False)
        contacts.armed = False
        assert contacts == [] and retired.is_dir() and list(retired.iterdir()) == []
        self._assert_one_notice(messages, retired)

    def test_symlink_is_noticed_and_its_target_never_read(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        retired = self._prepare(root_harness)
        target = tmp_path / "elsewhere.toml"
        target.write_bytes(_RETIRED_CONTENTS["secret-like"])
        retired.symlink_to(target)
        before, fingerprint = target.read_bytes(), _lstat_fingerprint(target)
        contacts = _install_contact_traps(monkeypatch, retired, target)
        messages = self._spy_warnings(monkeypatch)
        root_harness.start(saved_default=None, write_cache=False)
        assert contacts == []
        contacts.armed = False
        self._assert_one_notice(messages, retired)
        assert os.path.islink(retired) and os.readlink(retired) == str(target)
        assert target.read_bytes() == before and _lstat_fingerprint(target) == fingerprint

    def test_notice_is_not_repeated_by_later_catalog_work(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        retired = self._prepare(root_harness)
        retired.write_bytes(_RETIRED_CONTENTS["valid-catalog"])
        messages = self._spy_warnings(monkeypatch)

        def hold(root: RootHarness) -> None:
            root.registry.refresh()
            root.registry.select(OTHER_ID)

        root_harness.start(saved_default=None, write_cache=False, hold=hold)
        self._assert_one_notice(messages, retired)
        assert root_harness.registry.view().active_model_id == OTHER_ID

    def test_only_the_startup_existence_probe_references_the_retired_path_in_production(self) -> None:
        """Production names ``models_config_file`` only in ``xdg`` (path) and ``main`` (probe)."""
        repo = Path(main_mod.__file__).resolve().parent
        hits: dict[str, int] = {}
        for source in sorted(repo.glob("*.py")) + sorted(repo.glob("*/*.py")):
            if "tests" in source.parts or ".venv" in source.parts:
                continue
            count = source.read_text(encoding="utf-8").count("models_config_file")
            if count:
                hits[source.relative_to(repo).as_posix()] = count
        assert set(hits) == {"xdg.py", "main.py"}, hits
        probe = Path(main_mod.__file__).read_text(encoding="utf-8")
        assert probe.count("models_config_file") == 1
