"""Task 6.2: one model-independent compaction threshold over the complete input estimate.

The estimated input (system prompt + full chat history/images + the complete current
tool definitions) is compared only with ``agent.compaction_threshold_tokens``. Discovered
or cached model limits neither trigger, shrink nor reject a send, and the monitor
snapshot carries counts, the threshold, identity, turn and liveness only.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from context_monitor import ContextMonitor, ContextSnapshot
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from react_loop import (
    _LoopState,
    _prepare_request,
    _publish_context_snapshot,
    _tool_defs_by_server_for_context,
)
from token_estimator import estimate_messages_tokens
from tests.execution_harness import RecordingExecutor, build_context, run_react

FINISH = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
SUMMARY = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}

_BODIES = {
    "anthropic": {"content": [{"type": "text", "text": "ok"}]},
    "google": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
}
_HUGE = [{"role": "user", "content": "word " * 6000}]


def _llm(
    adapter: str,
    handler,
    metadata: ModelMetadata,
    *,
    agent: AgentConfig | None = None,
) -> LLMClient:
    model = ModelRecord("c/m", "c", "m", "M", metadata)
    base = {
        "openai": "https://openai.example.test/v1",
        "openrouter": "https://openrouter.example.test/api/v1",
        "google": "https://google.example.test/v1beta",
        "anthropic": "https://anthropic.example.test",
    }[adapter]
    connection = ResolvedProviderConnection("c", adapter, base, 5, "fake-key")
    return LLMClient(
        RunModel(model, connection, 1, 1),
        agent_config=agent or AgentConfig(max_retries=0, retry_delay=0),
        http_transport=httpx.MockTransport(handler),
    )


def _recorder(requests: list[httpx.Request], adapter: str = "openai"):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        if "Summarize these" in body:
            return httpx.Response(200, json=SUMMARY)
        return httpx.Response(200, json=_BODIES.get(adapter, FINISH))

    return handler


_TINY = ModelMetadata(context_tokens=300, input_tokens=200, output_tokens=100)
_LIMITS = {
    "none": ModelMetadata(),
    "tiny": _TINY,
    "huge": ModelMetadata(context_tokens=10_000_000, input_tokens=9_000_000, output_tokens=64),
}


# --- A. no live send path consults cached limits -------------------------------------


@pytest.mark.parametrize("adapter", ["openai", "openrouter", "google", "anthropic"])
@pytest.mark.parametrize("mode", ["text", "json"])
def test_http_send_is_not_rejected_by_tiny_cached_limits(adapter: str, mode: str) -> None:
    requests: list[httpx.Request] = []
    client = _llm(adapter, _recorder(requests, adapter), _TINY)
    try:
        client.chat(_HUGE, json_mode=mode == "json")
    finally:
        client.close()
    assert len(requests) == 1


def test_native_send_is_not_rejected_by_tiny_cached_limits() -> None:
    requests: list[httpx.Request] = []
    tools = [{"type": "function", "function": {"name": "go", "parameters": {"type": "object"}}}]
    client = _llm("openai", _recorder(requests), _TINY)
    try:
        client.chat_with_tools(_HUGE, tools)
    finally:
        client.close()
    assert len(requests) == 1
    assert json.loads(requests[0].content)["tools"] == tools


def test_ollama_send_is_not_rejected_by_tiny_cached_limits() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "model": "m", "created_at": "2026-10-14T00:00:00Z",
            "message": {"role": "assistant", "content": "ok"}, "done": True,
        })

    model = ModelRecord("c/m", "c", "m", "M", _TINY)
    connection = ResolvedProviderConnection("c", "ollama", "https://ollama.com", 5, "k")
    client = LLMClient(
        RunModel(model, connection, 1, 1),
        agent_config=AgentConfig(max_retries=0, retry_delay=0),
        http_transport=httpx.MockTransport(handler),
    )
    try:
        assert client.chat(_HUGE) == "ok"
    finally:
        client.close()
    assert len(requests) == 1


def test_provider_reported_context_rejection_is_still_the_normal_error() -> None:
    from providers._errors import LLMContextOverflowError

    client = _llm(
        "openai",
        lambda r: httpx.Response(400, json={"error": {"code": "context_length_exceeded",
                                                      "message": "context_length_exceeded"}}),
        _LIMITS["huge"],
    )
    try:
        with pytest.raises(LLMContextOverflowError):
            client.chat([{"role": "user", "content": "hi"}])
    finally:
        client.close()


# --- B. required output cap is configuration, not a reserve ---------------------------


@pytest.mark.parametrize("cap", [1024, 2000])
@pytest.mark.parametrize("name", ["none", "tiny", "huge"])
def test_anthropic_sends_the_configured_cap_whatever_the_cached_limits(cap: int, name: str) -> None:
    requests: list[httpx.Request] = []
    client = _llm(
        "anthropic", _recorder(requests, "anthropic"), _LIMITS[name],
        agent=AgentConfig(max_retries=0, retry_delay=0, required_output_cap_tokens=cap),
    )
    try:
        client.chat(_HUGE)
    finally:
        client.close()
    assert json.loads(requests[0].content)["max_tokens"] == cap


def test_graph_extraction_keeps_its_fixed_wire_and_ordinary_openai_has_no_cap() -> None:
    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _TINY)
    graph = LLMClient.for_graph_extraction(
        client._run_model, agent_config=AgentConfig(max_retries=0),
        http_transport=httpx.MockTransport(_recorder(requests)),
    )
    try:
        client.chat(_HUGE)
        graph.chat([{"role": "user", "content": "extract"}])
    finally:
        client.close()
        graph.close()
    ordinary, fixed = (json.loads(r.content) for r in requests)
    assert "max_tokens" not in ordinary and "max_completion_tokens" not in ordinary
    assert fixed["max_completion_tokens"] == 1024 and fixed["temperature"] == 0.1


# --- C. the loop compares the complete estimate with the threshold only ---------------


def _history(n: int = 6) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"history {i} " + "alpha beta " * 400}
        for i in range(n)
    ]


def _short_term(messages: list[dict]) -> MagicMock:
    short = MagicMock()
    short.get_messages.return_value = messages
    return short


def _bodies(requests: list[httpx.Request]) -> list[str]:
    return [r.content.decode() for r in requests]


@pytest.mark.parametrize("name", ["none", "tiny", "huge"])
def test_loop_below_threshold_sends_full_history_whatever_the_cached_limits(name: str) -> None:
    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _LIMITS[name])
    try:
        result, _calls, _progress = run_react(
            client, RecordingExecutor(), "GOAL", short_term=_short_term(_history()),
            compaction_threshold_tokens=60_000,
        )
    finally:
        client.close()
    assert result == "done"
    assert len(requests) == 1
    body = _bodies(requests)[0]
    assert "Summarize these" not in body and "history 0" in body and "history 5" in body


@pytest.mark.parametrize("name", ["none", "tiny", "huge"])
def test_loop_over_threshold_compacts_identically_whatever_the_cached_limits(name: str) -> None:
    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _LIMITS[name])
    try:
        result, _calls, _progress = run_react(
            client, RecordingExecutor(), "GOAL", short_term=_short_term(_history()),
            compaction_threshold_tokens=9_000,
        )
    finally:
        client.close()
    assert result == "done"
    bodies = _bodies(requests)
    assert any("Summarize these" in b for b in bodies[:-1])
    assert "Compacted context" in bodies[-1] and "earlier steps summarized" in bodies[-1]
    assert "GOAL" in bodies[-1]


def test_cached_limits_do_not_change_the_request_sequence() -> None:
    sequences = []
    for name in _LIMITS:
        requests: list[httpx.Request] = []
        client = _llm("openai", _recorder(requests), _LIMITS[name])
        try:
            run_react(
                client, RecordingExecutor(), "GOAL", short_term=_short_term(_history()),
                compaction_threshold_tokens=9_000,
            )
        finally:
            client.close()
        sequences.append(_bodies(requests))
    assert sequences[0] == sequences[1] == sequences[2]


_BIG_TOOL = {
    "type": "function",
    "function": {
        "name": "probe", "description": "def " * 1500,
        "parameters": {"type": "object", "properties": {}},
    },
}


def _ctx_state(client: LLMClient, *, tools: bool, images: bool):
    ctx = build_context(client, RecordingExecutor(), mcp_manager=None)
    ctx._tool_defs = [_BIG_TOOL] if tools else []
    messages = _history(5) + [{"role": "user", "content": "GOAL"}]
    if images:
        messages[-1]["images"] = ["aGVsbG8="]
    state = _LoopState(messages=messages, goal_idx=len(messages) - 1, max_steps=3)
    return ctx, state


def _tool_tokens(ctx) -> int:
    return sum(_tool_defs_by_server_for_context(ctx).values())


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("images", [False, True])
def test_trigger_boundary_is_the_published_total(tools: bool, images: bool) -> None:
    """Equal does not compact, one token over does; the total equals the snapshot's."""
    outcomes = {}
    for delta in (0, -1):
        requests: list[httpx.Request] = []
        client = _llm("openai", _recorder(requests), _TINY)
        try:
            ctx, state = _ctx_state(client, tools=tools, images=images)
            monitor = ContextMonitor()
            ctx.context_monitor = monitor
            _publish_context_snapshot(ctx, state, "sys")
            snap = monitor.read()
            total = snap.system_prompt_tokens + snap.chat_history_tokens + snap.tool_defs_tokens
            assert total == estimate_messages_tokens(
                state.messages, "sys", model="m"
            ) + _tool_tokens(ctx)
            ctx.compaction_threshold_tokens = total + delta
            before = state.messages
            _prepare_request(ctx, state, "sys", snap.tool_defs_tokens)
            outcomes[delta] = (state.messages is not before, any("Summarize" in b for b in _bodies(requests)))
        finally:
            client.close()
    assert outcomes[0] == (False, False)
    assert outcomes[-1][0] is True and outcomes[-1][1] is True


