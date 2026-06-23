#!/usr/bin/env python3
"""Smoke-test script for the graph-memory subsystem.

Verifies end-to-end that GraphMemoryStore + GraphMemoryWriter work correctly
against a real LadybugDB installation.  All scenarios use a fake embedder and
a deterministic LLM stub so NO external API keys are required.

Usage:
    python scripts/smoke_graph_memory.py [--db-dir /path/to/dir]

Exit code:
    0  — all scenarios passed
    1  — one or more scenarios failed or ladybug is not installed
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
import traceback

# ---------------------------------------------------------------------------
# Check ladybug availability first (hard dependency for this script)
# ---------------------------------------------------------------------------
try:
    import ladybug  # noqa: F401  # type: ignore[import]
except ImportError:
    print("ERROR: 'ladybug' package is not installed.", file=sys.stderr)
    print("Install it with:  pip install ladybug", file=sys.stderr)
    sys.exit(1)

# Ensure the repo root is on the path so graph_memory can be imported when the
# script is run from the repo root or from the scripts/ sub-directory.
import os

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from graph_memory import GraphMemoryStore, GraphMemoryWriter  # noqa: E402


# ---------------------------------------------------------------------------
# Fake embedder and LLM stub (no external APIs required)
# ---------------------------------------------------------------------------

_EMB_DIM = 16


def _fake_embedder(text: str) -> list[float]:
    """Deterministic 16-d unit embedding from SHA-256 of text."""
    digest = hashlib.sha256(text.encode()).digest()
    raw = [((b / 255.0) * 2.0 - 1.0) for b in digest[:16]]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


def _llm_stub(_prompt: str) -> str:
    """Return a deterministic Alice/Python extraction regardless of input."""
    return (
        '{"entities":['
        '{"name":"Alice","entity_type":"person"},'
        '{"name":"Python","entity_type":"tool"}],'
        '"facts":[{"source":"Alice","target":"Python",'
        '"relation_type":"USES","fact":"Alice uses Python for scripting."}]}'
    )


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


def _run(name: str, fn) -> bool:
    """Run fn(); print PASS or FAIL with traceback on failure.  Returns True on pass."""
    try:
        fn()
        print(f"  [{_PASS}] {name}")
        return True
    except Exception:  # noqa: BLE001
        print(f"  [{_FAIL}] {name}")
        traceback.print_exc(limit=4)
        return False


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_store_init(db_dir: str) -> None:
    """GraphMemoryStore creates a new DB file and loads the VECTOR extension."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s1"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    s.close()


def scenario_entity_add_search(db_dir: str) -> None:
    """Upserted entity appears in vector search results."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s2"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    try:
        ts = "2026-01-01T00:00:00Z"
        s.upsert_entity("Alice", "person", ts)
        result = s.search("Alice", k=5)
        names = [e["name"] for e in result["seeds"]]
        assert "Alice" in names, f"'Alice' not found in seeds: {names}"
    finally:
        s.close()


def scenario_episode_add_search(db_dir: str) -> None:
    """Stored episode appears in episode vector search results."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s3"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    try:
        ep_id = s.add_episode("Alice prefers dark mode.", user_id="user1")
        assert ep_id.startswith("ep:"), f"Unexpected episode ID: {ep_id}"
        result = s.search("Alice dark mode", k=5)
        ep_ids = [ep["id"] for ep in result["episodes"]]
        assert ep_id in ep_ids, f"Episode ID {ep_id} not found in: {ep_ids}"
    finally:
        s.close()


def scenario_relation_graph_expansion(db_dir: str) -> None:
    """Relation between two entities is visible via graph expansion."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s4"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    try:
        ts = "2026-01-01T00:00:00Z"
        alice_id = s.upsert_entity("Alice", "person", ts)
        python_id = s.upsert_entity("Python", "tool", ts)
        s.add_relation(alice_id, python_id, "USES", "Alice uses Python.", ts)
        result = s.search("Alice", k=5)
        relations = [f["relation"] for f in result["facts"]]
        assert "USES" in relations, f"'USES' relation not found in: {relations}"
    finally:
        s.close()


def scenario_writer_extraction(db_dir: str) -> None:
    """GraphMemoryWriter extracts entities and writes them to the store."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s5"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    writer = GraphMemoryWriter(
        store=s,
        llm_call_fn=_llm_stub,
        extract_every_n_turns=1,
        min_message_length=10,
    )
    try:
        writer.enqueue(
            "Alice uses Python for scripting and data analysis.",
            user_id="user1",
        )
        writer.flush()
        time.sleep(0.5)  # Let the background worker finish.
    finally:
        writer.stop()
        s.close()

    # Re-open the same DB and verify the entity was persisted.
    s2 = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s5"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    try:
        result = s2.search("Alice", k=5)
        names = [e["name"] for e in result["seeds"]]
        assert "Alice" in names, f"'Alice' not found after reopen: {names}"
    finally:
        s2.close()


def scenario_format_for_prompt(db_dir: str) -> None:
    """format_for_prompt returns non-empty, structured output after data is stored."""
    s = GraphMemoryStore(
        db_path=os.path.join(db_dir, "s6"),
        embedder_fn=_fake_embedder,
        embedding_dim=_EMB_DIM,
        buffer_pool_mb=64,
    )
    writer = GraphMemoryWriter(
        store=s,
        llm_call_fn=_llm_stub,
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

    try:
        output = s.format_for_prompt("Alice Python")
        assert output, "format_for_prompt returned empty string"
        assert "KNOWLEDGE GRAPH CONTEXT" in output, "Missing context header"
        assert "--- begin recalled facts ---" in output, "Missing facts delimiter"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db-dir",
        default=None,
        help="Directory for test DB files (default: temporary dir, auto-cleaned).",
    )
    args = parser.parse_args()

    use_tmp = args.db_dir is None
    db_dir = args.db_dir or tempfile.mkdtemp(prefix="smoke_graph_")

    print(f"\n🔬  Graph Memory Smoke Tests  (DB dir: {db_dir})\n")

    scenarios = [
        ("Store init + VECTOR extension load", lambda: scenario_store_init(db_dir)),
        ("Add entity + vector search",         lambda: scenario_entity_add_search(db_dir)),
        ("Add episode + episode search",        lambda: scenario_episode_add_search(db_dir)),
        ("Add relation + graph expansion",      lambda: scenario_relation_graph_expansion(db_dir)),
        ("Writer extraction pipeline",          lambda: scenario_writer_extraction(db_dir)),
        ("format_for_prompt output",            lambda: scenario_format_for_prompt(db_dir)),
    ]

    passed = sum(_run(name, fn) for name, fn in scenarios)
    total = len(scenarios)

    print(f"\n{'=' * 48}")
    print(f"  Results: {passed}/{total} passed")
    print(f"{'=' * 48}\n")

    if use_tmp:
        import shutil
        shutil.rmtree(db_dir, ignore_errors=True)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
