"""Task 12.20: every sub-agent RuntimeProfile through the actual ``main._run`` root.

Each case drives the real root via ``tests/root_startup_harness.py`` (graph
disabled, default fake logging) and reaches its profile only through the
existing production caller, which is also the run's lifecycle owner:

- ON_DEMAND_SUBAGENT: root ``BuiltinExecutor._exec_spawn_agent`` → real
  ``SubAgentSupervisor.submit`` → root factory; the supervisor's ``finally``
  closes the runner (pool boundary runs the submitted work inline, once);
- SCHEDULED_AGENT: real ``Scheduler._run_job`` via the existing
  ``ScheduledRootProbe.fire`` (one fire only) → supervisor ``finally``;
- PLAN_STEP_AGENT: real ``PlanExecutor`` late-binding the root factory
  (``model=None``; a ``model`` key in step args stays opaque) → step ``finally``;
- DIAGNOSTIC_AGENT: the same real plan route with a failed step, so
  ``PlanExecutor._diagnose_step_failure`` builds, registers, runs, deregisters
  and closes the diagnostic runner itself.

Replaced boundaries only: the harness's external effects (provider HTTP is the
harness router with ``serve_provider_chat``), the supervisor/plan worker pools
(inline), and ``agent_controller.react_loop``. That loop boundary is narrow and
documented: per run it sends ONE real chat through the run's pinned client (real
wire, credential and per-model usage) and then dispatches ONE real action
through the production ``react_loop._dispatch_action`` path for the harmless
local built-in ``shell_env_list`` (real ``BuiltinExecutor`` / scheduled
dispatch view, real ``_emit_tool_trace`` into whatever ``ctx.on_tool_trace`` the
production caller supplied). It wraps a supplied ``on_tool_trace`` with a
delegating observer and never invents one: on-demand, scheduled and diagnostic
runs keep ``None``; plan steps keep ``PlanExecutor``'s disposition observer.
It does not admit, construct, close or account for anything. Its locally
recorded progress messages prove that dispatch emitted them, not that any
operator notification was delivered; the real loop's per-step ``on_step`` and
finish paths are not exercised here. Independent production callbacks are
proven separately: the real ``PlanExecutor.execute(progress_cb=...)`` messages
(step completed / plan finished, recorded with the active Main) and, for a
``notify = true`` scheduled job, the scheduler's real completion handler and
the success notice it delivers to the root Telegram notify path.

Hygiene: an autouse fixture restores the exact structlog context bindings that
existed before each test (real runs bind and the supervisor clears the calling
thread's context).

A fresh real ``TokenUsageRegistry`` replaces the process-wide one BEFORE
``root.start`` (the real runtime captures it at construction); monkeypatch
restores it. Every ownership assertion is captured before fixture hygiene.

Reused (not duplicated) evidence: the 7.2/7.3 selector and gate matrices, the
three-profile Main gate in ``tests/test_main.py`` and the root construction
matrix in ``tests/test_agent_runtime_characterization.py``. The one
construction-only case below closes its runner as hygiene; it makes no
lifecycle-ownership claim.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

import agent_controller
import execution_plan
import outcome_utils
import react_loop
import token_usage
from agent_controller import SubAgentRunner
from agent_runtime import RuntimeOptions, RuntimeProfile
from builtin_executor import ScheduledToolDispatch
from execution_plan import ExecutionPlan, PlanExecutor, PlanStep
from react_loop import ReactContext
from sub_agent_registry import (
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    get_registry,
)
from tests.root_startup_harness import (
    ACME_HOST,
    GEM_HOST,
    MAIN_ID,
    OTHER_ID,
    SCHEDULED_TAG,
    SECRETS,
    RootHarness,
    ScheduledRootProbe,
    scheduled_job_toml,
)
from tests.test_scheduled_model_admission import _assert_scheduled_root_run
from tests.test_spawn_agent import _InlineExecutor, _root_binding, _RootAttemptSpies
from token_usage import TokenUsageRegistry

PROBE_TOOL = "shell_env_list"
SPAWN_TRACE = "r-12200d01"
PLAN_TRACE = "r-12200d02"
OPAQUE_ARGS = {"model": OTHER_ID, "note": "opaque"}
SPAWN_ANSWER = "on-demand-profile-done"
PLAN_OUTPUT = "plan-profile-done"
DIAG_ANALYSIS = "diagnostic-profile-analysis: retry with read access"
# ``serve_provider_chat`` usage per reply: 3 prompt + 1 completion tokens.
ONE_REPLY = {"prompt": 3, "completion": 1, "total": 4}
GEM_POST = f"https://{GEM_HOST}/v1beta/models/gem-1:generateContent"
ACME_POST = f"https://{ACME_HOST}/v1/messages"


def _fresh_usage(monkeypatch: pytest.MonkeyPatch) -> TokenUsageRegistry:
    """Install a fresh real usage registry; must run before ``root.start``."""
    usage = TokenUsageRegistry()
    monkeypatch.setattr(token_usage, "_registry", usage)
    return usage


@pytest.fixture(autouse=True)
def _restore_log_context() -> Iterator[None]:
    """Hygiene: reinstate the exact structlog context bindings present before the test.

    Autouse, so it is set up before and torn down after the root fixtures, and
    it still runs if their cleanup raises.
    """
    saved = structlog.contextvars.get_contextvars()
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**saved)


@pytest.fixture
def profile_root(
    tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[RootHarness, TokenUsageRegistry]]:
    """Default-boundary root plus its fresh usage registry; hygiene after assertions."""
    usage = _fresh_usage(monkeypatch)
    root = RootHarness(tmp_xdg, monkeypatch)
    try:
        yield root, usage
    finally:
        root.release_leftovers()


@pytest.fixture
def scheduled_profile_root(
    tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[RootHarness, TokenUsageRegistry]]:
    """Real-scheduler root plus its fresh usage registry; hygiene after assertions."""
    usage = _fresh_usage(monkeypatch)
    root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
    try:
        yield root, usage
    finally:
        root.release_leftovers()


def _spy_runner_closes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Delegating spy: ``(agent_id, client_already_closed)`` per ``SubAgentRunner.close``."""
    closes: list[tuple[str, bool]] = []
    original = SubAgentRunner.close

    def close(runner: SubAgentRunner) -> None:
        """Record which runner the production owner closes, then delegate."""
        closes.append((runner.agent_id, runner._llm is not None and runner._llm._closed))
        original(runner)

    monkeypatch.setattr(SubAgentRunner, "close", close)
    return closes


