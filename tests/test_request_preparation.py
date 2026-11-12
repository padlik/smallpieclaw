"""Request preparation: strict preview, bounded repair compaction, summary budget.

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
import pytest

from context_manager import maybe_compact
from model_registry import ModelMetadata
from model_request_budget import (
    ModelRequestBudgetError,
    compute_request_budget,
    repair_input_ceiling,
)
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


class TestRepairInputCeiling:
    def test_optional_reserve_bounded_by_input_limit(self):
        meta = ModelMetadata(context_tokens=8_192, input_tokens=5_000, output_tokens=1_024)
        assert repair_input_ceiling(meta, local_context_tokens=4096) == 5_000
        assert repair_input_ceiling(
            ModelMetadata(context_tokens=8_192, output_tokens=1_024), local_context_tokens=4096
        ) == 7_168

    def test_required_output_larger_than_context_is_c_minus_one(self):
        meta = ModelMetadata(context_tokens=10_000, output_tokens=12_000)
        assert repair_input_ceiling(meta, local_context_tokens=4096, output_cap_required=True) == 9_999
        budget = compute_request_budget(
            meta, input_tokens=100, local_context_tokens=4096, output_cap_required=True
        )
        assert (budget.output_cap, budget.hard_input_budget) == (9_900, 100)

    def test_input_limit_only_has_no_output_overlay(self):
        meta = ModelMetadata(input_tokens=5_000, output_tokens=65_536)
        assert repair_input_ceiling(meta, local_context_tokens=4096) == 5_000

    def test_neither_context_nor_input_assumes_local_context(self):
        assert repair_input_ceiling(ModelMetadata(), local_context_tokens=4096) == 3_072

    def test_fixed_graph_cap(self):
        meta = ModelMetadata(context_tokens=8_192)
        assert repair_input_ceiling(
            meta, local_context_tokens=4096, fixed_output_cap=1_024
        ) == 7_168

    @pytest.mark.parametrize(
        "meta, kwargs",
        [
            (ModelMetadata(context_tokens=8_192), {"output_cap_required": True}),
            (ModelMetadata(context_tokens=1_000, output_tokens=2_000), {}),
        ],
    )
    def test_unknown_required_output_or_impossible_reserve_is_rejected(self, meta, kwargs):
        with pytest.raises(ModelRequestBudgetError):
            repair_input_ceiling(meta, local_context_tokens=4096, **kwargs)


class TestOverflowAttributes:
    def test_provider_reported_overflow_has_no_numbers(self):
        assert LLMContextOverflowError("x").input_tokens is None
        assert LLMContextOverflowError("x", input_tokens=5, repair_input_ceiling=9).input_tokens is None

    def test_preview_exposes_budget_and_metadata_is_typed(self):
        client = _client(lambda r: pytest.fail("no I/O"), ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        try:
            preview = client.preview_request([{"role": "user", "content": "hi"}])
            assert preview.budget.hard_input_budget == preview.hard_input_budget == 7_168
            assert client.metadata == ModelMetadata(context_tokens=8_192, output_tokens=1_024)
        finally:
            client.close()


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
    def test_summary_has_own_budget_and_main_state_untouched(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=SUMMARY)

        client = _client(handler, ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        ctx = build_context(client, RecordingExecutor(), max_retries=2)
        state = _LoopState(messages=[], goal_idx=0, max_steps=1)
        try:
            assert _summarize_on_pin(ctx, "summarize me") == "- earlier steps summarized"
            assert len(calls) == 2
            assert state.attempt_state is None
        finally:
            client.close()

    def test_summary_failure_uses_deterministic_fallback(self):
        client = MagicMock()
        client.llm_cfg = {}
        messages = [{"role": "user", "content": "x " * 3000} for _ in range(6)]

        def summarize(_prompt):
            raise LLMContextOverflowError("too big")

        out, goal = maybe_compact(
            messages, "s", 500, client, force=True, repair_target=2_000,
            input_tokens=12_000, summarize=summarize,
        )
        assert any("Compacted context" in str(m["content"]) for m in out)
        assert out[goal]["content"].startswith("x")

    def test_force_works_below_256_floor(self):
        client = MagicMock()
        client.llm_cfg = {}
        messages = [{"role": "user", "content": "w " * 2000} for _ in range(6)]
        out, _goal = maybe_compact(
            messages, "s", 500, client, force=True, repair_target=120,
            input_tokens=9_000, summarize=lambda _p: "short",
        )
        assert out is not messages
        assert sum(len(str(m["content"])) for m in out) < sum(len(str(m["content"])) for m in messages)


TOOL_DEF = {
    "type": "function",
    "function": {"name": "probe", "description": "d", "parameters": {"type": "object", "properties": {}}},
}


def _big_tool_defs(words: int) -> list[dict]:
    definition = {**TOOL_DEF, "function": {**TOOL_DEF["function"], "description": "def " * words}}
    return [definition]


def _history(goal: str, n: int = 6) -> list[dict]:
    msgs = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"h{i} " + "alpha beta " * 300}
        for i in range(n)
    ]
    return msgs + [{"role": "user", "content": goal}]
