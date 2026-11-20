"""P3: execution-correctness regression scenarios.

High-level, deterministic ReAct flows built on tests/execution_harness.py. These
guard the P0/P1/P2 behaviors that are easy to silently regress:

- A multi-step tool flow succeeds and records ordered tool calls.
- Repeated non-JSON model output fails explicitly (not a fake ``finish``).
- A tool failure is surfaced to the model and is not reported as success.
- An image request runs only on an explicitly selected verified-vision pin;
  an unverified pin fails before any I/O and is never substituted.
- Each run is correlated by a trace ID visible in logs.
"""

from __future__ import annotations

import base64
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Iterator, Optional

import httpx
import pytest

from config_schema import AgentConfig, ProviderConfig, ResolvedProviderConnection
from confirmation import RETRY_PREFIX
from llm_client import LLMClient
from model_registry import (
    CatalogDocument,
    ModelMetadata,
    ModelRecord,
    ProviderState,
)
from tests.model_stores import ModelStores
from tests.execution_harness import (
    RecordingExecutor,
    ScriptedLLM,
    make_outcome,
    run_react,
)


class TestMultiStepFlow:
    def test_ordered_tool_calls_then_finish(self):
        llm = ScriptedLLM([
            '{"action": "tool", "tool": "shell", "args": {"command": "ls"}}',
            '{"action": "tool", "tool": "file_write", "args": {"path": "out.txt", "content": "x"}}',
            '{"action": "tool", "tool": "shell", "args": {"command": "cat out.txt"}}',
            '{"action": "finish", "result": "completed the task"}',
        ])
        ex = RecordingExecutor({
            "shell": make_outcome(output="ok"),
            "file_write": make_outcome(output="written"),
        })
        result, calls, _progress = run_react(llm, ex, "build and verify")
        assert result == "completed the task"
        assert ex.tool_order == ["shell", "file_write", "shell"]
        # The model saw the goal plus a tool-result message per executed tool.
        assert len(llm.calls) == 4

    def test_tool_result_fed_back_to_model(self):
        llm = ScriptedLLM([
            '{"action": "tool", "tool": "shell", "args": {"command": "whoami"}}',
            '{"action": "finish", "result": "done"}',
        ])
        ex = RecordingExecutor({"shell": make_outcome(output="OPERATOR_NAME")})
        run_react(llm, ex, "who am i")
        # The second LLM call must include the tool output in its message history.
        second_call_text = " ".join(str(m.get("content")) for m in llm.calls[1].messages)
        assert "OPERATOR_NAME" in second_call_text


class TestProtocolFailure:
    def test_repeated_non_json_fails_explicitly(self):
        llm = ScriptedLLM(["I am thinking out loud and not returning JSON at all."])
        ex = RecordingExecutor()
        result, calls, _progress = run_react(llm, ex, "do something")
        assert result.startswith("❌ Agent protocol error:")
        assert ex.calls == []  # no tool ran

    def test_protocol_failure_is_not_finish(self):
        llm = ScriptedLLM(["nope, still prose"])
        ex = RecordingExecutor()
        result, _calls, _progress = run_react(llm, ex, "task")
        assert "finish" not in result.lower() or result.startswith("❌")


class TestToolFailureSurfaced:
    def test_failed_tool_is_reported_as_failure_not_success(self):
        llm = ScriptedLLM([
            '{"action": "tool", "tool": "shell", "args": {"command": "false"}}',
            '{"action": "finish", "result": "acknowledged the failure"}',
        ])
        ex = RecordingExecutor({
            "shell": make_outcome(success=False, error="command failed", exit_code=1),
        })
        result, calls, _progress = run_react(llm, ex, "run failing command")
        assert ex.tool_order == ["shell"]
        # The follow-up LLM call must see a failure message, not a success.
        feedback = " ".join(str(m.get("content")) for m in llm.calls[1].messages)
        assert "failed" in feedback.lower()
        assert "succeeded" not in feedback.lower()
        assert result == "acknowledged the failure"