def _dispatch_probe_tool(
    ctx: ReactContext, goal: str, progress_callback: Callable[[str], None] | None,
) -> SimpleNamespace:
    """Dispatch one real ``shell_env_list`` action through ``react_loop._dispatch_action``.

    Observes the run context the production caller built. A supplied
    ``on_tool_trace`` is wrapped by a delegating observer (the original still
    runs) only for this dispatch and then restored, also on failure; ``None``
    stays ``None``.
    """
    seen = SimpleNamespace(
        on_tool_trace=ctx.on_tool_trace, builtin=ctx.builtin_executor, depth=ctx.depth,
        payload=getattr(ctx, "_context_payload", None), variant=getattr(ctx, "_prompt_variant", None),
        traces=[], sink=[], progress=[],
    )
    supplied = ctx.on_tool_trace
    if supplied is not None:

        def observe(trace: Any) -> None:
            """Record the production trace, then deliver it to the supplied callback."""
            seen.traces.append(trace)
            supplied(trace)

        ctx.on_tool_trace = observe

    def progress(message: str) -> None:
        """Record dispatch progress and forward it to the run's own callback."""
        seen.progress.append(message)
        if progress_callback is not None:
            progress_callback(message)

    state = react_loop._LoopState(messages=[], goal_idx=0, max_steps=1)
    try:
        seen.returned = react_loop._dispatch_action(
            ctx, {"action": "tool", "tool": PROBE_TOOL, "args": {}},
            seen.sink.append, state, goal, time.time(), progress,
        )
    finally:
        ctx.on_tool_trace = supplied
    seen.guard_eligible = state.guard_eligible
    return seen


