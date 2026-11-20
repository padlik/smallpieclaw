"""Tasks 3.1-3.2: terminal LLM failures reach callers truthfully.

A runner that returns the reserved ``❌`` terminal failure must be reported as a
failure by the supervisor (and so by parent retrieval, result logging and
scheduler completion) and by the plan runner, without whole-task replay.
Precedence everywhere: cancellation, then explicit structured outcomes, then the
reserved plain-string prefix.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from execution_plan import (
    ExecutionPlan,
    PlanExecutor,
    PlanStep,
    _standardize_sub_agent_result,
)
from react_loop import ReactContext
from sub_agent_registry import get_registry
from sub_agent_supervisor import CANCELLED_SENTINEL, SupervisionOptions
from tests.test_sub_agent_supervisor import FakeRunner, _seq_factory

FAILURE = "❌ ⏱️ Request timed out. Automatic retries were exhausted. Try the request again later."
EXPLICIT_SUCCESS_WITH_MARK = json.dumps({"success": True, "output": "❌ literal text, not a failure"})


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    registry = get_registry()
    for record in registry.list_active():
        record.cancel()
    for record in registry.list_active():
        registry.unregister(record.agent_id)


# ---------------------------------------------------------------------------
# 3.1 supervisor completion
# ---------------------------------------------------------------------------

def _supervise(make_builtin_executor, tmp_path, runner, *, scheduled=False):
    """Run one supervised sub-agent synchronously; return what every caller saw."""
    exc = make_builtin_executor(
        sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path),
    )
    result_calls: list[dict] = []
    completions: list = []
    options = SupervisionOptions(
        source="scheduled" if scheduled else "on-demand",
        job_tag="nightly" if scheduled else None,
        notify=scheduled,
        result_log_cb=lambda **kwargs: result_calls.append(kwargs),
        completion_cb=completions.append,
    )
    with patch.object(exc._supervisor._pool, "submit",
                      side_effect=lambda fn, *a, **kw: fn()):
        spawned = exc._exec_spawn_agent({"task": "work"}, options=options)
        retrieved = exc._exec_get_agent_result(
            {"agent_id": spawned["agent_id"], "timeout": 1}
        )
    record = get_registry().get_completed(runner.agent_id)
    return record, retrieved, result_calls, completions


def test_runtime_failure_is_recorded_as_failed(make_builtin_executor, tmp_path):
    record, _, _, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-fail", result=FAILURE),
    )
    assert record.status == "failed" and record.result == FAILURE


def test_parent_retrieval_of_runtime_failure_reports_failure(make_builtin_executor, tmp_path):
    _, retrieved, _, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-fail-get", result=FAILURE),
    )
    assert retrieved["success"] is False
    assert retrieved["status"] == "failed" and retrieved["error"] == FAILURE


def test_runtime_failure_is_logged_unsuccessful_with_its_text(make_builtin_executor, tmp_path):
    _, _, result_calls, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-fail-log", result=FAILURE),
    )
    assert [(c["result"], c["success"]) for c in result_calls] == [(FAILURE, False)]


def test_scheduled_completion_hands_off_failed_status(make_builtin_executor, tmp_path):
    _, _, _, completions = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-fail-sched", result=FAILURE),
        scheduled=True,
    )
    assert [(c.status, c.result) for c in completions] == [("failed", FAILURE)]


@pytest.mark.parametrize(
    "text",
    ["plain successful prose", '{"success": true, "output": "ok"}', EXPLICIT_SUCCESS_WITH_MARK],
    ids=["text", "json", "json-output-with-mark"],
)
def test_successful_text_and_json_outputs_are_retained(make_builtin_executor, tmp_path, text):
    record, retrieved, result_calls, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-ok", result=text),
    )
    assert record.status == "done" and record.result == text
    assert retrieved["success"] is True and retrieved["result"] == text
    assert [c["success"] for c in result_calls] == [True]


STRUCTURED_FAILURE = json.dumps(
    {"success": False, "output": "", "error": "failed", "exit_code": -1}
)


def test_explicit_structured_failure_is_reported_failed_with_its_content_unchanged(
    make_builtin_executor, tmp_path,
):
    record, retrieved, result_calls, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-struct-fail", result=STRUCTURED_FAILURE),
    )
    assert record.status == "failed" and record.result == STRUCTURED_FAILURE
    assert retrieved["success"] is False and retrieved["status"] == "failed"
    assert [(c["result"], c["success"]) for c in result_calls] == [(STRUCTURED_FAILURE, False)]


def test_embedded_success_true_does_not_rescue_a_plain_failure_text(
    make_builtin_executor, tmp_path,
):
    text = '❌ preface {"success": true, "output": "fine"}'
    record, retrieved, _, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-embedded-true", result=text),
    )
    assert record.status == "failed" and record.result == text
    assert retrieved["success"] is False


def test_whole_text_json_failure_with_surrounding_whitespace_is_failed(
    make_builtin_executor, tmp_path,
):
    text = f"\n  {STRUCTURED_FAILURE}\t\n"
    record, _, _, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-ws-fail", result=text),
    )
    assert record.status == "failed" and record.result == text


@pytest.mark.parametrize(
    "text",
    [
        'The report says {"success": false} but the work finished fine.',
        '```json\n{"success": false, "error": "x"}\n```',
        '{"success": false, "error": "x"',
        '{"success": "false", "output": "string flag"}',
        '{"success": 0, "output": "integer flag"}',
        '{"success": null}',
    ],
    ids=["prose-with-embedded-json", "fenced-json", "malformed-json",
         "string-flag", "integer-flag", "null-flag"],
)
def test_text_that_is_not_a_whole_boolean_outcome_stays_successful(
    make_builtin_executor, tmp_path, text,
):
    record, retrieved, result_calls, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-not-structured", result=text),
    )
    assert record.status == "done" and record.result == text
    assert retrieved["success"] is True
    assert [c["success"] for c in result_calls] == [True]


def test_cancellation_wins_over_a_failure_text(make_builtin_executor, tmp_path):
    runner = FakeRunner("sa-cancel-first")

    def cancel_then_fail(task):
        get_registry().get(runner.agent_id).cancel()
        return FAILURE

    runner.run = cancel_then_fail  # type: ignore[method-assign]
    exc = make_builtin_executor(
        sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path),
    )
    with patch.object(exc._supervisor._pool, "submit",
                      side_effect=lambda fn, *a, **kw: fn()):
        exc._exec_spawn_agent({"task": "work"})
    record = get_registry().get_completed(runner.agent_id)
    assert record.status == "cancelled" and record.result == CANCELLED_SENTINEL


def test_cancelled_sentinel_stays_cancelled(make_builtin_executor, tmp_path):
    record, _, _, _ = _supervise(
        make_builtin_executor, tmp_path, FakeRunner("sa-sentinel", result=CANCELLED_SENTINEL),
    )
    assert record.status == "cancelled"


# ---------------------------------------------------------------------------
# 3.2 plan runner normalization
# ---------------------------------------------------------------------------

def test_standardize_maps_plain_failure_text_to_nonrecoverable_failure():
    outcome = _standardize_sub_agent_result(FAILURE, "json")
    assert outcome["success"] is False and outcome["recoverable"] is False
    assert outcome["error"] == FAILURE


def test_standardize_keeps_explicit_successful_json_with_marked_output():
    outcome = _standardize_sub_agent_result(EXPLICIT_SUCCESS_WITH_MARK, "json")
    assert outcome["success"] is True
    assert outcome["output"] == "❌ literal text, not a failure"


def test_standardize_keeps_ordinary_text_successful():
    assert _standardize_sub_agent_result("all good", "text")["success"] is True


def test_standardize_keeps_cancellation_as_before():
    outcome = _standardize_sub_agent_result("[Cancelled]", "json")
    assert outcome["success"] is False and outcome["error"] == "Sub-agent was cancelled."


def _plan_ctx() -> ReactContext:
    return ReactContext(
        llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
    )


def _run_plan(steps, results_by_label):
    """Run a plan; every runner built is recorded by label, and closed."""
    built: list[tuple[str, MagicMock]] = []

    def factory(*_args, **kwargs):
        label = kwargs["label"]
        runner = MagicMock()
        runner.run.return_value = results_by_label[label]
        built.append((label, runner))
        return runner

    executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)
    outcome = executor.execute(ExecutionPlan(description="p", steps=steps), _plan_ctx())
    return outcome, built, executor


def test_plan_step_with_runtime_failure_fails_without_task_replay_or_diagnosis():
    outcome, built, _ = _run_plan(
        [PlanStep(id="a", tool="shell", args={})], {"plan-a": FAILURE},
    )
    step = outcome["results"]["a"]
    assert step["success"] is False and step["recoverable"] is False
    assert [label for label, _ in built] == ["plan-a"]  # no retry, no diagnose-a runner
    built[0][1].run.assert_called_once()


def test_plan_runtime_failure_skips_dependents_and_closes_runners():
    outcome, built, _ = _run_plan(
        [
            PlanStep(id="a", tool="shell", args={}),
            PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
        ],
        {"plan-a": FAILURE},
    )
    assert outcome["success"] is False
    assert "dependency 'a' did not succeed" in outcome["results"]["b"]["error"]
    assert [label for label, _ in built] == ["plan-a"]
    built[0][1].close.assert_called_once()


def test_plan_step_with_explicit_success_json_marked_output_still_succeeds():
    outcome, _, _ = _run_plan(
        [PlanStep(id="a", tool="shell", args={})], {"plan-a": EXPLICIT_SUCCESS_WITH_MARK},
    )
    assert outcome["success"] is True


def test_plan_cancellation_still_reports_cancelled_step():
    outcome, built, _ = _run_plan(
        [PlanStep(id="a", tool="shell", args={})], {"plan-a": "[Cancelled]"},
    )
    assert outcome["results"]["a"]["success"] is False
    assert [label for label, _ in built] == ["plan-a"]