_VISION_URLS = {"pt": "https://text.example.test/v1", "pv": "https://vision.example.test/v1"}
_VISION_AGENT = AgentConfig(max_retries=0, retry_delay=0)
_VISION_GOAL = "what is in this picture?"
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfixture-image-bytes"


def _vision_record(provider: str, upstream: str, vision: Optional[bool]) -> ModelRecord:
    """Build a catalog record whose provider-reported fields are exactly its known fields."""
    values = {"context_tokens": 128_000, "output_tokens": 2048,
              "vision": vision, "tool_calling": True}
    return ModelRecord(f"{provider}/{upstream}", provider, upstream, f"{upstream} display",
                       ModelMetadata(**values))


def _vision_resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    """External credential resolver returning a synthetic per-provider key."""
    return ResolvedProviderConnection(
        provider_id=config.name, adapter=config.type, base_url=config.base_url,
        request_timeout=config.request_timeout, api_key=f"secret-{config.name}",
    )


class _NoDiscovery:
    """Discovery stand-in: selection and admission must never refresh."""

    def fetch(self, connection: object) -> object:
        """Fail loudly if discovery is reached."""
        raise AssertionError("discovery must not run during admission")


class _VisionWorld:
    """Real settings file and registry with a text and a verified-vision model."""

    def __init__(self, tmp_path: Path, stack: ExitStack, text_vision: Optional[bool]) -> None:
        """Persist the two-model catalog and write the fixture image."""
        self.stack = stack
        self.text = _vision_record("pt", "text-x", text_vision)
        self.vision = _vision_record("pv", "vision-x", True)
        providers = {n: ProviderConfig(n, "openai", f"env:KEY_{n}", url, 9)
                     for n, url in _VISION_URLS.items()}
        self.stores = ModelStores.at(tmp_path)
        self.stores.cache.write(CatalogDocument(
            1, 1, tuple(ProviderState(n, "openai", "2026-09-01T00:00:00Z", "fresh") for n in providers),
            (self.text, self.vision),
        ))
        self.registry = self.stores.registry(providers, _NoDiscovery(), _vision_resolve)
        self.image = tmp_path / "pic.png"
        self.image.write_bytes(_IMAGE_BYTES)
        self.requests: list[httpx.Request] = []
        self.reject_next = False

    def _answer(self, request: httpx.Request) -> httpx.Response:
        """Record the wire request and answer with an OpenAI-compatible finish."""
        self.requests.append(request)
        if self.reject_next:
            self.reject_next = False
            return httpx.Response(400, json={"error": {"message": "image input is not supported"}})
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": '{"action": "finish", "result": "saw the image"}'}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    def admit_selected(self, model: ModelRecord) -> LLMClient:
        """Explicitly select ``model`` as Main, admit it and build a closed-at-teardown client."""
        self.registry.select(model.id)
        client = LLMClient(self.registry.admit(), agent_config=_VISION_AGENT,
                           http_transport=httpx.MockTransport(self._answer))
        self.stack.callback(client.close)
        return client


@pytest.fixture
def vision_world(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_VisionWorld]:
    """Yield a world whose text model has ``request.param`` vision metadata."""
    with ExitStack() as stack:
        yield _VisionWorld(tmp_path, stack, getattr(request, "param", False))


