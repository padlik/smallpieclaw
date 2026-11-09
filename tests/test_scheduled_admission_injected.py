"""Task 12.12 first slice: scheduled admission through an injected strict factory.

Real chain: ``Scheduler._run_job`` (the automatic cron fire entry) → real
``BuiltinExecutor.spawn_agent`` → real ``SubAgentSupervisor`` → injected strict
factory → real ``ModelRegistry.admit_scheduled`` (once) → real ``AgentRuntime``
→ real ``SubAgentRunner`` / ``AgentController`` / ``react_loop`` → real
``LLMClient`` on ``httpx.MockTransport``. Root ``main.py`` wiring is out of
scope. Only external boundaries are substituted: file system (``tmp_path``),
notify, the ``schedule`` library's global scheduler, discovery, credential
resolution, and HTTP. Instrumented subclasses always call ``super()``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import schedule

import agent_controller
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile
from config_schema import (
    AgentConfig,
    EmbeddingsConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from embedding_client import EmbeddingClient
from llm_client import LLMClient
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
    RunModel,
)
from model_discovery import ProviderInventory
from model_settings import ModelSettings
from outcome_utils import read_execution
from path_policy import PathPolicy, PathVerdict
from scheduler import Scheduler
from sub_agent_registry import get_registry
from token_usage import TokenUsageRegistry
from xdg import xdg_paths

_BOUND = 10.0
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")
TAG = "nightly_report"
RESULT = "scheduled-done"
AGENT = AgentConfig(max_retries=0, retry_delay=0)
URLS = {"pa": "https://a.example.test/v1", "pb": "https://b.example.test/v1"}


def _record(provider: str, upstream: str) -> ModelRecord:
    return ModelRecord(
        f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
        ModelMetadata(
            200_000, None, 2048, False, True, None,
        ),
    )


MODEL_A = _record("pa", "up/a")
MODEL_B = _record("pb", "up/b")


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during scheduled admission")


_FIRE_THREAD = "scheduled-fire"


class _SameInventoryDiscovery:
    """Discovery returning the already-saved A/B entries unchanged."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(self, connection: ResolvedProviderConnection) -> ProviderInventory:
        self.calls.append((connection.provider_id, threading.current_thread().name))
        models = tuple(m for m in (MODEL_A, MODEL_B) if m.provider_id == connection.provider_id)
        return ProviderInventory(connection.provider_id, "openai", models,
                                 "2026-10-01T12:00:00Z")


class _FireGate:
    """Bounded entered/release gate (fire-thread credentials or the chat wire)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.waits: list[bool] = []


class _Registry(ModelRegistry):
    """Real registry that counts admissions around the real implementation."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.scheduled_calls: list[object] = []
        self.scheduled_errors: list[str] = []
        self.ordinary_calls = 0

    def admit_scheduled(self, raw_selector: object = None) -> RunModel:
        self.scheduled_calls.append(raw_selector)
        try:
            return super().admit_scheduled(raw_selector)
        except ModelRegistryError as exc:
            self.scheduled_errors.append(exc.code)
            raise

    def admit(self, selector: str | None = None) -> RunModel:
        self.ordinary_calls += 1
        return super().admit(selector)


