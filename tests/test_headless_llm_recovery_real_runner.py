"""Task 1.4: headless LLM failure through the real runner path.

Real chain: ``AgentRuntime.create`` -> ``SubAgentRunner`` -> ``AgentController``
-> ``react_loop`` -> real ``LLMClient`` on ``httpx.MockTransport``, with a real
``CheckpointStore`` (writing enabled) and a real ``ConfirmationManager``. Only
HTTP is scripted. Headless runs are not checkpoint-eligible: they end terminally
and promptly with no new checkpoint, and no live retry surface exists to wait on.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from unittest.mock import MagicMock

import httpx
import pytest

import agent_controller
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile
from checkpoint_store import CheckpointStore
from config_schema import AgentConfig, ExecutorPaths
from llm_client import LLMClient
from model_registry import RunModel
from tests.execution_harness import _NullMemory
from tests.test_agent_runtime_characterization import (
    _registry,
    _unavailable_embedding,
)
from tests.inline_futures import run_inline_future

PROMPT_BOUND_SECONDS = 20  # a terminal headless failure must return far sooner
AUTOMATIC_RETRIES = 2
PROFILES = [
    RuntimeProfile.ON_DEMAND_SUBAGENT,
    RuntimeProfile.SCHEDULED_AGENT,
    RuntimeProfile.PLAN_STEP_AGENT,
    RuntimeProfile.DIAGNOSTIC_AGENT,
]


class _NullToolIndex:
    def search(self, *_a: object, **_k: object) -> list:
        return []

    def all_tools(self) -> list:
        return []


class _TrackedLLM(LLMClient):
    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _TrackedStore(CheckpointStore):
    """Real store that remembers every ownership lease it hands out."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.owners: list = []

    def acquire_owner(self, trace_id: str):  # type: ignore[override]
        owner = super().acquire_owner(trace_id)
        if owner is not None:
            self.owners.append(owner)
        return owner


