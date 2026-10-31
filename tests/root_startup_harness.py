"""Shared real-root startup harness for ``main._run`` composition tests.

Shared by ``tests/test_main.py`` (startup refresh, Main restore/gating,
controller admission, catalog presentation, embedding ownership, shutdown) and
``tests/test_agent_runtime_characterization.py`` (root sub-agent factory/profile
wiring). Both drive the same actual startup path, so the boundary setup lives
here once instead of being duplicated.

REAL (never replaced): ``main.load_config`` parsing, XDG paths, the managed
``models-config.toml`` file, ``ModelSettings``, ``ModelRegistry`` (refresh,
restore, admission), ``ModelDiscovery`` pagination, provider credential
resolution from environment references, ``EmbeddingClient``, ``ToolIndex``,
memories, ``BuiltinExecutor``, ``AgentController``, ``AgentRuntime``,
``main._make_sub_agent_factory``, ``SubAgentRunner`` and ``LLMClient``.

REPLACED external lifecycle boundaries only:

- outbound HTTP: ``httpx.HTTPTransport.handle_request`` is routed to an
  in-process router (``httpx.MockTransport`` semantics), so every default
  transport is served locally and unknown requests are recorded, never sent;
- logging backend: ``agent_logging.setup_logging`` / ``shutdown_log_store``;
- real-home dot bootstrap: ``main._bootstrap_dot_home`` returns temp dirs;
- scheduler threads: ``main.Scheduler`` is a recording fake (no thread) by
  default. ``RootHarness(..., real_scheduler=True)`` (task 7.3) keeps the REAL
  ``Scheduler``: its config gets an explicit ``[scheduler] enabled = false``
  (the scheduler defaults to enabled, which would start the cron thread), the
  external ``schedule`` library's global job list is isolated, and the actual
  instance is captured by a delegating ``__init__`` spy into ``scheduler``;
- Telegram polling: ``main.TelegramInterface`` is a fake whose ``run()``
  executes the test's ``hold`` callback while startup is complete, then returns;
- tests that drive a request additionally replace ``agent_controller.react_loop``;
  ``ScheduledRootProbe.fire`` also runs the supervisor's external worker pool
  inline (the real worker, callbacks, record and release still execute once).

OBSERVED through delegating spies that always call the original and never
replace a decision: ``ModelRegistry.__init__/refresh/restore_selection``,
``ModelDiscovery.__init__/fetch``, ``AgentRuntime.__init__``,
``LLMClient.__init__``, ``EmbeddingClient.__init__``, ``ToolIndex.__init__``,
``httpx.Client.__init__`` and ``main._make_sub_agent_factory``.

Opt-in graph shutdown support (task 12.26), default behavior unchanged:

- ``real_graph_writer=True`` keeps the REAL ``GraphMemoryWriter`` (its worker
  thread, the real extraction helper and per-call clients). Only the database
  is a boundary: a recording store delegates embeddings to the production
  ``embedder_fn`` (the root embedding client) and records writes. Delegating
  spies record ``stop`` (``graph_stop_timeout`` substitutes only the join
  timeout), extraction-client and embedding-client closes; the process-wide
  ``TokenUsageRegistry`` is replaced by a fresh real one (restored by
  monkeypatch);
- ``real_logging=True`` keeps the REAL ``setup_logging``/``shutdown_log_store``
  on the temp XDG paths behind delegating spies. Prior handlers, the prior
  active SQLite writer and structlog config/context are snapshotted and
  detached (never closed or stopped) before setup; ``restore_logging`` retires
  only the test-owned sinks and reinstates that prior state exactly.
  ``finish_graph_hygiene`` runs worker release, client and logging hygiene in
  dependency order and refuses to close dependencies of a surviving worker.

No live network, sleeps, mounts, real-home writes or daemon threads (except the
opt-in real graph worker and SQLite writer, ended by the hygiene helpers).
``release_leftovers`` closes clients production failed to close; it runs only
after a test's assertions and is hygiene, never ownership proof (ownership is
asserted from ``open_clients_at_exit``, captured the moment ``_run`` exits).
``release_graph_workers``/``restore_logging`` are hygiene in the same sense.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest
import schedule
import structlog

import agent_controller
import agent_logging
import graph_memory
import main as main_mod
import sqlite_log
import token_usage
from agent_controller import AgentController, SubAgentRunner
from agent_runtime import AgentRuntime
from builtin_executor import BuiltinExecutor
from config_schema import ResolvedProviderConnection
from embedding_client import EmbeddingClient
from llm_client import LLMClient
from model_discovery import ModelDiscovery, ProviderInventory
from model_registry import (
    CatalogDocument,
    CatalogView,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RefreshReport,
    RunModel,
)
from model_settings import ModelSettings
from scheduler import Scheduler
from sub_agent_registry import SubAgentRegistry
from token_usage import TokenUsageRegistry
from tool_index import ToolIndex
from xdg import xdg_paths

AGENT_NAME = "rootagent"
ACME_HOST = "anthropic.root.test"
GEM_HOST = "google.root.test"
EMBED_HOST = "embed.root.test"
GRAPH_HOST = "graph.root.test"
SECRETS = {
    "KEY_ACME": "sk-acme-root-secret-000111",
    "KEY_GEM": "gem-root-secret-222333",
    "EMBED_KEY": "embed-root-secret-444555",
    "GRAPH_KEY": "graph-root-secret-666777",
}
MAIN_ID = "acme/claude-a"
OTHER_ID = "gem/gem-1"
GONE_ID = "acme/claude-gone"
GRAPH_ID = "graph/graph-model"
GRAPH_UPSTREAM = "graph-model"
DISCOVERED = ("acme/claude-a", "acme/claude-b", "gem/gem-1", "gem/gem-2")
GRAPH_ENABLED_DISCOVERED = DISCOVERED + (GRAPH_ID,)
# Loggers whose handlers/propagation/level the real logging setup mutates.
_REAL_LOGGING_NAMED_LOGGERS = ("graph_memory", "sqlite_log_writer", "httpx", "telegram", "telegram.ext")
# The real store's embedder resolution, captured before any boundary patch.
_RESOLVE_STORE_EMBEDDER = graph_memory.GraphMemoryStore._resolve_embedder


def restore_structlog_state(config: dict[str, Any], configured: bool, context: dict[str, Any]) -> None:
    """Reinstate an exact structlog configuration and context-local bindings.

    ``config``/``configured``/``context`` come from ``structlog.get_config()``,
    ``structlog.is_configured()`` and ``structlog.contextvars.get_contextvars()``.
    """
    structlog.configure(**config)
    if not configured:
        # Test boundary: ``configure`` always marks structlog as configured.
        structlog._config._CONFIG.is_configured = False
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**context)


def _config_text(workspace: Path, *, graph_enabled: bool = False) -> str:
    """Return a providers/embeddings-only config (no legacy ``[[models]]``)."""
    graph_provider_section = (
        f"""
