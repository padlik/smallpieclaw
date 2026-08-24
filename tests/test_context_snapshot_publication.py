"""Tests for ReAct loop context-snapshot publication.

Covers OpenSpec change ``context-profile-command`` task 3.5: the loop publishes
a :class:`~context_monitor.ContextSnapshot` after each completed turn and marks
it ``is_live=False`` when the run ends.
"""

from __future__ import annotations

from react_loop import ReactContext, _LoopState, _publish_context_snapshot
from context_manager import _active_model
from context_monitor import ContextMonitor, ContextSnapshot
from token_estimator import estimate_tokens, estimate_messages_tokens



class _MinimalLLM:
    """Tiny LLM stub with a small llm_cfg."""

    def __init__(self) -> None:
        self.llm_cfg = {"model": "test", "max_tokens": 512, "context_window": 4096}
        self._active_idx = 0


def _make_context(monitor: ContextMonitor | None = None) -> ReactContext:
    # We intentionally use a plain stub: ReactContext only reads llm.llm_cfg.
    return ReactContext(  # type: ignore[arg-type]
        llm=_MinimalLLM(),
        tool_index=_NullToolIndex(),
        memory=_NullMemory(),
        builtin_executor=None,
        mcp_manager=None,
        skill_registry=None,
        ctx_max_tokens=4096,
        context_monitor=monitor,
    )


def test_publish_context_snapshot_writes_all_fields() -> None:
    """The helper publishes a snapshot with every expected field populated."""
    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    system = "system prompt text"
    state = _LoopState(
        messages=[{"role": "user", "content": "hello"}],
        goal_idx=0,
        max_steps=8,
        step=1,
    )

    _publish_context_snapshot(ctx, state, system)

    snapshot = monitor.read()
    assert snapshot is not None
    assert isinstance(snapshot, ContextSnapshot)
    assert snapshot.system_prompt_tokens == estimate_tokens(system, model=ctx.llm.llm_cfg.get("model"))
    assert snapshot.chat_history_tokens == max(
        0,
        estimate_messages_tokens(state.messages, system, model=ctx.llm.llm_cfg.get("model"))
        - snapshot.system_prompt_tokens,
    )
    assert snapshot.tool_defs_tokens == 0
    assert snapshot.tool_defs_by_server == {"builtin": 0}
    assert snapshot.completion_reserve == 512
    assert snapshot.effective_window == 4096
    assert snapshot.compaction_threshold == max(int((4096 - 512) * 0.85), 256)
    assert snapshot.headroom_nominal == (
        snapshot.compaction_threshold - snapshot.system_prompt_tokens - snapshot.chat_history_tokens
    )
    assert snapshot.headroom_real == (
        snapshot.compaction_threshold
        - snapshot.system_prompt_tokens
        - snapshot.chat_history_tokens
        - snapshot.tool_defs_tokens
    )
    assert snapshot.danger_level in {"safe", "approaching", "danger"}
    assert snapshot.is_live is True
    assert snapshot.turn == 1


def test_publish_context_snapshot_no_monitor_is_no_op() -> None:
    """A ReactContext without a monitor does not crash; no snapshot is published."""
    ctx = _make_context(None)
    state = _LoopState(messages=[], goal_idx=0, max_steps=8, step=0)

    _publish_context_snapshot(ctx, state, "system")
    # Should simply return without raising.


def test_run_ending_sets_is_live_false() -> None:
    """Publishing a replacement snapshot with is_live=False copies field values."""
    from dataclasses import replace

    monitor = ContextMonitor()
    original = ContextSnapshot(
        system_prompt_tokens=10,
        chat_history_tokens=20,
        tool_defs_tokens=30,
        tool_defs_by_server={"builtin": 30},
        completion_reserve=512,
        effective_window=4096,
        compaction_threshold=3000,
        headroom_nominal=100,
        headroom_real=70,
        danger_level="approaching",
        is_live=True,
        turn=3,
    )
    monitor.publish(original)

    idle = replace(original, is_live=False)
    monitor.publish(idle)

    current = monitor.read()
    assert current is not None
    assert current.is_live is False
    assert current.turn == 3
    assert current.system_prompt_tokens == 10


