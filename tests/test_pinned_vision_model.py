"""``vision_query`` routes only through the pinned run model; vision metadata never gates it.

Drives the real ``react_loop._exec_vision_query`` with a real pinned
``LLMClient`` over ``httpx.MockTransport``. Only the external HTTP boundary and
a verdict-only path policy (the file-read gate) are replaced. Direct-image
requests on the pinned client are covered in ``tests/test_p2_vision_fallback.py``
and the estimator image rows in ``tests/test_p3_token_estimation.py``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS, build_tool_definitions
from llm_client import LLMClient, LLMPermanentError
from model_registry import ModelMetadata
from path_policy import PathVerdict
from react_loop import ReactContext, _exec_vision_query
from tests.run_model_fixtures import make_run_model_client

_IMAGE_BYTES = b"fake-image-bytes"
_QUESTION = "What is in this picture?"
_PINNED_URL = "https://openai.example.test/v1/chat/completions"
# Flat per-image allowance charged by token_estimator (_IMAGE_TOKENS).
_IMAGE_TOKENS = 1000


class _AllowAllPolicy:
    """Verdict-only stand-in for ``PathPolicy.classify``; records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def classify(self, realpath: str, operation: str) -> tuple[PathVerdict, str]:
        """Allow every read and record the request."""
        self.calls.append((realpath, operation))
        return PathVerdict.ALLOWED, "r"


def _reply(request: httpx.Request) -> httpx.Response:
    """A deterministic OpenAI-compatible text reply."""
    del request
    return httpx.Response(200, json={"choices": [{"message": {"content": "a fake picture"}}], "usage": {}})


def _pinned(vision: bool | None, captured: list[httpx.Request], **metadata: Any) -> LLMClient:
    """A real pinned client whose every request is recorded."""
    meta = {"context_tokens": 8192, "output_tokens": 1024, **metadata}
    return make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or _reply(request)),
        metadata=ModelMetadata(vision=vision, tool_calling=True, **meta),
        upstream_id="pinned-model",
    )


def _image(tmp_path: Path) -> Path:
    """A harmless local image file read by the real encoder."""
    path = tmp_path / "picture.png"
    path.write_bytes(_IMAGE_BYTES)
    return path


def _context(client: LLMClient, policy: _AllowAllPolicy) -> ReactContext:
    """Minimal real context: pinned client, verdict-only policy, no confirmation."""
    return ReactContext(
        llm=client, tool_index=None, memory=None,
        builtin_executor=SimpleNamespace(path_policy=policy),
        mcp_manager=None, skill_registry=None, confirmation=None,
    )


