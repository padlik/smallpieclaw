"""Ordinary-run owners that survive the retirement of the selection guard (task 10.1).

The conversation selection guard, its tickets and the staged first request are gone, and no
preview, fit estimate or budget check gates a send. The claims below were never about the guard:
they are the ordinary-run behaviors the guard-focused suite happened to exercise (goal/memory/graph
recording and balanced supervisor cleanup after a normal finish, non-fatal graph retrieval failure,
retrieval through ``format_for_prompt``, cancellation before and during a run). They run on the
real controller, loop, registry-pinned client and recording MockTransport.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from agent_controller import AgentController
from builtin_executor import PendingConfirmations
from call_attempt_state import CallAttemptState
from checkpoint_store import CheckpointStore
from config_schema import AgentConfig, ExecutorPaths, ResolvedProviderConnection
from confirmation import GrantLifetime
from llm_client import LLMClient
from memory_store import ResultsMemory, ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
)
from tests.model_stores import ModelStores
from outcome_utils import with_execution
from tests.execution_harness import _NullToolIndex

A_CTX, B_CTX, C_CTX = 400_000, 24_000, 120_000
FINISH = '{"action":"finish","result":"done"}'
TOOL_FAIL = '{"action":"tool","tool":"shell","args":{"command":"x"}}'
UNKNOWN_TOOL = '{"action":"tool","tool":"nope","args":{}}'
NON_JSON = "this is not json"


def _body(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "prompt_tokens": 10, "completion_tokens": 3,
            "input_tokens": 10, "output_tokens": 3,
        },
    }


# ---------------------------------------------------------------------------
# Fakes (structural collaborators; never subclasses of production types)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeProviderConfig:
    name: str
    type: str
    api_key: str
    base_url: str
    request_timeout: int = 5


class _Discovery:
    def fetch(self, connection):  # pragma: no cover - never refreshed here
        raise AssertionError("no discovery in ordinary-run owner tests")


class _Supervisor:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.unaccounted: list[tuple[str, str]] = []
        self.drains = 0

    def register_active_turn(self, turn_id):
        self.registered.append(turn_id)

    def unregister_active_turn(self, turn_id):
        self.unregistered.append(turn_id)

    def unaccounted_children(self, turn_id):
        return list(self.unaccounted)

    def draining_children(self, turn_id):
        return []

    def drain_owned_children(self, turn_id, cancel=False):
        self.drains += 1
        return []

    def mark_result_retrieved(self, *_a):
        pass


class _Executor:
    scheduled = False
    path_policy = None
    conversation_id = "conv"

    def __init__(self) -> None:
        self._coordinator = None
        self._current_prompt_id = None
        self._pending_confirmations = PendingConfirmations()
        self._supervisor = _Supervisor()
        self.calls: list[tuple[str, dict]] = []
        self.outcome = with_execution({"success": True, "output": "ok", "error": ""}, "executed")
        self.on_execute = None

    def is_builtin(self, name: str) -> bool:
        return name != "nope"

    def execute(self, tool_name, args, **_kwargs):
        self.calls.append((tool_name, dict(args)))
        if self.on_execute is not None:
            self.on_execute()
        return dict(self.outcome)


class _Memory:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_event(self, text, *_a, **_k):
        self.events.append(text)

    def as_prompt_text(self, *_a, **_k):
        return ""


class _Wire:
    """One client's recording MockTransport with scripted replies."""

    def __init__(self, replies=None) -> None:
        self.requests: list[httpx.Request] = []
        self.replies = list(replies or [])
        self.on_request = None
        self.default = FINISH

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(len(self.requests))
        body = request.content.decode()
        if "Summarize these" in body:
            return httpx.Response(200, json=_body("- earlier steps summarized"))
        text = self.replies.pop(0) if self.replies else self.default
        return httpx.Response(200, json=_body(text))

    def main_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "Summarize these" not in r.content.decode()]