[providers.graph]
type = "openai"
api_key = "env:GRAPH_KEY"
base_url = "https://{GRAPH_HOST}"
request_timeout = 7
"""
        if graph_enabled
        else ""
    )
    graph_memory_section = (
        f"""
[graph_memory]
enabled = true
extraction_model = "{GRAPH_ID}"
"""
        if graph_enabled
        else ""
    )
    return f"""
[telegram]
bot_token = "123456:root-harness-token"
allowed_user_ids = [1]

[agent]
max_retries = 0
retry_delay = 0

[paths]
workspace_dir = "{workspace}"

[providers.acme]
type = "anthropic"
api_key = "env:KEY_ACME"
base_url = "https://{ACME_HOST}"
request_timeout = 7

[providers.gem]
type = "google"
api_key = "env:KEY_GEM"
base_url = "https://{GEM_HOST}"
request_timeout = 7
{graph_provider_section}
[embeddings]
provider = "openai"
model = "embed-small"
api_key = "env:EMBED_KEY"
base_url = "https://{EMBED_HOST}/v1"
{graph_memory_section}"""


def _saved_document(default_model: str | None, *, graph_enabled: bool = False) -> CatalogDocument:
    """Return a stale saved catalog lacking each provider's second page."""
    ids = {MAIN_ID, OTHER_ID} | ({GONE_ID} if default_model == GONE_ID else set())
    providers = [
        ProviderState("acme", "anthropic", "2026-09-01T00:00:00Z", "stale"),
        ProviderState("gem", "google", "2026-09-01T00:00:00Z", "stale"),
    ]
    if graph_enabled:
        ids.add(GRAPH_ID)
        providers.append(ProviderState("graph", "openai", "2026-09-01T00:00:00Z", "stale"))
    models = tuple(
        ModelRecord(model_id, *model_id.split("/", 1), model_id.split("/", 1)[1])
        for model_id in sorted(ids)
    )
    return CatalogDocument(1, 3, default_model, tuple(providers), models, ())


def _acme_row(upstream: str) -> dict[str, Any]:
    """Return one Anthropic ``/v1/models`` row for *upstream*."""
    return {"id": upstream, "display_name": f"Claude {upstream}",
            "max_input_tokens": 100_000, "max_tokens": 4096}


def _gem_row(upstream: str) -> dict[str, Any]:
    """Return one Google ``/v1beta/models`` row for *upstream*."""
    return {"name": f"models/{upstream}", "baseModelId": upstream,
            "inputTokenLimit": 32_000, "outputTokenLimit": 2048}


