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


@pytest.mark.timeout(10)
def test_cross_thread_zone_allow_writes_to_run_tracker() -> None:
    """Telegram zone-allow callback (different thread) must write to the run's tracker.

    Simulates the real flow: the agent thread captures the tracker at
    confirmation time via _zone_trackers, then a callback thread pops it
    and adds the grant. The agent thread must observe the grant on its
    own run-scoped tracker, not the default.
    """
    executor = BuiltinExecutor()
    run_tracker = GrantTracker()
    token = "test_token_123"

    # Agent thread: enter run context and stage a confirmation
    with executor.use_grant_tracker(run_tracker):
        assert executor.grant_tracker is run_tracker
        # Simulate _requires_confirmation capturing the tracker
        executor._zone_paths[token] = "/tmp/zone_x/file.txt"
        executor._zone_trackers[token] = executor.grant_tracker

        # Callback thread: pop the tracker and add the grant
        def callback() -> None:
            zone_path = executor._zone_paths.pop(token, "")
            tracker = executor._zone_trackers.pop(token, None)
            if zone_path and tracker is not None:
                tracker.add(zone_path)

        cb_thread = threading.Thread(target=callback)
        cb_thread.start()
        cb_thread.join()

        # Agent thread: verify the grant landed on the run tracker
        grant_dir = _parent_dir("/tmp/zone_x/file.txt")
        assert grant_dir in run_tracker.snapshot(), "Grant must be on the run tracker"
        assert grant_dir not in executor._default_grant_tracker.snapshot(), \
            "Grant must NOT leak to the default tracker"


def test_default_tracker_reset_at_run_start() -> None:
    """_default_grant_tracker.reset() at depth-0 run start clears stale grants."""
    executor = BuiltinExecutor()
    # Simulate a stale grant from a prior run's Telegram callback fallback
    executor._default_grant_tracker.add("/tmp/stale_zone/file.txt")
    assert _parent_dir("/tmp/stale_zone/file.txt") in executor._default_grant_tracker.snapshot()

    # Simulate depth-0 run start reset
    executor._default_grant_tracker.reset()

    assert _parent_dir("/tmp/stale_zone/file.txt") not in executor._default_grant_tracker.snapshot()