class _World:
    def __init__(
        self, tmp_path: Path, *, big_history: bool | None = True,
        cancel_event: threading.Event | None = None,
    ) -> None:
        provider = _FakeProviderConfig("p", "openai", "env:K", "https://openai.example.test/v1")
        metas = {"A": A_CTX, "B": B_CTX, "C": C_CTX}
        models = tuple(
            ModelRecord(
                f"p/{name}", "p", name, name,
                ModelMetadata(
                    context_tokens=ctx, output_tokens=100, vision=True, tool_calling=True,
                ),
            )
            for name, ctx in metas.items()
        )
        self.stores = stores = ModelStores.at(tmp_path)
        self.stores.cache.write(
            CatalogDocument(
                1, 2,
                (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
                models,
            )
        )
        stores.preference.write("p/A")
        self.registry = stores.registry(
            {"p": provider}, _Discovery(),
            lambda cfg: ResolvedProviderConnection(
                provider_id=cfg.name, adapter=cfg.type, base_url=cfg.base_url,
                request_timeout=cfg.request_timeout, api_key="fake-test-key",
            ),
        )
        self.registry.restore_selection()
        self.wires: dict[str, _Wire] = {}
        self.clients: dict[str, LLMClient] = {}
        self.short_term = ShortTermMemory(max_turns=80)
        if big_history:
            for i in range(24):
                self.short_term.add("user" if i % 2 == 0 else "assistant", ("alpha beta " * 400) + str(i))
        elif big_history is False:
            self.short_term.add("user", "hello")
            self.short_term.add("assistant", "hi")
        self.working = WorkingMemory()
        self.working.start_task("previous goal")
        self.memory = _Memory()
        self.executor = _Executor()
        self.store = CheckpointStore(str(tmp_path / "ckpt"))
        self.graph_writer = MagicMock()
        self.controller = AgentController(
            AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0),
            ExecutorPaths(
                tmp_dir=str(tmp_path / "tmp"), downloads_dir=str(tmp_path / "dl"),
                workspace_dir=str(tmp_path / "ws"),
            ),
            llm=self.client("A"),
            tool_index=_NullToolIndex(),
            memory=self.memory,
            short_term=self.short_term,
            working=self.working,
            builtin_executor=self.executor,
            checkpoint_store=self.store,
            cancel_event=cancel_event,
        )
        self.controller._graph_memory_writer = self.graph_writer

    def client(self, name: str) -> LLMClient:
        if name not in self.clients:
            wire = _Wire()
            self.wires[name] = wire
            run_model = self.registry.admit(f"p/{name}")
            self.clients[name] = LLMClient(
                run_model,
                agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0),
                http_transport=httpx.MockTransport(wire),
            )
        return self.clients[name]

    def snapshot(self) -> dict:
        return {
            "settings": self.stores.snapshot(),
            "short": json.dumps(self.short_term.to_dict(), sort_keys=True),
            "working": json.dumps(self.working.to_dict(), sort_keys=True),
            "ckpt": sorted(p.name for p in Path(self.store._checkpoint_dir).glob("*")),
        }

    def close(self) -> None:
        for client in self.clients.values():
            client.close()


@pytest.fixture
def world(tmp_path):
    w = _World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def small_world(tmp_path):
    w = _World(tmp_path, big_history=False)
    yield w
    w.close()


def _ckpt_bytes(world) -> dict[str, bytes]:
    root = Path(world.store._checkpoint_dir)
    return {p.name: p.read_bytes() for p in sorted(root.glob("*"))}


def _save_checkpoint(world, trace: str, model_id: str, goal: str, messages=None) -> None:
    ckpt = {
        "format_version": 2, "trace_id": trace, "user_goal": goal,
        "messages": messages if messages is not None else [{"role": "user", "content": goal}],
        "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
        "model_id": model_id, "attempt_state": CallAttemptState().to_dict(),
        "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
        "created_at": "2026-08-21T14:30:00Z",
    }
    owner = world.store.acquire_owner(trace)
    world.store.save(trace, ckpt, owner=owner)
    owner.release()


