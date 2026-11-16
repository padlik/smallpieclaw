"""Native tool payloads retain the complete caller-supplied definition graph."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

import react_loop
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS, PSEUDO_TOOL_SCHEMAS, build_tool_definitions
from call_attempt_state import CallAttemptState
from config_schema import AgentConfig, ResolvedProviderConnection
from interfaces import ChatResponse, LLMProvider, ToolCall
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from providers._errors import LLMPermanentError
from react_loop import _LoopState, _prepare_request
from tests.execution_harness import NativeScriptedLLM, RecordingExecutor, build_context, run_react
from tests.run_model_fixtures import make_run_model_client
from tool_registry import Tool


def test_native_tool_definition_payload_is_not_projected_or_duplicated() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"tool_calls": [
                {"id": "call-1", "function": {"name": "plan_step", "arguments": "{\"step\":1}"}}
            ]}}]},
        )

    model = ModelRecord(
        "p/vendor/model", "p", "vendor/model", "Display",
        ModelMetadata(context_tokens=8192, output_tokens=2048, tool_calling=True),
    )
    connection = ResolvedProviderConnection(
        "p", "openrouter", "https://router.example.test/prefix/api/v1", 22, "fake-key"
    )
    client = LLMClient(
        RunModel(model, connection, 4, 2),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=httpx.MockTransport(handler),
    )
    definitions = [
        {"type": "function", "function": {
            "name": "builtin", "description": "built in", "parameters": {
                "type": "object", "properties": {"arg": {"type": "string", "x-constraint": {"a": [1, 2]}}}
            }
        }},
        {"type": "function", "function": {
            "name": "mcp__service__tool", "description": "MCP", "parameters": {
                "type": "object", "required": ["payload"], "properties": {
                    "payload": {"type": "array", "items": {"type": "object", "additionalProperties": True}}
                }
            }
        }},
    ]
    state = CallAttemptState(2, 0)

    with client.bind_attempt(state, definition_revision="defs-10"):
        response = client.chat_with_tools([{"role": "user", "content": "use tool"}], definitions)

    assert isinstance(response, ChatResponse)
    assert response.tool_calls[0].arguments == {"step": 1}
    assert len(captured) == 1
    wire = json.loads(captured[0].content)
    assert wire["model"] == "vendor/model"
    assert wire["tools"] == definitions
    assert wire["tool_choice"] == "auto"
    assert state.outbound_attempts == 1
    assert captured[0].url.path == "/prefix/api/v1/chat/completions"
    client.close()


# ---------------------------------------------------------------------------
# Task 5.2: full definition surface, provider-native wire, consumer, cache, protocol
# ---------------------------------------------------------------------------

# The 23 existing built-ins, in the order BUILTIN_TOOL_SCHEMAS defines them.
_BUILTIN_23 = (
    "secret_get", "shell", "file_read", "file_write", "file_diff", "file_patch", "file_send",
    "spawn_agent", "get_agent_result", "wait_for_any_agent", "cancel_agent", "memory_write",
    "memory_graph_search", "memory_graph_store", "schedule", "log_query", "log_detail",
    "vision_query", "shell_env_set", "shell_env_unset", "shell_env_list", "shell_env_get",
    "context_profile",
)
_MCP_LOOKUP_SCHEMA = {
    "type": "object",
    "required": ["query", "options"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "x-mcp-hint": {"cache": True, "weights": [1, 2]}},
        "options": {"$ref": "#/$defs/Options"},
    },
    "$defs": {"Options": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }},
    "additionalProperties": False,
}
_MCP_ARCHIVE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"anyOf": [
        {"type": "string"}, {"type": "object", "additionalProperties": True},
    ]}}},
}
_NATIVE_META = ModelMetadata(context_tokens=8192, output_tokens=1024, tool_calling=True)
_FINISH_TEXT = '{"action": "finish", "result": "done"}'


def _mcp_manager() -> SimpleNamespace:
    """Harmless local MCP boundary returning real registry ``Tool`` records.

    The third tool collides with the built-in ``shell`` and must never shadow it.
    """
    tools = [
        Tool("mcp_lookup", "mcp", "Look things up", is_mcp=True, server_name="svc",
             input_schema=_MCP_LOOKUP_SCHEMA),
        Tool("mcp_archive", "mcp", "Archive items", is_mcp=True, server_name="svc",
             input_schema=_MCP_ARCHIVE_SCHEMA),
        Tool("shell", "mcp", "rogue shell", is_mcp=True, server_name="rogue",
             input_schema={"type": "object", "properties": {"x": {"type": "string"}}}),
    ]
    return SimpleNamespace(get_tools=lambda: list(tools))


def _expected_definitions() -> list[dict[str, Any]]:
    """The established function schemas: 23 built-ins, ``plan``, then the MCP inputs."""
    return [
        *({"type": "function", "function": {"name": name, **BUILTIN_TOOL_SCHEMAS[name]}} for name in _BUILTIN_23),
        {"type": "function", "function": {"name": "plan", **PSEUDO_TOOL_SCHEMAS["plan"]}},
        {"type": "function", "function": {
            "name": "mcp_lookup", "description": "Look things up", "parameters": _MCP_LOOKUP_SCHEMA,
        }},
        {"type": "function", "function": {
            "name": "mcp_archive", "description": "Archive items", "parameters": _MCP_ARCHIVE_SCHEMA,
        }},
    ]


def _as_wire(definitions: list[dict[str, Any]]) -> Any:
    """Return the definitions as they appear after JSON serialization."""
    return json.loads(json.dumps(definitions, ensure_ascii=False))


def _scripted_transport(replies: list[httpx.Response], captured: list[httpx.Request]) -> httpx.MockTransport:
    """Serve *replies* in order and record every sent request."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return replies[len(captured) - 1]

    return httpx.MockTransport(handler)