class RootHarness:
    """Install the replaced boundaries and spies, then drive ``main._run``."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        real_scheduler: bool = False,
        real_graph_writer: bool = False,
        real_logging: bool = False,
    ) -> None:
        """Bind temp roots and install environment, boundaries, and spies.

        ``real_scheduler`` keeps the actual ``Scheduler``; ``real_graph_writer``
        and ``real_logging`` keep the actual graph writer and logging backend
        (all opt-in, see module docstring).
        """
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.real_scheduler = real_scheduler
        self.real_graph_writer = real_graph_writer
        self.real_logging = real_logging
        self.graph_stop_timeout: float | None = None
        self.graph_stop_calls: list[tuple[float, float]] = []
        self.graph_stop_errors: list[BaseException] = []
        self.graph_alive_after_stop: list[bool] = []
        self.graph_barriers: list[threading.Event] = []
        self.usage_registry: TokenUsageRegistry | None = None
        self.log_writer: sqlite_log.SqliteLogWriter | None = None
        self._saved_logging: tuple[Any, ...] | None = None
        self.workspace = tmp_path / "workspace"
        self.tmp_dir = tmp_path / "agent-tmp"
        self.paths = xdg_paths(AGENT_NAME)
        self.hold: Callable[[RootHarness], None] | None = None
        self.events: list[str] = []
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.notices: list[str] = []
        self.refresh_reports: list[RefreshReport] = []
        self.registries: list[ModelRegistry] = []
        self.discoveries: list[ModelDiscovery] = []
        self.runtimes: list[tuple[AgentRuntime, dict[str, Any]]] = []
        self.llm_clients: list[LLMClient] = []
        self.embedding_clients: list[EmbeddingClient] = []
        self.embedding_scalar_calls: list[tuple[EmbeddingClient, str]] = []
        self.tool_indexes: list[ToolIndex] = []
        self.http_clients: list[httpx.Client] = []
        self.factory_calls: list[tuple[inspect.BoundArguments, Callable[..., SubAgentRunner]]] = []
        self.telegram: Any = None
        self.scheduler: Any = None
        self.llm_clients_at_hold: int | None = None
        self.embedding_clients_at_hold: list[EmbeddingClient] = []
        self.open_clients_at_exit: list[httpx.Client] | None = None
        self.graph_store_instances: list[Any] = []
        self.graph_writer_instances: list[Any] = []
        self.graph_extraction_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.app_cfg: Any = None
        self._install_environment()
        self._install_boundaries()
        self._install_spies()

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _install_environment(self) -> None:
        """Set synthetic credentials and pin process-wide temp settings."""
        mp = self.monkeypatch
        for name, value in SECRETS.items():
            mp.setenv(name, value)
        # _run rewrites these process-wide; setenv first so teardown restores them.
        for name in ("TMPDIR", "TMP", "TEMP"):
            mp.setenv(name, str(self.tmp_dir))
        system_tmp = self.tmp_path / "system-tmp"
        system_tmp.mkdir()
        mp.setattr(tempfile, "tempdir", str(system_tmp))

    def _install_boundaries(self) -> None:
        """Replace the external lifecycle boundaries listed in the module docstring."""
        mp = self.monkeypatch
        harness = self
        dot_home = self.tmp_path / "dot-home"

        def handle_request(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            """Serve every default-transport request from the in-process router."""
            return harness._route(request)

        def setup_logging(*_args: object, **_kwargs: object) -> str:
            """Skip the real log backend; return the store path ``_run`` expects."""
            return str(harness.paths.log_store)

        def bootstrap_dot_home(*_args: object) -> tuple[str, str]:
            """Create the dot-home exchange dirs under the test temp root."""
            skills, results = dot_home / "skills", dot_home / "results"
            skills.mkdir(parents=True, exist_ok=True)
            results.mkdir(parents=True, exist_ok=True)
            return str(skills), str(results)

        class FakeScheduler:
            """Thread-free scheduler boundary; records lifecycle calls only."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                """Record the production constructor arguments."""
                self.args, self.kwargs = args, kwargs
                harness.scheduler = self

            def start(self) -> None:
                """Record start without launching a scheduler thread."""
                harness.events.append("scheduler:start")

            def stop(self) -> None:
                """Record stop."""
                harness.events.append("scheduler:stop")

        class FakeTelegram:
            """Polling boundary: ``run()`` executes the hold with startup complete."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                """Record the production constructor arguments."""
                self.args, self.kwargs = args, kwargs
                harness.telegram = self
                harness.events.append("telegram:init")

            def run(self) -> None:
                """Snapshot startup products, then execute the test hold."""
                harness.events.append("telegram:run")
                harness.llm_clients_at_hold = len(harness.llm_clients)
                harness.embedding_clients_at_hold = list(harness.embedding_clients)
                if harness.hold is not None:
                    harness.hold(harness)

            def send_message_to_users(self, message: str) -> None:
                """Record an operator notice instead of sending it."""
                harness.notices.append(message)

            def send_html_to_users(self, message: str) -> None:
                """Record an HTML operator notice instead of sending it."""
                harness.notices.append(message)

            def send_subagent_confirmation_prompt(self, *args: object, **kwargs: object) -> None:
                """Fail loudly: no root test expects a sub-agent confirmation."""
                raise AssertionError(f"no confirmation expected: {args!r} {kwargs!r}")

        class FakeGraphStore:
            """Thread-free graph store boundary; records construction only."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                """Record the production constructor arguments."""
                self.args, self.kwargs = args, kwargs
                harness.graph_store_instances.append(self)
                harness.events.append("graph:store:init")

            def search(self, query: str, k: int = 10) -> dict:
                """Return an empty safe result."""
                del query, k
                return {}

            def format_for_prompt(self, query: str, max_entries: int = 10) -> str:
                """Return an empty safe context string."""
                del query, max_entries
                return ""

            def add_episode(self, *args: object, **kwargs: object) -> str:
                """Return a synthetic episode ID."""
                del args, kwargs
                return "ep:fake"

            def close(self) -> None:
                """Record a close event."""
                harness.events.append("graph:store:close")

        class FakeGraphWriter:
            """Thread-free graph writer boundary; records construction only."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                """Record the production constructor arguments."""
                self.args, self.kwargs = args, kwargs
                harness.graph_writer_instances.append(self)
                harness.events.append("graph:writer:init")

            def enqueue(self, text: str, user_id: str = "agent", source: str = "chat") -> None:
                """No-op enqueue."""
                del text, user_id, source

            def flush(self) -> None:
                """No-op flush."""

            def stop(self, join_timeout: float = 10.0) -> None:
                """No-op stop matching the writer protocol: no worker exists, no event."""
                del join_timeout

            def close(self) -> None:
                """Record a close event."""
                harness.events.append("graph:writer:close")

        mp.setattr(httpx.HTTPTransport, "handle_request", handle_request)
        if self.real_logging:
            self._install_real_logging()
        else:
            mp.setattr(agent_logging, "setup_logging", setup_logging)
            mp.setattr(agent_logging, "shutdown_log_store", lambda: None)
        mp.setattr(main_mod, "_bootstrap_dot_home", bootstrap_dot_home)
        if self.real_scheduler:
            self._install_real_scheduler()
        else:
            mp.setattr(main_mod, "Scheduler", FakeScheduler)
        mp.setattr(main_mod, "TelegramInterface", FakeTelegram)
        if self.real_graph_writer:
            self._install_real_graph_writer(FakeGraphStore)
        else:
            mp.setattr(graph_memory, "GraphMemoryStore", FakeGraphStore)
            mp.setattr(graph_memory, "GraphMemoryWriter", FakeGraphWriter)

    def _install_real_logging(self) -> None:
        """Keep the real logging backend behind delegating spies (opt-in)."""
        harness = self
        setup = agent_logging.setup_logging
        shutdown = agent_logging.shutdown_log_store

        @functools.wraps(setup)
        def setup_spy(*args: Any, **kwargs: Any) -> str:
            """Snapshot global logging state, delegate, then expose the SQLite writer."""
            harness._capture_logging_state()
            path = setup(*args, **kwargs)
            harness.log_writer = sqlite_log._active_writer
            harness.events.append("log_store:setup")
            return path

        @functools.wraps(shutdown)
        def shutdown_spy(*args: Any, **kwargs: Any) -> None:
            """Record the production shutdown point, then delegate."""
            harness.events.append("log_store:shutdown")
            shutdown(*args, **kwargs)

        self.monkeypatch.setattr(agent_logging, "setup_logging", setup_spy)
        self.monkeypatch.setattr(agent_logging, "shutdown_log_store", shutdown_spy)

    def _capture_logging_state(self) -> None:
        """Snapshot, then detach, every prior sink so real setup uses only test-owned ones.

        Real setup closes the handlers it finds and ``set_active_writer`` stops
        the previous writer, so prior handlers are detached (never closed) and
        the prior active SQLite writer is detached under its lock without being
        stopped. structlog configuration and context bindings are snapshotted;
        ``restore_logging`` reinstates all of it.
        """
        if self._saved_logging is not None:
            return
        loggers = [logging.getLogger()] + [logging.getLogger(n) for n in _REAL_LOGGING_NAMED_LOGGERS]
        saved_loggers = [(lg, lg.handlers[:], lg.propagate, lg.level) for lg in loggers]
        with sqlite_log._active_writer_lock:
            prior_writer = sqlite_log._active_writer
            sqlite_log._active_writer = None
        self._saved_logging = (
            saved_loggers, prior_writer, structlog.get_config(), structlog.is_configured(),
            structlog.contextvars.get_contextvars(),
        )
        for lg, handlers, _propagate, _level in saved_loggers:
            for handler in handlers:
                lg.removeHandler(handler)

    def _install_real_graph_writer(self, fake_store_cls: type) -> None:
        """Keep the real writer; replace only the graph database (opt-in)."""
        mp = self.monkeypatch
        harness = self

        class WorkerGraphStore(fake_store_cls):  # type: ignore[misc, valid-type]
            """Database boundary for the real worker: real embedder, recorded writes."""

            def __init__(self, *args: object, **kwargs: Any) -> None:
                """Record construction and resolve embeddings like the real store."""
                super().__init__(*args, **kwargs)
                self._embed, self._embed_batch = _RESOLVE_STORE_EMBEDDER(kwargs["embedder_fn"])
                self.closed = False
                self.writes: list[tuple[str, tuple[Any, ...], bool]] = []
                self.preloaded: list[dict[str, Any]] = []

            def _preload_embeddings(self, vectors: dict[str, Any]) -> None:
                """Record the worker's staged entity vectors."""
                self.preloaded.append(dict(vectors))

            def _clear_preloaded_embeddings(self) -> None:
                """Nothing staged in the database boundary."""

            def upsert_entity(self, name: str, entity_type: str, ts: str, vector: Any = None) -> str:
                """Record an entity write and whether the store was closed."""
                del ts, vector
                self.writes.append(("entity", (name, entity_type), self.closed))
                return f"entity:{name.lower()}"

            def add_relation(self, src_id: str, tgt_id: str, relation_type: str, fact: str,
                             ts: str, *args: object, **kwargs: object) -> None:
                """Record a relation write and whether the store was closed."""
                del ts, args, kwargs
                self.writes.append(("relation", (src_id, tgt_id, relation_type, fact), self.closed))

            def add_episode(self, content: str, user_id: str, source: str = "chat",
                            *args: object, **kwargs: object) -> str:
                """Embed through the production embedder like the real store, then record."""
                del args, kwargs
                try:
                    vector = self._embed(content[:500])
                except Exception:  # noqa: BLE001 — mirrors the real store's tolerance
                    vector = None
                self.writes.append(("episode", (content, user_id, source, vector), self.closed))
                return "ep:recorded"

            def close(self) -> None:
                """Mark closed and record the close event."""
                self.closed = True
                super().close()

        writer_cls = graph_memory.GraphMemoryWriter
        real_stop = writer_cls.stop

        @functools.wraps(real_stop)
        def stop_spy(writer: Any, join_timeout: float = 10.0) -> None:
            """Delegate the real stop; only the join timeout may be substituted."""
            used = join_timeout if harness.graph_stop_timeout is None else harness.graph_stop_timeout
            harness.graph_stop_calls.append((join_timeout, used))
            harness.events.append("graph:writer:stop:start")
            try:
                real_stop(writer, join_timeout=used)
            except BaseException as exc:
                harness.graph_stop_errors.append(exc)
                harness.graph_alive_after_stop.append(writer._thread.is_alive())
                harness.events.append("graph:writer:stop:raised")
                raise
            harness.graph_alive_after_stop.append(writer._thread.is_alive())
            harness.events.append("graph:writer:stop:returned")

        embedding_close = EmbeddingClient.close

        @functools.wraps(embedding_close)
        def embedding_close_spy(client: EmbeddingClient) -> None:
            """Record whether the root or another embedding client closes, then delegate."""
            root = harness.tool_indexes[0].llm if harness.tool_indexes else None
            harness.events.append("embedding:close:root" if client is root else "embedding:close:other")
            embedding_close(client)

        llm_close = LLMClient.close

        @functools.wraps(llm_close)
        def llm_close_spy(client: LLMClient) -> None:
            """Record the first close of a graph extraction client, then delegate."""
            if getattr(client, "_graph_policy", None) is not None and not client._closed:
                harness.events.append("graph:extraction_client:close")
            llm_close(client)

        self.usage_registry = TokenUsageRegistry()
        mp.setattr(token_usage, "_registry", self.usage_registry)
        mp.setattr(graph_memory, "_LADYBUG_AVAILABLE", True)  # the database is the boundary
        mp.setattr(graph_memory, "GraphMemoryStore", WorkerGraphStore)
        self._spy_init(writer_cls, self.graph_writer_instances)
        mp.setattr(writer_cls, "stop", stop_spy)
        mp.setattr(EmbeddingClient, "close", embedding_close_spy)
        mp.setattr(LLMClient, "close", llm_close_spy)

    def _install_real_scheduler(self) -> None:
        """Isolate the ``schedule`` library's global jobs and capture the actual Scheduler."""
        mp = self.monkeypatch
        harness = self
        isolated = schedule.Scheduler()
        mp.setattr(schedule, "default_scheduler", isolated)
        mp.setattr(schedule, "jobs", isolated.jobs)
        original = Scheduler.__init__

        @functools.wraps(original)
        def scheduler_init(obj: Scheduler, *args: object, **kwargs: object) -> None:
            """Delegate the real construction, then expose the instance."""
            original(obj, *args, **kwargs)
            harness.scheduler = obj

        mp.setattr(Scheduler, "__init__", scheduler_init)

    def _spy_init(self, cls: type, sink: list[Any], *, before: bool = False) -> None:
        """Record instances of *cls*; ``before`` also records failed constructions."""
        original = cls.__init__

        @functools.wraps(original)
        def init(obj: object, *args: object, **kwargs: object) -> None:
            """Delegate to the original constructor and record the instance."""
            if before:
                sink.append(obj)
            original(obj, *args, **kwargs)
            if not before:
                sink.append(obj)

        self.monkeypatch.setattr(cls, "__init__", init)

    def _install_spies(self) -> None:
        """Install the delegating, record-only spies listed in the module docstring."""
        mp = self.monkeypatch
        harness = self
        self._spy_init(ModelRegistry, self.registries)
        self._spy_init(ModelDiscovery, self.discoveries)
        self._spy_init(LLMClient, self.llm_clients, before=True)
        self._spy_init(EmbeddingClient, self.embedding_clients)
        self._spy_init(ToolIndex, self.tool_indexes)
        self._spy_init(httpx.Client, self.http_clients)

        runtime_init = AgentRuntime.__init__

        @functools.wraps(runtime_init)
        def runtime_spy(runtime: AgentRuntime, *args: object, **kwargs: Any) -> None:
            """Delegate construction and record the runtime with its keyword arguments."""
            runtime_init(runtime, *args, **kwargs)
            harness.runtimes.append((runtime, dict(kwargs)))

        refresh = ModelRegistry.refresh

        @functools.wraps(refresh)
        def refresh_spy(registry: ModelRegistry) -> RefreshReport:
            """Delegate the real refresh and record its window and report."""
            harness.events.append("refresh:start")
            report = refresh(registry)
            harness.refresh_reports.append(report)
            harness.events.append("refresh:end")
            return report

        restore = ModelRegistry.restore_selection

        @functools.wraps(restore)
        def restore_spy(registry: ModelRegistry) -> CatalogView:
            """Record the restore point, then delegate the real restore."""
            harness.events.append("restore")
            return restore(registry)

        fetch = ModelDiscovery.fetch

        @functools.wraps(fetch)
        def fetch_spy(
            discovery: ModelDiscovery, connection: ResolvedProviderConnection,
        ) -> ProviderInventory:
            """Record which provider is fetched, then delegate the real fetch."""
            harness.events.append(f"fetch:{connection.provider_id}")
            return fetch(discovery, connection)

        make_factory = main_mod._make_sub_agent_factory
        signature = inspect.signature(make_factory)

        @functools.wraps(make_factory)
        def factory_spy(*args: object, **kwargs: object) -> Callable[..., SubAgentRunner]:
            """Delegate to the real helper and record its bound arguments and result."""
            result = make_factory(*args, **kwargs)
            harness.factory_calls.append((signature.bind(*args, **kwargs), result))
            return result

        build_extraction = graph_memory.build_extraction_llm_call
        embedding_embed = EmbeddingClient.embed

        @functools.wraps(embedding_embed)
        def embed_spy(client: EmbeddingClient, text: str) -> list[float]:
            """Observe scalar dimension probes separately from tool-index batches."""
            harness.embedding_scalar_calls.append((client, text))
            return embedding_embed(client, text)

        @functools.wraps(build_extraction)
        def build_extraction_spy(*args: object, **kwargs: object) -> Callable[[str], str]:
            """Delegate to the real helper and record its arguments."""
            harness.graph_extraction_calls.append((args, dict(kwargs)))
            harness.events.append("graph:build_extraction_call")
            return build_extraction(*args, **kwargs)

        mp.setattr(AgentRuntime, "__init__", runtime_spy)
        mp.setattr(ModelRegistry, "refresh", refresh_spy)
        mp.setattr(ModelRegistry, "restore_selection", restore_spy)
        mp.setattr(ModelDiscovery, "fetch", fetch_spy)
        mp.setattr(main_mod, "_make_sub_agent_factory", factory_spy)
        mp.setattr(graph_memory, "build_extraction_llm_call", build_extraction_spy)
        mp.setattr(EmbeddingClient, "embed", embed_spy)

    # ------------------------------------------------------------------
    # External HTTP boundary
    # ------------------------------------------------------------------

    def _route(self, request: httpx.Request) -> httpx.Response:
        """Serve discovery pages, models.dev and embeddings; record anything else."""
        request.read()
        self.requests.append(request)
        host, path, method = request.url.host, request.url.path, request.method
        params = dict(request.url.params)
        if method == "GET" and host == ACME_HOST and path == "/v1/models":
            if not params:
                return httpx.Response(200, json={
                    "data": [_acme_row("claude-a")], "has_more": True, "last_id": "claude-a",
                })
            if params == {"after_id": "claude-a"}:
                return httpx.Response(200, json={"data": [_acme_row("claude-b")], "has_more": False})
        elif method == "GET" and host == GEM_HOST and path == "/v1beta/models":
            if not params:
                return httpx.Response(200, json={"models": [_gem_row("gem-1")], "nextPageToken": "tok-2"})
            if params == {"pageToken": "tok-2"}:
                return httpx.Response(200, json={"models": [_gem_row("gem-2")]})
        elif method == "GET" and host == "models.dev" and path == "/api.json":
            return httpx.Response(200, json={})
        elif method == "POST" and host == EMBED_HOST and path == "/v1/embeddings":
            body = json.loads(request.content)
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(200, json={"data": [{"embedding": [0.25, 0.75]} for _ in inputs]})
        elif method == "POST" and host == GRAPH_HOST and path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        elif method == "GET" and host == GRAPH_HOST and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": GRAPH_UPSTREAM}], "has_more": False})
        self.unexpected.append(f"{method} {request.url}")
        return httpx.Response(404, json={"error": "not served by the root harness"})

    def requests_to(self, host: str) -> list[httpx.Request]:
        """Return recorded outbound requests addressed to *host*, in order."""
        return [request for request in self.requests if request.url.host == host]

    def serve_provider_chat(
        self,
        *,
        failing_chat_hosts: tuple[str, ...] = (),
        failing_discovery_hosts: tuple[str, ...] = (),
    ) -> None:
        """Opt-in: also serve acme/gem chat, optionally failing chat or discovery per host.

        Install before ``start`` when discovery must fail (last-good entries stay
        stale). Everything else is delegated to the default router.
        """
        original = self._route

        def route(request: httpx.Request) -> httpx.Response:
            """Serve provider chat and the requested failures; delegate the rest."""
            host, path, method = request.url.host, request.url.path, request.method
            chat = method == "POST" and (
                (host == ACME_HOST and path == "/v1/messages")
                or (host == GEM_HOST and path.endswith(":generateContent"))
            )
            discovery = method == "GET" and path in ("/v1/models", "/v1beta/models")
            if not chat and not (discovery and host in failing_discovery_hosts):
                return original(request)
            request.read()
            self.requests.append(request)
            if discovery or host in failing_chat_hosts:
                return httpx.Response(503, json={"error": {"message": "provider outage"}})
            if host == ACME_HOST:
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": "pong"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                })
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "pong"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
            })

        self.monkeypatch.setattr(self, "_route", route)

    # ------------------------------------------------------------------
    # Driving the actual root
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        saved_default: str | None = MAIN_ID,
        graph_enabled: bool = False,
        hold: Callable[[RootHarness], None] | None = None,
        scheduler_toml: str | None = None,
    ) -> None:
        """Write config + saved catalog (+ optional scheduler file) and run ``main._run``."""
        self.hold = hold
        main_mod._create_xdg_dirs(self.paths)
        self.workspace.mkdir(parents=True, exist_ok=True)
        config_text = _config_text(self.workspace, graph_enabled=graph_enabled)
        if self.real_scheduler:
            # Explicit: the scheduler defaults to enabled and would start its cron thread.
            config_text += "\n[scheduler]\nenabled = false\n"
        self.paths.config_file.write_text(config_text)
        if scheduler_toml is not None:
            self.paths.scheduler_config.write_text(scheduler_toml, encoding="utf-8")
        ModelSettings(self.paths.models_config_file).write(
            _saved_document(saved_default, graph_enabled=graph_enabled)
        )
        cfg, app_cfg = main_mod.load_config(
            self.paths.config_file, vault_file=str(self.paths.secrets_file), agent_name=AGENT_NAME,
        )
        self.app_cfg = app_cfg
        assert app_cfg.graph_memory.enabled is graph_enabled
        try:
            main_mod._run(
                cfg=cfg, app_cfg=app_cfg, paths=self.paths,
                downloads_dir=str(self.workspace / "downloads"), tmp_dir=str(self.tmp_dir),
                workspace_dir=str(self.workspace), agent_name=AGENT_NAME,
            )
        finally:
            self.open_clients_at_exit = [c for c in self.http_clients if not c.is_closed]

    def release_leftovers(self) -> None:
        """Test hygiene after assertions: close anything production left open."""
        for client in self.http_clients:
            if not client.is_closed:
                client.close()

    def release_graph_workers(self, timeout: float = 10.0) -> list[bool]:
        """Test hygiene: open every barrier, then end and boundedly join real workers.

        A still-running worker gets one more sentinel (never production
        shutdown evidence). Raises ``AssertionError`` when any worker survives
        its bounded join, so callers stop before closing its dependencies.
        """
        for barrier in self.graph_barriers:
            barrier.set()
        if not self.real_graph_writer:
            return []
        alive: list[bool] = []
        for writer in self.graph_writer_instances:
            thread = writer._thread
            if thread.is_alive():
                writer._queue.put(graph_memory.GraphMemoryWriter._SENTINEL)
                thread.join(timeout=timeout)
            alive.append(thread.is_alive())
        if any(alive):
            raise AssertionError(
                "graph worker survived the bounded cleanup join; its dependencies are left open"
            )
        return alive

    def finish_graph_hygiene(self) -> None:
        """Test hygiene in dependency order: end workers, then close clients and logging.

        Raises before closing anything when a worker cannot be confirmed dead;
        once workers are dead, logging is restored even if client cleanup fails.
        """
        self.release_graph_workers()
        try:
            self.release_leftovers()
        finally:
            self.restore_logging()

    def restore_logging(self) -> None:
        """Test hygiene: retire the test-owned sinks and reinstate the prior logging state.

        Closes only handlers the real setup created and stops only test-owned
        SQLite writers; reattaches the still-open prior handlers with their
        exact levels/propagation, the prior active writer, and the exact
        structlog configuration and context bindings. Each step runs even if
        an earlier one fails.
        """
        if self._saved_logging is None:
            return
        saved_loggers, prior_writer, config, configured, context = self._saved_logging
        self._saved_logging = None
        try:
            for lg, handlers, _propagate, _level in saved_loggers:
                for handler in lg.handlers[:]:
                    lg.removeHandler(handler)
                    if handler not in handlers:
                        try:
                            handler.close()
                        except OSError:
                            pass
        finally:
            try:
                with sqlite_log._active_writer_lock:
                    current = sqlite_log._active_writer
                    sqlite_log._active_writer = prior_writer
                for writer in (self.log_writer, current):
                    if writer is not None and writer is not prior_writer:
                        writer.stop(timeout=5.0)
            finally:
                try:
                    for lg, handlers, propagate, level in saved_loggers:
                        for handler in handlers:
                            if handler not in lg.handlers:
                                lg.addHandler(handler)
                        lg.propagate = propagate
                        lg.setLevel(level)
                finally:
                    restore_structlog_state(config, configured, context)

    # ------------------------------------------------------------------
    # Captured root products
    # ------------------------------------------------------------------

    @property
    def registry(self) -> ModelRegistry:
        """The single ``ModelRegistry`` the root constructed."""
        assert len(self.registries) == 1, f"expected one root ModelRegistry, got {len(self.registries)}"
        return self.registries[0]

    @property
    def runtime(self) -> AgentRuntime:
        """The single ``AgentRuntime`` the root constructed."""
        assert len(self.runtimes) == 1, f"expected one root AgentRuntime, got {len(self.runtimes)}"
        return self.runtimes[0][0]

    @property
    def runtime_kwargs(self) -> dict[str, Any]:
        """Keyword arguments the root passed to its ``AgentRuntime``."""
        assert len(self.runtimes) == 1, f"expected one root AgentRuntime, got {len(self.runtimes)}"
        return self.runtimes[0][1]

    @property
    def main_controller(self) -> AgentController:
        """The main controller the root wired into Telegram."""
        agent = getattr(self.telegram, "agent", None)
        assert isinstance(agent, AgentController), "root did not wire the main controller into Telegram"
        return agent

    @property
    def builtin(self) -> BuiltinExecutor:
        """The root ``BuiltinExecutor`` shared with the main controller."""
        return self.main_controller.builtin_executor

    @property
    def agent_handler(self) -> Callable[..., str]:
        """The ordinary-request handler the root handed to Telegram."""
        assert self.telegram is not None and len(self.telegram.args) >= 2
        return self.telegram.args[1]

    @property
    def root_embedding(self) -> EmbeddingClient:
        """Return ToolIndex's root client, permitting only a closed graph probe."""
        startup = self.embedding_clients_at_hold
        assert len(self.tool_indexes) == 1, "expected one root ToolIndex"
        root = self.tool_indexes[0].llm
        assert isinstance(root, EmbeddingClient)
        assert sum(client is root for client in startup) == 1
        if not self.app_cfg.graph_memory.enabled:
            assert len(startup) == 1, f"expected one root EmbeddingClient at startup, got {len(startup)}"
        else:
            probes = [client for client in startup if client is not root]
            assert len(probes) <= 1, "unexpected extra graph embedding clients"
            assert all(client._http.is_closed for client in probes), "graph prerequisite probe left open"
        return root

    @property
    def graph_store(self) -> Any:
        """The single fake ``GraphMemoryStore`` the root constructed, if any."""
        if not self.graph_store_instances:
            raise AssertionError("root did not construct a GraphMemoryStore")
        assert len(self.graph_store_instances) == 1, (
            f"expected one root GraphMemoryStore, got {len(self.graph_store_instances)}"
        )
        return self.graph_store_instances[0]

    @property
    def graph_writer(self) -> Any:
        """The single fake ``GraphMemoryWriter`` the root constructed, if any."""
        if not self.graph_writer_instances:
            raise AssertionError("root did not construct a GraphMemoryWriter")
        assert len(self.graph_writer_instances) == 1, (
            f"expected one root GraphMemoryWriter, got {len(self.graph_writer_instances)}"
        )
        return self.graph_writer_instances[0]

    def factory_binding(self) -> tuple[object, object, Callable[..., SubAgentRunner]]:
        """Return ``(runtime, model_registry, factory)`` from the single helper call."""
        assert len(self.factory_calls) == 1, (
            f"root must build the sub-agent factory exactly once via the helper, "
            f"got {len(self.factory_calls)}"
        )
        bound, factory = self.factory_calls[0]
        return bound.arguments["runtime"], bound.arguments["model_registry"], factory


