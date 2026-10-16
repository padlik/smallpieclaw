"""Conversation selection guard: a first request that no longer fits after a
manual switch to a smaller model is rejected before any action or summary I/O."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

import react_loop
from agent_controller import AgentController
from builtin_executor import PendingConfirmations
from checkpoint_store import CheckpointStore
from call_attempt_state import CallAttemptState
from config_schema import (
    AgentConfig,
    ExecutorPaths,
    ResolvedProviderConnection,
)
from confirmation import GrantLifetime
from llm_client import LLMClient, LLMPermanentError
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    ProviderState,
)
from model_selection_guard import (
    GUARD_PREPARATION_FAILED_MESSAGE,
    GUARD_REJECTION_MESSAGE,
    ConversationSelectionGuard,
    SelectionBudgetIdentity,
)
from model_settings import ModelSettings
from react_loop import _outcome_guard_eligible
from tests.execution_harness import _NullToolIndex
from tests.run_model_fixtures import make_run_model_client

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
        raise AssertionError("no discovery in guard tests")


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
        self.outcome = {"success": True, "output": "ok", "error": ""}
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
                    sources=tuple(
                        (name, "provider")
                        for name in ("context_tokens", "output_tokens", "vision", "tool_calling")
                    ),
                ),
            )
            for name, ctx in metas.items()
        )
        self.settings_path = tmp_path / "models-config.toml"
        self.settings = ModelSettings(self.settings_path)
        self.settings.write(
            CatalogDocument(
                1, 2, "p/A",
                (ProviderState("p", "openai", "2026-09-01T00:00:00Z", "fresh"),),
                models, (),
            )
        )
        self.registry = ModelRegistry(
            {"p": provider}, self.settings, _Discovery(),
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

    def switch(self, name: str) -> LLMClient:
        """Persist a selection, then notify the guard with the admitted pin."""
        self.registry.select(f"p/{name}")
        run_model = self.registry.admit()
        assert run_model.model.id == f"p/{name}"
        client = self.client(name)
        # a fresh pin per selection revision
        self.clients.pop(name)
        self.clients[name] = LLMClient(
            run_model,
            agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0),
            http_transport=httpx.MockTransport(self.wires[name]),
        )
        client.close()
        self.controller.notify_manual_selection(self.clients[name].selection_identity())
        return self.clients[name]

    def snapshot(self) -> dict:
        return {
            "settings": self.settings_path.read_bytes(),
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


def _ident(model_id="p/X", rev=1, ceiling=1000, *, assumed=False, adapter="openai"):
    return SelectionBudgetIdentity(
        model_id=model_id, selection_revision=rev, adapter=adapter, input_ceiling=ceiling,
        assumed=assumed, metadata=ModelMetadata(), local_context_tokens=4096,
    )


def _baseline(target, identity=None):
    """Establish an unarmed baseline: a first notification has no comparison origin."""
    guard = target._selection_guard if isinstance(target, AgentController) else target
    identity = identity or _ident("p/A", 1, 100_000)
    guard.notify_manual_selection(identity)
    guard.complete(guard.begin(identity))
    assert not guard.armed


# ---------------------------------------------------------------------------
# Identity from the pinned client (policy-derived, never inferred)
# ---------------------------------------------------------------------------

class TestSelectionIdentity:
    def _client(self, adapter, metadata):
        return make_run_model_client(
            adapter, httpx.MockTransport(lambda r: pytest.fail("no I/O")), metadata=metadata
        )

    def test_optional_reserve_ceiling_and_safe_fields(self):
        client = self._client("openai", ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        try:
            ident = client.selection_identity()
            assert ident.input_ceiling == 7_168 and not ident.assumed
            assert ident.model_id == "test-connection/test-model"
            assert ident.adapter == "openai" and ident.selection_revision == 1
            assert "fake-test-key" not in repr(ident)
        finally:
            client.close()

    def test_required_output_larger_than_context_is_c_minus_one(self):
        client = self._client("anthropic", ModelMetadata(context_tokens=10_000, output_tokens=12_000))
        try:
            assert client.selection_identity().input_ceiling == 9_999
        finally:
            client.close()

    def test_required_unknown_output_is_unavailable(self):
        client = self._client("anthropic", ModelMetadata(context_tokens=10_000))
        try:
            assert client.selection_identity().input_ceiling is None
        finally:
            client.close()

    def test_input_limit_only_is_t_without_output_overlay(self):
        client = self._client("openai", ModelMetadata(input_tokens=5_000, output_tokens=65_536))
        try:
            assert client.selection_identity().input_ceiling == 5_000
        finally:
            client.close()

    def test_no_provider_limits_is_assumed_local_ceiling(self):
        client = self._client("openai", ModelMetadata())
        try:
            ident = client.selection_identity()
            assert ident.assumed and ident.input_ceiling == 3_072
        finally:
            client.close()

    def test_catalog_view_exposes_sanitized_selection_revision(self, world):
        before = world.registry.view().selection_revision
        view = world.registry.select("p/B")
        assert view.selection_revision == before + 1 == world.registry.view().selection_revision


# ---------------------------------------------------------------------------
# Pure transition rules (through the controller's public hooks)
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_smaller_arms_larger_clears(self, world):
        ctl = world.controller
        _baseline(ctl)
        assert not ctl.selection_guard_armed
        ctl.notify_manual_selection(_ident("p/B", 2, 10_000))
        assert ctl.selection_guard_armed
        ctl.notify_manual_selection(_ident("p/A", 3, 100_000))
        assert not ctl.selection_guard_armed

    def test_a_b_c_compares_against_b_and_does_not_rearm_a(self, world):
        ctl = world.controller
        _baseline(ctl)
        ctl.notify_manual_selection(_ident("p/B", 2, 10_000))
        ctl.notify_manual_selection(_ident("p/C", 3, 20_000))
        assert not ctl.selection_guard_armed

    def test_equal_carries_existing_protection(self, world):
        ctl = world.controller
        _baseline(ctl)
        ctl.notify_manual_selection(_ident("p/B", 2, 10_000))
        ctl.notify_manual_selection(_ident("p/B2", 3, 10_000))
        assert ctl.selection_guard_armed
        ctl.notify_manual_selection(_ident("p/C", 4, 20_000))
        ctl.notify_manual_selection(_ident("p/C2", 5, 20_000))
        assert not ctl.selection_guard_armed

    def test_unknown_or_incomparable_arms(self, world):
        ctl = world.controller
        _baseline(ctl)
        ctl.notify_manual_selection(_ident("p/U", 2, None))
        assert ctl.selection_guard_armed
        ctl.notify_manual_selection(_ident("p/A", 3, 100_000))
        assert ctl.selection_guard_armed  # previous ceiling unknown: cannot prove larger

    def test_assumed_larger_cannot_clear(self, world):
        ctl = world.controller
        _baseline(ctl, _ident("p/B", 1, 10_000))
        ctl.notify_manual_selection(_ident("p/N", 2, 20_000, assumed=True))
        assert ctl.selection_guard_armed

    def test_stale_and_out_of_order_revisions_are_ignored(self, world):
        ctl = world.controller
        _baseline(ctl, _ident("p/A", 5, 100_000))
        ctl.notify_manual_selection(_ident("p/B", 7, 10_000))
        assert ctl.selection_guard_armed
        ctl.notify_manual_selection(_ident("p/A", 6, 100_000))  # out of order
        ctl.notify_manual_selection(_ident("p/A", 7, 100_000))  # same revision replay
        assert ctl.selection_guard_armed

    def test_reset_clears_and_invalidates_old_tickets(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        guard.notify_manual_selection(_ident("p/B", 2, 10_000))
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        assert ticket.guarded
        guard.reset()
        assert not guard.armed
        guard.notify_manual_selection(_ident("p/A", 3, 100_000))
        guard.notify_manual_selection(_ident("p/B", 4, 10_000))
        assert guard.armed
        assert not guard.complete(ticket)
        assert guard.armed

    def test_history_restore_unknown_provenance_arms_conservatively(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        guard.notify_history_restored()
        assert guard.armed

    def test_history_restore_with_known_not_larger_origin_does_not_arm(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        guard.notify_history_restored(_ident("p/B", 1, 10_000))
        assert not guard.armed
        guard.notify_history_restored(_ident("p/Z", 1, 900_000))
        assert guard.armed

    def test_same_revision_different_canonical_id_cannot_clear(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        guard.notify_manual_selection(_ident("p/B", 2, 10_000))
        other = guard.begin(_ident("p/C", 2, 20_000))
        assert other.guarded and not guard.complete(other)
        assert guard.armed
        own = guard.begin(_ident("p/B", 2, 10_000))
        assert guard.complete(own) and not guard.armed

    def test_new_switch_after_ticket_invalidates_old_completion(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        guard.notify_manual_selection(_ident("p/B", 2, 10_000))
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        guard.notify_manual_selection(_ident("p/D", 3, 5_000))
        assert not guard.complete(ticket) and guard.armed

    def test_unarmed_ticket_is_not_guarded(self):
        guard = ConversationSelectionGuard()
        _baseline(guard)
        assert not guard.begin(_ident("p/A", 1, 100_000)).guarded

    def test_failed_persistence_makes_no_notification(self, world):
        ctl = world.controller
        _baseline(ctl)
        with patch.object(world.settings, "write", side_effect=OSError("disk")):
            with pytest.raises(ModelRegistryError):
                world.registry.select("p/B")
        # the caller notifies only after select() returned
        assert not ctl.selection_guard_armed


# ---------------------------------------------------------------------------
# Eligibility of dispatch outcomes for the one-shot accepted callback
# ---------------------------------------------------------------------------

class TestOutcomeEligibility:
    @pytest.mark.parametrize(
        "outcome, eligible",
        [
            ({"success": True, "output": "ok"}, True),
            ({"success": False, "error": "boom", "error_type": "tool_timeout"}, True),
            ({"success": False, "error": "x", "requires_confirmation": True, "token": "t"}, False),
            ({"success": False, "error": "x", "vision_reexecute": True}, False),
            ({"success": False, "error": "c", "_operator_cancelled": True}, False),
            ({"success": False, "error": "p", "error_type": "prohibited_path"}, False),
            ({"success": False, "error": "s", "error_type": "scheduled_approval_denied"}, False),
            ({"success": False, "error": "u", "_guard_refusal": True}, False),
            ({"success": False, "error": "c", "error_type": "confirmation_failed"}, False),
        ],
    )
    def test_outcomes(self, outcome, eligible):
        assert _outcome_guard_eligible(outcome) is eligible


# ---------------------------------------------------------------------------
# Controller integration: rejection leaves everything untouched
# ---------------------------------------------------------------------------

def _prime_state(world):
    world.controller._confirmation.grant_ledger.add("file_write", "/tmp", GrantLifetime.SESSION)
    world.executor._pending_confirmations.stage("tok-1", "file_write", {"path": "/tmp/x"})
    ckpt = {
        "format_version": 2, "trace_id": "r-keep0001", "user_goal": "keep", "messages": [{"role": "user", "content": "keep"}],
        "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0, "model_id": "p/A",
        "attempt_state": CallAttemptState().to_dict(),
        "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
        "created_at": "2026-08-21T14:30:00Z",
    }
    owner = world.store.acquire_owner("r-keep0001")
    world.store.save("r-keep0001", ckpt, owner=owner)
    owner.release()


class TestGuardedRejection:
    def test_reject_before_any_http_or_mutation(self, world, tmp_path):
        _prime_state(world)
        image = tmp_path / "image-a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        client_b = world.switch("B")
        assert world.controller.selection_guard_armed
        before = world.snapshot()
        history_objects = copy.deepcopy(world.short_term.get_messages())
        images = [str(image)]
        images_before = list(images)

        with patch("react_loop.maybe_compact", side_effect=AssertionError("compactor")), \
             patch.object(react_loop, "_summarize_on_pin", side_effect=AssertionError("summary")):
            result = world.controller.run("next question", images=images, admitted_llm=client_b)

        assert result == GUARD_REJECTION_MESSAGE
        assert "/models" in result and "/reset" in result
        assert world.wires["B"].requests == []
        assert all(not w.requests for w in world.wires.values())
        assert world.executor.calls == []
        assert world.memory.events == []
        world.graph_writer.enqueue.assert_not_called()
        assert world.executor._supervisor.registered == []
        assert world.executor._supervisor.unregistered == []
        assert world.executor._supervisor.drains == 0
        assert "tok-1" in world.executor._pending_confirmations._pending
        assert world.controller._confirmation.grant_ledger.check("file_write", "/tmp")
        assert world.executor._coordinator is None
        after = world.snapshot()
        assert after == before
        assert world.short_term.get_messages() == history_objects
        assert images == images_before
        assert world.working.goal == "previous goal"
        assert world.controller.selection_guard_armed

    def test_reject_repeats_until_larger_model_or_reset(self, world):
        client_b = world.switch("B")
        assert world.controller.run("q", admitted_llm=client_b) == GUARD_REJECTION_MESSAGE
        assert world.controller.run("q", admitted_llm=client_b) == GUARD_REJECTION_MESSAGE
        world.controller.reset_task(save=False)
        assert not world.controller.selection_guard_armed

    def test_larger_selection_clears_and_runs_normally(self, world):
        world.switch("B")
        client_c = world.switch("C")
        assert not world.controller.selection_guard_armed
        assert world.controller.run("q", admitted_llm=client_c) == "done"
        assert len(world.wires["C"].requests) >= 1

    def test_reject_with_checkpoint_resume_keeps_checkpoint(self, world):
        client_b = world.switch("B")
        ckpt = {
            "format_version": 2, "trace_id": "r-resume01", "user_goal": "alpha beta " * 9000,
            "messages": [{"role": "user", "content": "alpha beta " * 9000}],
            "step": 1, "goal_idx": 0, "max_steps": 8, "json_fail_streak": 0,
            "model_id": client_b.llm_cfg["id"],
            "attempt_state": CallAttemptState().to_dict(),
            "error_info": {"type": "timeout", "message": "t", "retryable": True, "detail": ""},
            "created_at": "2026-08-21T14:30:00Z",
        }
        owner = world.store.acquire_owner("r-resume01")
        world.store.save("r-resume01", ckpt, owner=owner)
        owner.release()
        before = world.snapshot()
        result = world.controller.run("ignored", resume_from="r-resume01", admitted_llm=client_b)
        assert result == GUARD_REJECTION_MESSAGE
        assert world.wires["B"].requests == []
        assert world.snapshot() == before
        owner = world.store.acquire_owner("r-resume01")
        try:
            assert world.store.load("r-resume01", owner=owner)["messages"] == ckpt["messages"]
        finally:
            owner.release()

    def test_unguarded_oversize_still_compacts(self, world):
        # no switch: guard unarmed, A pinned but history sized for a small model
        small = world.registry.admit("p/B")
        client = LLMClient(
            small, agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0),
            http_transport=httpx.MockTransport(world.wires.setdefault("Bu", _Wire())),
        )
        try:
            # legitimately known provenance: this very pin was notified and used before
            _baseline(world.controller, client.selection_identity())
            assert not world.controller.selection_guard_armed
            result = world.controller.run("q", admitted_llm=client)
            assert result == "done"
            assert any("Summarize these" in r.content.decode() for r in world.wires["Bu"].requests)
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Controller integration: fitting requests activate and clear only on use
# ---------------------------------------------------------------------------

class TestGuardedFit:
    def _arm_fitting(self, w):
        client = w.switch("B")
        assert w.controller.selection_guard_armed
        return client

    def test_fitting_usable_finish_activates_and_clears(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        assert w.controller.run("new goal", admitted_llm=client) == "done"
        assert not w.controller.selection_guard_armed
        assert w.working.goal != "previous goal"
        assert w.memory.events and w.memory.events[0].startswith("User request: new goal")
        w.graph_writer.enqueue.assert_called_once()
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered != []
        assert w.executor._coordinator is None  # balanced cleanup after activation

    def test_run_local_client_is_wire_client_and_pin_unchanged(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        pinned = w.controller.llm
        w.controller.run("new goal", admitted_llm=client)
        assert w.controller.llm is pinned
        assert w.wires["B"].requests and not w.wires["A"].requests

    def test_staged_system_and_tool_defs_built_once_and_transmitted(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        real_system = react_loop._build_system_prompt
        real_defs = react_loop.build_tool_definitions
        with patch.object(react_loop, "_build_system_prompt", wraps=real_system) as sys_spy, \
             patch.object(react_loop, "build_tool_definitions", wraps=real_defs) as defs_spy:
            w.controller.run("new goal", admitted_llm=client)
        assert sys_spy.call_count == 1
        assert defs_spy.call_count == 1
        sent = json.loads(w.wires["B"].main_requests()[0].content)
        assert len(sent["tools"]) >= 24  # 23 built-ins plus plan
        staged = defs_spy.spy_return if hasattr(defs_spy, "spy_return") else None
        if staged is not None:
            assert sent["tools"] == staged

    @staticmethod
    def _armed_at_second_request(w, client, replies, *, before=None) -> bool:
        """Guard state observed when the second request is sent (after step one)."""
        seen: list[bool] = []

        def on_request(n: int) -> None:
            if n == 2:
                seen.append(w.controller.selection_guard_armed)
                if before is not None:
                    before()

        w.wires["B"].replies = list(replies)
        w.wires["B"].on_request = on_request
        w.controller.run("new goal", admitted_llm=client)
        assert seen, "second request was never sent"
        return seen[0]

    def test_finish_veto_does_not_clear(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        w.executor._supervisor.unaccounted = [("agent-1", "running")]
        armed = self._armed_at_second_request(
            w, client, [FINISH], before=lambda: w.executor._supervisor.unaccounted.clear()
        )
        assert armed

    def test_unknown_tool_does_not_clear(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        assert self._armed_at_second_request(w, client, [UNKNOWN_TOOL])

    def test_local_vision_refusal_does_not_clear(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        assert self._armed_at_second_request(
            w, client, ['{"action":"tool","tool":"vision_query","args":{}}']
        )

    def test_ordinary_tool_failure_is_eligible(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        w.executor.outcome = {"success": False, "output": "", "error": "boom"}
        assert not self._armed_at_second_request(w, client, [TOOL_FAIL])

    def test_operator_cancel_during_tool_does_not_clear(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        w.wires["B"].replies = [TOOL_FAIL]
        w.executor.on_execute = w.controller.cancel
        w.controller.run("new goal", admitted_llm=client)
        assert w.controller.selection_guard_armed

    def test_accepted_callback_is_one_shot(self, small_world):
        w = small_world
        client = self._arm_fitting(w)
        w.wires["B"].replies = [TOOL_FAIL, TOOL_FAIL, FINISH]
        with patch.object(w.controller._selection_guard, "complete", wraps=w.controller._selection_guard.complete) as spy:
            w.controller.run("new goal", admitted_llm=client)
        assert spy.call_count == 1

    def test_same_revision_other_model_success_cannot_clear(self, small_world):
        w = small_world
        client_b = self._arm_fitting(w)
        other = w.client("C")  # admitted at the same global selection revision as B
        assert other.llm_cfg["selection_revision"] == client_b.llm_cfg["selection_revision"]
        w.controller.run("goal", admitted_llm=other)
        assert w.controller.selection_guard_armed
        w.controller.run("goal", admitted_llm=client_b)
        assert not w.controller.selection_guard_armed

    def test_run_started_before_switch_keeps_its_own_client(self, small_world):
        w = small_world
        client_a = w.client("A")
        gate_in, gate_out = threading.Event(), threading.Event()
        calls = {"n": 0}

        def hold(n):
            calls["n"] = n
            if n == 1:
                gate_in.set()
                assert gate_out.wait(5)

        w.wires["A"].on_request = hold
        w.wires["A"].replies = [TOOL_FAIL, FINISH]
        results: list[str] = []
        t = threading.Thread(
            target=lambda: results.append(w.controller.run("long run", admitted_llm=client_a))
        )
        t.start()
        assert gate_in.wait(5)
        client_b = w.switch("B")
        assert w.controller.run("other", admitted_llm=client_b) == "done"
        gate_out.set()
        t.join(10)
        assert results == ["done"]
        assert len(w.wires["A"].requests) == 2 and w.wires["B"].requests
        assert w.controller.llm is not client_b


class TestAnthropicSelectedJson:
    def test_json_selected_request_is_staged_without_native_count(self, tmp_path):
        w = _World(tmp_path, big_history=False)
        try:
            provider = _FakeProviderConfig("q", "anthropic", "env:K", "https://anthropic.example.test")
            w.registry.replace_connections({"p": w.registry._connections["p"], "q": provider})
            wire = _Wire()
            client = make_run_model_client(
                "anthropic", httpx.MockTransport(wire),
                metadata=ModelMetadata(context_tokens=60_000, output_tokens=500),
            )
            w.controller.notify_manual_selection(_ident("p/Big", 0, 500_000))
            w.controller.notify_manual_selection(client.selection_identity())
            assert w.controller.selection_guard_armed
            assert w.controller.run("goal", admitted_llm=client) == "done"
            body = json.loads(wire.requests[0].content)
            assert "tools" not in body
            assert not w.controller.selection_guard_armed
            client.close()
        finally:
            w.close()

    def test_parse_failure_does_not_clear(self, tmp_path):
        w = _World(tmp_path, big_history=False)
        wire = _Wire()
        client = make_run_model_client(
            "anthropic", httpx.MockTransport(wire),
            metadata=ModelMetadata(context_tokens=60_000, output_tokens=500),
        )
        try:
            w.controller.notify_manual_selection(_ident("p/Big", 0, 500_000))
            w.controller.notify_manual_selection(client.selection_identity())
            seen: list[bool] = []

            def on_request(n: int) -> None:
                if n == 2:
                    seen.append(w.controller.selection_guard_armed)

            wire.replies = [NON_JSON]
            wire.on_request = on_request
            wire.default = FINISH
            w.controller.run("goal", admitted_llm=client)
            assert seen == [True]
        finally:
            client.close()
            w.close()


# ---------------------------------------------------------------------------
# Graph preparation (protocol fakes): prepare once, commit on activation
# ---------------------------------------------------------------------------

class _Prepared:
    def __init__(self, text: str, log: list[str]) -> None:
        self.text = text
        self._log = log

    def commit_metrics(self) -> None:
        self._log.append("commit")


class _FakeGraph:
    def __init__(self, text: str = "", *, fail_prepare: bool = False, fail_commit: bool = False) -> None:
        self.text = text
        self.log: list[str] = []
        self.fail_prepare = fail_prepare
        self.fail_commit = fail_commit

    def prepare_for_prompt(self, query: str, max_entries: int = 10):
        self.log.append("prepare")
        if self.fail_prepare:
            raise RuntimeError("graph unavailable")
        prepared = _Prepared(self.text, self.log)
        if self.fail_commit:
            prepared.commit_metrics = lambda: (_ for _ in ()).throw(RuntimeError("commit"))
        return prepared

    def format_for_prompt(self, query: str, max_entries: int = 10) -> str:
        raise AssertionError("guarded path must use prepare_for_prompt only")


class TestGraphPreparation:
    def _spy_top_k(self, w, client):
        real = react_loop._build_system_prompt
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return real(**kwargs)

        with patch.object(react_loop, "_build_system_prompt", side_effect=spy):
            w.controller.run("graph goal", admitted_llm=client)
        return seen

    def test_reject_prepares_once_and_never_commits(self, world):
        graph = _FakeGraph("KNOWLEDGE GRAPH CONTEXT: x")
        world.controller._graph_memory = graph
        client = world.switch("B")
        assert world.controller.run("graph goal", admitted_llm=client) == GUARD_REJECTION_MESSAGE
        assert graph.log == ["prepare"]

    def test_fit_commits_once_after_activation(self, small_world):
        w = small_world
        graph = _FakeGraph("KNOWLEDGE GRAPH CONTEXT: x")
        w.controller._graph_memory = graph
        client = w.switch("B")
        seen = self._spy_top_k(w, client)
        assert graph.log == ["prepare", "commit"]
        assert seen["graph_context_section"] == "KNOWLEDGE GRAPH CONTEXT: x"
        assert seen["results_top_k"] == 0

    def test_empty_prepared_text_is_frozen_and_keeps_results_recall(self, small_world):
        w = small_world
        graph = _FakeGraph("")
        w.controller._graph_memory = graph
        client = w.switch("B")
        seen = self._spy_top_k(w, client)
        assert graph.log == ["prepare", "commit"]
        assert seen["graph_context_section"] == ""
        assert seen["results_top_k"] == 2

    def test_prepare_failure_continues_without_recall(self, small_world):
        w = small_world
        graph = _FakeGraph(fail_prepare=True)
        w.controller._graph_memory = graph
        client = w.switch("B")
        assert w.controller.run("graph goal", admitted_llm=client) == "done"
        assert graph.log == ["prepare"]

    def test_commit_failure_continues(self, small_world):
        w = small_world
        graph = _FakeGraph("ctx", fail_commit=True)
        w.controller._graph_memory = graph
        client = w.switch("B")
        assert w.controller.run("graph goal", admitted_llm=client) == "done"

    def test_cancel_before_activation_does_not_commit(self, small_world):
        w = small_world
        graph = _FakeGraph("ctx")
        w.controller._graph_memory = graph
        client = w.switch("B")
        real = graph.prepare_for_prompt

        def prepare_then_cancel(query, max_entries=10):
            prepared = real(query, max_entries)
            w.controller.cancel()
            return prepared

        graph.prepare_for_prompt = prepare_then_cancel
        w.controller.run("graph goal", admitted_llm=client)
        assert graph.log == ["prepare"]
        assert w.controller.selection_guard_armed

    def test_unguarded_path_uses_existing_format_for_prompt(self, small_world):
        w = small_world
        calls: list[str] = []
        graph = SimpleNamespace(
            format_for_prompt=lambda q, max_entries=10: calls.append("format") or "",
            prepare_for_prompt=lambda *a, **k: pytest.fail("unguarded must not prepare"),
        )
        w.controller._graph_memory = graph
        _baseline(w.controller, w.controller.llm.selection_identity())
        assert w.controller.run("graph goal") == "done"
        assert calls == ["format"]


# ---------------------------------------------------------------------------
# Review 1 corrections R1-R5, R7
# ---------------------------------------------------------------------------

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
        "settings": world.settings_path.read_bytes(),
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


class TestUnknownHistoryProtection:
    """R1: injected nonempty history of unknown provenance protects the first request."""

    def test_injected_history_arms_at_construction_without_notification(self, world):
        assert world.controller.selection_guard_armed

    def test_empty_injected_history_does_not_arm(self, tmp_path):
        w = _World(tmp_path, big_history=None)
        try:
            assert not w.controller.selection_guard_armed
        finally:
            w.close()

    def test_undersized_pin_rejects_without_manual_setup_or_compaction(self, world):
        client = world.client("B")
        before = _deferred_state(world)
        with patch("react_loop.maybe_compact", side_effect=AssertionError("compactor")), \
             patch.object(react_loop, "_summarize_on_pin", side_effect=AssertionError("summary")):
            assert world.controller.run("q", admitted_llm=client) == GUARD_REJECTION_MESSAGE
        assert world.wires["B"].requests == []
        assert _deferred_state(world) == before
        assert world.controller.selection_guard_armed

    def test_fitting_accepted_response_after_unknown_restore_clears(self, small_world):
        w = small_world
        assert w.controller.selection_guard_armed
        assert w.controller.run("q", admitted_llm=w.client("B")) == "done"
        assert not w.controller.selection_guard_armed

    def test_unknown_restore_is_not_treated_as_an_evaluated_revision(self, small_world):
        w = small_world
        w.controller.run("q", admitted_llm=w.client("B"))  # binds and clears
        w.controller.notify_history_restored()
        assert w.controller.selection_guard_armed
        # a later real notification is still evaluated against the bound identity
        w.controller.notify_manual_selection(w.client("C").selection_identity())
        assert not w.controller.selection_guard_armed


class TestDelayedNotificationAfterFirstAdmission:
    """A notification older than the bound admitted identity never regresses it."""

    def test_delayed_larger_notification_after_unknown_restore_admission_is_ignored(self):
        guard = ConversationSelectionGuard()
        guard.notify_history_restored()
        first = guard.begin(_ident("p/B", 2, 10_000))
        assert first.guarded
        guard.notify_manual_selection(_ident("p/A", 1, 100_000))  # delayed, older revision
        assert guard.armed
        assert guard.complete(first)
        assert not guard.armed
        assert not guard.begin(_ident("p/B", 2, 10_000)).guarded

    def test_stale_delivery_preserves_identity_and_token_until_completion(self):
        guard = ConversationSelectionGuard()
        guard.notify_history_restored()
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        token_before = guard._token
        guard.notify_manual_selection(_ident("p/A", 1, 100_000))
        assert guard._current.model_id == "p/B" and guard._token == token_before
        assert guard.complete(ticket)

    def test_same_revision_unevaluated_notification_is_still_processed(self):
        guard = ConversationSelectionGuard()
        guard.notify_history_restored()
        guard.begin(_ident("p/B", 2, 10_000))
        guard.notify_manual_selection(_ident("p/B", 2, 20_000))  # same revision, not evaluated
        assert guard._current.input_ceiling == 20_000
        assert guard._evaluated_revision == 2

    def test_controller_delayed_notification_during_first_admission(self, small_world):
        w = small_world
        client = w.client("B")
        rev = client.selection_identity().selection_revision
        log: list[str] = []

        class _Graph(_FakeGraph):
            delayed = True

            def prepare_for_prompt(self, query, max_entries=10):
                log.append("stage")
                if self.delayed:
                    self.delayed = False  # delivered during the first admission
                    w.controller.notify_manual_selection(_ident("p/A", rev - 1, 100_000))
                return _Prepared("", log)

            def format_for_prompt(self, query, max_entries=10):
                log.append("ordinary")
                return ""

        w.controller._graph_memory = _Graph()
        assert w.controller.selection_guard_armed
        assert w.controller.run("q", admitted_llm=client) == "done"
        assert not w.controller.selection_guard_armed
        assert w.controller.run("q2", admitted_llm=client) == "done"
        assert log.count("stage") == 1  # the second run is unguarded
        assert "ordinary" in log


class TestExplicitAdmissionDoesNotBorrowExemption:
    """R2: ticket decisions check the actual canonical ID and revision."""

    def _cleared_at_c(self, w):
        client_b = w.switch("B")
        assert w.controller.run("q", admitted_llm=client_b) == GUARD_REJECTION_MESSAGE
        client_c = w.switch("C")
        assert not w.controller.selection_guard_armed
        return client_c

    def test_explicit_b_checkpoint_resume_at_c_revision_is_rejected_untouched(self, world):
        client_c = self._cleared_at_c(world)
        assert world.controller.run("q", admitted_llm=client_c) == "done"
        goal = "alpha beta " * 9000
        _save_checkpoint(world, "r-bresume01", "p/B", goal)
        run_model = world.registry.admit("p/B")
        assert run_model.selection_revision == client_c.llm_cfg["selection_revision"]
        client_b = LLMClient(
            run_model, agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=3, retry_delay=0),
            http_transport=httpx.MockTransport(world.wires["B"]),
        )
        try:
            before = _ckpt_bytes(world)
            requests_before = len(world.wires["B"].requests)
            with patch("react_loop.maybe_compact", side_effect=AssertionError("compactor")), \
                 patch.object(react_loop, "_summarize_on_pin", side_effect=AssertionError("summary")):
                result = world.controller.run("ignored", resume_from="r-bresume01", admitted_llm=client_b)
            assert result == GUARD_REJECTION_MESSAGE
            assert len(world.wires["B"].requests) == requests_before
            assert _ckpt_bytes(world) == before
            _assert_owner_released(world, "r-bresume01")
            assert not world.controller.selection_guard_armed  # global C state preserved
        finally:
            client_b.close()

    def test_c_admission_keeps_its_larger_model_escape(self, world):
        client_c = self._cleared_at_c(world)
        assert world.controller.run("q", admitted_llm=client_c) == "done"

    def test_guard_level_mismatched_admission_is_guarded_but_cannot_clear(self):
        guard = ConversationSelectionGuard()
        guard.notify_manual_selection(_ident("p/A", 1, 100_000))
        guard.notify_manual_selection(_ident("p/B", 2, 10_000))
        guard.notify_manual_selection(_ident("p/C", 3, 20_000))
        assert not guard.armed
        explicit_b = guard.begin(_ident("p/B", 3, 10_000))
        assert explicit_b.guarded
        assert not guard.complete(explicit_b)
        assert not guard.begin(_ident("p/C", 3, 20_000)).guarded
        assert not guard.begin(_ident("p/A", 3, 100_000)).guarded


class TestHistoryReplacementInvalidatesTickets:
    """R3: every history replacement invalidates outstanding tickets."""

    def _armed(self):
        guard = ConversationSelectionGuard()
        _baseline(guard, _ident("p/A", 1, 100_000))
        guard.notify_manual_selection(_ident("p/B", 2, 10_000))
        assert guard.armed
        return guard

    def test_same_origin_restore_blocks_old_completion(self):
        guard = self._armed()
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        guard.notify_history_restored(_ident("p/B", 2, 10_000))
        assert not guard.complete(ticket)
        assert guard.armed

    def test_known_smaller_origin_restore_blocks_old_completion(self):
        guard = self._armed()
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        guard.notify_history_restored(_ident("p/S", 1, 5_000))
        assert not guard.complete(ticket)
        assert guard.armed

    def test_empty_restore_clears_and_blocks_old_completion(self):
        guard = self._armed()
        ticket = guard.begin(_ident("p/B", 2, 10_000))
        guard.notify_history_restored(empty=True)
        assert not guard.armed
        assert not guard.complete(ticket)

    def test_controller_empty_replacement_clears_protection(self, world):
        client = world.switch("B")
        assert world.controller.selection_guard_armed
        world.short_term.clear()
        world.controller.notify_history_restored()
        assert not world.controller.selection_guard_armed
        assert world.controller.run("q", admitted_llm=client) == "done"

    def test_controller_nonempty_replacement_invalidates_running_ticket(self, small_world):
        w = small_world
        client = w.switch("B")
        gate_in, gate_out = threading.Event(), threading.Event()

        def hold(n: int) -> None:
            if n == 1:
                gate_in.set()
                assert gate_out.wait(5)

        w.wires["B"].on_request = hold
        results: list[str] = []
        t = threading.Thread(target=lambda: results.append(w.controller.run("q", admitted_llm=client)))
        t.start()
        assert gate_in.wait(5)
        w.controller.notify_history_restored(w.client("B").selection_identity())
        gate_out.set()
        t.join(10)
        assert results == ["done"]
        assert w.controller.selection_guard_armed  # the new conversation stays protected


class TestFailClosedPreflight:
    """R4: activation happens only after a successful complete strict preview."""

    def _assert_nothing_started(self, w, before, result) -> None:
        assert result == GUARD_PREPARATION_FAILED_MESSAGE
        assert not any(wire.requests for wire in w.wires.values())
        assert _deferred_state(w) == before
        assert w.executor._supervisor.registered == []
        assert w.controller.selection_guard_armed

    def _fixed_client(self, adapter, wire, metadata):
        return make_run_model_client(adapter, httpx.MockTransport(wire), metadata=metadata)

    def test_optional_output_not_less_than_context_is_permanent_not_overflow(self, small_world):
        w = small_world
        _prime_grants(w)
        client = self._fixed_client(
            "openai", _Wire(), ModelMetadata(context_tokens=8_000, output_tokens=9_000)
        )
        try:
            with pytest.raises(LLMPermanentError):
                client.preview_request([{"role": "user", "content": "x"}], "s", json_mode=True)
            before = _deferred_state(w)
            result = w.controller.run("q", admitted_llm=client)
            self._assert_nothing_started(w, before, result)
        finally:
            client.close()

    def test_required_unknown_output_is_permanent_not_overflow(self, small_world):
        w = small_world
        _prime_grants(w)
        client = self._fixed_client("anthropic", _Wire(), ModelMetadata(context_tokens=60_000))
        try:
            with pytest.raises(LLMPermanentError):
                client.preview_request([{"role": "user", "content": "x"}], "s", json_mode=True)
            before = _deferred_state(w)
            result = w.controller.run("q", admitted_llm=client)
            self._assert_nothing_started(w, before, result)
        finally:
            client.close()

    def test_malformed_resumed_native_group_on_json_selected_pin_fails_closed(self, small_world):
        w = small_world
        wire = _Wire()
        client = self._fixed_client(
            "anthropic", wire, ModelMetadata(context_tokens=60_000, output_tokens=500)
        )
        try:
            call = {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
            messages = [
                {"role": "user", "content": "goal"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "user", "content": "interleaved before the tool result"},
                {"role": "tool", "tool_call_id": "c1", "content": "late result"},
            ]
            _save_checkpoint(w, "r-native001", client.llm_cfg["id"], "goal", messages)
            before = _deferred_state(w)
            result = w.controller.run("ignored", resume_from="r-native001", admitted_llm=client)
            assert result == GUARD_PREPARATION_FAILED_MESSAGE
            assert wire.requests == []
            assert _deferred_state(w) == before
            _assert_owner_released(w, "r-native001")
        finally:
            client.close()

    def test_mcp_schema_build_failure_is_not_silently_dropped(self, small_world, caplog):
        w = small_world
        _prime_grants(w)
        client = w.client("B")
        before = _deferred_state(w)
        with patch.object(react_loop, "build_tool_definitions", side_effect=RuntimeError("sk-secret-token")), \
             caplog.at_level("DEBUG"):
            result = w.controller.run("q", admitted_llm=client)
        self._assert_nothing_started(w, before, result)
        assert "sk-secret-token" not in result
        assert "sk-secret-token" not in caplog.text

    def test_ordinary_unguarded_schema_failure_still_falls_back(self, small_world):
        w = small_world
        _baseline(w.controller, w.controller.llm.selection_identity())
        calls = {"n": 0}
        real = react_loop.build_tool_definitions

        def flaky(mcp_manager=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(mcp_manager=None)

        with patch.object(react_loop, "build_tool_definitions", side_effect=flaky):
            assert w.controller.run("q") == "done"


class TestCancellationBeforeActivation:
    """R5: unactivated cancellation or rejection never retires an admitted checkpoint."""

    def _graph_cancelling(self, w):
        calls: list[str] = []

        def prepare(query, max_entries=10):
            calls.append("prepare")
            w.controller.cancel()
            return _Prepared("", calls)

        return SimpleNamespace(prepare_for_prompt=prepare), calls

    def test_cancel_during_retrieval_preserves_checkpoint_and_state(self, small_world):
        w = small_world
        _prime_grants(w)
        client = w.client("B")
        _save_checkpoint(w, "r-cancel001", client.llm_cfg["id"], "resume goal")
        graph, calls = self._graph_cancelling(w)
        w.controller._graph_memory = graph
        before = _deferred_state(w)
        result = w.controller.run("ignored", resume_from="r-cancel001", admitted_llm=client)
        assert result == "⚠️ Task stopped by operator."
        assert calls == ["prepare"]  # no commit: the staged preparation was abandoned
        assert _deferred_state(w) == before
        assert not w.wires["B"].requests
        _assert_owner_released(w, "r-cancel001")

    def test_cancel_during_retrieval_then_overflow_preserves_checkpoint(self, world):
        client = world.client("B")
        goal = "alpha beta " * 9000
        _save_checkpoint(world, "r-cancel002", client.llm_cfg["id"], goal)
        graph, _calls = self._graph_cancelling(world)
        world.controller._graph_memory = graph
        before = _deferred_state(world)
        result = world.controller.run("ignored", resume_from="r-cancel002", admitted_llm=client)
        assert result == GUARD_REJECTION_MESSAGE
        assert _deferred_state(world) == before
        _assert_owner_released(world, "r-cancel002")

    def test_already_cancelled_external_event_starts_no_retrieval(self, tmp_path):
        event = threading.Event()
        w = _World(tmp_path, big_history=False, cancel_event=event)
        try:
            prepares: list[str] = []
            w.controller._graph_memory = SimpleNamespace(
                prepare_for_prompt=lambda *a, **k: prepares.append("prepare") or _Prepared("", [])
            )
            client = w.client("B")
            event.set()
            before = _deferred_state(w)
            assert w.controller.run("q", admitted_llm=client) == "⚠️ Task stopped by operator."
            assert prepares == []
            assert _deferred_state(w) == before
            assert w.controller.selection_guard_armed
        finally:
            w.close()

    def test_activated_cancel_semantics_are_unchanged(self, small_world):
        w = small_world
        client = w.client("B")
        w.wires["B"].replies = [TOOL_FAIL]
        w.executor.on_execute = w.controller.cancel
        w.controller.run("q", admitted_llm=client)
        assert w.controller.selection_guard_armed
        assert w.executor._supervisor.registered == w.executor._supervisor.unregistered != []


class TestRejectionWording:
    """R7: the message makes no promise about preparation cost or embeddings."""

    def test_rejection_after_embedding_invocation_is_truthful(self, world):
        embeds: list[str] = []

        def prepare(query, max_entries=10):
            embeds.append(query)  # an existing retrieval embedding runs before the guard
            return _Prepared("", [])

        world.controller._graph_memory = SimpleNamespace(prepare_for_prompt=prepare)
        client = world.switch("B")
        result = world.controller.run("graph goal", admitted_llm=client)
        assert embeds == ["graph goal"]
        assert result == GUARD_REJECTION_MESSAGE
        assert "not sent" in result and "history is preserved" in result
        assert "/models" in result and "/reset" in result
        lowered = result.lower()
        assert "nothing was sent or changed" not in lowered
        for promise in ("free", "no cost", "zero", "nothing was"):
            assert promise not in lowered
        assert world.wires["B"].requests == []

    def test_preparation_failure_wording_has_the_same_properties(self):
        lowered = GUARD_PREPARATION_FAILED_MESSAGE.lower()
        assert "not sent" in lowered and "history is preserved" in lowered
        assert "nothing was" not in lowered


# ---------------------------------------------------------------------------
# Real Graph store contract (needs the staged prepare_for_prompt API)
# ---------------------------------------------------------------------------

def _embedder(text: str) -> list[float]:
    return [0.1, 0.2, 0.3, 0.4]


class TestRealGraphContract:
    @pytest.fixture
    def store(self, tmp_path):
        from graph_memory import GraphMemoryStore

        s = GraphMemoryStore(
            db_path=str(tmp_path / "graph_guard"), embedder_fn=_embedder,
            embedding_dim=4, buffer_pool_mb=64,
        )
        yield s
        s.close()

    @staticmethod
    def _counters(store) -> tuple[int, int, int]:
        return (store._retrieval_hits, store._retrieval_misses, store._context_injections)

    def test_rejected_guarded_run_leaves_graph_counters_zero(self, world, store):
        world.controller._graph_memory = store
        client = world.switch("B")
        assert world.controller.run("graph goal", admitted_llm=client) == GUARD_REJECTION_MESSAGE
        assert self._counters(store) == (0, 0, 0)

    def test_fitting_guarded_run_commits_exactly_once(self, small_world, store):
        w = small_world
        store.upsert_entity("Alice", "person", "2026-01-01T00:00:00Z")
        w.controller._graph_memory = store
        client = w.switch("B")
        assert w.controller.run("Alice", admitted_llm=client) == "done"
        hits, misses, injections = self._counters(store)
        assert hits + misses == 1
        assert injections == hits

    def test_prepare_is_read_only_until_commit(self, store):
        before = self._counters(store)
        prepared = store.prepare_for_prompt("anything", max_entries=3)
        assert isinstance(prepared.text, str)
        assert self._counters(store) == before
        prepared.commit_metrics()
        after = self._counters(store)
        assert sum(after) - sum(before) >= 1