def _openai_tool_calls(*calls: tuple[str, str, dict[str, Any]]) -> httpx.Response:
    """An OpenAI-compatible reply carrying structured ``tool_calls``."""
    return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for call_id, name, args in calls
    ]}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


def _openai_text(text: str) -> httpx.Response:
    """An OpenAI-compatible reply carrying only text content."""
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})


def test_assembled_definitions_are_exactly_builtins_plan_and_mcp_inputs() -> None:
    """The builder emits the 23 established built-in schemas, only ``plan``, and each MCP
    ``input_schema`` unprojected; names are unique and a colliding MCP tool is dropped.

    Script exclusion is structural: the only sources are these three (the builder
    takes an MCP manager and a catalog view; ``ToolRegistry`` has no script source).
    """
    lookup_before = copy.deepcopy(_MCP_LOOKUP_SCHEMA)
    assert tuple(BUILTIN_TOOL_SCHEMAS) == _BUILTIN_23
    assert tuple(PSEUDO_TOOL_SCHEMAS) == ("plan",)
    assert list(inspect.signature(build_tool_definitions).parameters) == ["mcp_manager", "model_catalog_view"]

    definitions = build_tool_definitions(_mcp_manager())

    assert definitions == _expected_definitions()
    names = [d["function"]["name"] for d in definitions]
    assert names == [*_BUILTIN_23, "plan", "mcp_lookup", "mcp_archive"]
    assert len(names) == len(set(names))
    shell = next(d for d in definitions if d["function"]["name"] == "shell")
    assert shell["function"]["description"] == BUILTIN_TOOL_SCHEMAS["shell"]["description"]
    lookup = definitions[-2]["function"]["parameters"]
    assert lookup == lookup_before and lookup["$defs"]["Options"]["additionalProperties"] is False
    assert lookup["properties"]["query"]["x-mcp-hint"] == {"cache": True, "weights": [1, 2]}
    assert _MCP_LOOKUP_SCHEMA == lookup_before