class _TrackedLLM(LLMClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _TrackedEmbedding(EmbeddingClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _InstrumentedScheduler(Scheduler):
    """Real scheduler; every run release is observed after the real release."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.released: list[tuple[str, threading.Thread, int]] = []
        self.released_event = threading.Event()
        super().__init__(*args, **kwargs)

    def _release_run(self, tag: str, run_id: str | None = None) -> None:
        records = len([e for e in self.execution_log.read_recent() if e["tag"] == tag])
        super()._release_run(tag, run_id)
        if run_id is not None:
            self.released.append((run_id, threading.current_thread(), records))
            self.released_event.set()


class _NullToolIndex:
    def search(self, *_a: object, **_k: object) -> list:
        return []

    def all_tools(self) -> list:
        return []


class _StrictFactory:
    """Injected sub_agent_factory: one real scheduled admission, then the real runtime.

    Runs synchronously on the scheduler fire thread, whose launch path converts
    any exception into a recorded failure, so it records observations for the
    test body instead of asserting here.
    """

    def __init__(self, world: "_World") -> None:
        self.world = world
        self.runtime: AgentRuntime | None = None
        self.calls: list[dict] = []
        self.pins: list[RunModel] = []
        self.runners: list[object] = []

    def __call__(self, *, model, context_key, label, notify_fn, trace_id,
                 context_payload, runtime_profile):
        sched = self.world.sched
        state = json.loads(self.world.paths.scheduler_state.read_text(encoding="utf-8"))
        self.calls.append({
            "kwargs": dict(model=model, context_key=context_key, label=label,
                           notify_fn=notify_fn, trace_id=trace_id,
                           context_payload=context_payload),
            "profile": runtime_profile,
            "run_ids": set(sched._active_runs.get(TAG, set())),
            "running": TAG in sched._running_jobs,
            "last_run": state["history"].get(TAG, {}).get("last_run"),
            "records": [e for e in sched.execution_log.read_recent() if e["tag"] == TAG],
        })
        pin = self.world.registry.admit_scheduled(model)
        self.pins.append(pin)
        runner = self.runtime.create(
            RuntimeProfile.SCHEDULED_AGENT,
            RuntimeOptions(run_model=pin, context_key=context_key, trace_id=trace_id,
                           label=label, context_payload=context_payload),
            notify_fn=notify_fn,
        )
        self.runners.append(runner)
        return runner


def _real_policy(root: Path) -> PathPolicy:
    """Real PathPolicy under *root*: workspace default-allowed, agent dirs prohibited."""
    dirs = {name: root / name for name in (
        "workspace", "downloads", "skills", "results", "data", "state", "config",
    )}
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    vault, config = root / "vault.toml", dirs["config"] / "config.toml"
    vault.write_text('[secrets]\nk = "v"\n', encoding="utf-8")
    config.write_text("\n", encoding="utf-8")
    return PathPolicy.create(
        agent_name="scheduled-admission-test", workspace_dir=str(dirs["workspace"]),
        downloads_dir=str(dirs["downloads"]), tmp_dir="/tmp/scheduled-admission-test",
        skills_dir=str(dirs["skills"]), results_dir=str(dirs["results"]),
        data_home=str(dirs["data"]), state_home=str(dirs["state"]),
        config_home=str(dirs["config"]), vault_path=str(vault), config_path=str(config),
        logger=logging.getLogger("test"),
    )


class _World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                 make_builtin_executor, *, select: bool, job_model: str | int | None,
                 stale_provider: bool = False, discovery: object | None = None,
                 gate: _FireGate | None = None, chat_gate: _FireGate | None = None,
                 policy: bool = False, chat_script: list[dict] | None = None) -> None:
        self.gate = gate
        self.chat_gate = chat_gate
        # Scripted provider messages answered before the default finish response.
        self.chat_script = list(chat_script or [])
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            monkeypatch.setenv(var, str(tmp_path / var.lower()))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        self.paths = xdg_paths("scheduled-admission-test")
        assert str(self.paths.scheduler_state).startswith(str(tmp_path))
        self.paths.config_home.mkdir(parents=True, exist_ok=True)
        self.paths.state_home.mkdir(parents=True, exist_ok=True)
        model_line = ""
        if job_model is not None:
            # json.dumps of a plain string or int is also a valid TOML literal.
            assert type(job_model) in (str, int), job_model
            model_line = f"model = {json.dumps(job_model)}\n"
        self.paths.scheduler_config.write_text(
            f'[jobs.{TAG}]\nenabled = true\nschedule = "cron"\ncron = "0 4 1 1 *"\n'
            f'task = "write report"\nnotify = false\n{model_line}', encoding="utf-8",
        )

        self.resolved: list[str] = []
        self.fire_resolved: list[str] = []
        self.other_resolved: list[str] = []
        self.chat_requests: list[httpx.Request] = []
        self.embed_requests: list[httpx.Request] = []
        self.chats: list[_TrackedLLM] = []
        self.embeddings: list[_TrackedEmbedding] = []
        self.embed_http: list[httpx.Client] = []
        self.usage = TokenUsageRegistry()
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", URLS[n], 9) for n in URLS}
        self.settings_path = tmp_path / "models-config.toml"
        settings = ModelSettings(self.settings_path)
        settings.write(CatalogDocument(
            1, 2, None,
            tuple(
                ProviderState(n, "openai", "2026-09-01T00:00:00Z",
                              "stale" if stale_provider and n == "pb" else "fresh")
                for n in providers
            ),
            (MODEL_A, MODEL_B),
        ))
        self.registry = _Registry(providers, settings, discovery or _NoDiscovery(), self._resolve)
        if select:
            self.registry.select(MODEL_A.id)
        self.document = self.settings_path.read_bytes()
        monkeypatch.setattr(agent_controller, "LLMClient", self._make_llm)

        self.factory = _StrictFactory(self)
        self.policy = _real_policy(tmp_path / "policy") if policy else None
        self.executor = make_builtin_executor(sub_agent_factory=self.factory,
                                              path_policy=self.policy)
        self.factory.runtime = AgentRuntime(
            agent_cfg=AGENT, embedding_factory=self._embedding,
            paths=ExecutorPaths(tmp_dir=str(tmp_path / "tmp"),
                                downloads_dir=str(tmp_path / "dl"),
                                workspace_dir=str(tmp_path / "ws")),
            tool_index=_NullToolIndex(), builtin_executor=self.executor,
            usage_registry=self.usage, data_dir=str(tmp_path / "data"),
            scheduled_max_iterations=4,
        )
        self.sched = _InstrumentedScheduler(
            config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=MagicMock(), agent_fn=MagicMock(), paths=self.paths,
            scheduler_config_path=str(self.paths.scheduler_config),
            builtin_executor=self.executor,
        )

    def _resolve(self, config: ProviderConfig) -> ResolvedProviderConnection:
        self.resolved.append(config.name)
        if threading.current_thread().name == _FIRE_THREAD:
            self.fire_resolved.append(config.name)
            if self.gate is not None:
                self.gate.entered.set()
                self.gate.waits.append(self.gate.release.wait(_BOUND))
        else:
            self.other_resolved.append(config.name)
        return ResolvedProviderConnection(
            provider_id=config.name, adapter=config.type, base_url=config.base_url,
            request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
        )

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.chat_requests.append(request)
        if self.chat_gate is not None:
            self.chat_gate.entered.set()
            self.chat_gate.waits.append(self.chat_gate.release.wait(_BOUND))
        if self.chat_script:
            return httpx.Response(200, json={
                "choices": [{"message": self.chat_script.pop(0), "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"action": "finish", "result": RESULT})}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    def _embed(self, request: httpx.Request) -> httpx.Response:
        self.embed_requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.5]}]})

    def _make_llm(self, run_model: RunModel, **kwargs: object) -> _TrackedLLM:
        client = _TrackedLLM(run_model, http_transport=httpx.MockTransport(self._chat), **kwargs)
        self.chats.append(client)
        return client

    def _embedding(self, event: threading.Event) -> EmbeddingClient:
        http_client = httpx.Client(transport=httpx.MockTransport(self._embed))
        self.embed_http.append(http_client)
        client = _TrackedEmbedding(
            EmbeddingsConfig(provider="openai", model="embed-1", api_key="embed-key"), {},
            http_client=http_client, cancel_event=event,
        )
        self.embeddings.append(client)
        return client

    def records(self) -> list[dict]:
        return [e for e in self.sched.execution_log.read_recent() if e["tag"] == TAG]

    def teardown(self) -> None:
        self.executor.shutdown(graceful_timeout=_BOUND)
        workers = {thread for _r, thread, _n in self.sched.released}
        for worker in workers - {threading.current_thread()}:
            worker.join(_BOUND)
            assert not worker.is_alive(), "sub-agent pool worker did not drain"
        for http_client in self.embed_http:
            http_client.close()
        for client in self.chats:
            LLMClient.close(client)
        if self.sched._thread is not None:
            self.sched.stop()


@pytest.fixture
def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
               make_builtin_executor) -> Iterator:
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    worlds: list[_World] = []

    def build(**kwargs: object) -> _World:
        world = _World(tmp_path, monkeypatch, make_builtin_executor, **kwargs)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.teardown()


def _assert_admitted_at_entry(world: _World, raw: object) -> str:
    """One factory entry with the exact contract kwargs, after run admission."""
    assert len(world.factory.calls) == 1, world.records()
    call = world.factory.calls[0]
    assert call["profile"] is RuntimeProfile.SCHEDULED_AGENT
    assert call["kwargs"] == dict(model=raw, context_key=None, label=TAG, notify_fn=None,
                                  trace_id=None, context_payload={})
    assert type(call["kwargs"]["model"]) is type(raw), "raw selector type not preserved"
    assert call["running"] and len(call["run_ids"]) == 1
    assert call["last_run"], "last_run was not persisted before admission"
    assert call["records"] == [], "terminal record existed before admission"
    (run_id,) = call["run_ids"]
    assert _RUN_ID_RE.match(run_id)
    assert world.registry.scheduled_calls == [raw]
    assert world.registry.ordinary_calls == 0
    return run_id


def _registry_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "model_registry" and r.levelno == logging.WARNING]


def _pb_status(world: _World) -> str:
    return {p.provider_id: p.status for p in world.registry.view().providers}["pb"]


# (raw saved selector, stale pb provider) shared by both gates.
_UNCACHED_B = ModelRecord("pb/not-cached", "pb", "not-cached", "not-cached")
_SELECTORS = [(None, False), (MODEL_B.id, False), ("ghost/not-known", False),
              (123, False), (MODEL_B.id, True)]
_SELECTOR_IDS = ["inherit-main", "explicit-b", "unknown-connection", "malformed-int",
                 "explicit-b-stale"]


@pytest.mark.parametrize(
    ("raw", "stale", "expected", "warnings"),
    [(raw, stale, expected, warns) for (raw, stale), (expected, warns) in zip(
        _SELECTORS,
        [(MODEL_A, 0), (MODEL_B, 0), (MODEL_A, 1), (MODEL_A, 1), (MODEL_B, 0)],
    )] + [(_UNCACHED_B.id, False, _UNCACHED_B, 0)],  # task 4.1: uncached exact target is pinned
    ids=[*_SELECTOR_IDS, "uncached-exact"],
)
def test_cron_fire_admits_once_and_completes_on_pinned_model(
    make_world, caplog: pytest.LogCaptureFixture, raw: str | int | None, stale: bool,
    expected: ModelRecord, warnings: int,
) -> None:
    caplog.set_level(logging.WARNING, logger="model_registry")
    world = make_world(select=True, job_model=raw, stale_provider=stale)
    assert _pb_status(world) == ("stale" if stale else "fresh")

    world.sched._run_job(TAG)
    assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())

    run_id = _assert_admitted_at_entry(world, raw)
    assert len(_registry_warnings(caplog)) == warnings, caplog.records
    (pin,) = world.factory.pins
    assert pin.model == expected and world.resolved == [expected.provider_id]
    (client,) = world.chats
    assert client._run_model is pin
    (request,) = world.chat_requests
    assert str(request.url) == f"{URLS[expected.provider_id]}/chat/completions"
    assert request.headers["authorization"] == f"Bearer secret-{expected.provider_id}"
    assert json.loads(request.content)["model"] == expected.upstream_id
    usage = world.usage.get_today()
    assert usage[expected.id] == {"prompt": 3, "completion": 2, "total": 5}
    assert expected.upstream_id not in usage

    (record,) = world.records()
    assert record["run_id"] == run_id and record["status"] == "done"
    assert record["success"] is True and record["result"] == RESULT
    assert record["model"] == expected.id
    assert world.sched.notify.call_count == 0, "notify=false run was notified"
    released_run, _worker, records_at_release = world.sched.released[0]
    assert released_run == run_id and records_at_release == 1

    (runner,) = world.factory.runners
    assert get_registry().get(runner.agent_id) is None
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    assert client.close_count == 1 and client._closed
    (embedding,) = world.embeddings
    assert embedding.close_count == 1
    assert not world.embed_http[0].is_closed, "borrowed embedding HTTP closed by runner"
    assert world.embed_requests == []
    assert world.registry.view().active_model_id == MODEL_A.id
    assert world.settings_path.read_bytes() == world.document
    assert _pb_status(world) == ("stale" if stale else "fresh")


@pytest.mark.parametrize(
    ("raw", "stale", "evidence"),
    [(raw, stale, evidence) for (raw, stale), evidence in zip(
        _SELECTORS,
        ["inherit", MODEL_B.id, "ghost/not-known", "malformed (not a string)", MODEL_B.id],
    )],
    ids=_SELECTOR_IDS,
)
def test_selector_does_not_bypass_missing_main(
    make_world, caplog: pytest.LogCaptureFixture, raw: str | int | None, stale: bool,
    evidence: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="model_registry")
    world = make_world(select=False, job_model=raw, stale_provider=stale)
    assert _pb_status(world) == ("stale" if stale else "fresh")
    managed_before = get_registry().count_managed()

    world.sched._run_job(TAG)

    run_id = _assert_admitted_at_entry(world, raw)
    assert _registry_warnings(caplog) == [], "selector classified before the Main gate"
    assert world.factory.pins == [] and world.factory.runners == []
    assert world.resolved == []
    assert world.chats == [] and world.embeddings == []
    assert world.chat_requests == [] and world.embed_requests == []
    assert get_registry().count_managed() == managed_before

    (record,) = world.records()
    assert record["run_id"] == run_id and record["status"] == "failed"
    assert record["success"] is False
    assert "no active selection" in record["result"]
    assert record["model"] == evidence
    alerts = [c.args[0] for c in world.sched.notify.call_args_list]
    assert len(alerts) == 1 and TAG in alerts[0] and "failed to spawn" in alerts[0]
    for text in (record["result"], alerts[0]):
        assert "secret-" not in text and "env:KEY_" not in text
    assert [(r, n) for r, _t, n in world.sched.released] == [(run_id, 1)]
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    assert world.registry.view().active_model_id is None
    assert world.settings_path.read_bytes() == world.document


def test_same_entry_refresh_during_admission_credentials_does_not_fail_launch(
    make_world, caplog: pytest.LogCaptureFixture,
) -> None:
    """Task 4.1: a real refresh publishing a new document while the fire thread resolves
    credentials does not invalidate the explicit scheduled target (no document recheck)."""
    caplog.set_level(logging.WARNING, logger="model_registry")
    discovery, gate = _SameInventoryDiscovery(), _FireGate()
    world = make_world(select=True, job_model=MODEL_B.id, discovery=discovery, gate=gate)
    registry = world.registry
    before_view = registry.view()
    fire_errors: list[BaseException] = []
    fired = threading.Event()

    def fire() -> None:
        try:
            world.sched._run_job(TAG)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the parent below
            fire_errors.append(exc)
        finally:
            fired.set()

    thread = threading.Thread(target=fire, name=_FIRE_THREAD, daemon=True)
    thread.start()
    try:
        assert gate.entered.wait(_BOUND), ("fire thread never resolved", fire_errors,
                                           world.records())
        assert world.fire_resolved == ["pb"] and world.other_resolved == []
        assert world.records() == [] and TAG in world.sched._running_jobs
        assert len(world.factory.calls) == 1 and registry.scheduled_calls == [MODEL_B.id]
        assert world.chats == [] and world.embeddings == [] and world.chat_requests == []

        report = registry.refresh()
        assert report.refreshed_provider_ids == ("pa", "pb") and report.persisted is True
        after_view = registry.view()
        assert after_view.active_model_id == MODEL_A.id
        assert after_view.revision == before_view.revision + 1
        assert after_view.selection_revision == before_view.selection_revision
        assert after_view.models == before_view.models == (MODEL_A, MODEL_B)
        after_bytes = world.settings_path.read_bytes()
        assert after_bytes != world.document
    finally:
        gate.release.set()
        thread.join(_BOUND)
    assert not thread.is_alive() and fired.is_set()
    assert fire_errors == [] and gate.waits == [True]

    assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())
    run_id = _assert_admitted_at_entry(world, MODEL_B.id)
    assert registry.scheduled_errors == []
    (pin,) = world.factory.pins
    assert pin.model == MODEL_B and pin.catalog_revision == before_view.revision
    assert world.fire_resolved == ["pb"] and world.other_resolved == ["pa", "pb"]
    assert discovery.calls == [("pa", "MainThread"), ("pb", "MainThread")]
    (request,) = world.chat_requests
    assert json.loads(request.content)["model"] == MODEL_B.upstream_id
    assert _registry_warnings(caplog) == []

    (record,) = world.records()
    assert record["run_id"] == run_id and record["status"] == "done" and record["success"] is True
    assert record["model"] == MODEL_B.id
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    assert registry.view() == after_view
    assert world.settings_path.read_bytes() == after_bytes


def test_running_scheduled_run_cancelled_during_inflight_request(make_world) -> None:
    """Operator cancel of an admitted, running scheduled run while its request is on the wire.

    State axis: the run-level registry/execution-record status ``cancelled``
    (supervisor terminal path), not an ``outcome_utils`` execution stamp. The
    gated response is still delivered after release, so the pinned client
    charges it to canonical B (openai_provider tracks usage after a delivered
    response; the only client cancel check precedes the send).
    """
    gate = _FireGate()
    world = make_world(select=True, job_model=MODEL_B.id, chat_gate=gate)

    world.sched._run_job(TAG)
    try:
        assert gate.entered.wait(_BOUND), ("request never reached the wire", world.records())
        (runner,) = world.factory.runners
        record = get_registry().get(runner.agent_id)
        assert record is not None and record.status == "running"
        (pin,) = world.factory.pins
        assert pin.model == MODEL_B and world.resolved == ["pb"]
        assert len(world.chat_requests) == 1 and world.records() == []
        assert TAG in world.sched._running_jobs
        record.cancel()
        assert record.is_cancelled and runner.is_cancelled
    finally:
        gate.release.set()
    assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())
    assert gate.waits == [True]

    run_id = _assert_admitted_at_entry(world, MODEL_B.id)
    (request,) = world.chat_requests
    assert str(request.url) == f"{URLS['pb']}/chat/completions"
    assert world.resolved == ["pb"] and world.factory.pins == [pin]

    (terminal,) = world.records()
    assert terminal["run_id"] == run_id and terminal["status"] == "cancelled", terminal
    assert terminal["result"] == "[Cancelled]" and terminal["success"] is False
    assert terminal["model"] == MODEL_B.id
    assert world.sched.notify.call_count == 0, "notify=false run was notified"
    assert [(r, n) for r, _t, n in world.sched.released] == [(run_id, 1)]

    usage = world.usage.get_today()
    assert usage == {MODEL_B.id: {"prompt": 3, "completion": 2, "total": 5}}, usage

    assert get_registry().get(runner.agent_id) is None
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    (client,) = world.chats
    # One close from record.cancel() (close_http) and one from runner cleanup.
    assert client.close_count == 2 and client._closed and client._http.is_closed
    (embedding,) = world.embeddings
    assert embedding.close_count == 1
    assert not world.embed_http[0].is_closed, "borrowed embedding HTTP closed by runner"
    assert world.embed_requests == []
    assert world.registry.view().active_model_id == MODEL_A.id
    assert world.settings_path.read_bytes() == world.document


def test_agents_cancel_managed_landing_as_admitted_run_registers_cancels_before_any_request(
    make_world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/agents cancel managed`` (``cancel_all_managed``) once the run is addressable.

    Operator cancellation of a scheduled run targets registry records
    (``/agents cancel``; ``/stop`` also performs this ``cancel_all_managed``
    sweep). Records exist only once admission returns its runner. A cancel
    landing at that instant ends the run as ``cancelled`` with no provider
    request, clients closed, and tracking released.
    """
    import sub_agent_registry

    real_register = sub_agent_registry.register_run
    cancelled: list[int] = []

    def register_then_stop(*args: object, **kwargs: object):
        record = real_register(*args, **kwargs)
        cancelled.append(get_registry().cancel_all_managed())
        return record

    monkeypatch.setattr(sub_agent_registry, "register_run", register_then_stop)
    world = make_world(select=True, job_model=MODEL_B.id)

    world.sched._run_job(TAG)
    assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())

    run_id = _assert_admitted_at_entry(world, MODEL_B.id)
    assert cancelled == [1]
    assert world.resolved == ["pb"] and world.chat_requests == []
    (terminal,) = world.records()
    assert terminal["run_id"] == run_id and terminal["status"] == "cancelled", terminal
    assert terminal["result"] == "[Cancelled]" and terminal["success"] is False
    assert terminal["model"] == MODEL_B.id
    assert world.sched.notify.call_count == 0, "notify=false run was notified"
    assert [(r, n) for r, _t, n in world.sched.released] == [(run_id, 1)]
    assert world.usage.get_today() == {}
    (runner,) = world.factory.runners
    assert get_registry().get(runner.agent_id) is None
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    (client,) = world.chats
    assert client._closed
    (embedding,) = world.embeddings
    assert embedding.close_count == 1


def test_shutdown_during_admission_is_a_recorded_launch_failure(make_world) -> None:
    """Executor shutdown while admission resolves credentials on the fire thread.

    The supervisor pool refuses the admitted run, so submission rolls back its
    registration and the scheduler records a failed launch under the minted
    run ID, alerts despite ``notify=false``, and releases tracking.
    """
    gate = _FireGate()
    world = make_world(select=True, job_model=MODEL_B.id, gate=gate)
    managed_before = get_registry().count_managed()
    fire_errors: list[BaseException] = []

    def fire() -> None:
        try:
            world.sched._run_job(TAG)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the parent below
            fire_errors.append(exc)

    thread = threading.Thread(target=fire, name=_FIRE_THREAD, daemon=True)
    thread.start()
    try:
        assert gate.entered.wait(_BOUND), ("fire thread never resolved", world.records())
        assert world.records() == [] and TAG in world.sched._running_jobs
        world.executor.shutdown(graceful_timeout=0)
    finally:
        gate.release.set()
        thread.join(_BOUND)
    assert not thread.is_alive() and fire_errors == [] and gate.waits == [True]

    run_id = _assert_admitted_at_entry(world, MODEL_B.id)
    assert world.chat_requests == []
    (record,) = world.records()
    assert record["run_id"] == run_id and record["status"] == "failed"
    assert record["success"] is False and record["model"] == MODEL_B.id
    alerts = [c.args[0] for c in world.sched.notify.call_args_list]
    assert len(alerts) == 1 and TAG in alerts[0] and "failed to spawn" in alerts[0]
    for text in (record["result"], alerts[0]):
        assert "secret-" not in text and "env:KEY_" not in text
    assert [(r, n) for r, _t, n in world.sched.released] == [(run_id, 1)]
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    assert get_registry().count_managed() == managed_before


def test_run_now_of_agentic_legacy_json_job_uses_normal_admission(make_world) -> None:
    """A JSON-only agentic job's ``run_now`` takes the same scheduled lifecycle.

    The legacy file is read non-destructively; its raw selector reaches one
    ``admit_scheduled``, the run finishes on that pin, its terminal record
    carries the minted run ID, tracking is released, and the file is untouched.
    """
    legacy_tag = "legacy_agentic"
    world = make_world(select=True, job_model=None)
    legacy_bytes = (json.dumps({legacy_tag: {
        "schedule_type": "cron", "cron": "0 5 1 1 *", "task": "legacy report",
        "enabled": True, "notify": False, "model": MODEL_B.id,
    }}, indent=2) + "\n").encode("utf-8")
    world.paths.scheduler_jobs.write_bytes(legacy_bytes)
    assert "error" not in world.sched.reload()
    assert "legacy, read-only" in world.sched.show_job(legacy_tag)["schedule"]

    assert world.sched.run_now(legacy_tag) == {"success": True}
    assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())

    assert world.registry.scheduled_calls == [MODEL_B.id]
    assert world.registry.ordinary_calls == 0
    (call,) = world.factory.calls
    assert call["profile"] is RuntimeProfile.SCHEDULED_AGENT
    assert call["kwargs"]["model"] == MODEL_B.id and call["kwargs"]["label"] == legacy_tag
    (pin,) = world.factory.pins
    assert pin.model == MODEL_B and world.resolved == ["pb"]
    (request,) = world.chat_requests
    assert str(request.url) == f"{URLS['pb']}/chat/completions"
    (record,) = [e for e in world.sched.execution_log.read_recent() if e["tag"] == legacy_tag]
    assert _RUN_ID_RE.match(record["run_id"])
    assert record["status"] == "done" and record["result"] == RESULT
    assert record["model"] == MODEL_B.id
    assert [r for r, _t, _n in world.sched.released] == [record["run_id"]]
    assert legacy_tag not in world.sched._running_jobs
    assert not world.sched._active_runs.get(legacy_tag)
    assert world.sched.show_job(legacy_tag)["last_run"] is not None
    assert world.paths.scheduler_jobs.read_bytes() == legacy_bytes
    assert world.registry.view().active_model_id == MODEL_A.id


