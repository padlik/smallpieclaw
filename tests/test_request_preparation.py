"""Request preparation with real provider transport and separate summary state.

Task 2.2 relocated the native-group real-client cases to
``tests/test_native_tool_contract.py`` and the preparation-failure checkpoint
cases to ``tests/test_react_loop_error_recovery.py`` /
``tests/test_agent_controller_resume.py``. Task 2.3 relocated
``TestSummaryCall::test_cancellation_propagates_through_compaction`` to
``tests/test_context_manager.py::test_cancellation_propagates_through_compaction``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from model_registry import ModelMetadata
from providers._errors import (
    LLMContextOverflowError,
)
from react_loop import _LoopState, _summarize_on_pin
from tests.execution_harness import RecordingExecutor, build_context, run_react
from tests.run_model_fixtures import make_run_model_client

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


class TestOverflowAttributes:
    def test_provider_reported_overflow_has_no_numbers(self):
        error = LLMContextOverflowError("x", status_code=413)
        assert error.status_code == 413 and LLMContextOverflowError("x", status_code=99).status_code is None
        assert not hasattr(error, "input_tokens") and not hasattr(error, "repair_input_ceiling")



class TestRealReactPreparation:
    def test_unguarded_real_client_with_extras_metadata_fits_without_stub(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        try:
            assert "vision" in client.llm_cfg["metadata"]
            result, _calls, _progress = run_react(client, RecordingExecutor(), "do it")
            assert result == "done"
            assert len(requests) == 1
        finally:
            client.close()

    def test_oversized_history_is_summarized_then_fits(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=9_000, output_tokens=200))
        short_term = MagicMock()
        short_term.get_messages.return_value = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"history {i} " + "alpha beta " * 400}
            for i in range(6)
        ]
        try:
            result, _calls, _progress = run_react(
                client, RecordingExecutor(), "GOAL", short_term=short_term,
                compaction_threshold_tokens=9_000,
            )
            assert result == "done"
            bodies = [r.content.decode() for r in requests]
            assert any("Summarize these" in b for b in bodies[:-1])
            assert "Compacted context" in bodies[-1] and "GOAL" in bodies[-1]
        finally:
            client.close()

class TestSummaryCall:
    def test_summary_is_one_shot_and_main_state_untouched(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=SUMMARY)

        client = _client(handler, ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        ctx = build_context(client, RecordingExecutor(), max_retries=2)
        state = _LoopState(messages=[], goal_idx=0, max_steps=1)
        try:
            assert _summarize_on_pin(ctx, "summarize me") == "- earlier steps summarized"
            assert len(calls) == 1
            assert state.attempt_state is None
        finally:
            client.close()