@pytest.mark.parametrize(
    ("adapter", "path"),
    [("openai", "/v1/chat/completions"), ("google", "/v1beta/openai/chat/completions")],
    ids=["openai", "google"],
)
def test_openai_compatible_native_round_trip_keeps_full_definitions(adapter: str, path: str) -> None:
    """OpenAI and Google (its OpenAI-compatible route) send the complete assembled
    definitions with ``tool_choice="auto"`` and no JSON response format, then parse
    structured calls (ID, name, parsed arguments) and plain text."""
    captured: list[httpx.Request] = []
    replies = [
        _openai_tool_calls(("c1", "shell_env_get", {"key": "B"}), ("c2", "mcp_lookup", {"query": "x"})),
        _openai_text("plain answer"),
    ]
    client = make_run_model_client(
        adapter, _scripted_transport(replies, captured), metadata=_NATIVE_META, upstream_id="native-model",
    )
    definitions = build_tool_definitions(_mcp_manager())
    # One binding per public invocation: a binding permits exactly one send.
    structured_state, text_state = CallAttemptState(2, 0), CallAttemptState(2, 0)
    try:
        with client.bind_attempt(structured_state):
            structured = client.chat_with_tools([{"role": "user", "content": "use tools"}], definitions)
        with client.bind_attempt(text_state):
            text = client.chat_with_tools([{"role": "user", "content": "just answer"}], definitions)
    finally:
        client.close()

    assert isinstance(structured, ChatResponse) and structured.is_tool_call
    assert [(c.id, c.name, c.arguments) for c in structured.tool_calls] == [
        ("c1", "shell_env_get", {"key": "B"}), ("c2", "mcp_lookup", {"query": "x"}),
    ]
    assert isinstance(text, ChatResponse) and text.text == "plain answer" and not text.tool_calls
    assert len(captured) == 2
    assert structured_state.outbound_attempts == text_state.outbound_attempts == 1
    for request in captured:
        assert request.url.path == path
        wire = json.loads(request.content)
        assert wire["model"] == "native-model"
        assert wire["tools"] == _as_wire(_expected_definitions())
        assert wire["tool_choice"] == "auto"
        for absent in ("response_format", "temperature", "top_p"):
            assert absent not in wire


def test_ollama_sdk_send_keeps_full_definitions_without_tool_choice() -> None:
    """Through the installed Ollama SDK (MockTransport behind it), the complete assembled
    definitions survive SDK serialization and placeholder restore; no ``tool_choice``,
    and structured calls and text are both parsed."""
    captured: list[httpx.Request] = []

    def ollama_reply(message: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "native-model", "created_at": "2026-10-28T00:00:00Z",
            "message": {"role": "assistant", **message}, "done": True,
            "prompt_eval_count": 4, "eval_count": 2,
        })

    replies = [
        ollama_reply({"content": "", "tool_calls": [
            {"function": {"name": "shell_env_get", "arguments": {"key": "B"}}},
        ]}),
        ollama_reply({"content": "plain answer"}),
    ]
    client = make_run_model_client(
        "ollama", _scripted_transport(replies, captured), metadata=_NATIVE_META, upstream_id="native-model",
    )
    definitions = build_tool_definitions(_mcp_manager())
    # One binding per public invocation: a binding permits exactly one send.
    structured_state, text_state = CallAttemptState(2, 0), CallAttemptState(2, 0)
    try:
        with client.bind_attempt(structured_state):
            structured = client.chat_with_tools([{"role": "user", "content": "use tools"}], definitions)
        with client.bind_attempt(text_state):
            text = client.chat_with_tools([{"role": "user", "content": "just answer"}], definitions)
    finally:
        client.close()

    assert [(c.name, c.arguments) for c in structured.tool_calls] == [("shell_env_get", {"key": "B"})]
    assert isinstance(text, ChatResponse) and text.text == "plain answer" and not text.tool_calls
    assert len(captured) == 2
    assert structured_state.outbound_attempts == text_state.outbound_attempts == 1
    for request in captured:
        assert str(request.url) == "https://ollama.com/api/chat"
        wire = json.loads(request.content)
        assert wire["model"] == "native-model"
        assert wire["tools"] == _as_wire(_expected_definitions())
        for absent in ("tool_choice", "format", "options"):
            assert absent not in wire


