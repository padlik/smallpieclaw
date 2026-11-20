"""ReAct uses the canonical ``chat`` / ``chat_with_tools`` methods (remove-obsolete-model-paths 2.1).

The native capability probe only resolves the ``chat_with_tools`` callable: it never
invokes the provider, native stays first, an unsupported case is handled locally before
any send, and no post-send failure falls back to JSON mode.
"""

from __future__ import annotations

from interfaces import ChatResponse
from providers._errors import LLMPermanentError
from tests.execution_harness import NativeScriptedLLM, RecordingExecutor, ScriptedLLM, run_react

_FINISH = '{"action": "finish", "result": "done"}'


def test_native_is_tried_first_and_the_probe_sends_nothing() -> None:
    llm = NativeScriptedLLM([ChatResponse(text=_FINISH)])

    result, _calls, _progress = run_react(llm, RecordingExecutor(), "goal")

    assert result == "done"
    assert len(llm.tool_calls_seen) == 1  # one native request for one turn: the probe added none
    assert llm.json_mode_calls == []


def test_client_without_chat_with_tools_uses_the_json_chat_path() -> None:
    llm = ScriptedLLM([_FINISH])
    assert not hasattr(llm, "chat_with_tools")

    result, _calls, _progress = run_react(llm, RecordingExecutor(), "goal")

    assert result == "done"
    assert len(llm.calls) == 1


def test_local_native_unsupported_converts_to_json_before_any_send() -> None:
    llm = NativeScriptedLLM([NotImplementedError("unsupported")])

    result, _calls, _progress = run_react(llm, RecordingExecutor(), "goal")

    assert result == "done"
    assert len(llm.tool_calls_seen) == 1
    assert len(llm.json_mode_calls) == 1


def test_post_send_native_failure_never_falls_back_to_json() -> None:
    llm = NativeScriptedLLM([LLMPermanentError("rejected after send")])

    # The failed send reaches the operator retry prompt; nobody answers, so a short
    # prompt timeout keeps the real retry policy but bounds the wait deterministically.
    run_react(llm, RecordingExecutor(), "goal", retry_timeout_seconds=1)

    assert llm.json_mode_calls == []
    assert len(llm.tool_calls_seen) == 1  # sent once natively, never re-sent as JSON
