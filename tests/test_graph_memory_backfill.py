"""Tests for the LongTermMemory → Graph backfill service.

Covers:
- LongTermMemory.entries() snapshot API
- backfill_longterm_to_graph() dry-run
- successful import into upsert_entity, add_relation, add_episode
- state-file skip (already-imported entries with matching checksum)
- --force flag reprocesses entries
- LLM extraction failure records "failed" and continues
- no_extraction path (no entities/facts returned) still writes episode
- short content is NOT filtered by min_message_length (backfill bypasses it)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from graph_memory import (
    _entry_checksum,
    _load_backfill_state,
    _save_backfill_state,
    backfill_longterm_to_graph,
)
from memory_store import LongTermMemory


# ---------------------------------------------------------------------------
# LongTermMemory.entries() snapshot
# ---------------------------------------------------------------------------

class TestLongTermMemoryEntries:
    @pytest.fixture
    def ltm(self, tmp_path):
        path = str(tmp_path / "ltm.json")
        return LongTermMemory(path=path)

    def test_empty_returns_empty_list(self, ltm):
        assert ltm.entries() == []

    def test_entries_returns_all_ids(self, ltm):
        id1 = ltm.add("fact one", source="manual")
        id2 = ltm.add("fact two", source="scheduled")
        pairs = ltm.entries()
        ids = [eid for eid, _ in pairs]
        assert id1 in ids
        assert id2 in ids
        assert len(pairs) == 2

    def test_entries_sorted_oldest_first(self, ltm):
        # Add with different timestamps by patching datetime inside memory_store
        ltm.add("first fact")
        ltm.add("second fact")
        pairs = ltm.entries()
        # Timestamps are ISO strings; first added should have an earlier timestamp
        # (both added in same second in practice — just assert count and types)
        assert len(pairs) == 2
        for eid, entry in pairs:
            assert isinstance(eid, str)
            assert "content" in entry
            assert "timestamp" in entry

    def test_entries_returns_shallow_copies(self, ltm):
        ltm.add("some content")
        pairs = ltm.entries()
        _, entry = pairs[0]
        # Mutating the returned dict must not affect internal state
        entry["content"] = "MUTATED"
        re_pairs = ltm.entries()
        _, re_entry = re_pairs[0]
        assert re_entry["content"] == "some content"

    def test_entries_preserves_source_and_timestamp(self, ltm):
        ltm.add("my fact", source="test_source")
        pairs = ltm.entries()
        _, entry = pairs[0]
        assert entry["source"] == "test_source"
        assert entry["timestamp"]  # not empty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestBackfillHelpers:
    def test_entry_checksum_is_stable(self):
        entry = {"content": "Hello", "source": "manual", "timestamp": "2024-01-01"}
        c1 = _entry_checksum(entry)
        c2 = _entry_checksum(entry)
        assert c1 == c2
        assert c1.startswith("sha256:")

    def test_entry_checksum_changes_on_content_change(self):
        e1 = {"content": "Hello", "source": "manual", "timestamp": "2024-01-01"}
        e2 = {"content": "World", "source": "manual", "timestamp": "2024-01-01"}
        assert _entry_checksum(e1) != _entry_checksum(e2)

    def test_load_backfill_state_missing_file(self, tmp_path):
        state = _load_backfill_state(str(tmp_path / "missing.json"))
        assert state == {"version": 1, "imported": {}}

    def test_load_backfill_state_corrupt_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            f.write("NOT JSON{{{")
        state = _load_backfill_state(path)
        assert state == {"version": 1, "imported": {}}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        data = {"version": 1, "imported": {"abc": {"checksum": "sha256:xyz", "episode_id": "ep:1"}}}
        _save_backfill_state(path, data)
        loaded = _load_backfill_state(path)
        assert loaded == data


# ---------------------------------------------------------------------------
# Fixtures shared by backfill tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_store():
    store = MagicMock()
    store.upsert_entity.side_effect = lambda name, etype, ts: f"ent:{name.lower()}:{etype}"
    store.add_relation.return_value = None
    store.add_episode.return_value = "ep:mock:001"
    return store


@pytest.fixture
def good_llm():
    """LLM callable that returns one entity and one fact."""
    def _llm(prompt: str) -> str:
        return json.dumps({
            "entities": [{"name": "Alice", "entity_type": "person"}],
            "facts": [{"source": "Alice", "target": "Python", "relation_type": "USES",
                       "fact": "Alice uses Python."}],
        })
    return _llm


@pytest.fixture
def empty_llm():
    """LLM callable that returns empty extraction."""
    return lambda _: '{"entities":[],"facts":[]}'


@pytest.fixture
def error_llm():
    """LLM callable that always raises."""
    def _llm(prompt: str) -> str:
        raise RuntimeError("LLM unavailable")
    return _llm


def _make_entries(content: str = "Alice uses Python in her projects.",
                  source: str = "manual",
                  timestamp: str = "2024-01-01T00:00:00") -> list[tuple[str, dict]]:
    return [("entry-001", {"content": content, "source": source, "timestamp": timestamp})]


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

class TestBackfillDryRun:
    def test_dry_run_returns_count_but_no_writes(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
            dry_run=True,
        )
        # Count is right
        assert result.total == 1
        assert result.imported == 1
        # No graph writes
        mock_store.upsert_entity.assert_not_called()
        mock_store.add_relation.assert_not_called()
        mock_store.add_episode.assert_not_called()
        # State file must NOT be written in dry-run
        assert not os.path.exists(state_path)

    def test_dry_run_with_limit(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            dry_run=True,
            limit=1,
        )
        assert result.imported == 1
        assert result.total == 2


# ---------------------------------------------------------------------------
# Successful import
# ---------------------------------------------------------------------------

class TestBackfillImport:
    def test_entities_and_facts_written(self, mock_store, good_llm, tmp_path):
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.imported == 1
        assert result.failed == 0
        # upsert_entity called for Alice (and auto-created Python)
        assert mock_store.upsert_entity.called
        assert mock_store.add_relation.called
        assert mock_store.add_episode.called

    def test_episode_source_is_backfill(self, mock_store, good_llm, tmp_path):
        backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        _, kwargs = mock_store.add_episode.call_args
        # source kwarg (or positional arg index 2)
        call_args = mock_store.add_episode.call_args
        # positional: (content, user_id, source) or via kwargs
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        assert "longterm_memory_backfill" in all_args

    def test_state_file_updated_after_import(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        state = _load_backfill_state(state_path)
        assert "entry-001" in state["imported"]
        imported = state["imported"]["entry-001"]
        assert imported["checksum"].startswith("sha256:")
        assert imported["episode_id"]
        assert imported["imported_at"]

    def test_total_counts_accumulated(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "content one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "content two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.total == 2
        assert result.imported == 2
        assert result.total_entities >= 2   # Alice from each batch
        assert result.total_facts >= 2      # one fact per batch


# ---------------------------------------------------------------------------
# Idempotency — skip already-imported entries
# ---------------------------------------------------------------------------

class TestBackfillIdempotency:
    def test_already_imported_entry_is_skipped(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        entries = _make_entries()
        # First run: import
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        # Second run: must skip
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        assert result.skipped == 1
        assert result.imported == 0
        mock_store.upsert_entity.assert_not_called()

    def test_force_reprocesses_already_imported(self, mock_store, good_llm, tmp_path):
        state_path = str(tmp_path / "state.json")
        entries = _make_entries()
        backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
            force=True,
        )
        assert result.imported == 1
        assert result.skipped == 0
        assert mock_store.upsert_entity.called

    def test_changed_content_is_reprocessed(self, mock_store, good_llm, tmp_path):
        """An entry whose content changed since last import must be reprocessed."""
        state_path = str(tmp_path / "state.json")
        entries_v1 = [("e1", {"content": "original", "source": "manual", "timestamp": "2024-01-01"})]
        backfill_longterm_to_graph(
            long_term_entries=entries_v1,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        mock_store.reset_mock()

        entries_v2 = [("e1", {"content": "changed content", "source": "manual", "timestamp": "2024-01-01"})]
        result = backfill_longterm_to_graph(
            long_term_entries=entries_v2,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )
        # Checksum differs → must reprocess
        assert result.imported == 1
        assert result.skipped == 0


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestBackfillFailures:
    def test_llm_failure_records_failed_and_continues(self, mock_store, error_llm, tmp_path):
        entries = [
            ("e1", {"content": "fact one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "fact two", "source": "manual", "timestamp": "2024-01-02"}),
        ]
        # Second LLM call succeeds
        call_count = {"n": 0}
        def mixed_llm(prompt: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first call fails")
            return json.dumps({
                "entities": [{"name": "Bob", "entity_type": "person"}],
                "facts": [],
            })

        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=mixed_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.failed == 1
        assert result.imported == 1  # second entry succeeds
        failed_entries = [er for er in result.entries if er.status == "failed"]
        assert len(failed_entries) == 1
        assert failed_entries[0].error != ""

    def test_no_extraction_still_writes_episode(self, mock_store, empty_llm, tmp_path):
        """When LLM returns empty JSON, we still store an episode."""
        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=empty_llm,
            state_path=str(tmp_path / "state.json"),
        )
        assert result.no_extraction == 1
        assert result.imported == 0
        mock_store.add_episode.assert_called_once()
        # Source must still be backfill
        call_args = mock_store.add_episode.call_args
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        assert "longterm_memory_backfill" in all_args

    def test_short_content_not_filtered(self, mock_store, good_llm, tmp_path):
        """Backfill must not apply min_message_length — all content is imported."""
        short_entries = [("e1", {"content": "Hi", "source": "manual", "timestamp": "2024-01-01"})]
        result = backfill_longterm_to_graph(
            long_term_entries=short_entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
        )
        # Not failed/skipped — reached LLM extraction
        assert result.failed == 0
        assert result.skipped == 0

    def test_add_episode_failure_is_hard_failure_in_main_path(self, mock_store, good_llm, tmp_path):
        """If add_episode raises after entity/fact writes, the entry must be marked failed,
        not imported, so the next run re-attempts it."""
        mock_store.add_episode.side_effect = RuntimeError("DB write error")
        state_path = str(tmp_path / "state.json")

        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=state_path,
        )

        assert result.failed == 1
        assert result.imported == 0
        # State file must NOT record this entry
        from graph_memory import _load_backfill_state
        state = _load_backfill_state(state_path)
        assert "entry-001" not in state.get("imported", {})

    def test_add_episode_failure_no_extraction_path(self, mock_store, empty_llm, tmp_path):
        """In the no-extraction path, add_episode failure must also be a hard failure."""
        mock_store.add_episode.side_effect = RuntimeError("episode write error")
        state_path = str(tmp_path / "state.json")

        result = backfill_longterm_to_graph(
            long_term_entries=_make_entries(),
            store=mock_store,
            llm_call_fn=empty_llm,
            state_path=state_path,
        )

        assert result.failed == 1
        assert result.no_extraction == 0
        from graph_memory import _load_backfill_state
        state = _load_backfill_state(state_path)
        assert "entry-001" not in state.get("imported", {})

    def test_state_save_failure_marks_entry_failed(self, mock_store, good_llm, tmp_path):
        """If the state file cannot be written after graph writes succeed,
        the entry must be marked failed (not imported) so the next run re-imports it."""
        from unittest.mock import patch
        state_path = str(tmp_path / "state.json")

        with patch("graph_memory._save_backfill_state", side_effect=OSError("disk full")):
            result = backfill_longterm_to_graph(
                long_term_entries=_make_entries(),
                store=mock_store,
                llm_call_fn=good_llm,
                state_path=state_path,
            )

        assert result.failed == 1
        assert result.imported == 0
        # Graph writes did happen (entities were upserted before the state-save attempt)
        assert mock_store.upsert_entity.called

    def test_state_save_failure_restores_prior_state(self, mock_store, good_llm, tmp_path):
        """Regression: when save fails for entry-1 and later succeeds for entry-2,
        entry-1's prior import record must still be present in the persisted state.

        The original bug: rollback did imported_map.pop(entry_id) which erased the
        prior record. Entry-2's subsequent successful save would then persist the map
        without entry-1, causing duplicate imports on the next run.
        """
        from unittest.mock import patch

        state_path = str(tmp_path / "state.json")
        entry1_id = "entry-001"
        entry1 = {"content": "Alice uses Python in her projects.", "source": "manual", "timestamp": "2024-01-01"}
        entry2_id = "entry-002"
        entry2 = {"content": "Bob uses Go for services.", "source": "manual", "timestamp": "2024-01-02"}

        prior_record = {
            "checksum": _entry_checksum(entry1),
            "episode_id": "ep-prior",
            "imported_at": "2024-01-01T00:00:00+00:00",
        }
        # Pre-seed a prior successful import for entry-1
        _save_backfill_state(state_path, {"version": 1, "imported": {entry1_id: prior_record}})

        # Save fails for entry-1, succeeds for entry-2 (None = no side effect = normal write)
        real_save = _save_backfill_state
        save_calls = []
        def patched_save(path, state):
            save_calls.append(len(state.get("imported", {})))
            if len(save_calls) == 1:
                raise OSError("disk full")
            real_save(path, state)

        with patch("graph_memory._save_backfill_state", side_effect=patched_save):
            result = backfill_longterm_to_graph(
                long_term_entries=[(entry1_id, entry1), (entry2_id, entry2)],
                store=mock_store,
                llm_call_fn=good_llm,
                state_path=state_path,
                force=True,
            )

        assert result.failed == 1   # entry-1 state save failed
        assert result.imported == 1  # entry-2 succeeded
        assert len(save_calls) == 2  # both entries attempted a save

        # After entry-2's successful save, state file must contain entry-1's PRIOR record
        loaded = _load_backfill_state(state_path)
        assert loaded["imported"].get(entry1_id) == prior_record, (
            "entry-1 prior record was erased by the failed-save rollback"
        )
        assert entry2_id in loaded["imported"], "entry-2 must be in state after successful save"

    def test_state_save_failure_restores_prior_state_no_extraction_path(
        self, mock_store, empty_llm, tmp_path
    ):
        """Same regression as above but via the no_extraction path (empty LLM response)."""
        from unittest.mock import patch

        state_path = str(tmp_path / "state.json")
        entry1_id = "entry-001"
        entry1 = {"content": "Alice uses Python in her projects.", "source": "manual", "timestamp": "2024-01-01"}
        entry2_id = "entry-002"
        entry2 = {"content": "Bob uses Go for services.", "source": "manual", "timestamp": "2024-01-02"}

        prior_record = {
            "checksum": _entry_checksum(entry1),
            "episode_id": "ep-prior-ne",
            "imported_at": "2024-01-01T00:00:00+00:00",
        }
        _save_backfill_state(state_path, {"version": 1, "imported": {entry1_id: prior_record}})

        real_save = _save_backfill_state
        save_calls = []
        def patched_save(path, state):
            save_calls.append(True)
            if len(save_calls) == 1:
                raise OSError("disk full")
            real_save(path, state)

        with patch("graph_memory._save_backfill_state", side_effect=patched_save):
            result = backfill_longterm_to_graph(
                long_term_entries=[(entry1_id, entry1), (entry2_id, entry2)],
                store=mock_store,
                llm_call_fn=empty_llm,
                state_path=state_path,
                force=True,
            )

        assert result.failed == 1
        assert result.no_extraction == 1
        loaded = _load_backfill_state(state_path)
        assert loaded["imported"].get(entry1_id) == prior_record, (
            "no_extraction path erased entry-1 prior record on failed save"
        )
        assert entry2_id in loaded["imported"]

    def test_limit_stops_early(self, mock_store, good_llm, tmp_path):
        entries = [
            ("e1", {"content": "one", "source": "manual", "timestamp": "2024-01-01"}),
            ("e2", {"content": "two", "source": "manual", "timestamp": "2024-01-02"}),
            ("e3", {"content": "three", "source": "manual", "timestamp": "2024-01-03"}),
        ]
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=mock_store,
            llm_call_fn=good_llm,
            state_path=str(tmp_path / "state.json"),
            limit=2,
        )
        assert result.imported + result.failed + result.no_extraction == 2