class _ProfileLoop:
    """Narrow ``agent_controller.react_loop`` boundary: one real chat, one real dispatch."""

    def __init__(self, answers: list[str]) -> None:
        """Return ``answers[i]`` from the i-th run."""
        self.answers = answers
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self, ctx: ReactContext, goal: str, progress_callback: Any = None,
        images: Any = None, **kwargs: Any,
    ) -> str:
        """Chat once on the run's pinned client, then dispatch the probe tool."""
        index = len(self.calls)
        call = SimpleNamespace(goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.calls.append(call)
        call.earlier_closed = [earlier.llm._closed for earlier in self.calls[:index]]
        call.reply = ctx.llm.chat([{"role": "user", "content": "ping"}])
        call.open_at_work = not ctx.llm._closed and not ctx.llm._http.is_closed
        call.dispatch = _dispatch_probe_tool(ctx, goal, progress_callback)
        return self.answers[index]


def _assert_probe_dispatched(seen: SimpleNamespace) -> None:
    """The real built-in ran once through the production action path."""
    assert seen.returned is None and seen.guard_eligible is True
    assert len(seen.sink) == 1 and seen.sink[0].startswith(f"Tool '{PROBE_TOOL}' succeeded")
    assert any(m.startswith(f"__TOOL_END__:ok:{PROBE_TOOL}") for m in seen.progress), seen.progress
    assert seen.depth == 1


def _execute_root_plan(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any],
    closes: list[tuple[str, bool]],
) -> None:
    """Run a one-step plan through a real PlanExecutor late-bound to the root factory."""
    outcome["binding"] = _root_binding(root)
    spies = _RootAttemptSpies(root, monkeypatch)
    outcome["spies"] = spies
    monkeypatch.setattr(execution_plan, "ThreadPoolExecutor", _InlineExecutor)
    executor = PlanExecutor(max_concurrent=1)
    ctx = ReactContext(
        llm=None, tool_index=None, memory=None, builtin_executor=root.builtin,
        mcp_manager=None, skill_registry=None, trace_id=PLAN_TRACE,
    )
    _runtime, _registry, factory = root.factory_binding()
    outcome["late_bound"] = (executor._sub_agent_factory is None, executor._get_factory(ctx) is factory)
    args = dict(OPAQUE_ARGS)
    plan = ExecutionPlan(description="profile plan", steps=[PlanStep(id="a", tool="shell", args=args)])
    outcome["args"] = args
    progress: list[tuple[str, str | None]] = []
    outcome["progress"] = progress
    # Real plan progress callback through the existing ``execute`` API (12.13 data shape).
    outcome["result"] = executor.execute(
        plan, ctx,
        progress_cb=lambda msg: progress.append((msg, root.registry.view().active_model_id)),
    )
    # Production ownership snapshot, before any fixture hygiene.
    outcome["closes"] = list(closes)
    outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
    outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
    outcome["view_after"] = root.registry.view()
    outcome["settings_after"] = root.paths.models_cache_file.read_bytes()