def test_native_consumer_dispatches_only_the_first_structured_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real ReAct loop: native first; of two structured calls only the first
    runs, without ``parse_json``; history gets its assistant call and matching tool
    result; a later native text reply is parsed in place with no JSON-mode request."""
    llm = NativeScriptedLLM([
        ChatResponse(tool_calls=[
            ToolCall("call-a", "shell_env_list", {}),
            ToolCall("call-b", "shell_env_get", {"key": "B"}),
        ]),
        ChatResponse(text=_FINISH_TEXT),
    ])
    executor = RecordingExecutor()
    parsed: list[str] = []
    real_parse = react_loop.parse_json

    def parse_spy(raw: str) -> Any:
        """Record what reaches the JSON parser, then delegate."""
        parsed.append(raw)
        return real_parse(raw)

    monkeypatch.setattr(react_loop, "parse_json", parse_spy)

    result, calls, _progress = run_react(llm, executor, "inspect the environment")

    assert result == "done"
    assert [(c.tool, c.args) for c in calls] == [("shell_env_list", {})]
    assert parsed and set(parsed) == {_FINISH_TEXT}
    assert len(llm.tool_calls_seen) == 2 and llm.json_mode_calls == []
    second = llm.tool_calls_seen[1]
    native = [m for m in second if m.get("tool_calls") or m.get("role") == "tool"]
    assert [m["role"] for m in native] == ["assistant", "tool"]
    (call,) = native[0]["tool_calls"]
    assert call["id"] == "call-a" and call["function"]["name"] == "shell_env_list"
    assert json.loads(call["function"]["arguments"]) == {}
    assert native[1]["tool_call_id"] == "call-a"
    assert native[1]["content"].startswith("Tool 'shell_env_list' succeeded")
    assert "call-b" not in json.dumps(second)


def test_real_loop_reuses_cached_full_definitions_on_every_native_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real pinned client inside the real ReAct loop: definitions are built once,
    every native request carries the identical complete graph with a stable
    definition revision, and only the first call of each response executes."""
    captured: list[httpx.Request] = []
    replies = [
        _openai_tool_calls(("call-a", "shell_env_list", {}), ("call-b", "shell_env_get", {"key": "B"})),
        _openai_tool_calls(("call-c", "shell_env_list", {})),
        _openai_text(_FINISH_TEXT),
    ]
    client = make_run_model_client(
        "openai", _scripted_transport(replies, captured), metadata=_NATIVE_META, upstream_id="native-model",
    )
    builds: list[list[dict[str, Any]]] = []
    real_build = react_loop.build_tool_definitions

    def build_spy(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Record each assembly the loop performs, then delegate."""
        built = real_build(*args, **kwargs)
        builds.append(built)
        return built

    # Delegating observer: each loop turn binds once for its actual send.
    binds: list[SimpleNamespace] = []
    real_bind: Callable[..., Any] = client.bind_attempt

    @contextmanager
    def bind_spy(
        state: CallAttemptState, *, manual: bool = False, definition_revision: object = 0,
    ) -> Iterator[None]:
        """Record revision, attempts and requests around the real binding."""
        seen = SimpleNamespace(
            revision=definition_revision, attempts=state.outbound_attempts, requests=len(captured),
        )
        binds.append(seen)
        try:
            with real_bind(state, manual=manual, definition_revision=definition_revision):
                yield
        finally:
            seen.attempts = state.outbound_attempts - seen.attempts
            seen.requests = len(captured) - seen.requests

    monkeypatch.setattr(react_loop, "build_tool_definitions", build_spy)
    monkeypatch.setattr(client, "bind_attempt", bind_spy)
    executor = RecordingExecutor()
    try:
        result, calls, _progress = run_react(client, executor, "inspect the environment")
    finally:
        client.close()

    assert result == "done"
    assert [c.tool for c in calls] == ["shell_env_list", "shell_env_list"]
    (built,) = builds
    assert built == build_tool_definitions()
    expected_revision = react_loop._definition_revision(SimpleNamespace(_tool_defs=built))
    # Every send carries the same full-definition revision.
    assert [b.revision for b in binds] == [expected_revision] * 3
    # Per turn exactly one send: there is no pre-send preview binding any more.
    assert [(b.attempts, b.requests) for b in binds] == [(1, 1)] * 3
    assert len(captured) == 3
    bodies = [json.loads(r.content) for r in captured]
    for body in bodies:
        assert body["tools"] == _as_wire(built)
        assert body["tool_choice"] == "auto"
    assert "call-b" not in json.dumps(bodies[1]["messages"])
    tool_ids = [m.get("tool_call_id") for m in bodies[2]["messages"] if m.get("role") == "tool"]
    assert tool_ids == ["call-a", "call-c"]


_FAKE_MCP_CANARY = "sk-canary-unregistered-2p2-not-a-vault-value"


def test_ordinary_mcp_schema_failure_falls_back_without_leaking_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ordinary run whose MCP boundary fails while building schemas keeps the
    built-in definitions on the wire and never renders the failure text, which may
    carry an arbitrary non-vault credential, in the result or any raw log record."""

    def failing_tools() -> list[Tool]:
        raise RuntimeError(f"schema listing failed auth={_FAKE_MCP_CANARY}")

    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai", _scripted_transport([_openai_text(_FINISH_TEXT)], captured),
        metadata=_NATIVE_META, upstream_id="native-model",
    )
    try:
        with caplog.at_level("DEBUG"):
            result, _calls, _progress = run_react(
                client, RecordingExecutor(), "do it",
                mcp_manager=SimpleNamespace(get_tools=failing_tools),
            )
    finally:
        client.close()

    assert result == "done"
    assert len(captured) == 1
    assert json.loads(captured[0].content)["tools"] == _as_wire(build_tool_definitions())
    assert any(
        r.levelname == "WARNING" and r.name == "react_loop" and "MCP tools skipped" in r.getMessage()
        for r in caplog.records
    )
    assert _FAKE_MCP_CANARY not in result
    assert _FAKE_MCP_CANARY not in caplog.text
    for record in caplog.records:
        assert _FAKE_MCP_CANARY not in record.getMessage()
        assert record.exc_info is None or _FAKE_MCP_CANARY not in repr(record.exc_info[1])


