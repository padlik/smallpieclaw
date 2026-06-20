"""
memory_store.py
---------------
Lightweight JSON-backed key-value memory store.
Used by the agent to persist facts between sessions
(e.g. known services, last backup time, user preferences).

Also provides 4-tier memory architecture:
  ShortTermMemory  — in-memory ring buffer of recent conversation turns
  WorkingMemory    — in-memory current task context
  LongTermMemory   — persisted vector index of important facts
  ResultsMemory    — persisted vector index of past task results
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_JSON_FILE_ERRORS = (OSError, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list, b: list) -> float:
    """Pure-Python cosine similarity — no numpy required."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MemoryStore:
    """
    Simple persistent memory backed by a JSON file.
    Thread-safe: all mutations are protected by an RLock so parallel
    sub-agents can call memory_write concurrently without data races.
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save_with_retry()

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._save_with_retry()

    def all(self) -> dict[str, Any]:
        """Return a copy of all stored key-value pairs."""
        with self._lock:
            return dict(self._data)

    def update(self, updates: dict[str, Any]) -> None:
        """Batch-update multiple keys."""
        with self._lock:
            self._data.update(updates)
            self._save_with_retry()

    def purge_matching(self, *substrings: str) -> int:
        """Delete keys whose names contain any substring as a whole segment
        (delimited by underscores, hyphens, or string boundaries; plural 's' allowed).

        Examples: ``purge_matching("model")`` matches ``available_models``,
        ``active_model``, ``llm_model_config`` but NOT ``remodel_schedule``
        (the 're' prefix is not a delimiter).

        Internal keys starting with '_' are never purged.
        Returns the number of keys deleted.
        """
        import re
        pattern = re.compile(
            r"(?:(?<![a-zA-Z])|^)("
            + "|".join(re.escape(s) for s in substrings)
            + r")s?(?:[^a-zA-Z]|$)",
            re.IGNORECASE,
        )
        with self._lock:
            to_delete = [
                k for k in self._data
                if not k.startswith("_") and pattern.search(k)
            ]
            for k in to_delete:
                del self._data[k]
            if to_delete:
                self._save_with_retry()
        return len(to_delete)

    def as_prompt_text(self) -> str:
        """Format memory as a short text block suitable for LLM context."""
        with self._lock:
            if not self._data:
                return "No persistent memory entries."
            lines = []
            for k, v in self._data.items():
                lines.append(f"  {k}: {json.dumps(v)}")
            return "\n".join(lines)

    def record_event(self, event: str) -> None:
        """Append a timestamped event to the event log."""
        with self._lock:
            log: list = self._data.setdefault("_event_log", [])
            log.append({"time": datetime.utcnow().isoformat(), "event": event})
            # Keep only last 50 events to avoid unbounded growth
            self._data["_event_log"] = log[-50:]
            self._save_with_retry()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r") as f:
                        self._data = json.load(f)
                    logger.debug("Memory loaded from %s (%d keys)", self.path, len(self._data))
                except _JSON_FILE_ERRORS as exc:
                    logger.warning("Could not load memory from %s: %s — starting fresh", self.path, exc)
                    self._data = {}
            else:
                # Seed with useful defaults
                self._data = {
                    "known_services": [],
                    "notes": [],
                }
                self._save_with_retry()

    def _save_with_retry(self, attempts: int = 3, base_delay: float = 0.05) -> None:
        """Write to disk with exponential-backoff retry. Caller must hold _lock."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{id(self)}"
        delay = base_delay
        for attempt in range(1, attempts + 1):
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                if attempt < attempts:
                    logger.warning("MemoryStore save attempt %d/%d failed: %s — retrying", attempt, attempts, exc)
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("MemoryStore save failed after %d attempts: %s", attempts, exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# ShortTermMemory
# ---------------------------------------------------------------------------

class ShortTermMemory:
    """In-memory ring buffer of recent conversation turns."""

    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self._buffer: deque = deque(maxlen=max_turns)

    def add(self, role: str, content: str) -> None:
        self._buffer.append({"role": role, "content": content})

    def get_messages(self) -> list:
        return list(self._buffer)

    def as_prompt_text(self) -> str:
        messages = list(self._buffer)[-10:]
        if not messages:
            return "No recent conversation."
        lines = [f"  [{m['role']}]: {m['content'][:200]}" for m in messages]
        return "\n".join(lines)

    def clear(self) -> None:
        self._buffer.clear()

    def to_dict(self) -> list:
        """Serialise message buffer to a JSON-serialisable list."""
        return list(self._buffer)

    @classmethod
    def from_dict(cls, data: list, max_turns: int = 20) -> "ShortTermMemory":
        """Deserialise from a list of {role, content} dicts.

        Silently ignores malformed entries so a corrupted context file
        does not crash the sub-agent.
        """
        obj = cls(max_turns=max_turns)
        for msg in data:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                obj._buffer.append({"role": str(msg["role"]), "content": str(msg["content"])})
        return obj


# ---------------------------------------------------------------------------
# WorkingMemory
# ---------------------------------------------------------------------------

class WorkingMemory:
    """In-memory current task context."""

    def __init__(self):
        self._goal: str = ""
        self._steps: list = []
        self._started_at: str = ""

    def start_task(self, goal: str) -> None:
        self._goal = goal
        self._steps = []
        self._started_at = datetime.utcnow().isoformat()

    def add_step(self, action: str, details: dict) -> None:
        self._steps.append({
            "action": action,
            "details": details,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def to_summary_text(self) -> str:
        lines = [f"Goal: {self._goal}"]
        for i, step in enumerate(self._steps, 1):
            detail_str = json.dumps(step["details"])[:100]
            lines.append(f"  Step {i}: {step['action']} - {detail_str}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "goal": self._goal,
            "steps": self._steps,
            "started_at": self._started_at,
        }

    def has_content(self) -> bool:
        return bool(self._goal)

    @property
    def goal(self) -> str:
        return self._goal

    @property
    def steps(self) -> list:
        return self._steps

    def clear(self) -> None:
        self._goal = ""
        self._steps = []
        self._started_at = ""


# ---------------------------------------------------------------------------
# LongTermMemory
# ---------------------------------------------------------------------------

class LongTermMemory:
    """Persisted vector index of important facts.

    LEGACY / BACKFILL-ONLY (P2 consolidation): this JSON store is no longer
    written or read at runtime. Runtime semantic recall is served by graph
    memory (graph_memory.py). This class is retained only so existing
    ``data/longterm_memory.json`` files can be migrated into graph memory via
    ``backfill_graph_memory.py``.
    """

    def __init__(self, path: str, llm=None):
        self.path = path
        self.llm = llm
        self._data: dict = {}
        self._lock = threading.RLock()
        self._load()

    def add(self, content: str, source: str = "manual") -> str:
        entry_id = str(uuid.uuid4())
        vector = []
        if self.llm:
            try:
                vector = self.llm.embed(content)
            except Exception as exc:
                logger.warning("LongTermMemory embed failed: %s", exc)
        with self._lock:
            self._data[entry_id] = {
                "content": content,
                "source": source,
                "timestamp": datetime.utcnow().isoformat(),
                "vector": vector,
            }
            self._save_with_retry()
        return entry_id

    def search(self, query: str, top_k: int = 3) -> list:
        with self._lock:
            if not self._data:
                return []
        query_vec = []
        if self.llm:
            try:
                query_vec = self.llm.embed(query)
            except Exception as exc:
                logger.warning("LongTermMemory search embed failed: %s", exc)
        with self._lock:
            if not query_vec:
                entries = sorted(self._data.values(), key=lambda e: e.get("timestamp", ""), reverse=True)
                return entries[:top_k]
            scored = []
            for entry_id, entry in self._data.items():
                vec = entry.get("vector", [])
                if vec:
                    score = _cosine_similarity(query_vec, vec)
                    scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def as_prompt_text(self, query: str = "", top_k: int = 3) -> str:
        entries = self.search(query, top_k) if query else self._latest(top_k)
        if not entries:
            return "No long-term memory entries."
        lines = []
        for entry in entries:
            ts = entry.get("timestamp", "")[:10]
            lines.append(f"  [{ts}] {entry['content']}")
        return "\n".join(lines)

    def entries(self) -> list[tuple[str, dict]]:
        """Return a snapshot of all entries as (entry_id, entry_dict) pairs.

        The returned dicts are shallow copies — safe to read without holding
        the lock, but callers must not mutate them.  Pairs are sorted oldest-
        first by timestamp so backfill / iteration order is deterministic.
        """
        with self._lock:
            return sorted(
                ((eid, dict(entry)) for eid, entry in self._data.items()),
                key=lambda pair: pair[1].get("timestamp", ""),
            )

    def _latest(self, top_k: int) -> list:
        with self._lock:
            return sorted(self._data.values(), key=lambda e: e.get("timestamp", ""), reverse=True)[:top_k]

    def _load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path) as f:
                        self._data = json.load(f)
                except _JSON_FILE_ERRORS as exc:
                    logger.warning("LongTermMemory load failed: %s", exc)
                    self._data = {}

    def _save_with_retry(self, attempts: int = 3, base_delay: float = 0.05) -> None:
        """Write to disk with exponential-backoff retry. Caller must hold _lock."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{id(self)}"
        delay = base_delay
        for attempt in range(1, attempts + 1):
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                if attempt < attempts:
                    logger.warning("LongTermMemory save attempt %d/%d failed: %s — retrying", attempt, attempts, exc)
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("LongTermMemory save failed after %d attempts: %s", attempts, exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# ResultsMemory
# ---------------------------------------------------------------------------

class ResultsMemory:
    """Persisted vector index of past task results."""

    def __init__(self, path: str, llm=None):
        self.path = path
        self.llm = llm
        self._data: dict = {}
        self._lock = threading.RLock()
        self._load()

    def add_result(self, goal: str, summary: str, tools_used: list = None) -> str:
        result_id = str(uuid.uuid4())
        content = f"Goal: {goal}\nResult: {summary}"
        vector = []
        if self.llm:
            try:
                vector = self.llm.embed(content)
            except Exception as exc:
                logger.warning("ResultsMemory embed failed: %s", exc)
        with self._lock:
            self._data[result_id] = {
                "goal": goal,
                "summary": summary,
                "tools_used": tools_used or [],
                "timestamp": datetime.utcnow().isoformat(),
                "vector": vector,
            }
            self._save_with_retry()
        return result_id

    def search(self, query: str, top_k: int = 3) -> list:
        with self._lock:
            if not self._data:
                return []
        query_vec = []
        if self.llm:
            try:
                query_vec = self.llm.embed(query)
            except Exception as exc:
                logger.warning("ResultsMemory search embed failed: %s", exc)
        with self._lock:
            if not query_vec:
                entries = sorted(self._data.values(), key=lambda e: e.get("timestamp", ""), reverse=True)
                return entries[:top_k]
            scored = []
            for entry_id, entry in self._data.items():
                vec = entry.get("vector", [])
                if vec:
                    score = _cosine_similarity(query_vec, vec)
                    scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def as_prompt_text(self, query: str = "", top_k: int = 3) -> str:
        entries = self.search(query, top_k) if query else self._latest(top_k)
        if not entries:
            return "No past results."
        lines = []
        for entry in entries:
            ts = entry.get("timestamp", "")[:10]
            lines.append(f"  [{ts}] Goal: {entry['goal']}")
            lines.append(f"    Result: {entry['summary']}")
        return "\n".join(lines)

    def _latest(self, top_k: int) -> list:
        with self._lock:
            return sorted(self._data.values(), key=lambda e: e.get("timestamp", ""), reverse=True)[:top_k]

    def _load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path) as f:
                        self._data = json.load(f)
                except _JSON_FILE_ERRORS as exc:
                    logger.warning("ResultsMemory load failed: %s", exc)
                    self._data = {}

    def _save_with_retry(self, attempts: int = 3, base_delay: float = 0.05) -> None:
        """Write to disk with exponential-backoff retry. Caller must hold _lock."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{id(self)}"
        delay = base_delay
        for attempt in range(1, attempts + 1):
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                if attempt < attempts:
                    logger.warning("ResultsMemory save attempt %d/%d failed: %s — retrying", attempt, attempts, exc)
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("ResultsMemory save failed after %d attempts: %s", attempts, exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