def _record_chat_errors(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Delegating observer on the pinned ``chat``: records what the real call raised."""
    raised: list[BaseException] = []
    real_chat = client.chat

    def chat(*args: Any, **kwargs: Any) -> str:
        try:
            return real_chat(*args, **kwargs)
        except BaseException as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(client, "chat", chat)
    return raised


@pytest.mark.parametrize("vision", [False, None], ids=["cached-false", "unknown"])
def test_vision_query_with_unverified_vision_still_sends_image_on_pinned_route(
    tmp_path: Path, vision: bool | None,
) -> None:
    """False/unknown vision metadata is display only: the tool's image request goes out
    once on the pinned route, carrying the pinned upstream model and the encoded image."""
    captured: list[httpx.Request] = []
    client = _pinned(vision, captured)
    policy = _AllowAllPolicy()
    image = _image(tmp_path)
    try:
        outcome = _exec_vision_query(_context(client, policy), {"path": str(image), "question": _QUESTION})
    finally:
        client.close()

    assert (outcome["success"], outcome["output"]) == (True, "a fake picture")
    (request,) = captured
    assert str(request.url) == _PINNED_URL
    body = json.loads(request.content)
    assert body["model"] == "pinned-model"
    (message,) = body["messages"]
    assert message["content"][1]["type"] == "image_url"
    assert client.llm_cfg["model"] == "pinned-model"
    assert policy.calls == [(str(image.resolve()), "read")]


def test_vision_query_provider_rejection_is_a_normal_tool_failure_without_retarget(
    tmp_path: Path,
) -> None:
    """A genuine provider rejection of the image request fails the tool once with a
    sanitized message: one request, no second model, raw provider body not echoed."""
    captured: list[httpx.Request] = []
    client = make_run_model_client(
        "openai",
        httpx.MockTransport(lambda request: captured.append(request) or httpx.Response(
            400, json={"error": {"message": "raw-provider-body-canary: no image input"}},
        )),
        metadata=ModelMetadata(vision=False, tool_calling=True, context_tokens=8192, output_tokens=1024),
        upstream_id="pinned-model",
    )
    image = _image(tmp_path)
    try:
        outcome = _exec_vision_query(_context(client, _AllowAllPolicy()), {"path": str(image), "question": _QUESTION})
        assert client.llm_cfg["model"] == "pinned-model"
    finally:
        client.close()

    assert outcome["success"] is False
    assert outcome["error"].startswith("vision_query LLM call failed:")
    assert "raw-provider-body-canary" not in outcome["error"]
    assert len(captured) == 1


def test_vision_query_with_verified_vision_sends_image_on_pinned_route(tmp_path: Path) -> None:
    """Verified vision: exactly one request on the pinned route carrying the pinned
    upstream model, the question text and the encoded image."""
    captured: list[httpx.Request] = []
    client = _pinned(True, captured)
    image = _image(tmp_path)
    try:
        outcome = _exec_vision_query(_context(client, _AllowAllPolicy()), {"path": str(image), "question": _QUESTION})
    finally:
        client.close()

    assert (outcome["success"], outcome["output"]) == (True, "a fake picture")
    (request,) = captured
    assert str(request.url) == _PINNED_URL
    assert request.headers["authorization"] == "Bearer fake-test-key"
    body = json.loads(request.content)
    assert body["model"] == "pinned-model"
    (message,) = body["messages"]
    assert message["content"] == [
        {"type": "text", "text": _QUESTION},
        {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{base64.b64encode(_IMAGE_BYTES).decode('ascii')}",
        }},
    ]


def test_vision_query_has_no_model_override(tmp_path: Path) -> None:
    """The tool schema exposes only ``path`` and ``question``; a ``model`` (or endpoint)
    argument is ignored and the request still goes to the pinned model and route."""
    for definition in (
        BUILTIN_TOOL_SCHEMAS["vision_query"],
        next(d["function"] for d in build_tool_definitions() if d["function"]["name"] == "vision_query"),
    ):
        assert set(definition["parameters"]["properties"]) == {"path", "question"}
    # The descriptor documents exactly the same two arguments (no model argument).
    descriptor_args = BUILTIN_TOOLS["vision_query"].description.split("Args:", 1)[1].split("Returns", 1)[0]
    assert "path (str" in descriptor_args and "question (str" in descriptor_args
    assert "model" not in descriptor_args

    captured: list[httpx.Request] = []
    client = _pinned(True, captured)
    image = _image(tmp_path)
    args = {
        "path": str(image), "question": _QUESTION,
        "model": "other/vision-model", "base_url": "https://other.example.test/v1",
    }
    try:
        outcome = _exec_vision_query(_context(client, _AllowAllPolicy()), args)
    finally:
        client.close()

    assert outcome["success"] is True
    (request,) = captured
    assert str(request.url) == _PINNED_URL
    assert json.loads(request.content)["model"] == "pinned-model"
    assert client.llm_cfg["model"] == "pinned-model"


def test_vision_query_image_estimate_counts_toward_hard_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With verified vision, a question that fits alone exceeds the hard input budget
    once the image allowance is counted, and is rejected locally before any send."""
    captured: list[httpx.Request] = []
    client = _pinned(True, captured, context_tokens=1500, output_tokens=1024)
    image = _image(tmp_path)
    try:
        text_only = client.preview_request([{"role": "user", "content": _QUESTION}])
        assert text_only.input_tokens < text_only.hard_input_budget
        assert text_only.input_tokens + _IMAGE_TOKENS > text_only.hard_input_budget

        raised = _record_chat_errors(client, monkeypatch)
        outcome = _exec_vision_query(_context(client, _AllowAllPolicy()), {"path": str(image), "question": _QUESTION})

        (error,) = raised
        assert isinstance(error, LLMPermanentError)
        assert "local budget" in str(error)
        assert outcome["success"] is False and outcome["error"] == f"vision_query LLM call failed: {error}"
        assert captured == []
    finally:
        client.close()
