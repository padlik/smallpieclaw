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
    LLMPermanentError,
)
from react_loop import _LoopState, _prepare_request, _summarize_on_pin
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
    def test_recoverable_oversize_carries_numbers_only(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=2_000, output_tokens=200))
        try:
            with pytest.raises(LLMContextOverflowError) as info:
                client.chat([{"role": "user", "content": "secret-marker " * 4000}])
            exc = info.value
            assert exc.repair_input_ceiling == 1_800
            assert exc.input_tokens > 1_800
            assert "secret-marker" not in str(exc) and "prep-model" not in str(exc)
            assert requests == []
        finally:
            client.close()

    def test_provider_reported_overflow_has_no_numbers(self):
        assert LLMContextOverflowError("x").input_tokens is None
        assert LLMContextOverflowError("x", input_tokens=5, repair_input_ceiling=9).input_tokens is None

    def test_unknown_required_output_stays_plain_permanent(self):
        client = _client(
            lambda r: pytest.fail("no I/O"), ModelMetadata(context_tokens=2_000), adapter="anthropic"
        )
        try:
            with pytest.raises(LLMPermanentError) as info:
                client.chat([{"role": "user", "content": "word " * 4000}])
            assert not isinstance(info.value, LLMContextOverflowError)
        finally:
            client.close()

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
            result, _calls, _progress = run_react(client, RecordingExecutor(), "GOAL", short_term=short_term)
            assert result == "done"
            bodies = [r.content.decode() for r in requests]
            assert any("Summarize these" in b for b in bodies[:-1])
            assert "Compacted context" in bodies[-1] and "GOAL" in bodies[-1]
        finally:
            client.close()

    def test_irreducible_system_prompt_fails_with_zero_io(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=2_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        state = _LoopState(messages=[{"role": "user", "content": "goal"}], goal_idx=0, max_steps=3)
        try:
            with pytest.raises(LLMContextOverflowError):
                _prepare_request(ctx, state, "system " * 5000, 2_000, False)
            assert requests == []
        finally:
            client.close()

    def test_sub_256_budget_gets_forced_repair_or_final_reject(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=300, output_tokens=100))
        ctx = build_context(client, RecordingExecutor())
        messages = [
            {"role": "user", "content": "GOAL"},
            {"role": "assistant", "content": "a " * 2000},
            {"role": "user", "content": "b " * 2000},
            {"role": "assistant", "content": "c " * 2000},
            {"role": "user", "content": "d " * 2000},
        ]
        state = _LoopState(messages=messages, goal_idx=0, max_steps=3)
        try:
            try:
                _prepare_request(ctx, state, "s", 300, False)
            except LLMContextOverflowError:
                pass
            else:
                assert state.last_budget is not None
            assert state.messages is not messages
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

    def test_oversized_summary_prompt_has_zero_io(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=300, output_tokens=100))
        ctx = build_context(client, RecordingExecutor())
        try:
            with pytest.raises(LLMContextOverflowError):
                _summarize_on_pin(ctx, "word " * 5000)
            assert requests == []
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


def test_snapshot_publication_uses_preview_budget_with_real_client():
    from context_monitor import ContextMonitor

    monitor = ContextMonitor()
    client = _client(_recording([]), ModelMetadata(context_tokens=8_192, output_tokens=1_024))
    try:
        result, _calls, _progress = run_react(
            client, RecordingExecutor(), "do it", context_monitor=monitor
        )
        snapshot = monitor.read()
        assert result == "done"
        assert snapshot.effective_window == 8_192
        assert snapshot.completion_reserve == 1_024
    finally:
        client.close()


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


class TestIndispensableDiagnostic:
    @pytest.mark.parametrize(
        "goal, system, defs_words",
        [
            ("goal " * 4000, "s", 5),
            ("goal", "system " * 4000, 5),
            ("goal", "s", 4000),
            ("goal " * 700, "system " * 700, 700),
        ],
        ids=["goal", "system", "definitions", "combined"],
    )
    def test_irreducible_part_makes_zero_http(self, goal, system, defs_words):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=2_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = _big_tool_defs(defs_words)
        messages = _history(goal)
        state = _LoopState(messages=messages, goal_idx=len(messages) - 1, max_steps=3)
        before = list(messages)
        try:
            with pytest.raises(LLMContextOverflowError):
                _prepare_request(ctx, state, system, 2_000, True)
            assert requests == []
            assert state.messages == before
        finally:
            client.close()

    def test_fitting_diagnostic_but_failing_final_never_sends_diagnostic(self):
        requests = []
        client = _client(_recording(requests), ModelMetadata(context_tokens=2_000, output_tokens=200))
        ctx = build_context(client, RecordingExecutor())
        ctx._tool_defs = []
        try:
            budget = client.preview_request([{"role": "user", "content": "x"}], "s").hard_input_budget
            words = 1
            while client.preview_request(
                [{"role": "user", "content": "goal " * (words + 5)}], "s"
            ).input_tokens <= budget - 30:
                words += 5
            goal = "goal " * words
            assert client.preview_request([{"role": "user", "content": goal}], "s").input_tokens <= budget
            messages = _history(goal)
            state = _LoopState(messages=messages, goal_idx=len(messages) - 1, max_steps=3)
            with pytest.raises(LLMContextOverflowError):
                _prepare_request(ctx, state, "s", 2_000, False)
            bodies = [r.content.decode() for r in requests]
            assert all("Summarize these" in body for body in bodies)
        finally:
            client.close()
