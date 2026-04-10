"""
sub_agent_registry.py — Global registry of active sub-agents.

Tracks all running SubAgentRunner instances so they can be listed
(/agents command) and cancelled (/agents cancel <id>).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SubAgentRecord:
    agent_id: str
    label: str          # context_key or "on-demand"
    model: str
    task_preview: str   # first 80 chars of task
    started_at: float   # time.time()
    source: str         # "scheduled" | "on-demand"
    # mutable fields updated by the runner
    iteration: int = 0
    max_iterations: int = 8
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _llm_client: object = field(default=None, repr=False)  # LLMClient — for immediate HTTP interrupt

    def cancel(self) -> None:
        self._cancel_event.set()
        # Close the HTTP client to immediately interrupt any in-progress LLM request.
        # The resulting transport exception is caught as LLMCancelledError in _with_retry.
        if self._llm_client is not None:
            try:
                self._llm_client._http.close()
            except Exception:
                pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

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
    def __init__(self):
        self._lock = threading.Lock()
        self._agents: dict[str, SubAgentRecord] = {}

    def register(self, record: SubAgentRecord) -> None:
        with self._lock:
            self._agents[record.agent_id] = record

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def get(self, agent_id: str) -> Optional[SubAgentRecord]:
        with self._lock:
            return self._agents.get(agent_id)

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


# Module-level singleton
_registry = SubAgentRegistry()


def get_registry() -> SubAgentRegistry:
    return _registry
