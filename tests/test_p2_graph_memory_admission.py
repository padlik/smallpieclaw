"""P2: graph-memory admission and confidence controls.

Covers the additive, non-destructive admission metadata added to graph memory:

- New writes carry admission_status / confidence.
- Auto-extracted chat facts are stored as ``observed`` (not confirmed).
- ``memory_graph_store`` is confirmation-gated and stores ``confirmed`` memory
  only after operator approval (Telegram at depth 0, headless bridge at depth>=1).
- Legacy rows without admission metadata are read as ``observed``.
- Prompt formatting labels admission/confidence and keeps the untrusted framing.
- The schema migration is additive and degrades gracefully when the DB engine
  cannot add the columns (no rebuild required).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from builtin_executor import BuiltinExecutor
from graph_memory import (
    ADMISSION_CONFIRMED,
    ADMISSION_OBSERVED,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_OBSERVED,
    GraphMemoryStore,
    _coerce_admission,
)


# ---------------------------------------------------------------------------
# Fixtures (self-contained — mirror test_graph_memory.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ladybug(monkeypatch):
    db_mock = MagicMock()
    conn_mock = MagicMock()
    empty_result = MagicMock()
    empty_result.has_next.return_value = False
    conn_mock.execute.return_value = empty_result

    ladybug_mock = MagicMock()
    ladybug_mock.Database = MagicMock(return_value=db_mock)
    ladybug_mock.Connection = MagicMock(return_value=conn_mock)

    monkeypatch.setattr("graph_memory.ladybug", ladybug_mock)
    monkeypatch.setattr("graph_memory._LADYBUG_AVAILABLE", True)
    return {"ladybug": ladybug_mock, "db": db_mock, "conn": conn_mock}


@pytest.fixture
def embedder():
    def _embed(text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]
    return _embed


@pytest.fixture
def store(mock_ladybug, embedder, tmp_path):
    return GraphMemoryStore(
        db_path=str(tmp_path / "graph"),
        embedder_fn=embedder,
        embedding_dim=4,
        buffer_pool_mb=64,
    )


# ---------------------------------------------------------------------------
# Migration / detection
# ---------------------------------------------------------------------------

class TestAdmissionMigration:
    def test_migration_runs_alter_statements(self, store, mock_ladybug):
        executed = [c.args[0] for c in mock_ladybug["conn"].execute.call_args_list]
        alters = [q for q in executed if isinstance(q, str) and q.startswith("ALTER TABLE")]
        # Episode.admission_status, Episode.confidence, RELATES_TO.admission_status
        assert any("Episode ADD admission_status" in q for q in alters)
        assert any("Episode ADD confidence" in q for q in alters)
        assert any("RELATES_TO ADD admission_status" in q for q in alters)

    def test_meta_available_when_probe_succeeds(self, store):
        assert store._has_admission_meta is True

    def test_meta_unavailable_degrades_gracefully(self, mock_ladybug, embedder, tmp_path):
        # Make the detection probe (RETURN admission_status) raise to simulate a
        # DB engine that cannot add the columns. Migration must not crash and the
        # store must fall back to legacy behaviour.
        conn = mock_ladybug["conn"]

        def _execute(query, params=None):
            if "RETURN e.admission_status" in query or "RETURN r.admission_status" in query:
                raise RuntimeError("no such property: admission_status")
            res = MagicMock()
            res.has_next.return_value = False
            return res

        conn.execute.side_effect = _execute
        s = GraphMemoryStore(
            db_path=str(tmp_path / "legacy"),
            embedder_fn=embedder,
            embedding_dim=4,
            buffer_pool_mb=64,
        )
        assert s._has_admission_meta is False


# ---------------------------------------------------------------------------
# Write paths carry admission metadata
# ---------------------------------------------------------------------------

class TestAdmissionWrites:
    def test_add_episode_observed_by_default(self, store, mock_ladybug):
        mock_ladybug["conn"].execute.reset_mock()
        store.add_episode("a note", user_id="u")
        params = [c.args[1] for c in mock_ladybug["conn"].execute.call_args_list
                  if len(c.args) > 1 and isinstance(c.args[1], dict) and "adm" in c.args[1]]
        assert params and params[-1]["adm"] == ADMISSION_OBSERVED
        assert params[-1]["conf"] == CONFIDENCE_OBSERVED

    def test_add_episode_confirmed(self, store, mock_ladybug):
        mock_ladybug["conn"].execute.reset_mock()
        store.add_episode(
            "approved note", user_id="u",
            admission_status=ADMISSION_CONFIRMED, confidence=CONFIDENCE_CONFIRMED,
        )
        params = [c.args[1] for c in mock_ladybug["conn"].execute.call_args_list
                  if len(c.args) > 1 and isinstance(c.args[1], dict) and "adm" in c.args[1]]
        assert params and params[-1]["adm"] == ADMISSION_CONFIRMED
        assert params[-1]["conf"] == CONFIDENCE_CONFIRMED

    def test_add_relation_observed_by_default(self, store, mock_ladybug):
        mock_ladybug["conn"].execute.reset_mock()
        store.add_relation("ent:a", "ent:b", "USES", "a uses b", "2024-01-01T00:00:00Z")
        params = [c.args[1] for c in mock_ladybug["conn"].execute.call_args_list
                  if len(c.args) > 1 and isinstance(c.args[1], dict) and "adm" in c.args[1]]
        assert params and params[-1]["adm"] == ADMISSION_OBSERVED
        assert params[-1]["conf"] == CONFIDENCE_OBSERVED

    def test_add_relation_legacy_no_meta(self, store, mock_ladybug):
        store._has_admission_meta = False
        mock_ladybug["conn"].execute.reset_mock()
        store.add_relation("ent:a", "ent:b", "USES", "a uses b", "2024-01-01T00:00:00Z")
        # Legacy query must not reference admission_status.
        queries = [c.args[0] for c in mock_ladybug["conn"].execute.call_args_list]
        assert all("admission_status" not in q for q in queries)

    def test_observed_write_protects_confirmed_fact(self, store, mock_ladybug):
        """A non-confirmed merge must NOT overwrite a confirmed edge's fact.

        The ON MATCH clause guards r.fact/r.valid_at with a CASE that keeps the
        existing value when the stored edge is confirmed and the incoming write
        is not, so observed extraction cannot poison operator-approved facts.
        """
        mock_ladybug["conn"].execute.reset_mock()
        store.add_relation(
            "ent:a", "ent:b", "USES", "observed overwrite", "2024-01-02T00:00:00Z",
            admission_status=ADMISSION_OBSERVED, confidence=CONFIDENCE_OBSERVED,
        )
        merge_q = next(
            c.args[0] for c in mock_ladybug["conn"].execute.call_args_list
            if "MERGE" in c.args[0] and "RELATES_TO" in c.args[0]
        )
        assert "ON MATCH SET" in merge_q
        assert "CASE" in merge_q
        assert "r.admission_status='confirmed'" in merge_q
        assert "$adm<>'confirmed'" in merge_q

    def test_confirmed_write_upgrades_existing_edge(self, store, mock_ladybug):
        """A confirmed merge still runs the upgrade-only status/confidence SET."""
        mock_ladybug["conn"].execute.reset_mock()
        store.add_relation(
            "ent:a", "ent:b", "USES", "approved", "2024-01-02T00:00:00Z",
            admission_status=ADMISSION_CONFIRMED, confidence=CONFIDENCE_CONFIRMED,
        )
        upgrade = [
            c for c in mock_ladybug["conn"].execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
            and c.args[1].get("adm") == ADMISSION_CONFIRMED
            and "MERGE" not in c.args[0]
        ]
        assert upgrade, "confirmed write should emit an upgrade SET statement"


# ---------------------------------------------------------------------------
# Read / format labelling
# ---------------------------------------------------------------------------

class TestAdmissionFormatting:
    def _wire_facts(self, conn, fact_rows, ep_rows=None):
        seed_result = MagicMock()
        seed_result.has_next.side_effect = [True, False]
        seed_result.get_next.return_value = (
            {"id": "ent:alice:person", "name": "Alice", "entity_type": "person"}, 0.1,
        )
        graph_result = MagicMock()
        graph_result.has_next.side_effect = [True] * len(fact_rows) + [False]
        graph_result.get_next.side_effect = fact_rows
        ep_result = MagicMock()
        ep_rows = ep_rows or []
        ep_result.has_next.side_effect = [True] * len(ep_rows) + [False]
        ep_result.get_next.side_effect = ep_rows
        conn.execute.side_effect = [seed_result, graph_result, ep_result]

    def test_confirmed_ranked_before_observed_and_labelled(self, store, mock_ladybug):
        conn = mock_ladybug["conn"]
        self._wire_facts(conn, [
            ("Alice", "USES", "Alice uses Python", "Python", ADMISSION_OBSERVED, 0.6),
            ("Alice", "LIKES", "Alice likes tea", "Tea", ADMISSION_CONFIRMED, 0.95),
        ])
        output = store.format_for_prompt("Alice")
        assert "untrusted recalled memory" in output
        assert "[confirmed" in output
        assert "[observed" in output
        # Confirmed fact must be listed before the observed one.
        assert output.index("Alice likes tea") < output.index("Alice uses Python")

    def test_legacy_rows_treated_as_observed(self, store, mock_ladybug):
        conn = mock_ladybug["conn"]
        # 4-tuple = legacy row without admission/confidence columns.
        self._wire_facts(conn, [("Alice", "USES", "Alice uses Python", "Python")])
        output = store.format_for_prompt("Alice")
        assert "[observed" in output
        # No fact line should be labelled confirmed (header legend aside).
        fact_lines = [ln for ln in output.splitlines() if "Alice uses Python" in ln]
        assert fact_lines and all("[confirmed" not in ln for ln in fact_lines)


# ---------------------------------------------------------------------------
# memory_graph_store confirmation gating
# ---------------------------------------------------------------------------

class TestMemoryGraphStoreConfirmation:
    @pytest.fixture
    def executor_with_graph(self):
        b = BuiltinExecutor(default_timeout=30)
        b._graph_memory = MagicMock()
        b._graph_memory.add_episode.return_value = "ep:1"
        b._graph_memory_writer = MagicMock()
        return b

    def test_depth0_requires_confirmation(self, executor_with_graph):
        b = executor_with_graph
        out = b.execute("memory_graph_store", {"content": "remember this"}, caller_depth=0)
        assert out.get("requires_confirmation") is True
        b._graph_memory.add_episode.assert_not_called()

    def test_subagent_uses_headless_bridge_and_stores_confirmed(self, executor_with_graph):
        b = executor_with_graph
        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, desc, tag):
            tokens_seen.append(token)

        b._subagent_confirm_prompt_fn = prompt_fn

        def _approve():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                b.signal_headless_confirm(tokens_seen[0], True)

        t = threading.Thread(target=_approve, daemon=True)
        t.start()
        out = b.execute("memory_graph_store", {"content": "sub-agent fact"}, caller_depth=1)
        t.join(timeout=4.0)

        assert out["success"] is True
        _, kwargs = b._graph_memory.add_episode.call_args
        assert kwargs.get("admission_status") == ADMISSION_CONFIRMED

    def test_subagent_denied_writes_nothing(self, executor_with_graph):
        b = executor_with_graph
        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, desc, tag):
            tokens_seen.append(token)

        b._subagent_confirm_prompt_fn = prompt_fn

        def _deny():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                b.signal_headless_confirm(tokens_seen[0], False)

        t = threading.Thread(target=_deny, daemon=True)
        t.start()
        out = b.execute("memory_graph_store", {"content": "sub-agent fact"}, caller_depth=1)
        t.join(timeout=4.0)

        assert out["success"] is False
        b._graph_memory.add_episode.assert_not_called()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class TestCoerceAdmission:
    def test_known_values_pass_through(self):
        assert _coerce_admission("confirmed") == ADMISSION_CONFIRMED
        assert _coerce_admission("OBSERVED") == ADMISSION_OBSERVED

    def test_unknown_falls_back_to_observed(self):
        assert _coerce_admission(None) == ADMISSION_OBSERVED
        assert _coerce_admission("garbage") == ADMISSION_OBSERVED