class _Chain:
    """Real runtime/runner chain on a scripted transport; every runner is wired alike."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                 make_builtin_executor, profile: RuntimeProfile,
                 respond) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[_TrackedLLM] = []
        self.runners: list = []
        self.respond = respond
        self.profile = profile
        self.store = _TrackedStore(str(tmp_path / "data"))
        self.checkpoint_dir = Path(self.store._checkpoint_dir)

        def chat(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.respond(request)

        def make_llm(run_model: RunModel, **kwargs: object) -> _TrackedLLM:
            client = _TrackedLLM(run_model, http_transport=httpx.MockTransport(chat), **kwargs)
            self.clients.append(client)
            return client

        monkeypatch.setattr(agent_controller, "LLMClient", make_llm)
        self.registry = _registry(tmp_path / "models")
        self.executor = make_builtin_executor()
        self.runtime = AgentRuntime(
            agent_cfg=AgentConfig(max_retries=AUTOMATIC_RETRIES, retry_delay=0),
            embedding_factory=_unavailable_embedding,
            paths=ExecutorPaths(tmp_dir=str(tmp_path / "tmp"),
                                downloads_dir=str(tmp_path / "dl"),
                                workspace_dir=str(tmp_path / "ws")),
            tool_index=_NullToolIndex(), base_memory=_NullMemory(),
            builtin_executor=self.executor, data_dir=str(tmp_path / "data"),
        )
        self.runner = self.build_runner(profile)

    def build_runner(self, profile: RuntimeProfile, **options: object):
        """Build one real runner and wire checkpointing (writes enabled)."""
        pin = (self.registry.admit_scheduled()
               if profile is RuntimeProfile.SCHEDULED_AGENT else self.registry.admit())
        runner = self.runtime.create(profile, RuntimeOptions(run_model=pin, **options))
        controller = runner._agent
        controller.checkpoint_store = self.store
        controller._checkpoint_enabled = True
        self.runners.append(runner)
        return runner

    def factory(self, model=None, context_key=None, label="on-demand", notify_fn=None,
                max_iterations=None, on_tool_trace=None, cancel_event=None, trace_id=None,
                context_payload=None, prompt_variant=None,
                runtime_profile=RuntimeProfile.ON_DEMAND_SUBAGENT):
        """Sub-agent factory with the production call contract (supervisor and plan)."""
        del model, notify_fn, on_tool_trace
        return self.build_runner(
            runtime_profile, max_iterations=max_iterations, context_key=context_key,
            context_payload=context_payload, prompt_variant=prompt_variant,
            trace_id=trace_id, cancel_event=cancel_event, label=label,
        )

    def run(self) -> tuple[str, float]:
        started = time.monotonic()
        result = self.runner.run("do the task")
        return result, time.monotonic() - started

    def checkpoint_files(self) -> list[str]:
        if not self.checkpoint_dir.exists():
            return []
        return sorted(os.listdir(self.checkpoint_dir))


def _unavailable(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "unavailable"}})


def _context_rejected(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json={"error": {"code": "context_length_exceeded",
                                               "message": "context_length_exceeded"}})


def _unknown_conflict(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(409, json={"error": {"message": "conflict"}})


@pytest.fixture
def chain_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_builtin_executor):
    chains: list[_Chain] = []

    def build(profile: RuntimeProfile, respond) -> _Chain:
        chain = _Chain(tmp_path, monkeypatch, make_builtin_executor, profile, respond)
        chains.append(chain)
        return chain

    yield build
    for chain in chains:
        for runner in chain.runners:
            runner.close()


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    ("respond", "sends", "guidance"),
    [
        pytest.param(_unavailable, 1 + AUTOMATIC_RETRIES, "exhausted", id="transient-exhausted"),
        pytest.param(_context_rejected, 1, "Reduce the request or context", id="context"),
        pytest.param(_unknown_conflict, 1, "No operator retry is available", id="unknown"),
    ],
)
def test_headless_failure_is_terminal_without_wait_token_or_checkpoint(
    chain_for, profile, respond, sends, guidance,
):
    chain = chain_for(profile, respond)

    result, elapsed = chain.run()

    assert result.startswith("❌") and guidance in result
    assert len(chain.requests) == sends  # exactly the existing automatic budget, no more
    assert elapsed < PROMPT_BOUND_SECONDS / 2
    assert chain.checkpoint_files() == []  # checkpointing is enabled yet nothing was written


def test_unknown_failure_does_not_claim_exhaustion(chain_for):
    chain = chain_for(RuntimeProfile.ON_DEMAND_SUBAGENT, _unknown_conflict)
    result, _ = chain.run()
    assert "exhausted" not in result


def test_headless_failure_releases_owned_resources(chain_for):
    chain = chain_for(RuntimeProfile.ON_DEMAND_SUBAGENT, _unavailable)

    chain.run()
    chain.runner.close()

    assert chain.store.owners, "the run never took a checkpoint lease"
    assert all(owner.released for owner in chain.store.owners)
    assert [client.close_count for client in chain.clients] == [1]
    assert not chain.runner.is_cancelled
    assert threading.active_count() >= 1


# ---------------------------------------------------------------------------
# 3.3 caller boundaries: supervisor and plan over the same real runner chain
# ---------------------------------------------------------------------------

def test_supervisor_reports_headless_llm_failure_and_cleans_up(chain_for):
    from unittest.mock import patch

    from sub_agent_registry import get_registry

    chain = chain_for(RuntimeProfile.ON_DEMAND_SUBAGENT, _unavailable)
    chain.runners.clear()
    chain.runner.close()
    chain.clients.clear()
    chain.requests.clear()
    chain.executor._sub_agent_factory = chain.factory

    with patch.object(chain.executor._supervisor._pool, "submit",
                      side_effect=run_inline_future):
        spawned = chain.executor._exec_spawn_agent({"task": "do the task"})
    retrieved = chain.executor._exec_get_agent_result(
        {"agent_id": spawned["agent_id"], "timeout": 1}
    )

    assert retrieved["success"] is False and retrieved["status"] == "failed"
    assert retrieved["error"].startswith("❌") and "exhausted" in retrieved["error"]
    assert len(chain.requests) == 1 + AUTOMATIC_RETRIES
    assert chain.checkpoint_files() == []
    (runner,) = chain.runners
    assert get_registry().get(runner.agent_id) is None  # registration released
    assert [client.close_count for client in chain.clients] == [1]  # owned client closed once


def test_plan_fails_step_once_without_replay_diagnosis_or_dependent_runs(chain_for):
    from execution_plan import ExecutionPlan, PlanExecutor, PlanStep
    from react_loop import ReactContext

    chain = chain_for(RuntimeProfile.PLAN_STEP_AGENT, _unavailable)
    chain.runners.clear()
    chain.runner.close()
    chain.clients.clear()
    chain.requests.clear()
    ctx = ReactContext(
        llm=MagicMock(), tool_index=_NullToolIndex(), memory=_NullMemory(),
        builtin_executor=None, mcp_manager=None, skill_registry=None,
    )
    plan = ExecutionPlan(description="p", steps=[
        PlanStep(id="a", tool="shell", args={}),
        PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
    ])

    outcome = PlanExecutor(max_concurrent=2, sub_agent_factory=chain.factory).execute(plan, ctx)

    step = outcome["results"]["a"]
    assert outcome["success"] is False
    assert step["success"] is False and step["recoverable"] is False
    assert "dependency 'a' did not succeed" in outcome["results"]["b"]["error"]
    assert len(chain.runners) == 1
    assert len(chain.requests) == 1 + AUTOMATIC_RETRIES  # one step run, no task retry or diagnosis
    assert chain.checkpoint_files() == []
    assert [client.close_count for client in chain.clients] == [1]
