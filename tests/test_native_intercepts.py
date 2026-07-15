"""Native tool-calling intercept coverage for react_loop.

These tests target the native tool-calling path in ``react_loop`` where a
provider returns structured ``ToolCall`` objects rather than json_mode text.
Three tool names are intercepted *before* the generic ``_dispatch_tool`` path —
``create_tool``, ``plan``, and ``vision_query`` — and one provider-capability
gap (``NotImplementedError``) must fall back to json_mode.

Each test scripts a native ``ChatResponse`` via ``NativeScriptedLLM`` and
asserts the intercept ran without reaching ``_dispatch_tool``.

Note the native ``plan`` argument shape: ``tc.arguments`` *is* the plan payload
(``{"description", "steps", "timeout"}``), unlike the json_mode path where the
payload is nested under ``action_obj["plan"]``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from confirmation import ConfirmationManager
from interfaces import ChatResponse, ToolCall
from tests.execution_harness import NativeScriptedLLM, RecordingExecutor, run_react


class TestNativePlanIntercept:
    """A native ``plan`` tool call runs the plan executor, not ``_dispatch_tool``."""

    def test_native_plan_builds_plan_from_arguments_directly(self):
        # Native `plan` args ARE the plan payload — no nested "plan" key (this is
        # the shape difference from json_mode's action_obj["plan"]).
        plan_args = {
            "description": "greet the world",
            "steps": [
                {"id": "s1", "tool": "shell", "args": {"command": "echo hi"}},
            ],
            "timeout": 120,
        }
        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[ToolCall(id="p1", name="plan", arguments=plan_args)]),
            ChatResponse(text='{"action": "finish", "result": "done"}'),
        ])
        ex = RecordingExecutor()

        # Capture the ExecutionPlan the intercept builds; the harness has no
        # sub-agent factory, so the real executor would just report one is
        # missing. Patching execute lets us both assert the plan shape and
        # return a clean success.
        captured: dict = {}

        def fake_execute(self, plan, ctx, progress_cb=None):
            captured["plan"] = plan
            return {
                "success": True,
                "results": {"s1": {"success": True, "output": "hi", "error": "", "exit_code": 0}},
                "errors": [],
            }

        with patch("execution_plan.PlanExecutor.execute", fake_execute), \
                patch("react_loop._dispatch_tool") as mock_dispatch:
            result, _calls, _progress = run_react(llm, ex, "greet")

        assert result == "done"
        # The plan intercept must not fall through to the generic tool dispatch.
        mock_dispatch.assert_not_called()

        # The ExecutionPlan was built from tc.arguments *directly*: a regression
        # that wrongly read tc.arguments["plan"] would yield an empty steps list.
        built = captured["plan"]
        assert built.description == "greet the world"
        assert built.timeout == 120
        assert len(built.steps) == 1
        step = built.steps[0]
        assert step.id == "s1"
        assert step.tool == "shell"
        assert step.args == {"command": "echo hi"}

        # The plan result is fed back to the model on the next turn.
        second = llm.tool_calls_seen[1]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "expected a tool-result message for the plan"
        assert "Plan execution results" in str(tool_msgs[0]["content"])


class TestNativeCreateToolIntercept:
    """A native ``create_tool`` call routes through the tool-creation flow."""

    def test_native_create_tool_triggers_creator(self):
        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[ToolCall(
                id="c1", name="create_tool",
                arguments={
                    "name": "greet",
                    "language": "python",
                    "code": "print('hi')",
                    "description": "greets the user",
                },
            )]),
            ChatResponse(text='{"action": "finish", "result": "done"}'),
        ])
        ex = RecordingExecutor()

        # Approve the creation so request_tool_create returns immediately rather
        # than blocking on operator input (its default is a 300s event wait).
        cm = ConfirmationManager()
        cm.request_tool_create = lambda token, tool_info, progress_cb: "create"

        creator = MagicMock()
        creator.create.return_value = {
            "success": True, "name": "greet", "path": "/tools_generated/greet.py",
        }

        with patch("react_loop._dispatch_tool") as mock_dispatch:
            result, _calls, _progress = run_react(
                llm, ex, "make a greeter", confirmation=cm, creator=creator,
            )

        assert result == "done"
        # create_tool has its own dispatch helper; it must not hit _dispatch_tool.
        mock_dispatch.assert_not_called()

        # The creator was invoked with the native argument values, in order.
        creator.create.assert_called_once_with(
            "greet", "python", "print('hi')", "greets the user",
        )

        # Success feedback is fed back to the model on the next turn.
        second = llm.tool_calls_seen[1]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "expected a tool-result message for create_tool"
        assert "created successfully" in str(tool_msgs[0]["content"])


class TestNativeVisionQueryIntercept:
    """A native ``vision_query`` call runs image analysis, not ``_dispatch_tool``."""

    def test_native_vision_query_returns_analysis(self):
        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[ToolCall(
                id="v1", name="vision_query",
                arguments={"path": "/tmp/pic.png", "question": "What is this?"},
            )]),
            ChatResponse(text='{"action": "finish", "result": "done"}'),
        ])
        ex = RecordingExecutor()

        # The vision path asks the LLM to describe the encoded image. Capture the
        # messages it hands the model so we can assert the native path/question
        # propagate, and return a fixed analysis string.
        captured_vision: dict = {}

        def fake_chat(messages, system=None, progress_cb=None, json_mode=False):
            captured_vision["messages"] = messages
            return "A golden retriever in a park."

        llm.chat = fake_chat

        # Patch encoding so the file need not exist on disk.
        with patch("react_loop._encode_images", return_value=["<b64>"]), \
                patch("react_loop._dispatch_tool") as mock_dispatch:
            result, _calls, _progress = run_react(llm, ex, "describe the image")

        assert result == "done"
        # vision_query is handled inline (it needs LLM access), never _dispatch_tool.
        mock_dispatch.assert_not_called()
        # It also never routes through the registered-tool executor.
        assert ex.calls == []

        # The native path + question flowed into the vision LLM call.
        vision_msg = captured_vision["messages"][0]
        assert vision_msg["content"] == "What is this?"
        assert vision_msg["images"] == ["/tmp/pic.png"]

        # The analysis is fed back to the model on the next turn.
        second = llm.tool_calls_seen[1]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "expected a tool-result message for vision_query"
        assert "A golden retriever in a park." in str(tool_msgs[0]["content"])


class TestNativeNotImplementedFallback:
    """A provider that cannot do native tool calling falls back to json_mode."""

    def test_not_implemented_falls_back_to_json_mode(self):
        # The native attempt raises NotImplementedError → the loop must switch to
        # the json_mode chat_with_fallback path and finish without propagating.
        llm = NativeScriptedLLM([NotImplementedError("no native tool calling")])
        ex = RecordingExecutor()

        # Spy on the fallback to confirm it is invoked with json_mode=True, while
        # still delegating to the scripted implementation (which returns finish).
        seen_json_mode: list[bool] = []
        orig = llm.chat_with_fallback

        def spy(messages, system=None, progress_cb=None, json_mode=False):
            seen_json_mode.append(json_mode)
            return orig(messages, system=system, progress_cb=progress_cb, json_mode=json_mode)

        llm.chat_with_fallback = spy

        result, _calls, _progress = run_react(llm, ex, "do something")

        # Completed normally — no exception propagated to the caller.
        assert result == "done"
        assert ex.calls == []
        # Native was attempted exactly once before the fallback fired.
        assert len(llm.tool_calls_seen) == 1
        # The fallback used json_mode=True and actually ran.
        assert seen_json_mode == [True]
        assert llm.json_mode_calls, "expected a json_mode fallback call"
