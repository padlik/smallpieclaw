"""Task 6.7: native history integrity, pinned payload identity and complete schemas.

Removing preview-fit gating must not remove protocol validation. Strict native-history
validation runs before any send or compaction (enabled, disabled, below-threshold and
no-eligible-middle paths alike) and fails with one fixed sanitized error before provider
I/O; the wire model equals the pinned upstream ID; and the assembled tool schemas reach
every adapter (including the real Ollama SDK) unchanged. The retired preview and
budget methods are absent, so no supported request path can consult them.

Everything runs against the real pinned ``LLMClient`` over ``httpx.MockTransport``.
"""

from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import context_manager
from builtin_tools.schemas import build_tool_definitions
from call_attempt_state import CallAttemptState
from context_manager import CompactionFailedError, validate_native_history
from interfaces import ChatResponse
from llm_client import LLMClient
from model_registry import ModelMetadata
from providers._errors import LLMPermanentError
from react_loop import _LoopState, _prepare_request
from tests.execution_harness import RecordingExecutor, build_context, run_react
from tests.run_model_fixtures import make_run_model_client
from tool_registry import Tool

SECRET = "CANARY-NATIVE-SECRET-6071"
FOREIGN = "FOREIGN-MODEL-CANARY-4417"
FINISH = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
SUMMARY = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}
NATIVE_META = ModelMetadata(context_tokens=8192, output_tokens=1024, tool_calling=True)


def _call(call_id: Any, name: str = "probe", arguments: Any = "{}") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _assistant(*ids: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [_call(i) for i in ids]}