def test_tool_definitions_alone_push_the_same_history_over_the_threshold() -> None:
    results = {}
    for tools in (False, True):
        requests: list[httpx.Request] = []
        client = _llm("openai", _recorder(requests), _TINY)
        try:
            ctx, state = _ctx_state(client, tools=tools, images=False)
            messages_total = estimate_messages_tokens(state.messages, "sys", model="m")
            ctx.compaction_threshold_tokens = messages_total + 50
            before = state.messages
            _prepare_request(ctx, state, "sys", _tool_tokens(ctx))
            results[tools] = state.messages is not before
        finally:
            client.close()
    assert results == {False: False, True: True}


def test_images_are_counted_once_in_history() -> None:
    client = _llm("openai", _recorder([]), _TINY)
    try:
        with_img, state_i = _ctx_state(client, tools=False, images=True)
        without, state_n = _ctx_state(client, tools=False, images=False)
        mon_i, mon_n = ContextMonitor(), ContextMonitor()
        with_img.context_monitor, without.context_monitor = mon_i, mon_n
        _publish_context_snapshot(with_img, state_i, "sys")
        _publish_context_snapshot(without, state_n, "sys")
    finally:
        client.close()
    delta = mon_i.read().chat_history_tokens - mon_n.read().chat_history_tokens
    assert delta > 0
    assert mon_i.read().system_prompt_tokens == mon_n.read().system_prompt_tokens


