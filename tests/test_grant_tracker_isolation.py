"""Isolation tests for BuiltinExecutor's per-run GrantTracker.

Ensures concurrent runs (main agent + sub-agents, or concurrent sub-agents)
cannot read each other's request grants by relying on a ContextVar binding.
"""

from __future__ import annotations

import os
import threading
import pytest

from builtin_executor import BuiltinExecutor
from builtin_tools.access_control import GrantTracker


def _parent_dir(path: str) -> str:
    """Return the parent directory of the realpath expansion for path."""
    return os.path.dirname(os.path.realpath(os.path.expanduser(path)))


@pytest.mark.timeout(10)
def test_concurrent_sub_agents_isolated_grants() -> None:
    """Two concurrent runs using the same executor must see only their own grants."""
    executor = BuiltinExecutor()
    results: dict[str, frozenset[str]] = {}

    def run_with_grant(run_id: str, path: str) -> None:
        gt = GrantTracker()
        gt.add(path)
        with executor.use_grant_tracker(gt):
            # Yield to increase chance of interleaving and prove isolation.
            import time

            time.sleep(0.05)
            active = executor.grant_tracker
            results[run_id] = active.snapshot()

    t1 = threading.Thread(target=run_with_grant, args=("a", "/tmp/zone_a/file.txt"))
    t2 = threading.Thread(target=run_with_grant, args=("b", "/tmp/zone_b/file.txt"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    grant_a = _parent_dir("/tmp/zone_a/file.txt")
    grant_b = _parent_dir("/tmp/zone_b/file.txt")

    assert grant_a in results["a"], "Run A must see its own grant"
    assert grant_b in results["b"], "Run B must see its own grant"
    assert grant_b not in results["a"], "Run A must NOT see run B's grant"
    assert grant_a not in results["b"], "Run B must NOT see run A's grant"


def test_use_grant_tracker_restores_previous_value() -> None:
    """Exiting the context manager must restore the prior ContextVar value."""
    executor = BuiltinExecutor()
    outer = GrantTracker()
    inner = GrantTracker()

    assert executor.grant_tracker is executor._default_grant_tracker
    with executor.use_grant_tracker(outer):
        assert executor.grant_tracker is outer
        with executor.use_grant_tracker(inner):
            assert executor.grant_tracker is inner
        assert executor.grant_tracker is outer
    assert executor.grant_tracker is executor._default_grant_tracker


def test_outside_context_falls_back_to_default_tracker() -> None:
    """Reads outside a use_grant_tracker block use the executor-wide default."""
    executor = BuiltinExecutor()
    executor._default_grant_tracker.add("/tmp/default_zone/file.txt")

    grant_dir = _parent_dir("/tmp/default_zone/file.txt")
    assert grant_dir in executor.grant_tracker.snapshot()