def _result(call_id: Any, content: Any = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


GOAL = {"role": "user", "content": "GOAL"}
PLAIN = {"role": "assistant", "content": f"plain {SECRET}"}


# ---------------------------------------------------------------------------
# A. The validator itself: every malformed shape raises the one fixed error
# ---------------------------------------------------------------------------

INVALID: dict[str, list[dict[str, Any]]] = {
    "empty-call-id": [GOAL, _assistant(""), _result("")],
    "bool-call-id": [GOAL, _assistant(True), _result(True)],
    "int-call-id": [GOAL, _assistant(7), _result(7)],
    "none-call-id": [GOAL, _assistant(None), _result(None)],
    "duplicate-call-ids-in-one-group": [GOAL, _assistant("d", "d"), _result("d")],
    "missing-result": [GOAL, _assistant("a", "b"), _result("a")],
    "no-result-at-all": [GOAL, _assistant("a")],
    "duplicate-result": [GOAL, _assistant("a"), _result("a"), _result("a")],
    "unknown-result-id": [GOAL, _assistant("a"), _result("a"), _result("zzz")],
    "leading-orphan-result": [_result("a"), GOAL],
    "orphan-after-plain-message": [GOAL, PLAIN, _result("a")],
    "result-after-user-message": [GOAL, _assistant("a"), {"role": "user", "content": "u"}, _result("a")],
    "result-of-previous-group": [GOAL, _assistant("a"), _result("a"), _assistant("b"), _result("a")],
    "content-none": [GOAL, _assistant("a"), _result("a", None)],
    "content-list": [GOAL, _assistant("a"), _result("a", [{"type": "text", "text": "x"}])],
    "content-dict": [GOAL, _assistant("a"), _result("a", {"k": 1})],
    "content-int": [GOAL, _assistant("a"), _result("a", 5)],
    "result-id-bool": [GOAL, _assistant("a"), _result(True)],
    "result-id-missing": [GOAL, _assistant("a"), {"role": "tool", "content": "ok"}],
    "call-without-function": [GOAL, {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
                              _result("a")],
    "call-with-unparseable-arguments": [GOAL, {"role": "assistant", "content": None,
                                               "tool_calls": [_call("a", arguments="{nope")]}, _result("a")],
}


def _tag_secret(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy whose first message carries the canary so leaks are observable."""
    tagged = copy.deepcopy(messages)
    tagged[0]["content"] = f"{tagged[0]['content']} {SECRET}"
    return tagged


class TestValidatorTable:
    @pytest.mark.parametrize("kind", sorted(INVALID))
    def test_malformed_group_raises_the_fixed_sanitized_error(self, kind: str) -> None:
        messages = _tag_secret(INVALID[kind])
        before = copy.deepcopy(messages)
        with pytest.raises(LLMPermanentError) as info:
            validate_native_history(messages)
        assert str(info.value) == context_manager._MALFORMED_HISTORY
        assert SECRET not in str(info.value) and SECRET not in repr(info.value.args)
        assert info.value.__cause__ is None
        assert messages == before  # never repaired

    @pytest.mark.parametrize(
        "messages",
        [
            [GOAL, _assistant("a", "b", "c"), _result("c"), _result("a"), _result("b")],
            [GOAL, _assistant(" "), _result(" ")],
            [GOAL, _assistant("x"), _result("x"), PLAIN, _assistant("x"), _result("x")],
            [GOAL, {"role": "assistant", "content": "with text", "tool_calls": [_call("a", arguments={"k": 1})]},
             _result("a", "")],
        ],
        ids=["reversed-result-order", "whitespace-id-is-not-trimmed", "id-reused-across-complete-groups",
             "content-bearing-assistant-dict-arguments-empty-result"],
    )
    def test_valid_groups_are_accepted_unchanged(self, messages: list[dict[str, Any]]) -> None:
        before = copy.deepcopy(messages)
        validate_native_history(messages)
        assert messages == before


# ---------------------------------------------------------------------------
# B. Every prepare state validates the source before any provider I/O
# ---------------------------------------------------------------------------

def _big(role: str, tag: str) -> dict[str, Any]:
    return {"role": role, "content": f"{tag} " + "alpha beta " * 400}


SHAPES: dict[str, list[dict[str, Any]]] = {
    "no-eligible-middle": [GOAL, _assistant("a", "b")],
    "with-eligible-middle": [GOAL, _big("assistant", "old1"), _big("user", "old2"),
                             _assistant("a", "b"), _result("a")],
}


def _recording_client(seen: list[str], metadata: ModelMetadata = NATIVE_META) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen.append(body)
        return httpx.Response(200, json=SUMMARY if "Summarize these" in body else FINISH)

    return make_run_model_client("openai", httpx.MockTransport(handler), metadata=metadata,
                                 upstream_id="prep-model")


class TestEveryPrepareStateFailsBeforeIO:
    @pytest.mark.parametrize("disabled", [False, True], ids=["enabled", "auto-disabled"])
    @pytest.mark.parametrize("threshold", [10, 10_000_000], ids=["above-threshold", "below-threshold"])
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_malformed_source_is_rejected_with_zero_io(
        self, disabled: bool, threshold: int, shape: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        seen: list[str] = []
        client = _recording_client(seen)
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=threshold)
        messages = _tag_secret(SHAPES[shape])
        before = copy.deepcopy(messages)
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        state.auto_compaction_disabled = disabled
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(LLMPermanentError) as info:
                _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert not isinstance(info.value, CompactionFailedError)
        assert seen == []  # neither a summary nor a main request
        assert state.messages == before and state.auto_compaction_disabled is disabled
        assert SECRET not in str(info.value) + repr([r.__dict__ for r in caplog.records])
        assert not [r for r in caplog.records if "Automatic compaction failed" in r.getMessage()]


# ---------------------------------------------------------------------------
# C. No preview / fit / budget consult on any supported send path
# ---------------------------------------------------------------------------

@pytest.fixture
def gates_trapped() -> None:
    """The preview/fit/budget gate no longer exists, so no send path can consult it."""
    for name in ("preview_request", "_compute_budget", "_preview_for_request", "selection_identity"):
        assert not hasattr(LLMClient, name), name


METADATA = {
    "tiny": ModelMetadata(context_tokens=1, input_tokens=1, output_tokens=1, tool_calling=True),
    "huge": ModelMetadata(context_tokens=10**9, input_tokens=10**9, output_tokens=10**9, tool_calling=True),
    "unknown": ModelMetadata(tool_calling=True),
}
VALID_HISTORY = [GOAL, _assistant("a", "b"), _result("b", "rb"), _result("a", "ra")]


def _ollama_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m", "created_at": "2026-10-28T00:00:00Z",
        "message": {"role": "assistant", "content": content}, "done": True,
        "prompt_eval_count": 1, "eval_count": 1,
    })


@pytest.mark.usefixtures("gates_trapped")
class TestNoPreviewGateOnSend:
    @pytest.mark.parametrize("meta", sorted(METADATA))
    @pytest.mark.parametrize("mode", ["text", "json", "native"])
    def test_openai_requests_send_whatever_the_cached_limits(self, meta: str, mode: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=FINISH)

        client = make_run_model_client("openai", httpx.MockTransport(handler), metadata=METADATA[meta],
                                       upstream_id="m")
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                if mode == "native":
                    client.chat_with_tools(copy.deepcopy(VALID_HISTORY), build_tool_definitions(), system="s")
                else:
                    client.chat(copy.deepcopy(VALID_HISTORY), system="s", json_mode=mode == "json")
        finally:
            client.close()
        assert len(seen) == 1
        wire = json.loads(seen[0].content)
        assert wire["model"] == "m" and ("tools" in wire) is (mode == "native")

    @pytest.mark.parametrize("meta", sorted(METADATA))
    def test_ollama_sdk_native_request_sends_whatever_the_cached_limits(self, meta: str) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "ollama", httpx.MockTransport(lambda r: seen.append(r) or _ollama_reply("hi")),
            metadata=METADATA[meta], upstream_id="m",
        )
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                response = client.chat_with_tools([GOAL], build_tool_definitions())
        finally:
            client.close()
        assert isinstance(response, ChatResponse) and response.text == "hi" and len(seen) == 1

    def test_prepare_request_compacts_a_valid_multi_call_history_and_sends(self) -> None:
        seen: list[str] = []
        client = _recording_client(seen, ModelMetadata(context_tokens=4_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor(), compaction_threshold_tokens=300)
        messages = [GOAL, _big("assistant", "old1"), _big("user", "old2"),
                    _assistant("a", "b"), _result("a", "ra"), _result("b", "rb"),
                    {"role": "assistant", "content": "r1"}, {"role": "user", "content": "r2"}]
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        try:
            _prepare_request(ctx, state, "sys", 0)
        finally:
            client.close()
        assert len(seen) == 1 and "Summarize these" in seen[0]
        assert any("Compacted context" in str(m.get("content")) for m in state.messages)
        validate_native_history(state.messages)

    @pytest.mark.parametrize("adapter", ["openai", "ollama"])
    def test_oversized_request_is_sent_without_a_preview_fit_gate(self, adapter: str) -> None:
        """A request far beyond the cached limits is still sent once, byte for byte."""
        seen: list[httpx.Request] = []
        if adapter == "ollama":
            transport = httpx.MockTransport(lambda r: seen.append(r) or _ollama_reply("hi"))
        else:
            transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH))
        client = make_run_model_client(adapter, transport, metadata=ModelMetadata(
            context_tokens=4_000, input_tokens=4_000, output_tokens=200, tool_calling=True), upstream_id="m")
        big = "oversized " * 60_000
        state = CallAttemptState(0, 0)
        try:
            with client.bind_attempt(state):
                client.chat_with_tools([{"role": "user", "content": big}], build_tool_definitions(), system="s")
        finally:
            client.close()
        assert len(seen) == 1 and state.outbound_attempts == 1
        assert big in json.loads(seen[0].content)["messages"][-1]["content"]

    def test_loop_run_sends_every_step_with_no_gate_consulted(self) -> None:
        seen: list[str] = []
        tool = {"choices": [{"message": {"content": '{"action":"tool","tool":"probe","args":{}}'}}]}
        replies = [tool, FINISH]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content.decode())
            return httpx.Response(200, json=replies.pop(0))

        client = make_run_model_client("openai", httpx.MockTransport(handler), metadata=METADATA["tiny"],
                                       upstream_id="m")
        try:
            result, calls, _progress = run_react(client, RecordingExecutor(), "GOAL",
                                                 compaction_threshold_tokens=10_000_000, max_retries=0)
        finally:
            client.close()
        assert result == "done" and len(calls) == 1 and len(seen) == 2


# ---------------------------------------------------------------------------
# D. Pinned upstream payload identity
# ---------------------------------------------------------------------------

UPSTREAM_IDS = ["plain-model", "vendor/sub model", " spaced/name:tag "]


class TestPinnedPayloadIdentity:
    @pytest.mark.parametrize("upstream", UPSTREAM_IDS, ids=["plain", "slash-space", "outer-spaces"])
    def test_openai_wire_model_is_the_exact_upstream_id(self, upstream: str) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "openai", httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH)),
            metadata=NATIVE_META, upstream_id=upstream,
        )
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat([GOAL])
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat_with_tools([GOAL], build_tool_definitions())
        finally:
            client.close()
        assert [json.loads(r.content)["model"] for r in seen] == [upstream, upstream]

    @pytest.mark.parametrize("upstream", UPSTREAM_IDS, ids=["plain", "slash-space", "outer-spaces"])
    def test_ollama_sdk_wire_model_is_the_exact_upstream_id(self, upstream: str) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "ollama", httpx.MockTransport(lambda r: seen.append(r) or _ollama_reply("hi")),
            metadata=NATIVE_META, upstream_id=upstream,
        )
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat_with_tools([GOAL], build_tool_definitions())
        finally:
            client.close()
        assert [json.loads(r.content)["model"] for r in seen] == [upstream]

    def test_retargeting_and_display_mutation_do_not_move_the_wire_model(self) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "openai", httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH)),
            metadata=NATIVE_META, upstream_id="pinned/model",
        )
        try:
            with pytest.raises(LLMPermanentError):
                client.set_model(FOREIGN)
            client.llm_cfg["model"] = FOREIGN  # a display/config copy, not the pin
            client.llm_cfg["id"] = FOREIGN
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat([GOAL])
        finally:
            client.close()
        assert [json.loads(r.content)["model"] for r in seen] == ["pinned/model"]

    @pytest.mark.parametrize("mode", ["json", "native"])
    def test_a_payload_naming_another_model_fails_before_any_io(self, mode: str) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "openai", httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH)),
            metadata=NATIVE_META, upstream_id="pinned/model",
        )
        try:
            with pytest.raises(LLMPermanentError) as info:
                client._prepare_request({"model": FOREIGN, "messages": [GOAL], "extra": SECRET}, mode)
        finally:
            client.close()
        assert seen == []
        assert FOREIGN not in str(info.value) and SECRET not in str(info.value)
        assert "pinned" in str(info.value)


# ---------------------------------------------------------------------------
# E. Complete builtin / pseudo / MCP schemas reach the wire unchanged
# ---------------------------------------------------------------------------

HUGE_DESCRIPTION = "very long MCP description " * 1200  # ~32k characters
GENERATION_NAMED_SCHEMA = {
    "type": "object",
    "required": ["temperature", "options"],
    "properties": {
        "temperature": {"type": "number", "minimum": 0, "maximum": 2, "description": HUGE_DESCRIPTION},
        "top_p": {"type": "number"},
        "max_tokens": {"type": "integer", "enum": [1, 2, 3]},
        "options": {"type": "object", "properties": {
            "temperature": {"type": "string", "enum": ["a", "b"]},
            "nested": {"type": "array", "items": {"type": "object", "required": ["top_p"],
                                                  "properties": {"top_p": {"type": "number"}}}},
        }},
    },
    "$defs": {"X": {"type": "string", "description": "d" * 5000}},
}


def _mcp_manager() -> SimpleNamespace:
    tools = [
        Tool("mcp_big", "mcp", HUGE_DESCRIPTION, is_mcp=True, server_name="svc",
             input_schema=GENERATION_NAMED_SCHEMA),
        Tool("mcp_small", "mcp", "small", is_mcp=True, server_name="svc2",
             input_schema={"type": "object", "properties": {"q": {"type": "string"}}}),
    ]
    return SimpleNamespace(get_tools=lambda: list(tools))


class TestCompleteSchemasOnWire:
    def test_assembly_keeps_every_schema_whole(self) -> None:
        definitions = build_tool_definitions(_mcp_manager())
        by_name = {d["function"]["name"]: d["function"] for d in definitions}
        assert len(definitions) == 23 + 1 + 2 and "plan" in by_name
        assert by_name["mcp_big"]["parameters"] == GENERATION_NAMED_SCHEMA
        assert by_name["mcp_big"]["description"] == HUGE_DESCRIPTION

    @pytest.mark.parametrize("adapter", ["openai", "ollama"])
    @pytest.mark.parametrize("meta", ["tiny", "huge"])
    def test_wire_tools_equal_the_assembled_definitions(self, adapter: str, meta: str) -> None:
        seen: list[httpx.Request] = []
        if adapter == "ollama":
            transport = httpx.MockTransport(lambda r: seen.append(r) or _ollama_reply("hi"))
        else:
            transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH))
        client = make_run_model_client(adapter, transport, metadata=METADATA[meta], upstream_id="m")
        definitions = build_tool_definitions(_mcp_manager())
        expected = json.loads(json.dumps(definitions, ensure_ascii=False))
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat_with_tools([GOAL], definitions)
        finally:
            client.close()
        wire = json.loads(seen[0].content)
        assert wire["tools"] == expected
        assert [t["function"]["name"] for t in wire["tools"]] == [t["function"]["name"] for t in definitions]
        big = next(t for t in wire["tools"] if t["function"]["name"] == "mcp_big")["function"]
        assert big["parameters"]["properties"]["temperature"]["description"] == HUGE_DESCRIPTION
        assert big["parameters"]["properties"]["options"]["properties"]["nested"]["items"]["required"] == ["top_p"]
        for top_level in ("temperature", "top_p", "max_tokens"):
            assert top_level not in wire  # only caller-owned tool data carries those names


# ---------------------------------------------------------------------------
# F. A valid multi-call history reaches the wire with exact IDs and payloads
# ---------------------------------------------------------------------------

class TestMultiCallHistoryWire:
    def test_ids_order_and_result_payloads_are_sent_unchanged(self) -> None:
        seen: list[httpx.Request] = []
        client = make_run_model_client(
            "openai", httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FINISH)),
            metadata=NATIVE_META, upstream_id="m",
        )
        history = [
            GOAL,
            {"role": "assistant", "content": None, "tool_calls": [
                _call("call_A", "probe", '{"n": 1}'), _call("call_B", "probe", '{"n": 2}'), _call("call_C")]},
            _result("call_C", "rc \u2603 \"quoted\""),
            _result("call_A", "ra\nline2"),
            _result("call_B", ""),
            _assistant("call_A"),  # reused ID in a separate complete group
            _result("call_A", "second"),
        ]
        before = copy.deepcopy(history)
        validate_native_history(history)
        try:
            with client.bind_attempt(CallAttemptState(0, 0)):
                client.chat_with_tools(history, build_tool_definitions(), system="sys")
        finally:
            client.close()
        wire = json.loads(seen[0].content)
        assert wire["messages"][0]["role"] == "system" and wire["messages"][1:] == before
        assert history == before
