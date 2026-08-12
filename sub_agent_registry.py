"""
sub_agent_registry.py — Global registry of active sub-agents.

Tracks all running SubAgentRunner instances so they can be listed
(/agents command) and cancelled (/agents cancel <id>).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Closed set of source categories for visible sub-agent executions.
# See ADR-0006 (use-source-categories-for-agent-visibility).
SOURCE_ON_DEMAND = "on-demand"
SOURCE_SCHEDULED = "scheduled"
SOURCE_PLAN_STEP = "plan-step"
SOURCE_DIAGNOSTIC = "diagnostic"

# All source categories visible to operators through /agents and counted by
# /status. Ordering is display order.
VISIBLE_SOURCES = (
    SOURCE_ON_DEMAND,
    SOURCE_SCHEDULED,
    SOURCE_PLAN_STEP,
    SOURCE_DIAGNOSTIC,
)

# Sources that count against the global ``max_subagents`` capacity guard and are
# targeted by ``/agents cancel managed``. Plan-step and diagnostic runs are
# visible/cancellable but governed by PlanExecutor's own concurrency controls.
CAPACITY_COUNTED_SOURCES = frozenset({SOURCE_ON_DEMAND, SOURCE_SCHEDULED})


@dataclass
class SubAgentRecord:
    agent_id: str
    label: str          # context_key, job tag, or "on-demand"
    model: str
    task_preview: str   # first 80 chars of task
    started_at: float   # time.time()
    source: str         # one of VISIBLE_SOURCES
    # mutable fields updated by the runner
    iteration: int = 0
    max_iterations: int = 8
    # result fields — populated when the sub-agent finishes
    status: str = "running"          # "running" | "done" | "failed" | "cancelled"
    result: Optional[str] = None     # final output text (or file path for response_format="file")
    result_type: str = "text"        # "text" | "json" | "file"
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _result_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _llm_client: object = field(default=None, repr=False)  # LLMClient — for immediate HTTP interrupt
    _timeout_cancelled: bool = field(default=False, repr=False)  # set by get_agent_result on timeout
    prompt_id: Optional[str] = field(default=None, repr=False)

    def cancel(self) -> None:
        self._cancel_event.set()
        # Close the HTTP transport to immediately interrupt any in-progress LLM request.
        # For openai/google/anthropic: closes self._http (httpx.Client).
        # For ollama: also closes the ollama Client's internal httpx transport.
        # The resulting transport exception is caught as LLMCancelledError in _with_retry.
        if self._llm_client is not None:
            try:
                self._llm_client.close_http()  # type: ignore[attr-defined]
            except Exception:
                pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def timeout_cancelled(self) -> bool:
        """True when get_agent_result cancelled this run due to a timeout."""
        return self._timeout_cancelled

    def signal_result(self) -> None:
        """Signal that the sub-agent result is available."""
        self._result_event.set()

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    def elapsed_str(self) -> str:
        secs = int(self.elapsed_seconds)
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"


class SubAgentRegistry:
    _COMPLETED_TTL: float = 300.0

    def __init__(self):
        self._lock = threading.Lock()
        self._agents: dict[str, SubAgentRecord] = {}
        self._completed: dict[str, SubAgentRecord] = {}
        self._completed_at: dict[str, float] = {}

    def register(self, record: SubAgentRecord) -> None:
        with self._lock:
            self._agents[record.agent_id] = record

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            rec = self._agents.pop(agent_id, None)
            if rec is not None and rec.status in ("done", "failed", "cancelled"):
                now = time.time()
                self._completed[agent_id] = rec
                self._completed_at[agent_id] = now
                cutoff = now - self._COMPLETED_TTL
                stale = [k for k, t in self._completed_at.items() if t < cutoff]
                for k in stale:
                    self._completed.pop(k, None)
                    self._completed_at.pop(k, None)

    def get(self, agent_id: str) -> Optional[SubAgentRecord]:
        with self._lock:
            return self._agents.get(agent_id)

    def get_completed(self, agent_id: str) -> Optional[SubAgentRecord]:
        """Return a recently-completed record from the TTL cache, or None."""
        with self._lock:
            rec = self._completed.get(agent_id)
            if rec is None:
                return None
            age = time.time() - self._completed_at.get(agent_id, 0.0)
            if age > self._COMPLETED_TTL:
                self._completed.pop(agent_id, None)
                self._completed_at.pop(agent_id, None)
                return None
            return rec

    def find_by_label(self, label: str) -> Optional[SubAgentRecord]:
        """Find a record by label (e.g. scheduler job tag)."""
        with self._lock:
            for r in self._agents.values():
                if r.label == label:
                    return r
        return None

    def list_active(self) -> list[SubAgentRecord]:
        with self._lock:
            return list(self._agents.values())

    def cancel(self, agent_id: str) -> bool:
        """Cancel by agent_id or label. Returns True if found."""
        with self._lock:
            # try exact id match first
            rec = self._agents.get(agent_id)
            if rec is None:
                # try label match
                for r in self._agents.values():
                    if r.label == agent_id:
                        rec = r
                        break
        if rec is not None:
            rec.cancel()
            return True
        return False

    def update_iteration(self, agent_id: str, iteration: int) -> None:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec:
                rec.iteration = iteration

    def count(self) -> int:
        with self._lock:
            return len(self._agents)

    def count_managed(self) -> int:
        """Return count of globally capacity-counted sub-agents.

        Managed (capacity-counted) sources are ``on-demand`` and ``scheduled``;
        ``plan-step`` and ``diagnostic`` records are visible but excluded.
        """
        with self._lock:
            return sum(
                1 for r in self._agents.values()
                if r.source in CAPACITY_COUNTED_SOURCES
            )

    def cancel_all_managed(self) -> int:
        """Cancel all globally capacity-counted sub-agents atomically.

        Targets only ``on-demand`` and ``scheduled`` records — the sources that
        consume the global ``max_subagents`` cap. Returns count cancelled.
        """
        with self._lock:
            targets = [
                r for r in self._agents.values()
                if r.source in CAPACITY_COUNTED_SOURCES
            ]
        for rec in targets:
            rec.cancel()
        return len(targets)


# Module-level singleton
_registry = SubAgentRegistry()


def get_registry() -> SubAgentRegistry:
    return _registry


def register_run(
    runner: Any,
    *,
    source: str,
    label: str,
    task_preview: str,
    result_type: str = "text",
) -> SubAgentRecord:
    """Create, wire, and register a ``SubAgentRecord`` for a runner.

    Shared by ``SubAgentSupervisor`` and ``PlanExecutor`` so record creation and
    cancel-event / LLM-client / on-step wiring stay identical across launch
    paths. Shares the runner's cancel event and LLM client with the record so
    ``/agents cancel`` can interrupt an in-progress HTTP request, and wires
    iteration tracking so ``/agents`` shows step progress.

    Args:
        runner: A ``SubAgentRunner``-like object exposing ``agent_id``,
            ``_model_id``, ``_cancel_event``, ``_llm``, and ``_agent``.
        source: One of :data:`VISIBLE_SOURCES`.
        label: Display label (context_key, job tag, or plan/diagnostic label).
        task_preview: Task text; truncated to 80 chars for display.
        result_type: ``"text"`` | ``"json"`` | ``"file"``.

    Returns:
        The registered :class:`SubAgentRecord`.
    """
    record = SubAgentRecord(
        agent_id=runner.agent_id,
        label=label,
        model=runner._model_id,
        task_preview=task_preview[:80],
        started_at=time.time(),
        source=source,
        max_iterations=runner._agent.max_iterations,
        result_type=result_type,
    )
    record._cancel_event = runner._cancel_event
    record._llm_client = runner._llm

    agent_id = runner.agent_id
    runner._agent._on_step = lambda s: get_registry().update_iteration(agent_id, s)

    get_registry().register(record)
    return record


def deregister_run(agent_id: str) -> None:
    """Remove a run's record from the global registry. Symmetric to register_run."""
    get_registry().unregister(agent_id)