def test_snapshot_totals_match_maybe_compact_total() -> None:
    """Snapshot token accounting must equal maybe_compact's total.

    This is the invariant promised in ADR-0022 / design.md: the compaction total
    and the displayed total are the same number. The snapshot must therefore
    split ``estimate_messages_tokens(messages, system, model)`` exactly the
    same way ``maybe_compact`` folds it together, before adding tool-def tokens.
    """
    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    system = "system prompt text with some content"
    state = _LoopState(
        messages=[
            {"role": "system", "content": "do things"},
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"},
        ],
        goal_idx=1,
        max_steps=8,
        step=2,
    )

    _publish_context_snapshot(ctx, state, system)

    snapshot = monitor.read()
    assert snapshot is not None
    model = _active_model(ctx.llm)
    compaction_total = estimate_messages_tokens(state.messages, system, model=model)
    assert (
        snapshot.system_prompt_tokens + snapshot.chat_history_tokens == compaction_total
    )


def test_snapshot_passed_tool_defs_avoids_recompute() -> None:
    """Supplying pre-computed tool-def metadata uses those values unchanged."""
    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    state = _LoopState(
        messages=[{"role": "user", "content": "hello"}],
        goal_idx=0,
        max_steps=8,
        step=1,
    )

    _publish_context_snapshot(
        ctx, state, "sys",
        tool_defs_by_server={"builtin": 42, "mcp": 100},
        tool_defs_tokens=142,
    )

    snapshot = monitor.read()
    assert snapshot is not None
    assert snapshot.tool_defs_tokens == 142
    assert snapshot.tool_defs_by_server == {"builtin": 42, "mcp": 100}


def test_publish_context_snapshot_exception_is_swallowed() -> None:
    """A diagnostic publication failure must not propagate to the caller."""
    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    ctx._tool_defs = []  # force grouping path; tool_index.registry.get will blow up
    state = _LoopState(
        messages=[{"role": "user", "content": "hello"}],
        goal_idx=0,
        max_steps=8,
        step=1,
    )

    # The helper computes grouping internally when no pre-computed values are
    # supplied. _NullRegistry.get returns None, which group_tool_defs_by_server
    # tolerates, so this path does not crash. To simulate a crash, monkey-patch a
    # broken registry that raises.
    class _BrokenRegistry:
        def get(self, _name):
            raise RuntimeError("boom")

    class _BrokenToolIndex:
        def search(self, *_a, **_k):
            return []

        def all_tools(self):
            return []

        @property
        def registry(self):
            return _BrokenRegistry()

    ctx.tool_index = _BrokenToolIndex()

    _publish_context_snapshot(ctx, state, "sys")  # must not raise


def test_tool_defs_grouping_cache_reused_across_turns() -> None:
    """group_tool_defs_by_server runs only once per run when cached on ctx."""
    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    ctx._tool_defs = []  # empty tool defs to keep estimates trivial
    ctx.context_monitor = monitor

    state1 = _LoopState(
        messages=[{"role": "user", "content": "hello"}],
        goal_idx=0,
        max_steps=8,
        step=1,
    )

    # First call supplies pre-computed values so the helper uses them as-is.
    _publish_context_snapshot(
        ctx, state1, "sys",
        tool_defs_by_server={"builtin": 7, "mcp": 3},
        tool_defs_tokens=10,
    )

    snapshot1 = monitor.read()
    assert snapshot1 is not None
    assert snapshot1.tool_defs_tokens == 10
    assert snapshot1.tool_defs_by_server == {"builtin": 7, "mcp": 3}

    # Second turn with no pre-computed values: the helper recomputes fresh. We
    # only verify it does not crash (the test for caching happens at the loop
    # level in test_react_loop_caches_tool_def_grouping_per_run).
    state2 = _LoopState(
        messages=[{"role": "user", "content": "hello again"}],
        goal_idx=0,
        max_steps=8,
        step=2,
    )

    _publish_context_snapshot(ctx, state2, "sys")

    snapshot2 = monitor.read()
    assert snapshot2 is not None
    assert snapshot2.turn == 2


