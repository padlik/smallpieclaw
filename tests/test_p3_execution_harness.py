"""P3: execution-correctness regression scenarios.

High-level, deterministic ReAct flows built on tests/execution_harness.py. These
guard the P0/P1/P2 behaviors that are easy to silently regress:

- A multi-step tool flow succeeds and records ordered tool calls.
- Repeated non-JSON model output fails explicitly (not a fake ``finish``).
- A tool failure is surfaced to the model and is not reported as success.
- An image request skips a non-vision primary and uses a vision fallback.
- Each run is correlated by a trace ID visible in logs.
"""

from __future__ import annotations

from tests.execution_harness import (
    RecordingExecutor,
    ScriptedLLM,
    build_real_llm,
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


class TestVisionRouting:
    _NONVISION = {"name": "text", "provider": "openai", "model": "text-x",
                  "api_key": "sk", "base_url": "https://api.openai.com/v1"}
    _VISION = {"name": "vis", "provider": "openai", "model": "vision-x",
               "api_key": "sk", "base_url": "https://api.openai.com/v1", "vision": True}

    def test_image_request_uses_vision_fallback_not_primary(self):
        llm, used = build_real_llm(
            [self._NONVISION, self._VISION], default="text-x", fallback=["vision-x"],
            script=['{"action": "finish", "result": "saw the image"}'],
        )
        ex = RecordingExecutor()
        result, _calls, _progress = run_react(
            llm, ex, "what is in this picture?", images=["/tmp/pic.png"],
        )
        assert result == "saw the image"
        # The non-vision primary must never have been invoked.
        assert used == ["vision-x"]


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