def _assert_owner_released(world, trace: str) -> None:
    owner = world.store.acquire_owner(trace)
    assert owner is not None
    owner.release()


def _deferred_state(world) -> dict:
    return {
        "events": list(world.memory.events),
        "goal": world.working.goal,
        "short": json.dumps(world.short_term.to_dict(), sort_keys=True),
        "settings": world.stores.snapshot(),
        "ckpt": _ckpt_bytes(world),
        "pending": "tok-1" in world.executor._pending_confirmations._pending,
        "grant": world.controller._confirmation.grant_ledger.check("file_write", "/tmp"),
        "enqueued": world.graph_writer.enqueue.call_count,
        "registered": list(world.executor._supervisor.registered),
        "coordinator": world.executor._coordinator,
    }


def _prime_grants(world) -> None:
    world.controller._confirmation.grant_ledger.add("file_write", "/tmp", GrantLifetime.SESSION)
    world.executor._pending_confirmations.stage("tok-1", "file_write", {"path": "/tmp/x"})


class TestOrdinaryRunOwners:
    def test_ordinary_finish_updates_goal_memory_graph_and_balances_supervisor(self, small_world):
        w = small_world
        client = w.client("B")
        assert w.controller.run("new goal", admitted_llm=client) == "done"
        assert w.working.goal != "previous goal"
        assert w.memory.events and w.memory.events[0].startswith("User request: new goal")
        w.graph_writer.enqueue.assert_called_once()
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered != []
        assert w.executor._coordinator is None  # balanced cleanup after the run

    def test_graph_retrieval_failure_continues_run_without_recall(self, small_world):
        w = small_world
        calls: list[str] = []

        def failing(query, max_entries=10):
            calls.append("format")
            raise RuntimeError("graph unavailable")

        w.controller._graph_memory = SimpleNamespace(format_for_prompt=failing)
        assert w.controller.run("graph goal", admitted_llm=w.client("B")) == "done"
        assert calls == ["format"]
        assert len(w.wires["B"].main_requests()) == 1
        assert "graph unavailable" not in w.wires["B"].main_requests()[0].content.decode()

    def test_ordinary_run_retrieves_graph_context_via_format_for_prompt(self, small_world):
        w = small_world
        calls: list[str] = []
        w.controller._graph_memory = SimpleNamespace(
            format_for_prompt=lambda q, max_entries=10: calls.append("format") or "",
            prepare_for_prompt=lambda *a, **k: pytest.fail("the ordinary run does not stage a preparation"),
        )
        assert w.controller.run("graph goal", admitted_llm=w.client("B")) == "done"
        assert calls == ["format"]
        assert len(w.wires["B"].main_requests()) == 1

    def test_nonempty_graph_context_suppresses_redundant_results_recall(self, small_world, tmp_path):
        w = small_world
        results = ResultsMemory(str(tmp_path / "results.json"))
        results.add_result("graph goal", "PAST RESULT MARKER")
        w.controller.results = results
        w.controller._graph_memory = SimpleNamespace(
            format_for_prompt=lambda query, max_entries=10: "GRAPH CONTEXT MARKER"
        )
        assert w.controller.run("graph goal", admitted_llm=w.client("B")) == "done"
        body = w.wires["B"].main_requests()[0].content.decode()
        assert "GRAPH CONTEXT MARKER" in body
        assert "PAST RESULT MARKER" not in body

    def test_empty_graph_context_keeps_results_recall(self, small_world, tmp_path):
        w = small_world
        results = ResultsMemory(str(tmp_path / "results.json"))
        results.add_result("graph goal", "PAST RESULT MARKER")
        w.controller.results = results
        w.controller._graph_memory = SimpleNamespace(
            format_for_prompt=lambda query, max_entries=10: ""
        )
        assert w.controller.run("graph goal", admitted_llm=w.client("B")) == "done"
        assert "PAST RESULT MARKER" in w.wires["B"].main_requests()[0].content.decode()

    def test_ordinary_run_accounts_for_one_real_graph_retrieval(self, small_world, tmp_path):
        from graph_memory import GraphMemoryStore

        store = GraphMemoryStore(
            db_path=str(tmp_path / "graph_ordinary"),
            embedder_fn=lambda text: [0.1, 0.2, 0.3, 0.4],
            embedding_dim=4,
            buffer_pool_mb=64,
        )
        try:
            store.upsert_entity("Alice", "person", "2026-01-01T00:00:00Z")
            small_world.controller._graph_memory = store
            assert small_world.controller.run("Alice", admitted_llm=small_world.client("B")) == "done"
            hits, misses, injections = (
                store._retrieval_hits, store._retrieval_misses, store._context_injections
            )
            assert hits + misses == 1
            assert injections == hits
        finally:
            store.close()

    def test_cancel_during_retrieval_preserves_checkpoint_and_request_state(self, small_world):
        w = small_world
        _prime_grants(w)
        client = w.client("B")
        _save_checkpoint(w, "r-cancel001", client.llm_cfg["id"], "resume goal")
        saved_checkpoint = _ckpt_bytes(w)
        saved_history = w.short_term.to_dict()
        calls: list[str] = []

        def format_then_cancel(query, max_entries=10):
            calls.append("format")
            w.controller.cancel()
            return ""

        w.controller._graph_memory = SimpleNamespace(format_for_prompt=format_then_cancel)
        result = w.controller.run("ignored", resume_from="r-cancel001", admitted_llm=client)
        assert result == "⚠️ Task stopped by operator."
        assert calls == ["format"]
        assert not w.wires["B"].requests  # cancelled before any provider I/O
        assert _ckpt_bytes(w) == saved_checkpoint
        assert w.short_term.to_dict() == saved_history
        _assert_owner_released(w, "r-cancel001")

    def test_cancel_after_tool_definitions_preserves_admitted_checkpoint(self, small_world, monkeypatch):
        """Cancellation after graph retrieval but before send keeps the original recovery state."""
        import react_loop

        w = small_world
        client = w.client("B")
        _save_checkpoint(w, "r-defcancel", client.llm_cfg["id"], "resume goal")
        saved_checkpoint = _ckpt_bytes(w)
        saved_history = w.short_term.to_dict()
        original_build = react_loop.build_tool_definitions

        def build_then_cancel(*args, **kwargs):
            definitions = original_build(*args, **kwargs)
            w.controller.cancel()
            return definitions

        monkeypatch.setattr(react_loop, "build_tool_definitions", build_then_cancel)
        result = w.controller.run("ignored", resume_from="r-defcancel", admitted_llm=client)
        assert _ckpt_bytes(w) == saved_checkpoint
        assert w.short_term.to_dict() == saved_history
        assert not w.wires["B"].requests
        assert result in ("[Cancelled]", "⚠️ Task stopped by operator.")
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered
        _assert_owner_released(w, "r-defcancel")

    def test_cancel_during_provider_request_preparation_preserves_checkpoint(self, small_world, monkeypatch):
        """A prepared HTTP request is not an outbound attempt or checkpoint retirement."""
        w = small_world
        client = w.client("B")
        _save_checkpoint(w, "r-prepcancel", client.llm_cfg["id"], "resume goal")
        saved_checkpoint = _ckpt_bytes(w)
        saved_history = w.short_term.to_dict()
        original_prepare = client._prepare_request
        prepared: list[httpx.Request] = []

        def prepare_then_cancel(payload, mode):
            request = original_prepare(payload, mode)
            prepared.append(request)
            w.controller.cancel()
            return request

        monkeypatch.setattr(client, "_prepare_request", prepare_then_cancel)
        w.controller.run("ignored", resume_from="r-prepcancel", admitted_llm=client)
        assert prepared
        assert _ckpt_bytes(w) == saved_checkpoint
        assert w.short_term.to_dict() == saved_history
        assert not w.wires["B"].requests
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered
        _assert_owner_released(w, "r-prepcancel")

    def _failing_client_with_saved_checkpoint(self, w, trace, on_request=None):
        client = w.client("B")
        requests: list[httpx.Request] = []

        def reject(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if on_request is not None:
                on_request()
            # A non-transient, retryable (unknown) rejection: no automatic resend.
            return httpx.Response(
                409, json={"error": {"type": "conflict", "message": "conflict"}}
            )

        assert client._http is not None
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(reject))
        _save_checkpoint(w, trace, client.llm_cfg["id"], "resume goal")
        return client, requests

    def test_sent_failure_saves_new_checkpoint_for_this_model_and_releases_owner(self, small_world):
        """The actual send retires the old snapshot; the new failure records this model."""
        w = small_world
        client, requests = self._failing_client_with_saved_checkpoint(w, "r-retrycancel")
        original_checkpoint = _ckpt_bytes(w)
        result = w.controller.run(
            "ignored", resume_from="r-retrycancel", admitted_llm=client,
            checkpoint_recovery_eligible=True,
        )
        assert len(requests) == 1
        assert "Use /resume to continue." in result
        after = _ckpt_bytes(w)
        assert set(after) == set(original_checkpoint) and after != original_checkpoint
        saved = json.loads(after["r-retrycancel.json"])
        assert saved["model_id"] == client.llm_cfg["id"]
        assert saved["error_info"]["retryable"] is True
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered
        _assert_owner_released(w, "r-retrycancel")

    def test_cancel_during_sent_request_creates_no_new_checkpoint(self, small_world):
        """Cancellation wins at failure handling: the retired snapshot is not resurrected."""
        w = small_world
        client, requests = self._failing_client_with_saved_checkpoint(
            w, "r-sentcancel", on_request=w.controller.cancel,
        )
        w.controller.run(
            "ignored", resume_from="r-sentcancel", admitted_llm=client,
            checkpoint_recovery_eligible=True,
        )
        assert len(requests) == 1
        assert _ckpt_bytes(w) == {}
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered
        _assert_owner_released(w, "r-sentcancel")

    def test_already_cancelled_external_event_starts_no_retrieval_and_preserves_checkpoint(self, tmp_path):
        event = threading.Event()
        w = _World(tmp_path, big_history=False, cancel_event=event)
        try:
            retrievals: list[str] = []
            w.controller._graph_memory = SimpleNamespace(
                format_for_prompt=lambda *a, **k: retrievals.append("format") or ""
            )
            client = w.client("B")
            _save_checkpoint(w, "r-precancel", client.llm_cfg["id"], "resume goal")
            saved_checkpoint = _ckpt_bytes(w)
            saved_history = w.short_term.to_dict()
            event.set()
            assert w.controller.run("ignored", resume_from="r-precancel", admitted_llm=client) == "⚠️ Task stopped by operator."
            assert retrievals == []
            assert not w.wires["B"].requests  # no provider I/O for a run cancelled before its first step
            assert _ckpt_bytes(w) == saved_checkpoint
            assert w.short_term.to_dict() == saved_history
            assert w.executor._supervisor.registered == w.executor._supervisor.unregistered
            _assert_owner_released(w, "r-precancel")
        finally:
            w.close()

    def test_in_run_cancel_keeps_balanced_supervisor_cleanup(self, small_world):
        w = small_world
        client = w.client("B")
        w.wires["B"].replies = [TOOL_FAIL]
        w.executor.on_execute = w.controller.cancel
        w.controller.run("q", admitted_llm=client)
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered != []
