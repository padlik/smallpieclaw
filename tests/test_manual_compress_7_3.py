"""Task 7.3: manual ``/compress`` publishes only on validated success.

Real ``AgentController``, root run-client factory, ``ModelRegistry`` and ``LLMClient`` on
``httpx.MockTransport`` (shared scaffolding from the 7.2 module). Failure, empty, invalid
and cancelled summaries leave short-term history and working memory untouched, never
truncate deterministically, never echo provider text, and never disable automatic
compaction of a later run.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import agent_controller
import main
import react_loop as react_loop_module
from conversation_io import _save_conversation as save_conversation
from llm_client import LLMCancelledError, LLMError
from config_schema import ProviderConfig
from memory_store import ShortTermMemory
from model_registry import CatalogDocument, ModelMetadata, ModelRecord, ModelRegistry, ProviderState
from model_settings import ModelSettings
from telegram_commands import cmd_compress
from tests.test_main_run_admission import _Memory, _NullToolIndex
from tests import test_manual_compress_7_2 as base
from tests.test_manual_compress_7_2 import CANARY_KEY, CANARY_URL, HOST_A, _everything_logged
from llm_client import LLMClient

@pytest.fixture
def world(tmp_path, monkeypatch):
    """Real 7.2 scaffolding (root factory, registry, counted MockTransport clients)."""
    w = base._World(tmp_path, monkeypatch)
    yield w
    for client in w.clients:
        LLMClient.close(client)
    w.embed_http.close()


@pytest.fixture
def prod_logging(tmp_path):
    """Real production logging pipeline with no registered secrets (7.2 fixture body)."""
    yield from base.prod_logging.__wrapped__(tmp_path)


BODY_CANARY = f"upstream says {CANARY_KEY} at {CANARY_URL}"


def _assert_failed_unchanged(world, msg: str, before: dict) -> None:
    assert msg.startswith("❌") and "unchanged" in msg.lower()
    assert "deterministic" not in msg.lower() and "truncat" not in msg.lower()
    assert world.state() == before
    assert all(
        "deterministic fallback" not in m["content"] for m in world.short_term.get_messages()
    )


def test_success_publishes_one_stripped_assistant_entry(world) -> None:
    world.seed()
    world.working_before = world.state()["working"]
    world.controller._run_client_factory = _factory_returning(world, "  - kept fact \n\n")

    msg = world.controller.compress_context()

    assert msg.startswith("✅ Context compressed.")
    assert world.short_term.get_messages() == [
        {"role": "assistant", "content": "[Compressed context summary]\n- kept fact"}
    ]
    assert world.state()["working"] == world.working_before


def _factory_returning(world, value):
    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.return_value = value
        return client

    return factory


def test_provider_503_is_terminal_unchanged_and_leak_free(world, prod_logging) -> None:
    world.seed()
    world.status = 503
    before = world.state()

    msg = world.controller.compress_context()

    logged = _everything_logged(*prod_logging)
    _assert_failed_unchanged(world, msg, before)
    assert [t.close_count for t in world.transports] == [1]
    assert "boom" not in msg and "boom" not in logged
    assert "RuntimeError" not in msg and "503" not in msg.split("Reason")[-1]
    assert world.embedding.embed("probe") == [0.5]  # shared embedding stays open (compress scope)


def test_provider_error_body_canary_never_reaches_reply_or_logs(world, prod_logging) -> None:
    world.seed()
    before = world.state()
    world.status = 200

    def handler(request: httpx.Request) -> httpx.Response:
        world.requests.append(request)
        return httpx.Response(503, json={"error": {"message": BODY_CANARY}})

    world._chat = handler  # type: ignore[method-assign]
    msg = world.controller.compress_context()

    logged = _everything_logged(*prod_logging)
    _assert_failed_unchanged(world, msg, before)
    for canary in (CANARY_KEY, CANARY_URL, "private.internal"):
        assert canary not in msg
    assert CANARY_KEY not in logged and CANARY_URL not in logged


def test_real_whitespace_response_fails_via_llm_empty_response_error(world) -> None:
    world.seed()
    before = world.state()

    def blank(request: httpx.Request) -> httpx.Response:
        world.requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "  \n "}}]})

    world._chat = blank  # type: ignore[method-assign]
    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert [t.close_count for t in world.transports] == [1]


@pytest.mark.parametrize("value", [None, {"a": 1}, ["x"], True, 7, b"bytes", object(), "", "   \n\t "])
def test_invalid_or_empty_callback_result_is_controlled_failure(world, value) -> None:
    world.seed()
    before = world.state()
    closed = []

    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.return_value = value
        client.close.side_effect = lambda: closed.append(1)
        return client

    world.controller._run_client_factory = factory
    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert closed == [1]


def test_cancel_after_admission_preserves_history_and_closes(world) -> None:
    world.seed()
    before = world.state()
    original = world.factory

    def cancelling(event: threading.Event):
        binding = original(event)
        event.set()
        return binding

    world.controller._run_client_factory = cancelling
    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert world.requests == [] and [t.close_count for t in world.transports] == [1]


def test_cancel_during_provider_reply_does_not_publish_even_on_success(world) -> None:
    world.seed()
    before = world.state()
    captured: list[threading.Event] = []
    original = world.factory

    def capturing(event: threading.Event):
        captured.append(event)
        return original(event)

    world.controller._run_client_factory = capturing
    world.on_request = lambda: captured[0].set()  # operator cancels while reply is in flight

    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert [t.close_count for t in world.transports] == [1]


def test_llm_cancelled_error_from_client_is_cancel_not_provider_failure(world, prod_logging) -> None:
    world.seed()
    before = world.state()

    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.side_effect = LLMCancelledError(f"cancelled {CANARY_KEY}")
        return client

    world.controller._run_client_factory = factory
    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert "cancel" in msg.lower() and CANARY_KEY not in msg
    assert CANARY_KEY not in _everything_logged(*prod_logging)


def test_reset_task_keeps_deterministic_fallback_separately(world, monkeypatch) -> None:
    world.working.start_task("goal-7-3")
    world.working.add_step("act", {"k": "v" * 400})
    world.status = 503
    world.controller.results = MagicMock()
    saved = MagicMock()
    monkeypatch.setattr(agent_controller, "save_task_outcome", saved)
    working_text = world.working.to_summary_text()

    world.controller.reset_task()

    assert saved.call_args.kwargs["summary"] == working_text[:300]
    assert [t.close_count for t in world.transports] == [1]
    assert world.embedding.embed("probe") == [0.5]  # shared embedding stays open (reset scope)


# ---------------------------------------------------------------------------
# Claim: a failed manual compress never disables a later run's automatic compaction
# ---------------------------------------------------------------------------

MANUAL_PROMPT = "Summarize this conversation history as concise bullet points"
AUTO_PROMPT = "Summarize these intermediate agent steps"
FINISH = '{"action":"finish","result":"done"}'
MIDDLE = "MIDDLE-SENTINEL-7-3"


def _big_history(world) -> list[dict]:
    world.short_term.clear()
    for i in range(8):
        role = "user" if i % 2 == 0 else "assistant"
        tag = f"{MIDDLE}-{i} " if i == 3 else ""
        world.short_term.add(role, f"{tag}history {i} " + "alpha beta " * 400)
    return copy.deepcopy(world.short_term.get_messages())


def _prepare_real_run(world, monkeypatch, *, manual_reply) -> dict[str, list[str]]:
    """Real controller run: real react_loop, real pinned client, scripted HTTP by request kind."""
    monkeypatch.setattr(agent_controller, "react_loop", react_loop_module.react_loop)
    world.controller._agent_cfg = dataclasses.replace(
        world.controller._agent_cfg, compaction_threshold_tokens=300, max_retries=0,
    )
    world.controller.tool_index, world.controller.memory = _NullToolIndex(), _Memory()
    seen: dict[str, list[str]] = {"manual": [], "auto": [], "main": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        world.requests.append(request)
        kind = "manual" if MANUAL_PROMPT in body else "auto" if AUTO_PROMPT in body else "main"
        seen[kind].append(body)
        if kind == "manual":
            return manual_reply(request)
        content = "- auto summarized earlier steps" if kind == "auto" else FINISH
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    world._chat = handler  # type: ignore[method-assign]
    return seen


@pytest.mark.parametrize(
    "manual_reply",
    [
        lambda r: httpx.Response(503, json={"error": {"message": "boom"}}),
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "  \n "}}]}),
    ],
    ids=["provider-503", "whitespace"],
)
def test_failed_manual_then_ordinary_run_still_auto_compacts_original_middle(
    world, monkeypatch, manual_reply,
) -> None:
    seen = _prepare_real_run(world, monkeypatch, manual_reply=manual_reply)
    original = _big_history(world)

    msg = world.controller.compress_context()
    assert msg.startswith("❌") and world.short_term.get_messages() == original
    assert len(seen["manual"]) == 1 and seen["auto"] == [] and seen["main"] == []

    assert world.controller.run("GOAL") == "done"

    assert len(seen["auto"]) == 1 and len(seen["main"]) == 1  # one auto call, one normal step
    assert f"{MIDDLE}-3" in seen["auto"][0]  # the ORIGINAL (unmutated) middle was summarised
    assert "Compacted context" in seen["main"][0] and f"{MIDDLE}-3" not in seen["main"][0]
    assert len(world.clients) == 2 and [t.close_count for t in world.transports] == [1, 1]
    assert {r.url.host for r in world.requests} == {HOST_A}  # same Main pin route


# ---------------------------------------------------------------------------
# Claim: persistence and working memory untouched on every non-success branch
# ---------------------------------------------------------------------------

def _failure_setups(world):
    """Yield (name, arrange) pairs; each arranges one failing/cancelled/invalid branch."""
    def provider():
        world.status = 503

    def blank():
        world.controller._run_client_factory = _factory_returning(world, "  ")

    def non_string():
        world.controller._run_client_factory = _factory_returning(world, 42)

    def cancel():
        original = world.factory

        def cancelling(event):
            binding = original(event)
            event.set()
            return binding

        world.controller._run_client_factory = cancelling

    return [("provider", provider), ("blank", blank), ("non_string", non_string), ("cancel", cancel)]


@pytest.mark.parametrize("branch", ["provider", "blank", "non_string", "cancel"])
def test_non_success_branches_write_nothing_and_keep_object_identity(
    world, monkeypatch, tmp_path, branch,
) -> None:
    saves: list[object] = []
    monkeypatch.setattr(agent_controller, "_save_conversation", lambda *a, **k: saves.append(a))
    world.seed()
    conv = tmp_path / "conversation.json"
    save_conversation(str(conv), world.short_term)
    disk_before = conv.read_bytes()
    working_before, steps_before = world.working, list(world.working.steps)
    buffer_before = world.short_term._buffer
    dict(_failure_setups(world))[branch]()

    msg = world.controller.compress_context()

    assert msg.startswith("❌") and saves == []
    assert world.controller.working is working_before and world.working.steps == steps_before
    assert world.short_term._buffer is buffer_before  # same buffer object, never rebuilt
    save_conversation(str(conv), world.short_term)
    assert conv.read_bytes() == disk_before  # persisted conversation byte-equivalent


def test_history_seen_during_provider_reply_is_still_the_original_and_success_saves_one_entry(
    world, tmp_path,
) -> None:
    world.seed()
    original = copy.deepcopy(world.short_term.get_messages())
    seen: list[list[dict]] = []
    world.on_request = lambda: seen.append(copy.deepcopy(world.short_term.get_messages()))

    msg = world.controller.compress_context()

    assert msg.startswith("✅") and seen == [original]  # not pre-mutated during the call
    conv = tmp_path / "conversation.json"
    save_conversation(str(conv), world.short_term)
    saved = ShortTermMemory.from_dict(json.loads(conv.read_text())).get_messages()
    assert saved == [{"role": "assistant", "content": "[Compressed context summary]\nsummary-" + HOST_A}]


# ---------------------------------------------------------------------------
# Claim: no raw provider/exception text in reply or any production log field
# ---------------------------------------------------------------------------

class _CanaryLLMError(LLMError):
    def __repr__(self) -> str:
        return f"_CanaryLLMError({CANARY_KEY!r}, {CANARY_URL!r})"


@pytest.mark.parametrize("error", [LLMError, _CanaryLLMError, LLMCancelledError])
def test_client_exception_text_never_reaches_reply_or_production_log_pipeline(
    world, prod_logging, error,
) -> None:
    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.side_effect = error(f"rejected {CANARY_KEY} at {CANARY_URL}")
        return client

    world.controller._run_client_factory = factory
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    logged = _everything_logged(*prod_logging)
    assert "summary unavailable" in logged  # the manual warning really went through the pipeline
    for canary in (CANARY_KEY, CANARY_URL, "private.internal", "_CanaryLLMError"):
        assert canary not in msg and canary not in logged, canary
    _assert_failed_unchanged(world, msg, before)


@pytest.mark.parametrize("mode", ["success", "read_error"])
def test_cancel_signalled_inside_transport_is_cancel_not_provider_failure(
    world, prod_logging, mode,
) -> None:
    captured: list[threading.Event] = []
    original = world.factory

    def capturing(event: threading.Event):
        captured.append(event)  # the owned client's cancel event (real root factory)
        return original(event)

    world.controller._run_client_factory = capturing
    base_chat = world._chat

    def handler(request: httpx.Request) -> httpx.Response:
        captured[0].set()  # operator cancel observed while the request is in flight
        if mode == "read_error":
            raise httpx.ReadError("socket closed")
        return base_chat(request)

    world._chat = handler  # type: ignore[method-assign]
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert "cancel" in msg.lower()
    logged = _everything_logged(*prod_logging)
    assert "LLM_FAILED" not in logged and "socket closed" not in logged
    assert [t.close_count for t in world.transports] == [1]


def test_unexpected_base_exception_from_client_propagates_after_close_without_mutation(world) -> None:
    class _Stop(BaseException):
        pass

    closed: list[int] = []

    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.side_effect = _Stop()
        client.close.side_effect = lambda: closed.append(1)
        return client

    world.controller._run_client_factory = factory
    world.seed()
    before = world.state()
    with pytest.raises(_Stop):
        world.controller.compress_context()
    assert closed == [1] and world.state() == before


# ---------------------------------------------------------------------------
# Claim: ordinary SDK/provider failures at the client.chat boundary are a controlled
# failure (never re-raised: a re-raise reaches the global Telegram error logger's traceback)
# ---------------------------------------------------------------------------

OLLAMA_MODEL = ModelRecord("po/mo", "po", "mo", "mo", ModelMetadata(50_000, None, 2048, False, True, None))
SDK_CANARY = f"ORACLE_UNREGISTERED_PROVIDER_CANARY {CANARY_KEY} {CANARY_URL}"


@pytest.fixture
def ollama_world(world):
    """Root factory over a REAL official-cloud Ollama provider (real SDK client, counted MockTransport)."""
    providers = {"po": ProviderConfig("po", "ollama", "env:KEY_po", "https://ollama.com", 9)}
    settings = ModelSettings(world.tmp_path / "ollama-models.toml")
    settings.write(CatalogDocument(
        1, 2, None, (ProviderState("po", "ollama", "2026-09-01T00:00:00Z", "fresh"),), (OLLAMA_MODEL,),
    ))
    registry = ModelRegistry(providers, settings, base._NoDiscovery(), base._resolve)
    registry.select(OLLAMA_MODEL.id)
    world.controller._run_client_factory = main._make_run_client_factory(registry, base.AGENT, world.embedding)
    return world


def _malformed_ollama_reply(world, body) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        world.requests.append(request)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

    world._chat = handler  # type: ignore[method-assign]


MALFORMED = {
    "pydantic-content-object": {"message": {"role": "assistant", "content": {"private": SDK_CANARY}}, "done": True},
    "non-json-body": httpx.Response(200, content=f"<html>{SDK_CANARY}</html>".encode()),
}


@pytest.mark.parametrize("name", list(MALFORMED))
def test_real_ollama_sdk_malformed_http200_is_controlled_failure(ollama_world, prod_logging, name) -> None:
    world = ollama_world
    _malformed_ollama_reply(world, MALFORMED[name])
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()  # must NOT raise

    logged = _everything_logged(*prod_logging)
    _assert_failed_unchanged(world, msg, before)
    assert len(world.requests) == 1 and str(world.requests[0].url).startswith("https://ollama.com")
    assert [t.close_count for t in world.transports] == [1]
    for canary in ("ORACLE_UNREGISTERED_PROVIDER_CANARY", CANARY_KEY, CANARY_URL, "private.internal", "ValidationError"):
        assert canary not in msg and canary not in logged, canary


@pytest.mark.asyncio
async def test_real_ollama_malformed_reply_through_real_cmd_compress_never_escapes_to_global_handler(
    ollama_world, prod_logging,
) -> None:
    world = ollama_world
    _malformed_ollama_reply(world, MALFORMED["pydantic-content-object"])
    world.seed()
    before = world.state()
    iface = MagicMock()
    iface._is_authorized.return_value = True
    iface._get_agent_lock.return_value = asyncio.Lock()
    iface.agent_compress_fn = world.controller.compress_context
    iface._safe_edit = AsyncMock()
    iface._offer_pending_deferred_prompt = AsyncMock()
    update = MagicMock(
        effective_user=MagicMock(id=42),
        effective_message=MagicMock(reply_text=AsyncMock(return_value=MagicMock())),
    )

    await cmd_compress(iface, update, MagicMock(args=[]))  # an escaping exception would fail here

    reply = iface._safe_edit.await_args.args[1]
    assert reply.startswith("❌ Context not compressed") and "Compression failed" not in reply
    assert "ORACLE_UNREGISTERED_PROVIDER_CANARY" not in reply
    assert "ORACLE_UNREGISTERED_PROVIDER_CANARY" not in _everything_logged(*prod_logging)
    assert world.state() == before and [t.close_count for t in world.transports] == [1]


class _Hostile(Exception):
    """Formatting this exception in any way fails the test."""

    def __str__(self) -> str:
        raise AssertionError("exception was formatted")

    def __repr__(self) -> str:
        raise AssertionError("exception was formatted")


@pytest.mark.parametrize(
    "error",
    [TypeError(SDK_CANARY), ValueError(SDK_CANARY), KeyError(SDK_CANARY), OSError(SDK_CANARY), _Hostile()],
    ids=["TypeError", "ValueError", "KeyError", "OSError", "hostile-str-repr"],
)
def test_any_ordinary_sdk_boundary_exception_is_fixed_label_failure(world, prod_logging, error) -> None:
    closed: list[int] = []

    def factory(event: threading.Event):
        client = MagicMock()
        client.chat.side_effect = error
        client.close.side_effect = lambda: closed.append(1)
        return client

    world.controller._run_client_factory = factory
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    logged = _everything_logged(*prod_logging)
    _assert_failed_unchanged(world, msg, before)
    assert closed == [1] and "summary request failed" in msg
    for canary in ("ORACLE_UNREGISTERED_PROVIDER_CANARY", CANARY_KEY, CANARY_URL):
        assert canary not in msg and canary not in logged, canary


def test_cancel_wins_over_a_provider_failure_raised_after_cancel(world) -> None:
    captured: list[threading.Event] = []
    original = world.factory

    def capturing(event: threading.Event):
        captured.append(event)
        return original(event)

    world.controller._run_client_factory = capturing
    world.status = 503
    world.on_request = lambda: captured[0].set()
    world.seed()
    before = world.state()

    msg = world.controller.compress_context()

    _assert_failed_unchanged(world, msg, before)
    assert "cancel" in msg.lower()