def test_summary_request_is_not_previewed_against_cached_limits() -> None:
    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _TINY)
    try:
        ctx, state = _ctx_state(client, tools=False, images=False)
        ctx.compaction_threshold_tokens = 1_000
        _prepare_request(ctx, state, "sys", 0)
    finally:
        client.close()
    assert any("Summarize these" in b for b in _bodies(requests))
    assert "earlier steps summarized" in json.dumps(state.messages)


def test_guarded_first_request_is_not_rejected_by_cached_limits() -> None:
    from unittest.mock import patch

    import react_loop

    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _TINY)
    try:
        ctx, _state = _ctx_state(client, tools=False, images=False)
        with patch("react_loop._build_system_prompt", return_value=("system " * 2000, None)):
            staged = react_loop._stage_guarded_first_request(ctx, "GOAL", None, None)
    finally:
        client.close()
    assert staged.rejected is False
    assert staged.tool_defs and staged.system.startswith("system")
    assert requests == []


# --- D. snapshot contract ------------------------------------------------------------


def test_snapshot_fields_are_counts_threshold_identity_turn_and_liveness_only() -> None:
    names = {f.name for f in dataclasses.fields(ContextSnapshot)}
    assert names == {
        "system_prompt_tokens", "chat_history_tokens", "tool_defs_tokens",
        "tool_defs_by_server", "compaction_threshold", "is_live", "turn", "model_id",
    }
    import context_monitor

    assert not hasattr(context_monitor, "compute_danger_level")
    assert not hasattr(context_monitor, "compute_headroom_real")