def _image_urls(body: dict) -> list[str]:
    """Collect every image_url data URL in an OpenAI-compatible payload."""
    return [part["image_url"]["url"] for message in body["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]


class TestVisionRouting:
    """Image requests always go to the pinned model's own route; vision metadata never gates them.

    Replaces the retired ``test_image_request_uses_vision_fallback_not_primary``
    (raw-config automatic vision fallback): its image success and exact result
    are kept on an explicit vision selection, and a model with false/unknown
    vision metadata is sent the image itself instead of being refused locally or
    silently substituted.
    """

    def test_selected_verified_vision_pin_sends_image_and_finishes(self, vision_world):
        """The real client and loop send the image on the pinned vision route only."""
        client = vision_world.admit_selected(vision_world.vision)
        settings_bytes = vision_world.stores.snapshot()
        result, calls, _progress = run_react(
            client, RecordingExecutor(), _VISION_GOAL, images=[str(vision_world.image)],
        )
        assert result == "saw the image"
        assert calls == []
        assert len(vision_world.requests) == 1
        request = vision_world.requests[0]
        assert (request.url.host, request.url.path) == (
            "vision.example.test", "/v1/chat/completions")
        assert request.headers["authorization"] == "Bearer secret-pv"
        body = json.loads(request.content)
        assert body["model"] == "vision-x"
        expected_url = "data:image/png;base64," + base64.b64encode(_IMAGE_BYTES).decode("ascii")
        assert _image_urls(body) == [expected_url]
        # Pin, Main and persisted settings are untouched by the run.
        assert client.llm_cfg["id"] == vision_world.vision.id
        assert vision_world.registry.view().active_model_id == vision_world.vision.id
        assert vision_world.stores.snapshot() == settings_bytes

    @pytest.mark.parametrize("vision_world", [False, None], indirect=True,
                             ids=["cached-false", "unknown"])
    def test_unverified_pin_sends_image_on_its_own_route_without_substitution(self, vision_world):
        """False/unknown cached vision is display metadata: direct chat and the loop both
        send the image once each to the pinned model, with no alternate model."""
        client = vision_world.admit_selected(vision_world.text)
        settings_bytes = vision_world.stores.snapshot()
        images = [str(vision_world.image)]
        client.chat([{"role": "user", "content": _VISION_GOAL, "images": images}])
        assert len(vision_world.requests) == 1

        result, calls, progress = run_react(
            client, RecordingExecutor(), _VISION_GOAL, images=images,
        )
        assert result == "saw the image"
        assert calls == [] and not [m for m in progress if m.startswith(f"{RETRY_PREFIX}:")]
        assert len(vision_world.requests) == 2  # one provider call per request
        expected_url = "data:image/png;base64," + base64.b64encode(_IMAGE_BYTES).decode("ascii")
        for request in vision_world.requests:
            assert (request.url.host, request.url.path) == ("text.example.test", "/v1/chat/completions")
            body = json.loads(request.content)
            assert body["model"] == "text-x" and _image_urls(body) == [expected_url]
        assert client.llm_cfg["id"] == vision_world.text.id
        assert vision_world.registry.view().active_model_id == vision_world.text.id
        assert vision_world.stores.snapshot() == settings_bytes

    def test_provider_image_rejection_is_a_normal_failure_without_substitution(self, vision_world):
        """A text-only model's genuine provider rejection surfaces once, sanitized, with
        no retry on another model: the loop reports the provider failure."""
        client = vision_world.admit_selected(vision_world.text)
        vision_world.reject_next = True
        result, calls, progress = run_react(
            client, RecordingExecutor(), _VISION_GOAL, images=[str(vision_world.image)],
            retry_timeout_seconds=0,
        )
        assert result.startswith("❌")
        assert calls == [] and len(vision_world.requests) == 1
        assert {r.url.host for r in vision_world.requests} == {"text.example.test"}
        assert client.llm_cfg["id"] == vision_world.text.id
        assert vision_world.registry.view().active_model_id == vision_world.text.id
        assert "image input is not supported" not in result  # raw provider body is not echoed


class TestTraceCorrelation:
    def test_run_logs_include_trace_id(self, monkeypatch):
        # Trace identity is now bound into structlog contextvars at run entry
        # (a structured field on run/step events) rather than embedded in the raw
        # log message text.
        import agent_logging
        seen: dict = {}
        monkeypatch.setattr(agent_logging, "bind_run_context", lambda **kw: seen.update(kw))
        llm = ScriptedLLM(['{"action": "finish", "result": "ok"}'])
        ex = RecordingExecutor()
        run_react(llm, ex, "trivial", trace_id="r-scenario1")
        assert seen.get("trace") == "r-scenario1"
