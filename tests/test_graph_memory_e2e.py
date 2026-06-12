"""End-to-end tests for graph memory using real LadybugDB.

These tests are skipped automatically when the ``ladybug`` package is not
installed (it is an optional, commented-out dependency).  When ladybug IS
present they exercise the full store-write-search pipeline without any mocks:
real DB files are written to a temporary directory, the VECTOR extension is
loaded, and a deterministic fake embedder replaces the OpenAI embedding call
so no external API keys are needed.

Run:
    pytest tests/test_graph_memory_e2e.py -v

All tests are collected regardless; they appear as SKIPPED when ladybug is
absent so CI stays green and the intent is preserved.
"""

from __future__ import annotations

import hashlib
import time

import pytest

# Skip the entire module when ladybug is not installed.
ladybug = pytest.importorskip("ladybug", reason="ladybug not installed — skipping e2e tests")

from graph_memory import GraphMemoryStore, GraphMemoryWriter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_embedder(text: str) -> list[float]:
    """Deterministic 16-d unit-ish embedding based on SHA-256 of the text.

    Cheap and reproducible — no external API required.
    """
    digest = hashlib.sha256(text.encode()).digest()
    raw = [((b / 255.0) * 2.0 - 1.0) for b in digest[:16]]
    # Normalise to unit length so cosine similarity is well defined.
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


_EMB_DIM = 16


def _llm_stub_alice(_prompt: str) -> str:
    """Always extract a deterministic 'Alice / USES / Python' triple."""
    return (
        '{"entities":['
        '{"name":"Alice","entity_type":"person"},'
        '{"name":"Python","entity_type":"tool"}],'
        '"facts":[{"source":"Alice","target":"Python",'
        '"relation_type":"USES","fact":"Alice uses Python for scripting."}]}'
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """Provide a real GraphMemoryStore backed by a fresh LadybugDB file."""
    s = GraphMemoryStore(
        db_path=str(tmp_path / "graph_test"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGraphMemoryE2E:
    """Full-stack tests requiring a real LadybugDB installation."""

    def test_store_creates_db_and_loads_vector_extension(self, tmp_path):
        """GraphMemoryStore can be created; VECTOR extension loads without error."""
        s = GraphMemoryStore(
            db_path=str(tmp_path / "ext_test"),
            embedder_fn=_fake_embedder,
            embedding_dim=_EMB_DIM,
            buffer_pool_mb=64,
        )
        # If we got here the VECTOR extension loaded successfully.
        assert s is not None
        s.close()

    def test_add_and_search_entity(self, store):
        """Upserted entity appears in vector search results."""
        ts = "2026-01-01T00:00:00Z"
        store.upsert_entity("Alice", "person", ts)

        result = store.search("Alice", k=5)
        seed_names = [s["name"] for s in result["seeds"]]
        assert "Alice" in seed_names, f"Entity not found in seeds: {result}"

    def test_add_and_search_episode(self, store):
        """Stored episode appears in episode vector search results."""
        ep_id = store.add_episode("Alice prefers dark mode.", user_id="user1")
        assert ep_id.startswith("ep:")

        result = store.search("Alice dark mode", k=5)
        ep_ids = [ep["id"] for ep in result["episodes"]]
        assert ep_id in ep_ids, f"Episode not found: {result}"

    def test_add_relation_visible_via_graph_expansion(self, store):
        """Relation between two entities is expanded during search."""
        ts = "2026-01-01T00:00:00Z"
        alice_id = store.upsert_entity("Alice", "person", ts)
        python_id = store.upsert_entity("Python", "tool", ts)
        store.add_relation(alice_id, python_id, "USES", "Alice uses Python.", ts)

        result = store.search("Alice", k=5)
        # At least one fact should reference the USES relation.
        relations = [f["relation"] for f in result["facts"]]
        assert "USES" in relations, f"Relation not found in facts: {result}"

    def test_writer_extracts_and_stores_entity(self, store):
        """GraphMemoryWriter processes enqueued text and writes entities to store."""
        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm_stub_alice,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        try:
            writer.enqueue(
                "Alice uses Python for scripting and data analysis.",
                user_id="user1",
            )
            writer.flush()
            # Give the background worker time to finish.
            time.sleep(0.5)
        finally:
            writer.stop()

        result = store.search("Alice", k=5)
        seed_names = [s["name"] for s in result["seeds"]]
        assert "Alice" in seed_names, f"Entity not found after writer: {result}"

    def test_format_for_prompt_nonempty_after_store(self, store):
        """format_for_prompt returns a non-empty string once data is present."""
        writer = GraphMemoryWriter(
            store=store,
            llm_call_fn=_llm_stub_alice,
            extract_every_n_turns=1,
            min_message_length=10,
        )
        try:
            writer.enqueue(
                "Alice uses Python for scripting and data analysis.",
                user_id="user1",
            )
            writer.flush()
            time.sleep(0.5)
        finally:
            writer.stop()

        output = store.format_for_prompt("Alice Python")
        assert output, "format_for_prompt returned empty string after storing data"
        assert "KNOWLEDGE GRAPH CONTEXT" in output
        assert "--- begin recalled facts ---" in output
