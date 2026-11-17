"""Task 4.2: run admission pins one exact connection for the whole run, at the real root.

Every case drives ``main._run`` through ``tests/root_startup_harness.py`` and
reaches its scope through the production caller:

- ORDINARY: the Telegram-facing ``agent_handler`` -> ``AgentController`` -> root
  ``run_client_factory``;
- ON-DEMAND: ``BuiltinExecutor._exec_spawn_agent`` -> ``SubAgentSupervisor`` ->
  root sub-agent factory -> ``AgentRuntime.create``;
- PLAN / DIAGNOSTIC: a real ``PlanExecutor`` late-bound to the root factory.

Replaced boundaries: provider HTTP (the harness router), the supervisor/plan
worker pools (inline) and ``agent_controller.react_loop`` (a narrow boundary
that only sends chat requests through the run's pinned client). The registry's
REAL credential resolver is wrapped by a delegating counter; admission and
construction are never faked. Mutations (rotated environment key, Main
selection, a refresh that drops the admitted model from the catalog) are
injected at deterministic points: just before the production ``LLMClient``
constructor runs and between the run's two chat turns. Synthetic canaries only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import agent_controller
import execution_plan
from agent_runtime import RuntimeProfile
from execution_plan import ExecutionPlan, PlanExecutor, PlanStep
from llm_client import LLMClient
from model_registry import ModelRegistryError
from react_loop import ReactContext
from tests.root_startup_harness import (
    ACME_HOST,
    GEM_HOST,
    INVALID_SAVED,
    MAIN_ID,
    OTHER_ID,
    SECRETS,
    RootHarness,
)
from tests.test_spawn_agent import _InlineExecutor

ACME_POST = f"https://{ACME_HOST}/v1/messages"
GEM_POST = f"https://{GEM_HOST}/v1beta/models/gem-1:generateContent"
UNCACHED_ID = "acme/claude-never-listed"
ROTATED = "rotated-canary-key-987654"
TRACE = "r-42000001"


@pytest.fixture
def root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Default-boundary root serving provider chat; leftovers closed after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch)
    harness.serve_provider_chat()
    try:
        yield harness
    finally:
        harness.release_leftovers()


class _Resolutions(list):
    """Connections whose credentials were resolved for admission; refresh is excluded."""

    paused = False


def _count_resolutions(root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> _Resolutions:
    """Wrap the registry's real credential resolver with a delegating counter."""
    seen = _Resolutions()
    resolve = root.registry._credential_resolver

    def counted(config: Any) -> Any:
        """Record the owning connection (unless paused for a refresh), then resolve."""
        if not seen.paused:
            seen.append(config.name)
        return resolve(config)

    monkeypatch.setattr(root.registry, "_credential_resolver", counted)
    return seen


