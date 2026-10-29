"""Tests for main.py — CLI arg parsing, XDG dir creation, and startup flow helpers.

``TestRootStartupComposition`` drives the actual ``main._run`` through
``tests/root_startup_harness.py`` (see its docstring for the real versus
replaced boundaries). Graph memory stays disabled; graph pin invocation and
caller cutover belong to task 12.25, scheduler closure to 7.3.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import io
import json
import logging
import os
import re
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
from model_settings import ModelSettings
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
    GRAPH_ENABLED_DISCOVERED,
    GRAPH_HOST,
    GRAPH_ID,
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
        """Startup fully refreshes every provider, then restores the valid Main."""
        root_harness.start(saved_default=MAIN_ID)
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
        saved = ModelSettings(root.paths.models_config_file).load()
        assert saved.status == "valid" and saved.document.default_model == MAIN_ID
        assert tuple(m.id for m in saved.document.models) == DISCOVERED

        # No standing inference client; graph memory stays disabled (12.25 scope).
        assert root.llm_clients_at_hold == 0 and root.llm_clients == []
        assert root.main_controller.llm is None
        assert root.main_controller._graph_memory is None

    @pytest.mark.parametrize("saved_default", [None, GONE_ID], ids=["absent", "removed"])
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
                (RuntimeProfile.PLAN_STEP_AGENT, OTHER_ID),
                (RuntimeProfile.SCHEDULED_AGENT, OTHER_ID),
            ):
                with pytest.raises(ModelRegistryError) as info:
                    root.builtin._sub_agent_factory(model=selector, runtime_profile=profile)
                codes.append(info.value.code)
            outcome["codes"] = codes
            outcome["creates"] = creates

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
        assert root.llm_clients == [] and root.main_controller.llm is None
        view = root.registry.view()
        assert view.active_model_id is None
        assert view.remembered_model_id == saved_default
        assert view.selection_status == (
            "no_selection" if saved_default is None else "saved_selection_unavailable"
        )
        saved = ModelSettings(root.paths.models_config_file).load().document
        assert saved.default_model == saved_default
        assert tuple(m.id for m in saved.models) == DISCOVERED
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

        root_harness.start(saved_default=MAIN_ID, hold=hold)
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

    def test_root_owned_clients_close_on_normal_exit(self, root_harness: RootHarness) -> None:
        """Root-owned discovery and embedding clients are closed when ``_run`` returns."""
        root_harness.start(saved_default=MAIN_ID)
        root = root_harness
        assert root.discoveries, "root never constructed ModelDiscovery"
        for discovery in root.discoveries:
            assert discovery._client.is_closed and discovery._public_client.is_closed
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
            assert discovery._client.is_closed and discovery._public_client.is_closed
        assert root.root_embedding._http.is_closed
        assert root.open_clients_at_exit == []
        assert "scheduler:stop" in root.events

    @pytest.mark.parametrize("fault", ["second_discovery_client", "interrupt_during_refresh"])
    def test_early_model_setup_failure_releases_each_allocated_model_resource_once(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch, fault: str,
    ) -> None:
        """A fault after one discovery client or inside registry setup closes every
        allocated root model resource exactly once, before any fixture hygiene."""
        releases = _track_client_releases(monkeypatch)
        if fault == "second_discovery_client":
            spy_init = httpx.Client.__init__
            discovery_allocations: list[httpx.Client] = []

            def failing_init(client: httpx.Client, *args: Any, **kwargs: Any) -> None:
                """Fail the second discovery-shaped client allocation (external boundary)."""
                if kwargs == {"follow_redirects": False, "trust_env": False}:
                    discovery_allocations.append(client)
                    if len(discovery_allocations) == 2:
                        raise _StartupFault("second discovery client allocation failed")
                spy_init(client, *args, **kwargs)

            monkeypatch.setattr(httpx.Client, "__init__", failing_init)
        else:
            original_route = root_harness._route

            def interrupting_route(request: httpx.Request) -> httpx.Response:
                """Interrupt startup at the first provider discovery request."""
                if request.url.host in (ACME_HOST, GEM_HOST):
                    raise _StartupFault("interrupted during startup refresh")
                return original_route(request)

            monkeypatch.setattr(root_harness, "_route", interrupting_route)

        with pytest.raises(_StartupFault):
            root_harness.start(saved_default=MAIN_ID)
        root = root_harness

        # Ownership is judged now, before the fixture's release_leftovers hygiene.
        allocated = list(root.http_clients)
        (embedding,) = root.embedding_clients
        assert embedding._http in allocated
        if fault == "second_discovery_client":
            assert len(allocated) == 2  # root embedding + the first discovery client
            assert root.registries == [] and root.discoveries == []
        else:
            assert len(allocated) == 3
            (discovery,) = root.discoveries
            assert discovery._client in allocated and discovery._public_client in allocated
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

        root_harness.start(saved_default=None, hold=hold)
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
        saved = ModelSettings(root.paths.models_config_file).load().document
        assert saved.default_model == MAIN_ID

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
        """Valid graph selector builds store/writer independently of active Main."""
        root_harness.start(saved_default=None, graph_enabled=True)
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
        assert pin.connection.provider_id == "graph"
        assert pin.connection.adapter == "openai"
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
        assert len(probes) == 1
        assert probes[0]._http.is_closed
        assert root.unexpected == []

    @pytest.mark.parametrize("saved_default", [None, GONE_ID], ids=["no_selection", "main_gone"])
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
        assert len(probes) == 1
        assert probes[0]._http.is_closed
        assert "telegram:run" in root.events
        assert root.unexpected == []

    def test_graph_disabled_when_selector_unresolvable(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An invalid graph selector disables graph alone; Telegram/indexing survive."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_bad_selector(workspace: Path, *, graph_enabled: bool = False) -> str:
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            if graph_enabled:
                text = text.replace(
                    f'extraction_model = "{GRAPH_ID}"', 'extraction_model = "graph/missing"'
                )
            return text

        monkeypatch.setattr(rsh, "_config_text", config_with_bad_selector)

        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        assert "graph:writer:init" not in root.events
        self._assert_prerequisite_rejection(root, caplog, "extraction model is unusable")
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

        root_harness.start(saved_default=None, graph_enabled=True)
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

    def test_graph_disabled_when_extraction_budget_too_small(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A local context below the fixed extraction cap disables graph alone."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_small_budget(workspace: Path, *, graph_enabled: bool = False) -> str:
            """Set the generated agent budget without bypassing real startup."""
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            return text.replace("[agent]\n", "[agent]\nctx_max_tokens = 512\n")

        monkeypatch.setattr(rsh, "_config_text", config_with_small_budget)

        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        assert root.app_cfg.agent.ctx_max_tokens == 512
        self._assert_prerequisite_rejection(root, caplog, "cannot fit a request")
        assert root.registry.view().active_model_id is None
        assert root.graph_store_instances == []
        assert root.graph_writer_instances == []
        assert root.main_controller._graph_memory is None
        assert root.main_controller._graph_memory_writer is None
        assert "graph:store:init" not in root.events
        assert "telegram:run" in root.events
        assert len(root.tool_indexes) == 1
        assert root.unexpected == []

    def test_graph_disabled_when_extraction_credentials_unresolvable(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing graph provider credential disables graph alone after config parse."""
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_missing_graph_key(workspace: Path, *, graph_enabled: bool = False) -> str:
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            if graph_enabled:
                text = text.replace('api_key = "env:GRAPH_KEY"', 'api_key = "env:MISSING_GRAPH_KEY"')
            return text

        monkeypatch.setattr(rsh, "_config_text", config_with_missing_graph_key)
        monkeypatch.delenv("MISSING_GRAPH_KEY", raising=False)

        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        self._assert_prerequisite_rejection(root, caplog, "extraction model is unusable")
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
        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        assert 'provider = "unsupported-embedding-provider"' in root.paths.config_file.read_text()
        assert root.app_cfg.embeddings.unavailable_reason == (
            "embeddings.provider must name a supported adapter or configured provider connection"
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
        assert discovery._client.is_closed and discovery._public_client.is_closed
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
            root.start(saved_default=None, graph_enabled=True, hold=hold)
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
                root.start(saved_default=None, graph_enabled=True, hold=hold)

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
                root.start(saved_default=None, graph_enabled=True, hold=hold)

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
                root.start(saved_default=None, graph_enabled=True, hold=hold)

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
    """Task 12.20: the diagnostic profile is gated by an active Main like the other three.

    Separate from ``test_missing_main_keeps_commands_available_without_default_or_model_work``
    (on-demand/plan-step/scheduled), which stays unchanged.
    """

    @pytest.mark.parametrize("saved_default", [None, GONE_ID], ids=["absent", "removed"])
    def test_diagnostic_profile_refuses_valid_explicit_selector_without_active_main(
        self, root_harness: RootHarness, monkeypatch: pytest.MonkeyPatch,
        saved_default: str | None,
    ) -> None:
        """A valid explicit OTHER_ID is refused with no_active_selection before any
        credential, runtime, client, request or usage work; Main/settings unchanged."""
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
            outcome["settings_before"] = root.paths.models_config_file.read_bytes()
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
            with pytest.raises(ModelRegistryError) as info:
                factory(model=OTHER_ID, runtime_profile=RuntimeProfile.DIAGNOSTIC_AGENT)
            outcome["code"] = info.value.code
            outcome["resolved"], outcome["creates"] = resolved, creates
            requests, llm_clients, http_clients = marks
            outcome["new_requests"] = root.requests[requests:]
            outcome["new_llm_clients"] = root.llm_clients[llm_clients:]
            outcome["new_http_clients"] = root.http_clients[http_clients:]
            outcome["view_after"] = root.registry.view()
            outcome["settings_after"] = root.paths.models_config_file.read_bytes()

        root_harness.start(saved_default=saved_default, hold=hold)
        root = root_harness

        assert outcome["binding"] == (True, True, True)
        before = outcome["view_before"]
        assert before.active_model_id is None and before.remembered_model_id == saved_default
        assert OTHER_ID in {m.id for m in before.models}
        assert outcome["code"] == "no_active_selection"
        assert outcome["admissions"] == {"admit": [OTHER_ID], "admit_scheduled": []}
        assert outcome["resolved"] == [] and outcome["creates"] == []
        assert outcome["new_llm_clients"] == [] and outcome["new_http_clients"] == []
        assert outcome["new_requests"] == []
        assert usage.get_today() == {}
        assert outcome["view_after"] == before
        assert outcome["settings_after"] == outcome["settings_before"]
        assert root.registry.view().active_model_id is None
        assert root.unexpected == [] and root.open_clients_at_exit == []


class TestRootGraphStartupContainment:
    """Task 12.24: graph startup after a partly failed refresh, and independent
    containment of scheduler and graph failures with real bot commands."""

    def test_graph_resolves_stale_last_good_entry_after_failed_provider_refresh(
        self, root_harness: RootHarness,
    ) -> None:
        """The graph provider's discovery fails; after the complete refresh its saved
        last-good entry stays stale yet resolves to the graph pin, with no Main."""
        root_harness.serve_provider_chat(failing_discovery_hosts=(GRAPH_HOST,))
        root_harness.start(saved_default=None, graph_enabled=True)
        root = root_harness

        # The complete refresh ran once: acme/gem refreshed, graph failed and kept last-good.
        (report,) = root.refresh_reports
        assert sorted(report.refreshed_provider_ids) == ["acme", "gem"]
        assert report.stale_provider_ids == ("graph",)
        assert [provider for provider, _code in report.error_codes] == ["graph"]
        graph_discovery = root.requests_to(GRAPH_HOST)
        assert graph_discovery and all(
            r.method == "GET" and r.url.path == "/v1/models" for r in graph_discovery
        )
        view = root.registry.view()
        assert {p.provider_id: p.status for p in view.providers} == {
            "acme": "fresh", "gem": "fresh", "graph": "stale",
        }
        assert GRAPH_ID in {m.id for m in view.models}

        # Graph resolution, helper and store only after the whole refresh.
        refresh_end = root.events.index("refresh:end")
        assert root.events.index("graph:build_extraction_call") > refresh_end
        assert root.events.index("graph:store:init") > refresh_end
        ((args, kwargs),) = root.graph_extraction_calls
        pin, agent_config = args
        assert isinstance(pin, RunModel) and pin.model.id == GRAPH_ID
        assert pin.model.upstream_id == "graph-model"
        assert pin.connection.provider_id == "graph" and pin.connection.adapter == "openai"
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

        # No Main: none invented, no default persisted, no Main chat client, no inference.
        assert view.active_model_id is None and view.remembered_model_id is None
        saved = ModelSettings(root.paths.models_config_file).load().document
        assert saved.default_model is None and GRAPH_ID in {m.id for m in saved.models}
        assert root.llm_clients == [] and root.main_controller.llm is None
        assert [r for r in root.requests if r.method == "POST" and r.url.host != EMBED_HOST] == []
        assert root.unexpected == [] and root.open_clients_at_exit == []

    @pytest.mark.parametrize("scheduler_state", ["unparseable", "unreadable"])
    def test_invalid_scheduler_and_graph_selector_fail_independently_while_commands_work(
        self, tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        scheduler_state: str,
    ) -> None:
        """An unparseable/unreadable scheduler file and an unknown graph selector each fail
        alone: the real Scheduler is contained, graph is disabled, and the real bot
        ``/models`` and ``/jobs`` commands answer from the same root."""
        import telegram_interface
        from telegram_commands import cmd_jobs, cmd_models
        from tests import root_startup_harness as rsh

        original_config_text = rsh._config_text

        def config_with_bad_selector(workspace: Path, *, graph_enabled: bool = False) -> str:
            """Real config parsing with an unknown graph selector."""
            text = original_config_text(workspace, graph_enabled=graph_enabled)
            return text.replace(f'extraction_model = "{GRAPH_ID}"', 'extraction_model = "graph/missing"')

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

            root.start(saved_default=None, graph_enabled=True, hold=hold)

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
                root, caplog, "extraction model is unusable",
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
            saved = ModelSettings(root.paths.models_config_file).load().document
            assert saved.default_model is None
            assert outcome["clients_at_hold"] == 0 and root.llm_clients == []
            assert root.main_controller.llm is None
            assert [r for r in root.requests if r.method == "POST" and r.url.host != EMBED_HOST] == []

            # Root-owned resources were released by production before fixture hygiene.
            assert root.discoveries
            for discovery in root.discoveries:
                assert discovery._client.is_closed and discovery._public_client.is_closed
            assert root.root_embedding._http.is_closed
            assert root.open_clients_at_exit == []
            assert root.unexpected == []
        finally:
            root.release_leftovers()


# Exact corrupt inputs of tests/test_model_settings.py component cases, with a fake leak marker.
_SETTINGS_LEAK = "sk-settingsleak12345"
_STARTUP_SETTINGS: dict[str, bytes | None] = {
    "absent": None,
    "corrupt-toml": f"schema_version = 1\nrevision = [\n# {_SETTINGS_LEAK}\n".encode(),
    "invalid-schema": f'schema_version = 2\nrevision = 1\nnote = "{_SETTINGS_LEAK}"\n'.encode(),
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
        persists a fresh catalog without a Main; commands answer while ordinary model
        work is refused; graph resolves independently in the same run."""
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

        monkeypatch.setattr(rsh, "ModelSettings", _SetupWriter)
        root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
        settings_path = root.paths.models_config_file

        def backups() -> dict[str, bytes]:
            """Every ``.corrupt*`` sibling of the managed file and its bytes."""
            return {
                p.name: p.read_bytes()
                for p in sorted(settings_path.parent.glob(f"{settings_path.name}.corrupt*"))
            }

        # Delegating observers on the real ModelSettings read and the real FS replace.
        loads: list[SimpleNamespace] = []
        real_load = ModelSettings.load

        def load_spy(settings: ModelSettings) -> Any:
            """Record the input state around the real load, then return its result."""
            seen = SimpleNamespace(
                path=settings.path, events=list(root.events),
                before=settings.path.read_bytes() if settings.path.exists() else None,
            )
            result = real_load(settings)
            seen.status, seen.preserved = result.status, result.preserved_path
            seen.preserved_bytes = result.preserved_path.read_bytes() if result.preserved_path else None
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

        monkeypatch.setattr(ModelSettings, "load", load_spy)
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
            with pytest.raises(ModelRegistryError) as info:
                started.builtin._sub_agent_factory(model=OTHER_ID)
            outcome["factory_code"] = info.value.code
            requests, llm_clients, http_clients = marks
            outcome["new_requests"] = started.requests[requests:]
            outcome["new_llm"] = started.llm_clients[llm_clients:]
            outcome["new_http"] = started.http_clients[http_clients:]
            outcome["view"] = started.registry.view()

        try:
            root.start(saved_default=None, graph_enabled=graph_enabled, hold=hold)
            name = settings_path.name

            # Startup input: the real registry load saw the genuine absent/corrupt file first.
            first = loads[0]
            assert first.path == settings_path and "refresh:start" not in first.events
            if raw is None:
                assert (first.before, first.status, first.preserved) == (None, "missing", None)
            else:
                assert first.before == raw and first.status == "corrupt"
                assert first.preserved == settings_path.with_name(f"{name}.corrupt")
                assert first.preserved_bytes == raw and first.after == raw

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
                assert write.target == raw
                assert write.backups == {f"{name}.corrupt": raw}
            # The command refresh overwrites only the startup payload; backups untouched.
            assert "telegram:run" in command_write.events
            assert command_write.events.count("refresh:start") == 2
            assert command_write.target == write.incoming
            assert command_write.backups == write.backups

            # The first (startup) payload, parsed by the real ModelSettings off the
            # production path, is a valid fresh catalog with no Main/default.
            expected = GRAPH_ENABLED_DISCOVERED if graph_enabled else DISCOVERED
            startup_copy = tmp_path / "phase-check" / "startup-payload.toml"
            startup_copy.parent.mkdir()
            startup_copy.write_bytes(write.incoming)
            startup = ModelSettings(startup_copy, read_only=True).load()
            assert startup.status == "valid" and startup.document.default_model is None
            assert tuple(m.id for m in startup.document.models) == expected
            assert {p.status for p in startup.document.providers} == {"fresh"}

            # Final managed document = the LAST production payload: valid, fresh,
            # one revision after startup, still no Main/default.
            assert settings_path.read_bytes() == command_write.incoming
            final = ModelSettings(settings_path).load()
            assert final.status == "valid" and final.document.default_model is None
            assert final.document.revision == startup.document.revision + 1
            assert tuple(m.id for m in final.document.models) == expected
            assert {p.status for p in final.document.providers} == {"fresh"}
            assert backups() == ({} if raw is None else {f"{name}.corrupt": raw})

            # Main stays inactive: nothing remembered, nothing active, no Main client.
            for view in (outcome["view_after_models"], outcome["view"], root.registry.view()):
                assert view.active_model_id is None and view.remembered_model_id is None
                assert view.selection_status == "no_selection"
            assert root.llm_clients == [] and root.main_controller.llm is None

            # Actual commands answer from the same root registry.
            iface = outcome["iface"]
            assert iface.model_registry is root.registry
            chat: _TelegramMessage = outcome["chat"]
            (listing,) = [m for m in chat.replies if "Model Catalog" in m.text]
            assert MAIN_ID in listing.text and OTHER_ID in listing.text
            # Ordinary work is refused at the Main gate before credential/client/chat I/O.
            assert [m.text for m in chat.replies].count("🔄 Processing…") == 1
            prompts = {r.text: r.status for r in root.telegram._prompt_registry.list_recent(10)}
            assert prompts == {"ordinary request": "failed"}
            assert boundary.calls == []
            assert outcome["admissions"] == {"admit": [None, OTHER_ID], "admit_scheduled": []}
            assert outcome["factory_code"] == "no_active_selection"
            assert outcome["resolved"] == []
            # Marks were taken after /models: the ordinary phase made no request at all.
            assert outcome["new_llm"] == [] and outcome["new_http"] == []
            assert outcome["new_requests"] == []

            # Actionable no-Main warning; no input bytes leak anywhere observable.
            messages = [r.getMessage() for r in caplog.records]
            assert any(
                m.startswith("No main model is active (no_selection); select one with /models") for m in messages
            ), messages
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
                assert pin.model.upstream_id == "graph-model" and pin.connection.provider_id == "graph"
                assert pin.connection.api_key == SECRETS["GRAPH_KEY"]
                assert root.main_controller._graph_memory is root.graph_store is not None
                assert root.main_controller._graph_memory_writer is root.graph_writer is not None
                assert root.builtin._graph_memory is root.graph_store
            else:
                assert root.graph_store_instances == [] and root.graph_extraction_calls == []

            # Root-owned resources were released by production before hygiene.
            assert root.discoveries
            for discovery in root.discoveries:
                assert discovery._client.is_closed and discovery._public_client.is_closed
            assert root.root_embedding._http.is_closed
            assert root.open_clients_at_exit == []
            assert root.unexpected == []
        finally:
            root.release_leftovers()