def test_published_snapshot_uses_the_configured_threshold_and_pinned_identity() -> None:
    client = _llm("openai", _recorder([]), _LIMITS["huge"])
    try:
        ctx, state = _ctx_state(client, tools=True, images=False)
        ctx.compaction_threshold_tokens = 4321
        ctx.context_monitor = ContextMonitor()
        state.step = 4
        _publish_context_snapshot(ctx, state, "sys")
        snap = ctx.context_monitor.read()
    finally:
        client.close()
    assert snap.compaction_threshold == 4321
    assert (snap.model_id, snap.turn, snap.is_live) == ("c/m", 4, True)
    assert snap.tool_defs_tokens == sum(snap.tool_defs_by_server.values())
    assert snap.tool_defs_by_server["builtin"] == 0 and snap.tool_defs_tokens > 0


def test_zero_tool_registered_server_is_listed_and_snapshots_are_immutable() -> None:
    client = _llm("openai", _recorder([]), _TINY)
    try:
        mcp = MagicMock()
        mcp.list_servers.return_value = [{"name": "empty-server"}]
        ctx, state = _ctx_state(client, tools=True, images=False)
        ctx.mcp_manager = mcp
        ctx.context_monitor = ContextMonitor()
        _publish_context_snapshot(ctx, state, "sys")
        first = ctx.context_monitor.read()
        _publish_context_snapshot(ctx, dataclasses.replace(state, step=2), "sys")
        second = ctx.context_monitor.read()
    finally:
        client.close()
    assert first.tool_defs_by_server["empty-server"] == 0
    assert first is not second and first.turn != second.turn
    assert first.tool_defs_by_server is not second.tool_defs_by_server
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.turn = 99  # type: ignore[misc]


def test_runtime_wires_the_configured_threshold_into_the_context() -> None:
    from tests.test_agent_runtime_characterization import _make_controller, _run_capture

    controller = _make_controller(agent_cfg=AgentConfig(compaction_threshold_tokens=777))
    ctx, _result = _run_capture(controller)
    assert ctx.compaction_threshold_tokens == 777


# --- E. ownership-lookup failure never zeroes the tool-definition count ---------------


class _ThrowingRegistry:
    """Registry whose ownership lookup fails with a secret-looking message."""

    def get(self, _name: str):
        raise RuntimeError("registry-secret-token-123")


class _ThrowingToolIndex:
    registry = _ThrowingRegistry()

    def search(self, *_a, **_k):
        return []

    def all_tools(self):
        return []


def _run_with_registry(tool_index, threshold: int, monitor: ContextMonitor):
    requests: list[httpx.Request] = []
    client = _llm("openai", _recorder(requests), _TINY)
    try:
        result, _calls, _progress = run_react(
            client, RecordingExecutor(), "GOAL", short_term=_short_term(_history()),
            compaction_threshold_tokens=threshold, context_monitor=monitor,
            tool_index=tool_index,
        )
    finally:
        client.close()
    return result, requests


def test_registry_failure_still_counts_the_complete_schema_array(caplog) -> None:
    """Zero-degradation would stay under 9000 (history ~7.4k); the full schemas (~4.6k) compact."""
    from builtin_tools.schemas import build_tool_definitions

    healthy_monitor, broken_monitor = ContextMonitor(), ContextMonitor()
    _, healthy_requests = _run_with_registry(None, 9_000, healthy_monitor)
    result, broken_requests = _run_with_registry(_ThrowingToolIndex(), 9_000, broken_monitor)

    assert result == "done"
    healthy, broken = healthy_monitor.read(), broken_monitor.read()
    assert broken.tool_defs_tokens == sum(broken.tool_defs_by_server.values()) > 4_000
    assert broken.tool_defs_tokens == healthy.tool_defs_tokens
    # The same count that reached the threshold decision compacted the history.
    assert any("Summarize these" in b for b in _bodies(broken_requests))
    assert any("Summarize these" in b for b in _bodies(healthy_requests))
    # Every schema is still sent; nothing is truncated or dropped.
    sent_tools = json.loads(_bodies(broken_requests)[-1])["tools"]
    assert [t["function"]["name"] for t in sent_tools] == [
        t["function"]["name"] for t in build_tool_definitions(mcp_manager=None)
    ]
    assert "registry-secret-token-123" not in caplog.text


def test_zero_tools_is_a_legitimate_zero_even_when_grouping_fails() -> None:
    client = _llm("openai", _recorder([]), _TINY)
    try:
        ctx, state = _ctx_state(client, tools=False, images=False)
        ctx.tool_index = _ThrowingToolIndex()
        ctx.context_monitor = ContextMonitor()
        _publish_context_snapshot(ctx, state, "sys")
        snap = ctx.context_monitor.read()
    finally:
        client.close()
    assert snap.tool_defs_tokens == 0 and snap.tool_defs_by_server == {"builtin": 0}