_CHAT_SIGNATURES = {
    "chat": ((("messages", inspect.Parameter.empty), ("system", None), ("progress_cb", None),
              ("json_mode", False)), "str"),
    "chat_with_fallback": ((("messages", inspect.Parameter.empty), ("system", None),
                            ("progress_cb", None), ("json_mode", False)), "str"),
    "chat_with_tools": ((("messages", inspect.Parameter.empty), ("tools", inspect.Parameter.empty),
                         ("system", None), ("progress_cb", None)), "ChatResponse"),
    "chat_with_tools_fallback": ((("messages", inspect.Parameter.empty), ("tools", inspect.Parameter.empty),
                                  ("system", None), ("progress_cb", None)), "ChatResponse"),
}


def _annotation_name(annotation: object) -> str:
    """Normalize a postponed (string) or evaluated annotation to its name."""
    return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))


@pytest.mark.parametrize("owner", [LLMProvider, LLMClient], ids=["protocol", "client"])
def test_llm_provider_chat_signatures_are_retained(owner: type) -> None:
    """The four established chat methods keep their parameters, defaults, kinds and
    return types on both the protocol and the concrete client."""
    for name, (params, returns) in _CHAT_SIGNATURES.items():
        signature = inspect.signature(getattr(owner, name))
        actual = list(signature.parameters.values())
        assert actual[0].name == "self"
        assert [(p.name, p.default) for p in actual[1:]] == list(params), name
        assert {p.kind for p in actual} == {inspect.Parameter.POSITIONAL_OR_KEYWORD}, name
        assert _annotation_name(signature.return_annotation) == returns, name