def test_on_demand_explicit_selector_runs_supervised_on_its_pin(
    profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON_DEMAND explicit B under active Main A: real spawn/supervisor, B's wire and usage,
    real built-in dispatch, on-demand source, supervisor-owned close; Main unchanged."""
    root, usage = profile_root
    root.serve_provider_chat()
    loop = _ProfileLoop([SPAWN_ANSWER])
    monkeypatch.setattr(agent_controller, "react_loop", loop)
    closes = _spy_runner_closes(monkeypatch)
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Spawn through the root executor, retrieve, drain, snapshot ownership."""
        outcome["binding"] = _root_binding(started)
        spies = _RootAttemptSpies(started, monkeypatch)
        outcome["spies"] = spies
        supervisor = started.builtin._supervisor
        monkeypatch.setattr(supervisor, "_pool", _InlineExecutor())
        spawned = started.builtin._exec_spawn_agent(
            {"task": "profile work", "model": OTHER_ID}, caller_depth=0, trace_id=SPAWN_TRACE,
        )
        outcome["spawned"] = spawned
        outcome["closes"] = list(closes)
        outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
        outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
        outcome["fetched"] = started.builtin._exec_get_agent_result(
            {"agent_id": spawned.get("agent_id", ""), "timeout": 5}, caller_tag=f"main {SPAWN_TRACE}",
        )
        outcome["drain_errors"] = supervisor.drain_owned_children(SPAWN_TRACE, cancel=False, timeout=5)
        outcome["view_after"] = started.registry.view()
        outcome["settings_after"] = started.paths.models_cache_file.read_bytes()

    root.start(saved_default=MAIN_ID, hold=hold)
    spies: _RootAttemptSpies = outcome["spies"]

    assert outcome["binding"] == (True, True, True)
    assert spies.view_before.active_model_id == MAIN_ID
    spawned = outcome["spawned"]
    assert spawned["success"] is True, spawned
    agent_id = spawned["agent_id"]
    # One ordinary admission of the explicit selector; the pin reaches the real runtime.
    assert spies.admits == {"admit": [OTHER_ID], "admit_scheduled": []} and spies.codes == []
    (pin,) = spies.pins
    assert pin.model.id == OTHER_ID and spies.resolved == ["gem"]
    ((profile, options),) = spies.creates
    assert profile is RuntimeProfile.ON_DEMAND_SUBAGENT and isinstance(options, RuntimeOptions)
    assert options.run_model is pin and options.trace_id == SPAWN_TRACE
    (call,) = loop.calls
    assert call.pin is pin and call.reply == "pong" and call.open_at_work is True
    assert spies.new_llm_clients() == [call.llm]
    # Wire: exactly one request, to B's own endpoint with B's credential.
    (post,) = [r for r in spies.provider_requests() if r.method == "POST"]
    assert str(post.url) == GEM_POST and post.headers["x-goog-api-key"] == SECRETS["KEY_GEM"]
    assert "x-api-key" not in post.headers
    # Exact per-model usage on the fresh shared registry.
    assert usage.get_today() == {OTHER_ID: ONE_REPLY}
    # Real dispatch on the shared root executor; on-demand supplies no trace callback.
    seen = call.dispatch
    _assert_probe_dispatched(seen)
    assert seen.builtin is root.builtin
    assert seen.on_tool_trace is None and seen.traces == []
    assert isinstance(options.context_payload, dict) and seen.payload is options.context_payload
    assert seen.variant is None
    # On-demand source, released; supervisor-owned close happened inside the run.
    assert spies.registered == [(agent_id, SOURCE_ON_DEMAND)]
    assert get_registry().get(agent_id) is None
    assert outcome["closes"] == [(agent_id, False)]
    assert outcome["llm_closed"] == [True] and outcome["http_open"] == []
    fetched = outcome["fetched"]
    assert fetched["status"] == "done" and fetched["result"] == SPAWN_ANSWER, fetched
    assert outcome["drain_errors"] == []
    # Main A, its revision and the persisted settings are unchanged.
    assert outcome["view_after"] == spies.view_before
    assert outcome["settings_after"] == spies.settings_before
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_scheduled_explicit_selector_preserves_profile_lifecycle(
    scheduled_profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHEDULED explicit B: the full 7.3 root assertions plus exact usage, scheduled
    dispatch policy, no trace callback, and the supervisor-owned close (one fire)."""
    root, usage = scheduled_profile_root
    root.serve_provider_chat()
    closes = _spy_runner_closes(monkeypatch)
    probes: list[ScheduledRootProbe] = []
    dispatches: list[SimpleNamespace] = []
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Install the existing probe, add the dispatch after its chat, fire once."""
        probe = ScheduledRootProbe(started, monkeypatch)
        probes.append(probe)
        chat_loop = probe._loop

        def loop(ctx: ReactContext, goal: str, progress_callback: Any = None,
                 images: Any = None, **kwargs: Any) -> str:
            """Delegate the probe's chat, then dispatch the probe tool in the same run."""
            answer = chat_loop(ctx, goal, progress_callback, images, **kwargs)
            seen = _dispatch_probe_tool(ctx, goal, progress_callback)
            seen.open_at_work = not ctx.llm._closed and not ctx.llm._http.is_closed
            dispatches.append(seen)
            return answer

        monkeypatch.setattr(probe, "_loop", loop)
        probe.fire()
        outcome["closes"] = list(closes)

    root.start(
        saved_default=MAIN_ID, hold=hold, scheduler_toml=scheduled_job_toml(f'"{OTHER_ID}"'),
    )
    (probe,) = probes

    # Existing 7.3 evidence: one admit_scheduled, pin, profile/source/context, wire,
    # terminal record, released tracking, owned clients closed, Main/settings unchanged.
    _assert_scheduled_root_run(root, probe, raw=OTHER_ID, pin_id=OTHER_ID)
    assert usage.get_today() == {OTHER_ID: ONE_REPLY}
    (seen,) = dispatches
    _assert_probe_dispatched(seen)
    assert seen.open_at_work is True
    assert isinstance(seen.builtin, ScheduledToolDispatch) and seen.builtin._executor is root.builtin
    assert seen.on_tool_trace is None and seen.traces == []
    assert seen.payload == {} and seen.variant is None
    ((agent_id, _source),) = probe.registered
    assert outcome["closes"] == [(agent_id, False)]


def test_scheduled_owner_notice_contains_admitted_pin(
    scheduled_profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHEDULED explicit B with ``notify = true``: the supervisor hands the scheduler's
    real completion handler the admitted pin B, and the scheduler delivers its real
    success notice to the root Telegram notify path (one fire)."""
    root, usage = scheduled_profile_root
    root.serve_provider_chat()
    closes = _spy_runner_closes(monkeypatch)
    toml = scheduled_job_toml(f'"{OTHER_ID}"')
    assert toml.count("notify = false\n") == 1
    toml = toml.replace("notify = false\n", "notify = true\n")
    probes: list[ScheduledRootProbe] = []
    completions: list[Any] = []
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Observe the real completion handler, then fire the job once."""
        probe = ScheduledRootProbe(started, monkeypatch)
        probes.append(probe)
        make_handler = started.scheduler._run_completion_handler

        def handler_spy(run_id: str, meta: dict) -> Callable[[Any], None]:
            """Wrap the real per-run handler with a delegating observer."""
            complete = make_handler(run_id, meta)

            def observe(completion: Any) -> None:
                """Record the supervisor's handoff, then run the real handler."""
                completions.append(completion)
                complete(completion)

            return observe

        monkeypatch.setattr(started.scheduler, "_run_completion_handler", handler_spy)
        probe.fire()
        outcome["closes"] = list(closes)

    root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)
    (probe,) = probes
    after = probe.after

    assert root.scheduler._jobs_meta[SCHEDULED_TAG]["notify"] is True
    assert probe.admits == {"admit": [], "admit_scheduled": [OTHER_ID]} and probe.codes == []
    (pin,) = probe.pins
    assert pin.model.id == OTHER_ID
    ((profile, options),) = probe.creates
    assert profile is RuntimeProfile.SCHEDULED_AGENT and options.run_model is pin
    (call,) = probe.loop_calls
    assert call.pin is pin and call.reply == "pong"
    (post,) = after.provider_posts
    assert str(post.url) == GEM_POST and post.headers["x-goog-api-key"] == SECRETS["KEY_GEM"]
    assert usage.get_today() == {OTHER_ID: ONE_REPLY}
    # Owner terminal state: one done record under the admitted run, tracking released.
    (entry,) = probe.at_admission
    (run_id,) = entry.run_ids
    (record,) = after.records
    assert (record["run_id"], record["status"], record["model"]) == (run_id, "done", OTHER_ID)
    assert after.tracked is False and after.active_children == []
    # Real handoff carries the admitted pin's canonical ID, not Main A.
    (completion,) = completions
    assert (completion.tag, completion.status, completion.result, completion.model) == (
        SCHEDULED_TAG, "done", "scheduled-answer", OTHER_ID,
    )
    # Real success notice delivered through the root notify path, as production formats it.
    assert after.notices == [
        f"<b>✅ Scheduled job finished: {SCHEDULED_TAG}</b>\n\n"
        "<blockquote expandable>scheduled-answer</blockquote>"
    ]
    # Owned client closed by the supervisor before fixture hygiene; Main unchanged.
    ((agent_id, _source),) = probe.registered
    assert outcome["closes"] == [(agent_id, False)]
    assert after.llm_closed == [True] and after.http_open == []
    assert after.view == probe.view_before and after.view.active_model_id == MAIN_ID
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_plan_step_inherits_main_through_real_plan_executor(
    profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN_STEP: model=None inherits Main A despite opaque args; the real disposition
    observer receives the real dispatch trace; PlanExecutor closes the runner."""
    root, usage = profile_root
    root.serve_provider_chat()
    loop = _ProfileLoop([json.dumps({"success": True, "output": PLAN_OUTPUT, "error": "", "exit_code": 0})])
    monkeypatch.setattr(agent_controller, "react_loop", loop)
    closes = _spy_runner_closes(monkeypatch)
    outcome: dict[str, Any] = {}


    def hold(r: RootHarness) -> None:
        """Deliberate /models-style refresh first (startup no longer rediscovers a cached catalog;
        Anthropic budgeting needs the provider-reported max_tokens), then the plan run."""
        r.registry.refresh()
        _execute_root_plan(r, monkeypatch, outcome, closes)

    root.start(saved_default=MAIN_ID, hold=hold)
    spies: _RootAttemptSpies = outcome["spies"]

    assert outcome["binding"] == (True, True, True) and outcome["late_bound"] == (True, True)
    result = outcome["result"]
    assert result["success"] is True, result
    assert result["results"]["a"]["output"] == PLAN_OUTPUT
    # The real PlanExecutor progress callback reported the step under active Main A.
    assert ("Step 'a' completed successfully", MAIN_ID) in outcome["progress"]
    assert {active for _msg, active in outcome["progress"]} == {MAIN_ID}
    # The plan's execution credit comes only from the production trace observer.
    assert outcome_utils.read_execution(result) == "executed"
    assert spies.admits == {"admit": [None], "admit_scheduled": []} and spies.codes == []
    (pin,) = spies.pins
    assert pin.model.id == MAIN_ID and spies.resolved == ["acme"]
    ((profile, options),) = spies.creates
    assert profile is RuntimeProfile.PLAN_STEP_AGENT and options.run_model is pin
    assert (options.label, options.trace_id, options.prompt_variant) == ("plan-a", PLAN_TRACE, "sub-agent")
    (call,) = loop.calls
    assert call.pin is pin and call.open_at_work is True and call.reply == "pong"
    assert f"Arguments: {json.dumps(OPAQUE_ARGS, ensure_ascii=False)}" in call.goal
    seen = call.dispatch
    _assert_probe_dispatched(seen)
    assert seen.builtin is root.builtin and seen.variant == "sub-agent"
    assert seen.on_tool_trace is not None
    (trace,) = seen.traces
    assert (trace.tool_name, trace.success, trace.execution_disposition) == (PROBE_TOOL, True, "executed")
    (post,) = [r for r in spies.provider_requests() if r.method == "POST"]
    assert str(post.url) == ACME_POST and post.headers["x-api-key"] == SECRETS["KEY_ACME"]
    assert json.loads(post.content)["model"] == "claude-a"
    assert usage.get_today() == {MAIN_ID: ONE_REPLY}
    ((agent_id, source),) = spies.registered
    assert source == SOURCE_PLAN_STEP and get_registry().get(agent_id) is None
    assert outcome["closes"] == [(agent_id, False)]
    assert outcome["llm_closed"] == [True] and outcome["http_open"] == []
    assert outcome["args"] == OPAQUE_ARGS
    assert outcome["view_after"] == spies.view_before
    assert outcome["settings_after"] == spies.settings_before
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_failed_plan_step_triggers_real_diagnostic_on_inherited_main(
    profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIAGNOSTIC: a non-recoverable failed step makes PlanExecutor build a diagnostic
    runner (model=None → Main A), register/run/deregister/close it, and return its text."""
    root, usage = profile_root
    root.serve_provider_chat()
    failure = {
        "success": False, "output": "", "error": "access denied", "exit_code": 1,
        "error_type": "permission_denied", "recoverable": False,
    }
    loop = _ProfileLoop([json.dumps(failure), DIAG_ANALYSIS])
    monkeypatch.setattr(agent_controller, "react_loop", loop)
    closes = _spy_runner_closes(monkeypatch)
    outcome: dict[str, Any] = {}


    def hold(r: RootHarness) -> None:
        """Deliberate /models-style refresh first (startup no longer rediscovers a cached catalog;
        Anthropic budgeting needs the provider-reported max_tokens), then the plan run."""
        r.registry.refresh()
        _execute_root_plan(r, monkeypatch, outcome, closes)

    root.start(saved_default=MAIN_ID, hold=hold)
    spies: _RootAttemptSpies = outcome["spies"]

    assert outcome["binding"] == (True, True, True) and outcome["late_bound"] == (True, True)
    result = outcome["result"]
    step = result["results"]["a"]
    assert result["success"] is False and step["success"] is False
    assert step["error"] == "access denied" and step["diagnosis"] == DIAG_ANALYSIS
    assert result["errors"] == [f"Step 'a' diagnosis: {DIAG_ANALYSIS}", "Step 'a' failed: access denied"]
    # The real PlanExecutor finished callback reported the failure under active Main A.
    finished = [
        (msg, active) for msg, active in outcome["progress"]
        if msg.startswith("Plan 'profile plan' finished: ")
    ]
    assert len(finished) == 1, outcome["progress"]
    assert finished[0][0].startswith("Plan 'profile plan' finished: success=False, errors=2, elapsed=")
    assert finished[0][1] == MAIN_ID
    assert {active for _msg, active in outcome["progress"]} == {MAIN_ID}
    # Two ordinary admissions, both inheriting Main A (production passes model=None).
    assert spies.admits == {"admit": [None, None], "admit_scheduled": []} and spies.codes == []
    assert [p.model.id for p in spies.pins] == [MAIN_ID, MAIN_ID]
    assert spies.resolved == ["acme", "acme"]
    (plan_profile, plan_options), (diag_profile, diag_options) = spies.creates
    assert plan_profile is RuntimeProfile.PLAN_STEP_AGENT and plan_options.run_model is spies.pins[0]
    assert diag_profile is RuntimeProfile.DIAGNOSTIC_AGENT and diag_options.run_model is spies.pins[1]
    assert (diag_options.label, diag_options.trace_id) == ("diagnose-a", PLAN_TRACE)
    assert diag_options.context_payload is None and diag_options.prompt_variant is None
    plan_call, diag_call = loop.calls
    assert plan_call.pin is spies.pins[0] and diag_call.pin is spies.pins[1]
    assert diag_call.goal.startswith(
        "Analyze why plan step 'a' using tool 'shell' failed with error_type 'permission_denied'"
    )
    # Diagnostic work ran on its own open client after the step runner was closed.
    assert diag_call.open_at_work is True and diag_call.earlier_closed == [True]
    _assert_probe_dispatched(plan_call.dispatch)
    _assert_probe_dispatched(diag_call.dispatch)
    assert plan_call.dispatch.on_tool_trace is not None and len(plan_call.dispatch.traces) == 1
    # Diagnostic runs keep production semantics: no trace callback.
    assert diag_call.dispatch.on_tool_trace is None and diag_call.dispatch.traces == []
    assert diag_call.dispatch.builtin is root.builtin
    posts = [r for r in spies.provider_requests() if r.method == "POST"]
    assert [str(p.url) for p in posts] == [ACME_POST, ACME_POST]
    assert {p.headers["x-api-key"] for p in posts} == {SECRETS["KEY_ACME"]}
    assert usage.get_today() == {MAIN_ID: {"prompt": 6, "completion": 2, "total": 8}}
    (plan_id, plan_source), (diag_id, diag_source) = spies.registered
    assert (plan_source, diag_source) == (SOURCE_PLAN_STEP, SOURCE_DIAGNOSTIC)
    assert get_registry().get(plan_id) is None and get_registry().get(diag_id) is None
    assert outcome["closes"] == [(plan_id, False), (diag_id, False)]
    assert spies.new_llm_clients() == [plan_call.llm, diag_call.llm]
    assert outcome["llm_closed"] == [True, True] and outcome["http_open"] == []
    assert outcome["view_after"] == spies.view_before
    assert outcome["settings_after"] == spies.settings_before
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_diagnostic_root_factory_admits_explicit_selector_construction_only(
    profile_root: tuple[RootHarness, TokenUsageRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONSTRUCTION ONLY: the root factory's DIAGNOSTIC branch admits an explicit B once
    and forwards that pin. Never run; the close below is hygiene, not ownership proof."""
    root, usage = profile_root
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Construct one diagnostic runner through the installed factory, inspect, close."""
        spies = _RootAttemptSpies(started, monkeypatch)
        outcome["spies"] = spies
        runner = started.builtin._sub_agent_factory(
            model=OTHER_ID, label="diag-construct", runtime_profile=RuntimeProfile.DIAGNOSTIC_AGENT,
        )
        try:
            outcome["runner"] = (runner.model_id, runner._llm._run_model, runner.label)
            outcome["requests"] = spies.provider_requests()
        finally:
            runner.close()  # test hygiene only

    root.start(saved_default=MAIN_ID, hold=hold)
    spies: _RootAttemptSpies = outcome["spies"]

    assert spies.admits == {"admit": [OTHER_ID], "admit_scheduled": []}
    (pin,) = spies.pins
    ((profile, options),) = spies.creates
    assert profile is RuntimeProfile.DIAGNOSTIC_AGENT and options.run_model is pin
    assert outcome["runner"] == (OTHER_ID, pin, "diag-construct")
    assert outcome["requests"] == [] and spies.registered == []
    assert usage.get_today() == {}
    assert root.registry.view().active_model_id == MAIN_ID
