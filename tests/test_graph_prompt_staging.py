"""Staged graph prompt preparation: metrics commit separately from rendering."""

from __future__ import annotations

import hashlib
import threading

import pytest

import graph_memory
from graph_memory import GraphMemoryStore, PreparedGraphPrompt
from providers._errors import LLMError

_DIM = 16


def _fake_embedder(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    raw = [((b / 255.0) * 2.0 - 1.0) for b in digest[:_DIM]]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


class _Embedder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.store: GraphMemoryStore | None = None
        self.lock_free_checks: list[bool] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.store is not None:
            self.lock_free_checks.append(self._lock_acquirable_elsewhere())
        if self.fail:
            raise LLMError("embedding unavailable")
        return _fake_embedder(text)

    def _lock_acquirable_elsewhere(self) -> bool:
        assert self.store is not None
        result: list[bool] = []

        def probe() -> None:
            got = self.store._conn_lock.acquire(timeout=2)  # type: ignore[union-attr]
            result.append(got)
            if got:
                self.store._conn_lock.release()  # type: ignore[union-attr]

        t = threading.Thread(target=probe)
        t.start()
        t.join(5)
        return bool(result and result[0])


@pytest.fixture
def embedder() -> _Embedder:
    return _Embedder()


@pytest.fixture
def store(tmp_path, embedder):
    s = GraphMemoryStore(
        db_path=str(tmp_path / "graph_staging"),
        embedder_fn=embedder,
        embedding_dim=_DIM,
        buffer_pool_mb=64,
    )
    s.add_episode("Alice uses Python for scripting.", user_id="u1")
    embedder.calls.clear()
    embedder.store = s
    yield s
    s.close()


def _counters(store: GraphMemoryStore) -> tuple[int, int, int]:
    stats = store.get_stats()
    return (
        stats["retrieval_hits"],
        stats["retrieval_misses"],
        stats["context_injections"],
    )


def _empty_search(store: GraphMemoryStore, monkeypatch) -> None:
    monkeypatch.setattr(
        store, "search", lambda q, k=10: {"seeds": [], "facts": [], "episodes": []}
    )


def test_prepare_does_not_touch_counters_until_commit(store):
    prepared = store.prepare_for_prompt("Alice Python")
    assert isinstance(prepared, PreparedGraphPrompt)
    assert prepared.text
    assert _counters(store) == (0, 0, 0)
    prepared.commit_metrics()
    assert _counters(store) == (1, 0, 1)


def test_prepare_is_read_only_until_commit(tmp_path):
    """Relocated from tests/test_model_switch_budget_guard.py (task 2.2).

    An empty real graph store (miss path): preparation is read-only until the
    metrics commit, which then records at least one retrieval.
    """
    store = GraphMemoryStore(
        db_path=str(tmp_path / "graph_guard"), embedder_fn=lambda _text: [0.1, 0.2, 0.3, 0.4],
        embedding_dim=4, buffer_pool_mb=64,
    )
    try:
        before = _counters(store)
        prepared = store.prepare_for_prompt("anything", max_entries=3)
        assert isinstance(prepared.text, str)
        assert _counters(store) == before
        prepared.commit_metrics()
        after = _counters(store)
        assert sum(after) - sum(before) >= 1
    finally:
        store.close()


def test_discard_leaves_counters_unchanged(store):
    store.prepare_for_prompt("Alice Python")
    del_target = store.prepare_for_prompt("Alice Python")
    del del_target
    assert _counters(store) == (0, 0, 0)


def test_format_for_prompt_parity_and_immediate_counts(store):
    prepared = store.prepare_for_prompt("Alice Python", max_entries=5)
    text = store.format_for_prompt("Alice Python", max_entries=5)
    assert text == prepared.text
    assert "KNOWLEDGE GRAPH CONTEXT" in text
    assert _counters(store) == (1, 0, 1)


def test_text_is_read_only(store):
    prepared = store.prepare_for_prompt("Alice Python")
    with pytest.raises(AttributeError):
        prepared.text = "other"  # type: ignore[misc]


def test_empty_result_defers_miss_until_commit(store, monkeypatch):
    _empty_search(store, monkeypatch)
    prepared = store.prepare_for_prompt("nothing")
    assert prepared.text == ""
    assert _counters(store) == (0, 0, 0)
    prepared.commit_metrics()
    assert _counters(store) == (0, 1, 0)
    assert store.format_for_prompt("nothing") == ""
    assert _counters(store) == (0, 2, 0)


def test_degraded_embedding_is_deferred_miss(store, embedder):
    embedder.fail = True
    prepared = store.prepare_for_prompt("Alice")
    assert prepared.text == ""
    assert _counters(store) == (0, 0, 0)
    prepared.commit_metrics()
    assert _counters(store) == (0, 1, 0)


def test_repeated_commit_applies_once(store):
    prepared = store.prepare_for_prompt("Alice Python")
    prepared.commit_metrics()
    prepared.commit_metrics()
    prepared.commit_metrics()
    assert _counters(store) == (1, 0, 1)


def test_concurrent_commits_of_same_preparation_apply_once(store):
    prepared = store.prepare_for_prompt("Alice Python")
    barrier = threading.Barrier(8)

    def run() -> None:
        barrier.wait()
        prepared.commit_metrics()

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert _counters(store) == (1, 0, 1)


def test_independent_preparations_add_and_preserve_intervening_retrieval(store):
    a = store.prepare_for_prompt("Alice Python")
    b = store.prepare_for_prompt("Alice Python")
    store.format_for_prompt("Alice Python")
    assert _counters(store) == (1, 0, 1)
    a.commit_metrics()
    assert _counters(store) == (2, 0, 2)
    store.format_for_prompt("Alice Python")
    b.commit_metrics()
    assert _counters(store) == (4, 0, 4)


def test_commit_performs_no_retrieval_embedding_or_sql(store, embedder, monkeypatch):
    prepared = store.prepare_for_prompt("Alice Python")
    calls_before = len(embedder.calls)
    assert calls_before >= 1

    def boom(*_a, **_k):
        raise AssertionError("commit must not retrieve/execute")

    monkeypatch.setattr(store, "search", boom)
    monkeypatch.setattr(store, "_conn", type("C", (), {"execute": boom})())
    prepared.commit_metrics()
    assert len(embedder.calls) == calls_before
    assert (
        store._retrieval_hits,
        store._retrieval_misses,
        store._context_injections,
    ) == (1, 0, 1)


def test_prepare_embeds_once_without_lock_held(store, embedder):
    prepared = store.prepare_for_prompt("Alice Python")
    assert len(embedder.calls) >= 1
    n = len(embedder.calls)
    assert embedder.lock_free_checks and all(embedder.lock_free_checks)
    prepared.commit_metrics()
    store.get_stats()
    assert len(embedder.calls) == n


def test_render_exception_commits_nothing(store, monkeypatch):
    def broken(_value):
        raise ValueError("render failure")

    monkeypatch.setattr(graph_memory, "_sanitize_prompt_field", broken)
    with pytest.raises(ValueError):
        store.prepare_for_prompt("Alice Python")
    assert _counters(store) == (0, 0, 0)
    with pytest.raises(ValueError):
        store.format_for_prompt("Alice Python")
    assert _counters(store) == (0, 0, 0)


def test_get_stats_reads_consistent_triple(store):
    stop = threading.Event()
    bad: list[tuple[int, int, int]] = []

    def committer() -> None:
        while not stop.is_set():
            store.prepare_for_prompt("Alice Python").commit_metrics()

    t = threading.Thread(target=committer)
    t.start()
    try:
        for _ in range(25):
            triple = _counters(store)
            if triple[0] != triple[2]:
                bad.append(triple)
    finally:
        stop.set()
        t.join(10)
    assert not bad