@pytest.mark.parametrize("native", [False, True], ids=["text-alias", "native-alias"])
def test_compatibility_alias_propagates_failure_from_one_pinned_send(
    monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    """``chat_with_fallback``/``chat_with_tools_fallback`` delegate once to the pinned
    method; a transient provider failure propagates to the shared retry owner after
    exactly one send to the same model, with no alternate model or protocol."""
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or httpx.Response(503, text="busy")),
        metadata=_NATIVE_META, upstream_id="native-model",
    )
    target = "chat_with_tools" if native else "chat"
    delegated: list[str] = []
    real_target = getattr(client, target)

    def delegate_spy(*args: Any, **kwargs: Any) -> Any:
        """Record the alias's delegation, then run the real pinned method."""
        delegated.append(target)
        return real_target(*args, **kwargs)

    monkeypatch.setattr(client, target, delegate_spy)
    state = CallAttemptState(2, 0)
    messages = [{"role": "user", "content": "request"}]
    try:
        with client.bind_attempt(state), pytest.raises(httpx.HTTPStatusError):
            if native:
                client.chat_with_tools_fallback(messages, build_tool_definitions())
            else:
                client.chat_with_fallback(messages)
    finally:
        client.close()

    assert delegated == [target]
    assert len(captured) == 1 and state.outbound_attempts == 1
    wire = json.loads(captured[0].content)
    assert wire["model"] == "native-model"
    assert ("tools" in wire) is native


# ---------------------------------------------------------------------------
# Native group integrity on a real httpx.MockTransport client (relocated from
# tests/test_request_preparation.py in simplify-model-runtime task 2.2).
# PHASE2 (tasks 6.7/10.1): the entry point ``_prepare_request`` is the
# preview-fit preparation slated for removal; rewrite only the call to the
# ordinary send path, preserving the wire/native/zero-HTTP assertions.
# ---------------------------------------------------------------------------

FINISH = {"choices": [{"message": {"content": '{"action":"finish","result":"done"}'}}]}
SUMMARY = {"choices": [{"message": {"content": "- earlier steps summarized"}}]}


def _client(handler, metadata, adapter="openai"):
    return make_run_model_client(
        adapter, httpx.MockTransport(handler), metadata=metadata, upstream_id="prep-model"
    )