def _mutate_world(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, resolutions: _Resolutions,
) -> None:
    """Rotate both env keys, move Main, and refresh so the catalog drops the old models."""
    monkeypatch.setenv("KEY_ACME", ROTATED)
    monkeypatch.setenv("KEY_GEM", ROTATED)
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Serve narrowed discovery pages; delegate everything else."""
        if request.method == "GET" and request.url.host == ACME_HOST:
            return httpx.Response(200, json={"data": [{
                "id": "claude-z", "display_name": "Z", "max_input_tokens": 100_000, "max_tokens": 4096,
            }], "has_more": False})
        if request.method == "GET" and request.url.host == GEM_HOST:
            return httpx.Response(200, json={"models": [{
                "name": "models/gem-9", "baseModelId": "gem-9",
                "inputTokenLimit": 32_000, "outputTokenLimit": 2048,
            }]})
        return original(request)

    monkeypatch.setattr(root, "_route", route)
    root.registry.select(OTHER_ID)
    resolutions.paused = True  # the deliberate refresh resolves discovery credentials
    try:
        root.registry.refresh()
    finally:
        resolutions.paused = False


def _mutate_before_client_construction(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[], None],
) -> list[int]:
    """Run *mutate* once just before the production ``LLMClient`` constructor body."""
    fired: list[int] = []
    init = LLMClient.__init__

    def init_after_admission(client: LLMClient, *args: Any, **kwargs: Any) -> None:
        """Mutate the world after admission, before the client reads its pin."""
        if not fired:
            fired.append(len(root.requests))
            mutate()
        init(client, *args, **kwargs)

    monkeypatch.setattr(LLMClient, "__init__", init_after_admission)
    return fired


class _TwoTurnLoop:
    """``react_loop`` boundary: two chats on the pinned client with a mutation between."""

    def __init__(self, between: Callable[[], None], answers: list[str] | None = None) -> None:
        """Run *between* after each run's first turn; return ``answers[i]`` for run i."""
        self.between = between
        self.answers = answers
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self, ctx: ReactContext, goal: str, progress_callback: Any = None,
        images: Any = None, **kwargs: Any,
    ) -> str:
        """Send two chat turns through ``ctx.llm`` and record the pin."""
        index = len(self.calls)
        call = SimpleNamespace(llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.calls.append(call)
        ctx.llm.chat([{"role": "user", "content": "turn-1"}])
        if index == 0:
            self.between()
        ctx.llm.chat([{"role": "user", "content": "turn-2"}])
        return self.answers[index] if self.answers else "ok"


def _chat_posts(root: RootHarness, since: int) -> list[httpx.Request]:
    """Provider chat POSTs recorded after request index *since*."""
    return [
        r for r in root.requests[since:]
        if r.method == "POST" and r.url.host in (ACME_HOST, GEM_HOST)
    ]


def _on_demand(root: RootHarness, monkeypatch: pytest.MonkeyPatch, model: str) -> dict[str, Any]:
    """Spawn one on-demand run through the real root executor and supervisor."""
    monkeypatch.setattr(root.builtin._supervisor, "_pool", _InlineExecutor())
    return root.builtin._exec_spawn_agent(
        {"task": "pin work", "model": model}, caller_depth=0, trace_id=TRACE,
    )


def _plan(root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run a one-step plan through a real PlanExecutor late-bound to the root factory."""
    monkeypatch.setattr(execution_plan, "ThreadPoolExecutor", _InlineExecutor)
    ctx = ReactContext(
        llm=None, tool_index=None, memory=None, builtin_executor=root.builtin,
        mcp_manager=None, skill_registry=None, trace_id=TRACE,
    )
    plan = ExecutionPlan(description="pin plan", steps=[PlanStep(id="a", tool="shell", args={})])
    return PlanExecutor(max_concurrent=1).execute(plan, ctx)


def test_ordinary_run_keeps_its_admitted_connection_across_turns_and_mutation(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary Main run: one resolution, original endpoint/key on both turns despite
    a between-turn mutation; the next run follows the new Main."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Run two ordinary requests; mutate after admission and between turns."""
        resolutions = _count_resolutions(started, monkeypatch)
        loop = _TwoTurnLoop(lambda: _mutate_world(started, monkeypatch, resolutions))
        monkeypatch.setattr(agent_controller, "react_loop", loop)
        start = len(started.requests)
        started.agent_handler(1, "first", None, trace_id=TRACE)
        outcome["first"] = _chat_posts(started, start)
        outcome["first_resolutions"] = list(resolutions)
        outcome["closed"] = loop.calls[0].llm._closed
        start = len(started.requests)
        started.agent_handler(1, "second", None, trace_id="r-42000002")
        outcome["second"] = _chat_posts(started, start)
        outcome["total_resolutions"] = list(resolutions)
        outcome["pins"] = [c.pin.model.id for c in loop.calls]

    root.start(saved_default=MAIN_ID, hold=hold)

    first = outcome["first"]
    assert [str(r.url) for r in first] == [ACME_POST, ACME_POST]
    assert {r.headers["x-api-key"] for r in first} == {SECRETS["KEY_ACME"]}
    assert {json.loads(r.content)["model"] for r in first} == {"claude-a"}
    assert outcome["first_resolutions"] == ["acme"]
    assert outcome["closed"] is True
    # The next new run observes the new Main (gem) and its current key.
    second = outcome["second"]
    assert [str(r.url) for r in second] == [GEM_POST, GEM_POST]
    assert {r.headers["x-goog-api-key"] for r in second} == {ROTATED}
    assert outcome["total_resolutions"] == ["acme", "gem"]
    assert outcome["pins"] == [MAIN_ID, OTHER_ID]
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_on_demand_uncached_explicit_run_survives_mutation_after_admission(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On-demand explicit uncached upstream: sent verbatim once per turn to its owning
    route with its original key even when the world changes between admission and
    construction; the next run reads the rotated key."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Spawn the same explicit target twice around mutations."""
        resolutions = _count_resolutions(started, monkeypatch)
        loop = _TwoTurnLoop(lambda: None, ["one", "two"])
        monkeypatch.setattr(agent_controller, "react_loop", loop)
        _mutate_before_client_construction(
            started, monkeypatch, lambda: _mutate_world(started, monkeypatch, resolutions),
        )
        start = len(started.requests)
        outcome["spawn1"] = _on_demand(started, monkeypatch, UNCACHED_ID)
        outcome["first"] = _chat_posts(started, start)
        outcome["first_resolutions"] = list(resolutions)
        start = len(started.requests)
        outcome["spawn2"] = _on_demand(started, monkeypatch, UNCACHED_ID)
        outcome["second"] = _chat_posts(started, start)
        outcome["total_resolutions"] = list(resolutions)

    root.start(saved_default=MAIN_ID, hold=hold)

    assert outcome["spawn1"]["success"] is True, outcome["spawn1"]
    first = outcome["first"]
    assert [str(r.url) for r in first] == [ACME_POST, ACME_POST]
    assert {r.headers["x-api-key"] for r in first} == {SECRETS["KEY_ACME"]}
    assert {json.loads(r.content)["model"] for r in first} == {"claude-never-listed"}
    assert outcome["first_resolutions"] == ["acme"]
    second = outcome["second"]
    assert {r.headers["x-api-key"] for r in second} == {ROTATED}
    assert outcome["total_resolutions"] == ["acme", "acme"]
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_plan_step_and_diagnostic_keep_their_admitted_main_snapshot(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan step snapshots Main at its admission; a diagnostic admits afresh later. The
    mutation between the step's two turns cannot move the step's client."""
    failure = json.dumps({
        "success": False, "output": "", "error": "denied", "exit_code": 1,
        "error_type": "permission_denied", "recoverable": False,
    })
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Run a failing one-step plan with a mutation between the step's turns."""
        started.registry.refresh()
        resolutions = _count_resolutions(started, monkeypatch)
        loop = _TwoTurnLoop(lambda: _mutate_world(started, monkeypatch, resolutions), [failure, "diagnosis"])
        monkeypatch.setattr(agent_controller, "react_loop", loop)
        start = len(started.requests)
        outcome["result"] = _plan(started, monkeypatch)
        outcome["posts"] = _chat_posts(started, start)
        outcome["resolutions"] = list(resolutions)
        outcome["pins"] = [c.pin.model.id for c in loop.calls]

    root.start(saved_default=MAIN_ID, hold=hold)

    step_posts, diag_posts = outcome["posts"][:2], outcome["posts"][2:]
    assert [str(r.url) for r in step_posts] == [ACME_POST, ACME_POST]
    assert {r.headers["x-api-key"] for r in step_posts} == {SECRETS["KEY_ACME"]}
    # The diagnostic is a new run: it follows the Main selected by then.
    assert [str(r.url) for r in diag_posts] == [GEM_POST, GEM_POST]
    assert {r.headers["x-goog-api-key"] for r in diag_posts} == {ROTATED}
    assert outcome["resolutions"] == ["acme", "gem"]
    assert outcome["pins"] == [MAIN_ID, OTHER_ID]
    assert root.unexpected == [] and root.open_clients_at_exit == []


@pytest.mark.parametrize(
    "profile", [RuntimeProfile.PLAN_STEP_AGENT, RuntimeProfile.DIAGNOSTIC_AGENT],
    ids=["plan-step", "diagnostic"],
)
def test_explicit_uncached_target_runs_without_main_for_plan_and_diagnostic(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, profile: RuntimeProfile,
) -> None:
    """Plan/diagnostic construction through the root factory accepts an explicit target:
    no Main, no cache, no discovery; one resolution; the upstream is sent verbatim."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Build one runner for *profile* through the installed root factory and run it."""
        resolutions = _count_resolutions(started, monkeypatch)
        loop = _TwoTurnLoop(lambda: None, ["done"])
        monkeypatch.setattr(agent_controller, "react_loop", loop)
        start = len(started.requests)
        runner = started.builtin._sub_agent_factory(
            model=UNCACHED_ID, label="explicit", runtime_profile=profile,
        )
        try:
            outcome["constructed"] = (list(resolutions), _chat_posts(started, start))
            outcome["answer"] = runner.run("go")
        finally:
            runner.close()
        outcome["posts"] = _chat_posts(started, start)
        outcome["resolutions"] = list(resolutions)

    root.start(saved_default=INVALID_SAVED, write_cache=False, hold=hold)

    assert outcome["constructed"] == (["acme"], [])
    assert outcome["resolutions"] == ["acme"]
    assert [str(r.url) for r in outcome["posts"]] == [ACME_POST, ACME_POST]
    assert {json.loads(r.content)["model"] for r in outcome["posts"]} == {"claude-never-listed"}
    assert {r.headers["x-api-key"] for r in outcome["posts"]} == {SECRETS["KEY_ACME"]}
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_implicit_requests_without_main_fail_locally_in_every_scope(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no active Main every implicit scope fails before credential resolution,
    client construction and provider I/O, with the actionable /models guidance."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Exercise ordinary, on-demand, plan and diagnostic implicit admission."""
        resolutions = _count_resolutions(started, monkeypatch)
        loop = _TwoTurnLoop(lambda: None)
        monkeypatch.setattr(agent_controller, "react_loop", loop)
        clients, start = len(started.llm_clients), len(started.requests)
        try:
            started.agent_handler(1, "hi", None, trace_id=TRACE)
        except ModelRegistryError as exc:  # Telegram renders the /models guidance from this code
            outcome["ordinary"] = exc.code
        outcome["on_demand"] = _on_demand(started, monkeypatch, None)  # type: ignore[arg-type]
        outcome["plan"] = _plan(started, monkeypatch)
        for profile in (RuntimeProfile.PLAN_STEP_AGENT, RuntimeProfile.DIAGNOSTIC_AGENT):
            try:
                started.builtin._sub_agent_factory(model=None, runtime_profile=profile)
            except ValueError as exc:
                outcome[profile.name] = str(exc)
        outcome["io"] = (
            list(resolutions), len(started.llm_clients) - clients, len(started.requests) - start,
        )
        outcome["loop_calls"] = len(loop.calls)

    root.start(saved_default=INVALID_SAVED, hold=hold)

    assert outcome["io"] == ([], 0, 0)
    assert outcome["loop_calls"] == 0
    assert outcome["ordinary"] == "no_active_selection"
    assert outcome["on_demand"]["success"] is False
    assert "/models" in outcome["on_demand"]["suggestion"]
    assert outcome["plan"]["success"] is False
    assert set(outcome) >= {"PLAN_STEP_AGENT", "DIAGNOSTIC_AGENT"}
    assert root.unexpected == []


@pytest.mark.parametrize(
    "profile",
    [RuntimeProfile.ON_DEMAND_SUBAGENT, RuntimeProfile.PLAN_STEP_AGENT, RuntimeProfile.DIAGNOSTIC_AGENT],
    ids=["on-demand", "plan-step", "diagnostic"],
)
@pytest.mark.parametrize(
    ("selector", "code"),
    [
        (" acme/claude-a", "unknown_connection"),
        ("nope/claude-a", "unknown_connection"),
        ("acme", "malformed_selector"),
        ("acme/", "malformed_selector"),
    ],
)
def test_bad_explicit_selectors_fail_locally_without_main_substitution(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, profile: RuntimeProfile,
    selector: str, code: str,
) -> None:
    """A padded/unknown connection or malformed shape fails before credential resolution,
    client construction and provider I/O, and never substitutes the active Main."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Try to build one runner for *profile* with the bad selector."""
        resolutions = _count_resolutions(started, monkeypatch)
        clients, start = len(started.llm_clients), len(started.requests)
        try:
            started.builtin._sub_agent_factory(model=selector, runtime_profile=profile)
        except ModelRegistryError as exc:
            outcome["code"] = exc.code
        outcome["io"] = (
            list(resolutions), len(started.llm_clients) - clients, len(started.requests) - start,
        )

    root.start(saved_default=MAIN_ID, hold=hold)

    assert outcome["code"] == code
    assert outcome["io"] == ([], 0, 0)
    assert root.unexpected == []
