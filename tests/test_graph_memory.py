"""Tests for graph_memory.py — extraction parsing, store and writer logic.

Most tests mock LadybugDB and test the logic independently of the
database. Tests that require the actual ladybug package are skipped
if it is not installed.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

# Attempt to import ladybug; skip DB-touching tests if not available
try:
    import ladybug  # noqa: F401
    _LADYBUG_AVAILABLE = True
except ImportError:
    _LADYBUG_AVAILABLE = False

from graph_memory import (
    GraphMemoryStore,
    GraphMemoryWriter,
    parse_extraction,
    create_graph_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ladybug(monkeypatch):
    """Patch ladybug module with a minimal mock to allow GraphMemoryStore init."""
    db_mock = MagicMock()
    conn_mock = MagicMock()

    db_cls = MagicMock(return_value=db_mock)
    conn_cls = MagicMock(return_value=conn_mock)

    # QUERY_VECTOR_INDEX returns empty result by default
    empty_result = MagicMock()
    empty_result.has_next.return_value = False
    conn_mock.execute.return_value = empty_result

    ladybug_mock = MagicMock()
    ladybug_mock.Database = db_cls
    ladybug_mock.Connection = conn_cls

    monkeypatch.setattr("graph_memory.ladybug", ladybug_mock)
    monkeypatch.setattr("graph_memory._LADYBUG_AVAILABLE", True)
    return {"ladybug": ladybug_mock, "db": db_mock, "conn": conn_mock}


@pytest.fixture
def embedder():
    """Simple deterministic embedder returning fixed-size vectors."""
    def _embed(text: str) -> list[float]:
        # Return a fixed 4-dimensional vector (tiny, for testing)
        return [0.1, 0.2, 0.3, 0.4]
    return _embed


@pytest.fixture
def store(mock_ladybug, embedder, tmp_path):
    """GraphMemoryStore with mocked LadybugDB."""
    return GraphMemoryStore(
        db_path=str(tmp_path / "graph"),
        embedder_fn=embedder,
        embedding_dim=4,
        buffer_pool_mb=64,
    )


# ---------------------------------------------------------------------------
# parse_extraction tests
# ---------------------------------------------------------------------------

class TestParseExtraction:
    def test_valid_json(self):
        response = '{"entities": [{"name": "Alice", "entity_type": "person"}], "facts": [{"source": "Alice", "target": "Python", "relation_type": "USES", "fact": "Alice uses Python for scripting."}]}'
        result = parse_extraction(response)
        assert result is not None
        assert len(result.entities) == 1
        assert result.entities[0].name == "Alice"
        assert result.entities[0].entity_type == "person"
        assert len(result.facts) == 1
        assert result.facts[0].source == "Alice"
        assert result.facts[0].relation_type == "USES"

    def test_fenced_json(self):
        response = '```json\n{"entities": [{"name": "Bob", "entity_type": "person"}], "facts": []}\n```'
        result = parse_extraction(response)
        assert result is not None
        assert result.entities[0].name == "Bob"
        assert result.facts == []

    def test_json_with_prose_prefix(self):
        response = 'Here are the extracted entities:\n{"entities": [{"name": "Pi", "entity_type": "concept"}], "facts": []}'
        result = parse_extraction(response)
        assert result is not None
        assert result.entities[0].name == "Pi"

    def test_empty_entities_and_facts_returns_none(self):
        response = '{"entities": [], "facts": []}'
        result = parse_extraction(response)
        assert result is None

    def test_malformed_json_returns_none(self):
        result = parse_extraction("Not JSON at all")
        assert result is None

    def test_partial_json_no_closing_brace(self):
        result = parse_extraction('{"entities": [{"name": "X"')
        assert result is None

    def test_relation_type_uppercased(self):
        response = '{"entities": [{"name": "X", "entity_type": "other"}, {"name": "Y", "entity_type": "other"}], "facts": [{"source": "X", "target": "Y", "relation_type": "uses tool", "fact": "X uses Y."}]}'
        result = parse_extraction(response)
        assert result is not None
        assert result.facts[0].relation_type == "USES_TOOL"

    def test_entities_without_facts(self):
        response = '{"entities": [{"name": "Docker", "entity_type": "tool"}], "facts": []}'
        result = parse_extraction(response)
        # Empty facts is OK as long as there are entities
        assert result is not None
        assert result.entities[0].name == "Docker"

    def test_facts_missing_source_skipped(self):
        response = '{"entities": [{"name": "A", "entity_type": "other"}], "facts": [{"source": "", "target": "B", "relation_type": "X", "fact": "something"}]}'
        result = parse_extraction(response)
        # Source is empty — fact should be dropped; we still have one entity
        assert result is not None
        assert result.facts == []

    def test_extra_whitespace_in_fenced_block(self):
        response = '```\n\n  {"entities":[{"name":"C","entity_type":"concept"}],"facts":[]}\n\n```'
        result = parse_extraction(response)
        assert result is not None
        assert result.entities[0].name == "C"


# ---------------------------------------------------------------------------
# GraphMemoryStore tests (using mocked LadybugDB)
# ---------------------------------------------------------------------------

class TestGraphMemoryStoreUnavailable:
    def test_raises_without_ladybug(self, monkeypatch):
        monkeypatch.setattr("graph_memory._LADYBUG_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="ladybug"):
            GraphMemoryStore(
                db_path="/tmp/test",
                embedder_fn=lambda t: [0.0],
            )


class TestGraphMemoryStore:
    def test_upsert_entity_calls_execute(self, store, mock_ladybug):
        conn = mock_ladybug["conn"]
        eid = store.upsert_entity("Alice", "person", "2026-01-01T00:00:00Z")
        assert eid.startswith("ent:alice:person")
        # Should have called execute with MERGE and SET statements
        assert conn.execute.called

    def test_upsert_entity_returns_stable_id(self, store):
        id1 = store.upsert_entity("Alice", "person", "2026-01-01T00:00:00Z")
        id2 = store.upsert_entity("Alice", "person", "2026-01-02T00:00:00Z")
        assert id1 == id2

    def test_add_relation_calls_execute(self, store, mock_ladybug):
        conn = mock_ladybug["conn"]
        store.add_relation("ent:a:person", "ent:b:tool", "USES", "A uses B.", "2026-01-01T00:00:00Z")
        assert conn.execute.called

    def test_add_episode_calls_execute(self, store, mock_ladybug):
        conn = mock_ladybug["conn"]
        ep_id = store.add_episode("Test episode content", user_id="user1")
        assert ep_id.startswith("ep:")
        assert conn.execute.called

    def test_add_episode_ids_unique_same_millisecond(self, store, mock_ladybug):
        # IDs must not collide even when generated within the same millisecond
        ids = {store.add_episode(f"content {i}", user_id="user1") for i in range(50)}
        assert len(ids) == 50

    def test_search_empty_result(self, store, mock_ladybug):
        empty = MagicMock()
        empty.has_next.return_value = False
        mock_ladybug["conn"].execute.return_value = empty

        result = store.search("test query")
        assert result["seeds"] == []
        assert result["facts"] == []

    def test_format_for_prompt_empty(self, store, mock_ladybug):
        empty = MagicMock()
        empty.has_next.return_value = False
        mock_ladybug["conn"].execute.return_value = empty

        output = store.format_for_prompt("what does Alice prefer?")
        assert output == ""

    def test_format_for_prompt_with_results(self, store, mock_ladybug):
        # Simulate vector search returning one result
        conn = mock_ladybug["conn"]

        seed_result = MagicMock()
        seed_result.has_next.side_effect = [True, False]
        seed_result.get_next.return_value = (
            {"id": "ent:alice:person", "name": "Alice", "entity_type": "person"},
            0.1,
        )

        graph_result = MagicMock()
        graph_result.has_next.side_effect = [True, False]
        graph_result.get_next.return_value = ("Alice", "USES", "Alice uses Python.", "Python")

        conn.execute.side_effect = [seed_result, graph_result]

        output = store.format_for_prompt("Alice")
        assert "KNOWLEDGE GRAPH CONTEXT" in output
        assert "Alice" in output
        # Trust boundary: recalled facts are delimited and flagged as untrusted data
        assert "untrusted" in output.lower()
        assert "--- begin recalled facts ---" in output
        assert "--- end recalled facts ---" in output

    def test_close_called(self, store, mock_ladybug):
        store.close()
        # Should not raise; DB and conn close called
        mock_ladybug["db"].close.assert_called_once()
        mock_ladybug["conn"].close.assert_called_once()


# ---------------------------------------------------------------------------
# GraphMemoryWriter tests
# ---------------------------------------------------------------------------

class TestGraphMemoryWriter:
    def test_short_message_skipped(self, store):
        store.upsert_entity = MagicMock()
        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=lambda p: '{"entities":[],"facts":[]}',
            extract_every_n_turns=1,
            min_message_length=100,
        )
        writer.enqueue("short", user_id="u1")
        writer.flush()
        time.sleep(0.1)
        # Short message never queued — upsert should not have been called
        store.upsert_entity.assert_not_called()
        writer.stop()

    def test_batch_every_n_turns(self, store):
        """Messages enqueued before nth turn should be batched."""
        processed = []
        def _llm(prompt: str) -> str:
            processed.append(prompt)
            return '{"entities":[],"facts":[]}'

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm,
            extract_every_n_turns=3,
            min_message_length=10,
        )
        writer.enqueue("message one is long enough", user_id="u1")
        writer.enqueue("message two is long enough", user_id="u1")
        # Not yet at turn 3 — nothing should be queued
        time.sleep(0.05)
        assert processed == []

        writer.enqueue("message three is long enough", user_id="u1")
        time.sleep(0.2)  # Allow worker time to process
        assert len(processed) == 1
        writer.stop()

    def test_flush_forces_processing(self, store):
        """flush() should force-process pending items immediately."""
        processed = []
        def _llm(prompt: str) -> str:
            processed.append(prompt)
            return '{"entities":[],"facts":[]}'

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm,
            extract_every_n_turns=10,
            min_message_length=10,
        )
        writer.enqueue("a long enough message for testing", user_id="u1")
        writer.flush()
        time.sleep(0.2)
        assert len(processed) == 1
        writer.stop()

    def test_llm_error_does_not_crash_worker(self, store):
        """LLM errors should be swallowed; worker should remain alive."""
        def _llm_error(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm_error,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        writer.enqueue("a long enough message for testing", user_id="u1")
        writer.flush()
        time.sleep(0.2)
        assert writer._thread.is_alive()
        writer.stop()

    def test_entities_upserted_on_extraction(self, store):
        store.upsert_entity = MagicMock(return_value="ent:alice:person")
        store.add_relation = MagicMock()
        store.add_episode = MagicMock(return_value="ep:1")

        def _llm(prompt: str) -> str:
            return '{"entities":[{"name":"Alice","entity_type":"person"}],"facts":[]}'

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        writer.enqueue("A long enough message to process", user_id="u1")
        time.sleep(0.3)
        store.upsert_entity.assert_called_once()
        call_args = store.upsert_entity.call_args
        assert call_args[0][0] == "Alice"
        assert call_args[0][1] == "person"
        writer.stop()

    def test_facts_added_on_extraction(self, store):
        store.upsert_entity = MagicMock(side_effect=lambda n, t, ts: f"ent:{n.lower()}:{t}")
        store.add_relation = MagicMock()
        store.add_episode = MagicMock(return_value="ep:1")

        def _llm(prompt: str) -> str:
            return (
                '{"entities":['
                '{"name":"Alice","entity_type":"person"},'
                '{"name":"Python","entity_type":"tool"}],'
                '"facts":[{"source":"Alice","target":"Python","relation_type":"USES","fact":"Alice uses Python."}]}'
            )

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        writer.enqueue("A long enough message to process it", user_id="u1")
        time.sleep(0.3)
        assert store.add_relation.called
        writer.stop()

    def test_fact_with_unlisted_entity_creates_node(self, store):
        """A fact referencing an entity not in the entities list must still
        create the node so the relation is not silently dropped."""
        upserts: list[tuple] = []

        def _upsert(name, etype, ts):
            upserts.append((name, etype))
            return f"ent:{name.lower()}:{etype}"

        store.upsert_entity = MagicMock(side_effect=_upsert)
        store.add_relation = MagicMock()
        store.add_episode = MagicMock(return_value="ep:1")

        def _llm(prompt: str) -> str:
            # Fact references "Bob" and "Docker" but only "Bob" is in entities
            return (
                '{"entities":[{"name":"Bob","entity_type":"person"}],'
                '"facts":[{"source":"Bob","target":"Docker","relation_type":"USES",'
                '"fact":"Bob uses Docker."}]}'
            )

        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        writer.enqueue("A long enough message to process here", user_id="u1")
        time.sleep(0.3)
        # Docker (unlisted) must have been upserted as type "other"
        assert ("Docker", "other") in upserts
        # And the relation must have been added
        assert store.add_relation.called
        writer.stop()

    def test_stop_joins_worker_thread(self, store):
        store.add_episode = MagicMock(return_value="ep:1")
        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=lambda p: '{"entities":[],"facts":[]}',
            extract_every_n_turns=1,
            min_message_length=10,
        )
        assert writer._thread.is_alive()
        writer.stop()
        # After stop(), the worker thread must have terminated (joined)
        assert not writer._thread.is_alive()


# ---------------------------------------------------------------------------
# create_graph_memory factory tests
# ---------------------------------------------------------------------------

class TestCreateGraphMemory:
    def _make_cfg(self, enabled=True, **kwargs):
        from config_schema import GraphMemoryConfig, AppConfig, TelegramConfig, AgentConfig
        from config_schema import EmbeddingsConfig, SchedulerConfig, PathsConfig, ModelConfig
        gm = GraphMemoryConfig(enabled=enabled, **kwargs)
        models = [ModelConfig(name="t", provider="openai", model="gpt-4o-mini")]
        return AppConfig(
            telegram=TelegramConfig(bot_token="x"),
            agent=AgentConfig(),
            models=models,
            embeddings=EmbeddingsConfig(),
            scheduler=SchedulerConfig(),
            paths=PathsConfig(),
            graph_memory=gm,
        )

    def test_disabled_returns_nones(self):
        cfg = self._make_cfg(enabled=False)
        store, writer = create_graph_memory(
            cfg=cfg,
            embedder_fn=lambda t: [0.1],
            llm_call_fn=lambda p: "",
            embedding_dim=1,
        )
        assert store is None
        assert writer is None

    def test_missing_ladybug_returns_nones(self, monkeypatch):
        monkeypatch.setattr("graph_memory._LADYBUG_AVAILABLE", False)
        cfg = self._make_cfg(enabled=True)
        store, writer = create_graph_memory(
            cfg=cfg,
            embedder_fn=lambda t: [0.1],
            llm_call_fn=lambda p: "",
            embedding_dim=1,
        )
        assert store is None
        assert writer is None

    def test_enabled_creates_store_and_writer(self, mock_ladybug, tmp_path):
        cfg = self._make_cfg(enabled=True, db_path=str(tmp_path / "gm"))
        store, writer = create_graph_memory(
            cfg=cfg,
            embedder_fn=lambda t: [0.1] * 4,
            llm_call_fn=lambda p: "",
            embedding_dim=4,
        )
        assert store is not None
        assert writer is not None
        writer.stop()