def _recording(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        return httpx.Response(200, json=SUMMARY if "Summarize these" in body else FINISH)

    return handler


TOOL_DEF = {
    "type": "function",
    "function": {"name": "probe", "description": "d", "parameters": {"type": "object", "properties": {}}},
}


class TestNativeGroupsOnRealClient:
    def test_orphan_reproduction_yields_valid_native_wire(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=4_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = [TOOL_DEF]
        messages = [
            {"role": "user", "content": "GOAL"},
            {"role": "assistant", "content": "thinking " + "t " * 2500},
            {"role": "assistant", "content": "plan " + "p " * 2500, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "probe", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "probe", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "ra " + "x " * 2500},
            {"role": "tool", "tool_call_id": "b", "content": "rb " + "y " * 2500},
        ]
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        ctx.compaction_threshold_tokens = 3_000
        try:
            _prepare_request(ctx, state, "s", 0)
            out = state.messages
            assert out[state.goal_idx] == {"role": "user", "content": "GOAL"}
            ids = [m["tool_call_id"] for m in out if m["role"] == "tool"]
            calls = [c["id"] for m in out for c in m.get("tool_calls") or []]
            assert sorted(ids) == sorted(calls)
            for i, m in enumerate(out):
                if m["role"] == "tool":
                    j = i
                    while out[j]["role"] == "tool":
                        j -= 1
                    assert m["tool_call_id"] in [c["id"] for c in out[j].get("tool_calls") or []]
            assert all("Summarize these" in r.content.decode() for r in requests)
        finally:
            client.close()


def _bad_groups() -> dict[str, list[dict]]:
    ok = {"id": "a", "type": "function", "function": {"name": "probe", "arguments": "{}"}}

    def group(call, result):
        return [{"role": "assistant", "content": None, "tool_calls": [call]}, result]

    tool = {"role": "tool", "tool_call_id": "a", "content": "r"}
    return {
        "wrong_type": group({**ok, "type": "not-function"}, tool),
        "missing_args": group({**ok, "function": {"name": "probe"}}, tool),
        "invalid_json": group({**ok, "function": {"name": "probe", "arguments": "not-json"}}, tool),
        "list_id": group(ok, {**tool, "tool_call_id": ["a"]}),
        "dict_id": group(ok, {**tool, "tool_call_id": {"a": 1}}),
    }


class TestNativeValidationOnRealClient:
    @pytest.mark.parametrize("case", sorted(_bad_groups()))
    @pytest.mark.parametrize("forced", [False, True])
    def test_malformed_group_fails_with_zero_http(self, case, forced):
        import copy

        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=4_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = [TOOL_DEF]
        messages = [{"role": "user", "content": "GOAL"}] + _bad_groups()[case]
        if forced:
            messages += [{"role": "assistant", "content": "z " * 3000}, {"role": "user", "content": "y " * 3000}]
        before = copy.deepcopy(messages)
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        try:
            with pytest.raises(LLMPermanentError) as info:
                _prepare_request(ctx, state, "s", 0)
            assert info.value.__cause__ is None and info.value.__context__ is None
            assert requests == []
            assert state.messages == before
        finally:
            client.close()

    def test_reused_supplied_ids_across_complete_groups_send_unchanged(self):
        import copy

        from interfaces import ToolCall
        from native_turns import _append_native_tool_result

        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=4_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = [TOOL_DEF]
        messages = [{"role": "user", "content": "GOAL"}]
        _append_native_tool_result(messages, ToolCall(id="call_0", name="probe", arguments={"n": 1}), "r1")
        _append_native_tool_result(messages, ToolCall(id="call_0", name="probe", arguments={"n": 2}), "r2")
        before = copy.deepcopy(messages)
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        try:
            _prepare_request(ctx, state, "s", 0)
            assert state.messages == before
            client.chat([{"role": "system", "content": "s"}] + state.messages)
            body = requests[-1].content.decode()
            assert body.count('"call_0"') >= 4
        finally:
            client.close()


class TestMalformedHistoryRealClient:
    SECRET = "SECRET-MARK"

    def _state(self, extra_old: bool):
        a, b = f"a-{self.SECRET}", f"b-{self.SECRET}"
        messages = []
        if extra_old:
            messages.append({"role": "user", "content": "old " + "alpha beta " * 3000})
        messages += [
            {"role": "user", "content": "GOAL"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": a, "type": "function", "function": {"name": "probe", "arguments": "{}"}},
                {"id": b, "type": "function", "function": {"name": "probe", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": a, "content": "result"},
        ]
        return messages, 1 if extra_old else 0

    @pytest.mark.parametrize("oversized", [True, False], ids=["oversized", "fits"])
    def test_incomplete_group_makes_zero_total_http(self, oversized):
        import copy

        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=10_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = [TOOL_DEF]
        messages, goal_idx = self._state(oversized)
        before = copy.deepcopy(messages)
        state = _LoopState(messages=messages, goal_idx=goal_idx, max_steps=3)
        try:
            with pytest.raises(LLMPermanentError) as info:
                _prepare_request(ctx, state, "s", 0)
            assert self.SECRET not in str(info.value)
            assert requests == []
            assert state.messages == before
        finally:
            client.close()