# ---------------------------------------------------------------------------
# Scheduled tool dispositions through the real ReAct → ScheduledToolDispatch →
# shared BuiltinExecutor.execute path: executed (allowed read) and refused
# (noninteractive scheduled_approval_denied). Interactive deferred/cancelled
# dispositions and the scheduled attempt-signal association are covered by the
# existing real-executor suites (tests/test_execution_disposition.py,
# tests/test_scheduler_attempt_evidence.py, tests/test_subagent_confirm_grants.py)
# and are not repeated here. Dispositions are internal markers; they are read
# from the shared executor's return value, never from provider wire messages.
# ---------------------------------------------------------------------------


def _native_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    """One OpenAI-compatible native tool-call assistant message (provider wire shape)."""
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def _run_tool_job(world: _World, monkeypatch: pytest.MonkeyPatch, call_id: str) -> tuple:
    """Fire the job and assert the shared two-turn completion; return the one observed execute.

    Instrumentation only: the observer calls the real bound ``execute`` with the
    same arguments and returns its result object unchanged, recording
    ``read_execution`` of scheduled dispatches. The operator prompt callback and
    confirmation staging are spied (staging wraps the real method).
    """
    real_execute = world.executor.execute
    seen: list[tuple[str, str, dict]] = []

    def observed(tool_name, args=None, *a, scheduled=False, **k):
        result = real_execute(tool_name, args, *a, scheduled=scheduled, **k)
        if scheduled:
            seen.append((tool_name, read_execution(result), result))
        return result

    monkeypatch.setattr(world.executor, "execute", observed)
    prompt = world.executor._subagent_confirm_prompt_fn = MagicMock(name="operator_prompt")
    pending = world.executor._pending_confirmations
    with patch.object(pending, "stage", wraps=pending.stage) as stage:
        world.sched._run_job(TAG)
        assert world.sched.released_event.wait(_BOUND), ("run never released", world.records())
    stage.assert_not_called()
    prompt.assert_not_called()

    run_id = _assert_admitted_at_entry(world, MODEL_B.id)
    (pin,) = world.factory.pins
    assert pin.model == MODEL_B and world.resolved == ["pb"]
    assert len(world.chat_requests) == 2 and world.chat_script == []
    for request in world.chat_requests:
        assert str(request.url) == f"{URLS['pb']}/chat/completions"
        assert json.loads(request.content)["model"] == MODEL_B.upstream_id
    follow_up = json.loads(world.chat_requests[1].content)["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == call_id for m in follow_up)

    (record,) = world.records()
    assert record["run_id"] == run_id and record["status"] == "done", record
    assert record["success"] is True and record["result"] == RESULT
    assert record["model"] == MODEL_B.id
    assert world.sched.notify.call_count == 0, "notify=false run was notified"
    assert [(r, n) for r, _t, n in world.sched.released] == [(run_id, 1)]
    assert world.usage.get_today() == {MODEL_B.id: {"prompt": 6, "completion": 4, "total": 10}}

    (runner,) = world.factory.runners
    assert get_registry().get(runner.agent_id) is None
    assert TAG not in world.sched._running_jobs and not world.sched._active_runs.get(TAG)
    (client,) = world.chats
    assert client.close_count == 1 and client._closed
    (embedding,) = world.embeddings
    assert embedding.close_count == 1 and not world.embed_http[0].is_closed
    assert world.registry.view().active_model_id == MODEL_A.id
    assert world.settings_path.read_bytes() == world.document
    (one,) = seen
    return one


def test_scheduled_run_executes_allowed_native_file_read(
    make_world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    probe = tmp_path / "policy" / "workspace" / "probe.txt"
    world = make_world(select=True, job_model=MODEL_B.id, policy=True, chat_script=[
        _native_tool_call("call-read", "file_read", {"path": str(probe)}),
    ])
    real_probe = os.path.realpath(probe)
    assert real_probe.startswith(os.path.realpath(tmp_path) + os.sep)
    assert os.path.dirname(real_probe) == world.policy.workspace_dir
    assert world.policy.classify(real_probe, "read") == (PathVerdict.ALLOWED, "rw")
    probe.write_text("probe-body-7c1e\n", encoding="utf-8")

    tool_name, disposition, result = _run_tool_job(world, monkeypatch, "call-read")

    assert tool_name == "file_read" and disposition == "executed"
    assert result["success"] is True and "probe-body-7c1e" in result["output"]
    assert not result.get("requires_confirmation") and "token" not in result


def test_scheduled_run_refuses_unrecognised_native_file_write(
    make_world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    target = tmp_path / "policy" / "outside" / "sensitive.txt"
    world = make_world(select=True, job_model=MODEL_B.id, policy=True, chat_script=[
        _native_tool_call("call-write", "file_write",
                          {"path": str(target), "content": "must not land"}),
    ])
    real_target = os.path.realpath(target)
    assert real_target.startswith(os.path.realpath(tmp_path) + os.sep)
    assert world.policy.classify(real_target, "write") == (PathVerdict.UNRECOGNISED, "")

    tool_name, disposition, result = _run_tool_job(world, monkeypatch, "call-write")

    assert tool_name == "file_write" and disposition == "refused"
    assert result["success"] is False
    assert result["error_type"] == "scheduled_approval_denied"
    assert not result.get("requires_confirmation") and "token" not in result
    assert not target.exists() and not target.parent.exists()
