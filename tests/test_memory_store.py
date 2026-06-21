"""Tests for memory_store.py — MemoryStore, ShortTermMemory, WorkingMemory."""

from __future__ import annotations

import json
import threading

import pytest

from memory_store import (
    MemoryStore,
    ShortTermMemory,
    WorkingMemory,
    _cosine_similarity,
)


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        assert _cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_arbitrary_vectors(self):
        a = [1, 2, 3]
        b = [4, 5, 6]
        # manually: dot=32, |a|=sqrt(14), |b|=sqrt(77)
        expected = 32 / (14**0.5 * 77**0.5)
        assert _cosine_similarity(a, b) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class TestMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        path = str(tmp_path / "memory.json")
        return MemoryStore(path)

    def test_initial_state_has_defaults(self, store):
        assert store.get("known_services") == []
        assert store.get("last_health_check") is None
        assert store.get("notes") == []

    def test_set_and_get(self, store):
        store.set("foo", "bar")
        assert store.get("foo") == "bar"

    def test_get_default(self, store):
        assert store.get("nonexistent", 42) == 42

    def test_delete(self, store):
        store.set("x", 1)
        store.delete("x")
        assert store.get("x") is None

    def test_delete_nonexistent_no_error(self, store):
        store.delete("never_existed")

    def test_all_returns_copy(self, store):
        store.set("a", 1)
        data = store.all()
        data["a"] = 999
        assert store.get("a") == 1

    def test_update_batch(self, store):
        store.update({"k1": "v1", "k2": "v2"})
        assert store.get("k1") == "v1"
        assert store.get("k2") == "v2"

    def test_persistence(self, tmp_path):
        path = str(tmp_path / "mem.json")
        s1 = MemoryStore(path)
        s1.set("persistent", True)
        # Re-open
        s2 = MemoryStore(path)
        assert s2.get("persistent") is True

    def test_purge_matching(self, store):
        store.update({
            "active_model": "gpt-4",
            "available_models": ["a", "b"],
            "llm_model_config": {},
            "remodel_schedule": "never",  # should NOT match
            "_internal": "skip",  # underscore prefix — should NOT match
        })
        count = store.purge_matching("model")
        assert count == 3
        assert store.get("active_model") is None
        assert store.get("available_models") is None
        assert store.get("llm_model_config") is None
        assert store.get("remodel_schedule") == "never"
        assert store.get("_internal") == "skip"

    def test_record_event(self, store):
        store.record_event("boot")
        store.record_event("shutdown")
        log = store.get("_event_log")
        assert len(log) == 2
        assert log[0]["event"] == "boot"
        assert log[1]["event"] == "shutdown"

    def test_record_event_cap_at_50(self, store):
        for i in range(60):
            store.record_event(f"event_{i}")
        log = store.get("_event_log")
        assert len(log) == 50
        assert log[0]["event"] == "event_10"

    def test_as_prompt_text_empty(self, tmp_path):
        path = str(tmp_path / "empty.json")
        # Write empty dict to avoid seeded defaults
        with open(path, "w") as f:
            json.dump({}, f)
        store = MemoryStore(path)
        assert store.as_prompt_text() == "No persistent memory entries."

    def test_as_prompt_text_with_data(self, store):
        store.set("host", "server1")
        text = store.as_prompt_text()
        assert "host" in text
        assert "server1" in text

    def test_as_prompt_text_excludes_internal_keys(self, store):
        # Public keys are injected; internal ``_``-prefixed keys (e.g. the
        # event log) must be excluded to avoid prompt noise/token waste.
        store.set("host", "server1")
        store.record_event("boot")
        store.record_event("shutdown")
        text = store.as_prompt_text()
        assert "host" in text
        assert "server1" in text
        assert "_event_log" not in text
        assert "boot" not in text
        assert "shutdown" not in text

    def test_as_prompt_text_internal_only_returns_empty_message(self, tmp_path):
        # When only internal keys exist, the prompt block should fall back to the
        # standard "no entries" message rather than rendering internal state.
        path = str(tmp_path / "internal_only.json")
        with open(path, "w") as f:
            json.dump({}, f)
        store = MemoryStore(path)
        store.record_event("boot")
        assert store.as_prompt_text() == "No persistent memory entries."

    def test_thread_safety(self, tmp_path):
        path = str(tmp_path / "threaded.json")
        store = MemoryStore(path)
        errors = []

        def writer(n):
            try:
                for i in range(50):
                    store.set(f"thread_{n}_{i}", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All writes should be present
        assert store.get("thread_0_49") == 49
        assert store.get("thread_3_0") == 0

    def test_corrupt_file_loads_fresh(self, tmp_path):
        path = str(tmp_path / "corrupt.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        store = MemoryStore(path)
        assert store.all() == {}


# ---------------------------------------------------------------------------
# ShortTermMemory
# ---------------------------------------------------------------------------

class TestShortTermMemory:
    def test_add_and_get(self):
        stm = ShortTermMemory(max_turns=5)
        stm.add("user", "hello")
        stm.add("assistant", "hi")
        msgs = stm.get_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}

    def test_ring_buffer_overflow(self):
        stm = ShortTermMemory(max_turns=3)
        for i in range(5):
            stm.add("user", f"msg_{i}")
        msgs = stm.get_messages()
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg_2"

    def test_clear(self):
        stm = ShortTermMemory()
        stm.add("user", "x")
        stm.clear()
        assert stm.get_messages() == []

    def test_as_prompt_text_empty(self):
        stm = ShortTermMemory()
        assert stm.as_prompt_text() == "No recent conversation."

    def test_as_prompt_text_content(self):
        stm = ShortTermMemory()
        stm.add("user", "what time is it?")
        text = stm.as_prompt_text()
        assert "[user]" in text
        assert "what time is it?" in text

    def test_to_dict_and_from_dict(self):
        stm = ShortTermMemory(max_turns=10)
        stm.add("user", "a")
        stm.add("assistant", "b")
        data = stm.to_dict()
        restored = ShortTermMemory.from_dict(data, max_turns=10)
        assert restored.get_messages() == stm.get_messages()

    def test_from_dict_skips_malformed(self):
        data = [
            {"role": "user", "content": "ok"},
            {"bad": "entry"},
            "not a dict",
            {"role": "assistant", "content": "fine"},
        ]
        stm = ShortTermMemory.from_dict(data)
        msgs = stm.get_messages()
        assert len(msgs) == 2


# ---------------------------------------------------------------------------
# WorkingMemory
# ---------------------------------------------------------------------------

class TestWorkingMemory:
    def test_initial_state(self):
        wm = WorkingMemory()
        assert not wm.has_content()
        assert wm.goal == ""
        assert wm.steps == []

    def test_start_task(self):
        wm = WorkingMemory()
        wm.start_task("check disk")
        assert wm.has_content()
        assert wm.goal == "check disk"

    def test_add_step(self):
        wm = WorkingMemory()
        wm.start_task("test")
        wm.add_step("shell", {"command": "ls"})
        assert len(wm.steps) == 1
        assert wm.steps[0]["action"] == "shell"

    def test_to_summary_text(self):
        wm = WorkingMemory()
        wm.start_task("deploy")
        wm.add_step("shell", {"command": "git pull"})
        text = wm.to_summary_text()
        assert "Goal: deploy" in text
        assert "shell" in text

    def test_to_dict(self):
        wm = WorkingMemory()
        wm.start_task("x")
        d = wm.to_dict()
        assert d["goal"] == "x"
        assert isinstance(d["steps"], list)
        assert "started_at" in d

    def test_clear(self):
        wm = WorkingMemory()
        wm.start_task("y")
        wm.add_step("a", {})
        wm.clear()
        assert not wm.has_content()
        assert wm.steps == []