def test_tool_defs_grouping_failure_degrades_to_zero_in_loop() -> None:
    """When the loop's grouping computation fails, it degrades to 0 tokens."""
    from unittest.mock import MagicMock, patch
    from react_loop import react_loop

    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    ctx._tool_defs = []
    ctx._tool_defs_by_server = None
    ctx._tool_defs_tokens = 0
    ctx.context_monitor = monitor

    ctx.owns_cancel_event = True
    ctx.cancel_event.clear()
    ctx.memory = _NullMemory()
    ctx.short_term = None
    ctx.working = None
    ctx.results = None
    ctx.strategy_memory = None
    ctx.graph_memory = None
    ctx.graph_memory_writer = None
    ctx.skill_registry = None
    ctx.tool_index = _NullToolIndex()
    ctx.mcp_manager = None
    ctx.builtin_executor = None
    ctx.confirmation = MagicMock()
    ctx.confirmation.request_extension.return_value = "no"
    ctx.max_iterations = 3

    llm = MagicMock()
    llm.llm_cfg = {"model": "test", "max_tokens": 512, "context_window": 4096}
    llm.chat_with_fallback.return_value = '{"action":"finish","result":"done"}'
    llm.chat_with_tools_fallback = MagicMock(side_effect=NotImplementedError())
    ctx.llm = llm

    # Force the grouping computation to fail in the first turn.
    with patch("react_loop.group_tool_defs_by_server", side_effect=RuntimeError("boom")):
        result = react_loop(ctx, "hello")

    # The run should complete despite the grouping failure.
    assert result == "done"
    # The cache should have been populated with the degraded zero values.
    assert ctx._tool_defs_by_server == {}
    assert ctx._tool_defs_tokens == 0
    # A snapshot should still have been published with tool_defs_tokens=0.
    snapshot = monitor.read()
    assert snapshot is not None
    assert snapshot.tool_defs_tokens == 0
    assert snapshot.is_live is False


def test_react_loop_caches_tool_def_grouping_per_run() -> None:
    """The loop computes tool-def grouping once and reuses it across turns."""
    from unittest.mock import MagicMock, patch
    from react_loop import react_loop

    monitor = ContextMonitor()
    ctx = _make_context(monitor)
    ctx._tool_defs = []
    ctx.context_monitor = monitor

    ctx.owns_cancel_event = True
    ctx.cancel_event.clear()

    # Stub out the pieces of the loop we don't want to exercise.
    ctx.memory = _NullMemory()
    ctx.short_term = None
    ctx.working = None
    ctx.results = None
    ctx.strategy_memory = None
    ctx.graph_memory = None
    ctx.graph_memory_writer = None
    ctx.skill_registry = None
    ctx.tool_index = _NullToolIndex()
    ctx.mcp_manager = None
    ctx.builtin_executor = None
    ctx.confirmation = MagicMock()
    ctx.confirmation.request_extension.return_value = "no"
    ctx.max_iterations = 3

    # One LLM response that finishes immediately so the loop makes at least one
    # compaction/snapshot pass and then the is_live=False final publish.
    llm = MagicMock()
    llm.llm_cfg = {"model": "test", "max_tokens": 512, "context_window": 4096}
    llm.chat_with_fallback.return_value = '{"action":"finish","result":"done"}'
    llm.chat_with_tools_fallback = MagicMock(side_effect=NotImplementedError())
    ctx.llm = llm

    with patch("react_loop.group_tool_defs_by_server") as mock_group:
        mock_group.return_value = {"builtin": 5}
        react_loop(ctx, "hello")
        # Grouping should be called exactly once per run (in the first turn's
        # compaction/snapshot path) and then never again.
        assert mock_group.call_count == 1


# ---------------------------------------------------------------------------
# Minimal null collaborators
# ---------------------------------------------------------------------------


class _NullToolIndex:
    def search(self, *_a, **_k):
        return []

    def all_tools(self):
        return []

    @property
    def registry(self):
        return _NullRegistry()


class _NullRegistry:
    def get(self, _name):
        return None


class _NullMemory:
    def record_event(self, *_a, **_k):
        pass

    def as_prompt_text(self, *_a, **_k):
        return ""