# ----------------------------------------------------------------------
# Real-scheduler root runs (task 7.3)
# ----------------------------------------------------------------------

SCHEDULED_TAG = "nightly"


def scheduled_job_toml(model_literal: str | None, *, tag: str = SCHEDULED_TAG) -> str:
    """Return one far-future, ``notify = false`` job with job-owned context.

    ``model_literal`` is a raw TOML value literal; ``None`` omits the key
    (TOML has no null, so absence is the only ``None`` form).
    """
    model_line = "" if model_literal is None else f"model = {model_literal}\n"
    return (
        f"[jobs.{tag}]\n"
        'enabled = true\nschedule = "cron"\ncron = "0 2 1 1 *"\n'
        'task = "scheduled root probe"\nnotify = false\npreserve_context = true\n'
        f"{model_line}"
    )


class ScheduledRootProbe:
    """Delegating observers on a started real-scheduler root, plus one manual fire.

    Construct inside ``hold``. Every spy delegates to the original and never
    replaces a decision: ``admit``/``admit_scheduled`` (also snapshotting the
    scheduler's launch state at admission entry), the credential resolver,
    ``AgentRuntime.create`` and child-run registration. ``fire`` drives the
    actual ``Scheduler._run_job(tag, manual=True)`` once; only the supervisor's
    external worker pool runs inline and ``agent_controller.react_loop`` is the
    request boundary sending exactly one request through the run's pinned client.
    The runner is never run a second time.
    """

    def __init__(
        self, root: RootHarness, monkeypatch: pytest.MonkeyPatch, tag: str = SCHEDULED_TAG,
        messages: list[dict] | None = None,
    ) -> None:
        """Snapshot root state, then wrap the root's real collaborators.

        ``messages`` replaces the single ``ping`` request the boundary sends.
        """
        assert root.real_scheduler and isinstance(root.scheduler, Scheduler)
        self._messages = messages or [{"role": "user", "content": "ping"}]
        assert root.scheduler.builtin_executor is root.builtin, "scheduler not wired to the root executor"
        self.tag = tag
        self._root = root
        self._mp = monkeypatch
        self.view_before = root.registry.view()
        self.settings_before = root.paths.models_config_file.read_bytes()
        self.toml_before = root.paths.scheduler_config.read_bytes()
        self._marks = (len(root.requests), len(root.llm_clients), len(root.http_clients), len(root.notices))
        self.admits: dict[str, list[object]] = {"admit": [], "admit_scheduled": []}
        self.at_admission: list[SimpleNamespace] = []
        self.codes: list[str] = []
        self.pins: list[RunModel] = []
        self.resolved: list[str] = []
        self.creates: list[tuple[object, Any]] = []
        self.registered: list[tuple[str, str]] = []
        self.loop_calls: list[SimpleNamespace] = []
        self.after: SimpleNamespace | None = None
        registry = root.registry
        for name in self.admits:
            monkeypatch.setattr(registry, name, self._admission_spy(name, getattr(registry, name)))
        resolve = registry._credential_resolver

        def resolve_spy(config: Any) -> Any:
            """Record the provider whose credentials are resolved, then delegate."""
            self.resolved.append(config.name)
            return resolve(config)

        monkeypatch.setattr(registry, "_credential_resolver", resolve_spy)
        create = root.runtime.create

        def create_spy(profile: object, options: Any, **kwargs: Any) -> object:
            """Record the runtime construction request, then delegate."""
            self.creates.append((profile, options))
            return create(profile, options, **kwargs)

        monkeypatch.setattr(root.runtime, "create", create_spy)

    def _admission_spy(self, name: str, original: Callable[[object], RunModel]) -> Callable[..., RunModel]:
        """Return a spy recording selector, launch state, pin or typed refusal code."""

        def spy(selector: object = None) -> RunModel:
            """Record, then delegate to the real registry admission."""
            self.admits[name].append(selector)
            if name == "admit_scheduled":
                self.at_admission.append(self._launch_state())
            try:
                pin = original(selector)
            except ModelRegistryError as exc:
                self.codes.append(exc.code)
                raise
            self.pins.append(pin)
            return pin

        return spy

    def records(self) -> list[dict]:
        """Persisted execution records of this job, oldest first."""
        return [e for e in self._root.scheduler.execution_log.read_recent() if e.get("tag") == self.tag]

    def _state(self) -> dict:
        """This job's entry in the persisted scheduler state file."""
        state = json.loads(self._root.paths.scheduler_state.read_text(encoding="utf-8"))
        return state["jobs"][self.tag]

    def _launch_state(self) -> SimpleNamespace:
        """Run ID/tracking, persisted ``last_run`` and terminal records right now."""
        sched = self._root.scheduler
        return SimpleNamespace(
            run_ids=frozenset(sched._active_runs.get(self.tag, ())),
            running=self.tag in sched._running_jobs,
            persisted_last_run=self._state()["last_run"],
            records=self.records(),
        )

    def _loop(self, ctx: Any, goal: str, progress_callback: Any = None, images: Any = None, **kwargs: Any) -> str:
        """Request boundary: send one request through the run's pinned client."""
        call = SimpleNamespace(goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.loop_calls.append(call)
        call.reply = ctx.llm.chat(self._messages)
        return "scheduled-answer"

    def fire(self) -> None:
        """Fire the job once through the actual scheduler, then snapshot the aftermath."""
        root = self._root
        runs = SubAgentRegistry()
        register = runs.register

        def register_spy(record: Any) -> None:
            """Record the child run registration, then delegate."""
            self.registered.append((record.agent_id, record.source))
            register(record)

        self._mp.setattr(runs, "register", register_spy)

        def run_inline(fn: Callable[[], None], *_args: object, **_kwargs: object) -> MagicMock:
            """External pool boundary: run the real worker body once, inline."""
            fn()
            return MagicMock()

        with patch("sub_agent_registry.get_registry", return_value=runs), \
             patch.object(root.builtin._supervisor._pool, "submit", side_effect=run_inline), \
             patch.object(agent_controller, "react_loop", self._loop):
            root.scheduler._run_job(self.tag, manual=True)
        requests, llm_clients, http_clients, notices = self._marks
        sched = root.scheduler
        new_llm = root.llm_clients[llm_clients:]
        self.after = SimpleNamespace(
            view=root.registry.view(),
            settings=root.paths.models_config_file.read_bytes(),
            toml=root.paths.scheduler_config.read_bytes(),
            records=self.records(),
            state=self._state(),
            tracked=self.tag in sched._running_jobs or self.tag in sched._active_runs,
            active_children=runs.list_active(),
            provider_posts=[
                r for r in root.requests[requests:]
                if r.method == "POST" and r.url.host in (ACME_HOST, GEM_HOST)
            ],
            provider_requests=[r for r in root.requests[requests:] if r.url.host in (ACME_HOST, GEM_HOST)],
            llm_clients=len(new_llm),
            llm_closed=[client._closed for client in new_llm],
            http_clients=len(root.http_clients[http_clients:]),
            http_open=[c for c in root.http_clients[http_clients:] if not c.is_closed],
            notices=root.notices[notices:],
        )


def run_scheduled_root(
    root: RootHarness,
    monkeypatch: pytest.MonkeyPatch,
    model_literal: str | None,
    *,
    saved_default: str | None = MAIN_ID,
    before_fire: Callable[[RootHarness], None] | None = None,
    messages: list[dict] | None = None,
) -> ScheduledRootProbe:
    """Start the actual root with one job, then probe and fire it once inside ``hold``."""
    probes: list[ScheduledRootProbe] = []

    def hold(started: RootHarness) -> None:
        """Install the probe, apply the optional pre-fire change, fire once."""
        probe = ScheduledRootProbe(started, monkeypatch, messages=messages)
        probes.append(probe)
        if before_fire is not None:
            before_fire(started)
        probe.fire()

    root.start(saved_default=saved_default, hold=hold, scheduler_toml=scheduled_job_toml(model_literal))
    assert len(probes) == 1 and probes[0].after is not None, "the scheduled job was not fired"
    return probes[0]
