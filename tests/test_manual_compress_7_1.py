"""Task 7.1: manual ``/compress`` keeps its conversation-summary contract.

Real ``AgentController`` + real ``ShortTermMemory``/``WorkingMemory`` + a real
``LLMClient`` on ``httpx.MockTransport``; the real ``cmd_compress`` handler is
bound to the real ``compress_context``. The success path obtains its summary client
from an injected admission factory (task 7.2 contract); independent-admission
ownership is asserted in ``test_manual_compress_7_2``, and failure semantics are
task 7.3 -- neither is asserted here.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import context_manager
from agent_controller import AgentController
from config_schema import (
    AgentConfig,
    ExecutorPaths,
    ProviderConfig,
    ResolvedProviderConnection,
)
from conversation_io import _save_conversation
from llm_client import LLMClient
from memory_store import ShortTermMemory, WorkingMemory
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
)
from tests.model_stores import ModelStores
from telegram_commands import cmd_compress

AGENT = AgentConfig(ctx_max_tokens=4096, max_retries=1, retry_delay=0)
MODEL = ModelRecord("pa/up/a", "pa", "up/a", "a", ModelMetadata(50_000, None, 2048, False, True, None))
SUMMARY = "- sentinel summary bullet"
FIRST, MIDDLE, LAST = "FIRST_SENTINEL", "MIDDLE_SENTINEL", "LAST_SENTINEL"


class _NoDiscovery:
    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run")


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key="synthetic-key",
    )


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.requests: list[httpx.Request] = []
        provider = ProviderConfig("pa", "openai", "env:KEY_pa", "https://a.example.test/v1", 9)
        stores = ModelStores.at(tmp_path)
        stores.cache.write(CatalogDocument(
            1, 2, (ProviderState("pa", "openai", "2026-09-01T00:00:00Z", "fresh"),), (MODEL,),
        ))
        registry = stores.registry({"pa": provider}, _NoDiscovery(), _resolve)
        registry.select(MODEL.id)
        self.registry = registry
        self.clients: list[LLMClient] = []
        self.short_term = ShortTermMemory(max_turns=80)
        self.working = WorkingMemory()
        self.controller = AgentController(
            AGENT, ExecutorPaths(tmp_dir=str(tmp_path / "t")),
            tool_index=object(), memory=object(), llm=None,
            short_term=self.short_term, working=self.working,
            run_client_factory=self._admit_client,
        )

    def _admit_client(self, event: threading.Event) -> LLMClient:
        """Admit the active selection into a fresh owned client (task 7.2 contract)."""
        client = LLMClient(
            self.registry.admit(), agent_config=AGENT, cancel_event=event,
            http_transport=httpx.MockTransport(self._chat),
        )
        self.clients.append(client)
        return client

    def _chat(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": SUMMARY}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    def seed(self, turns: list[tuple[str, str]]) -> None:
        for role, text in turns:
            self.short_term.add(role, text)
        self.working.start_task("goal-7-1")
        self.working.add_step("shell", {"cmd": "ls"})


@pytest.fixture
def rig(tmp_path: Path):
    r = _Rig(tmp_path)
    yield r
    for client in r.clients:
        client.close()


def _trap_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("manual compression must not use the automatic partition")

    monkeypatch.setattr(context_manager, "_partition_compaction", boom)
    monkeypatch.setattr(context_manager, "maybe_compact", boom)


def test_manual_prompt_covers_whole_history_without_automatic_partition(rig, monkeypatch) -> None:
    _trap_automatic(monkeypatch)
    # Three messages: the automatic goal/middle/recent split has no eligible middle here.
    rig.seed([("user", FIRST), ("assistant", MIDDLE), ("user", LAST)])

    msg = rig.controller.compress_context()

    assert len(rig.requests) == 1
    prompt = json.loads(rig.requests[0].content)["messages"][-1]["content"]
    assert prompt.startswith("Summarize this conversation history as concise bullet points.")
    for sentinel in (FIRST, MIDDLE, LAST):
        assert sentinel in prompt
    assert msg.startswith("✅ Context compressed.")


def test_success_publishes_single_entry_and_leaves_working_memory(rig, tmp_path) -> None:
    rig.seed([("user", FIRST), ("assistant", MIDDLE)])
    working_before = copy.deepcopy(rig.working.to_dict())
    summary_text_before = rig.working.to_summary_text()
    started = rig.working._started_at
    steps = rig.working._steps

    rig.controller.compress_context()

    expected = [{"role": "assistant", "content": f"[Compressed context summary]\n{SUMMARY}"}]
    assert rig.short_term.get_messages() == expected
    assert rig.controller.working is rig.working
    assert rig.working.to_summary_text() == summary_text_before
    assert rig.working._steps is steps and rig.working._started_at == started
    assert rig.working.to_dict() == working_before
    path = tmp_path / "conv.json"
    _save_conversation(str(path), rig.short_term)
    reloaded = ShortTermMemory.from_dict(json.loads(path.read_text()))
    assert reloaded.get_messages() == expected


def _command_inputs(rig: _Rig):
    iface = MagicMock()
    iface._is_authorized.return_value = True
    iface._get_agent_lock.return_value = asyncio.Lock()
    iface.agent_compress_fn = rig.controller.compress_context
    iface._safe_edit = AsyncMock()
    iface._offer_pending_deferred_prompt = AsyncMock()
    update = MagicMock(
        effective_user=MagicMock(id=42),
        effective_message=MagicMock(reply_text=AsyncMock(return_value=MagicMock())),
    )
    return iface, update, MagicMock(args=[])


@pytest.mark.asyncio
async def test_busy_command_lock_refuses_before_any_summary_call_or_history_change(rig) -> None:
    rig.seed([("user", FIRST), ("assistant", MIDDLE), ("user", LAST)])
    history = copy.deepcopy(rig.short_term.get_messages())
    summary_text = rig.working.to_summary_text()
    iface, update, ctx = _command_inputs(rig)
    lock = iface._get_agent_lock(42)

    async with lock:
        # A missing guard would wait on the held lock forever; bound it so that fails.
        await asyncio.wait_for(cmd_compress(iface, update, ctx), timeout=5)
        assert lock.locked()  # still owned by the holder, not released by the refusal

    assert "busy" in update.effective_message.reply_text.call_args.args[0].lower()
    assert rig.requests == []
    assert rig.short_term.get_messages() == history
    assert rig.working.to_summary_text() == summary_text
    iface._safe_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_command_runs_real_controller_once_and_releases_lock(rig, monkeypatch) -> None:
    _trap_automatic(monkeypatch)
    rig.seed([("user", FIRST), ("assistant", MIDDLE), ("user", LAST)])
    iface, update, ctx = _command_inputs(rig)

    await cmd_compress(iface, update, ctx)

    assert len(rig.requests) == 1
    assert len(rig.short_term.get_messages()) == 1
    assert iface._safe_edit.await_args.args[1].startswith("✅ Context compressed.")
    assert not iface._get_agent_lock(42).locked()
