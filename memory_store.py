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
import os
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from vector_utils import cosine_similarity

logger = logging.getLogger(__name__)

_JSON_FILE_ERRORS = (OSError, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _atomic_save_json(path: str, data: Any, *, attempts: int = 3, base_delay: float = 0.05) -> None:
    """Write *data* as pretty JSON to *path* atomically with retry.

    Uses a unique same-directory temporary file and ``os.replace`` so an
    interrupted write cannot corrupt or truncate the existing file. Exponential
    backoff on ``OSError`` gives local I/O hiccups a chance to clear. Temp files
    are cleaned up on both success and failure where possible.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory,
        f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp",
    )
    delay = base_delay
    last_exc: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("Atomic save %s attempt %d/%d failed: %s — retrying", path, attempt, attempts, exc)
                time.sleep(delay)
                delay *= 2
            else:
                logger.error("Atomic save %s failed after %d attempts: %s", path, attempts, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if last_exc is not None:
        raise last_exc


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
        """Format memory as a short text block suitable for LLM context.

        Internal keys (prefixed with ``_``, e.g. ``_event_log``) are bookkeeping
        state and are deliberately excluded — they add prompt noise/tokens with
        no reasoning value to the model.
        """
        with self._lock:
            lines = []
            for k, v in self._data.items():
                if k.startswith("_"):
                    continue
                lines.append(f"  {k}: {json.dumps(v)}")
            if not lines:
                return "No persistent memory entries."
            return "\n".join(lines)

    def record_event(self, event: str) -> None:
        """Append a timestamped event to the event log."""
        with self._lock:
            log: list = self._data.setdefault("_event_log", [])
            log.append({"time": datetime.now(timezone.utc).isoformat(), "event": event})
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
        """Write to disk atomically with exponential-backoff retry. Caller must hold _lock."""
        _atomic_save_json(self.path, self._data, attempts=attempts, base_delay=base_delay)


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
        self._started_at = datetime.now(timezone.utc).isoformat()

    def add_step(self, action: str, details: dict) -> None:
        self._steps.append({
            "action": action,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
    """Legacy JSON vector index of important facts.

    BACKFILL-ONLY (G): this class is a migration shim. It is not wired into any
    runtime controller/scheduler and should not be written by normal runtime
    code. Runtime semantic recall is served by graph memory (graph_memory.py) and
    ResultsMemory. The class is retained only so existing
    ``data/longterm_memory.json`` files can be migrated into graph memory via
    ``backfill_graph_memory.py``.
    """

    is_migration_only = True

    def __init__(self, path: str, llm=None):
        self.path = path
        self.llm = llm
        self._data: dict = {}
        self._lock = threading.RLock()
        self._load()

    def add(self, content: str, source: str = "manual") -> str:
        """DEPRECATED: LongTermMemory is read-only migration support.

        Write paths remain only so existing tests and the backfill migration
        script can operate until the legacy JSON store is fully removed.
        Runtime code should not call this method.
        """
        logger.warning("LongTermMemory.add() is deprecated; write only for migration/backfill")
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
                    score = cosine_similarity(query_vec, vec)
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
        """Write to disk atomically with exponential-backoff retry. Caller must hold _lock."""
        _atomic_save_json(self.path, self._data, attempts=attempts, base_delay=base_delay)


def _task_outcome_text(
    goal: str,
    summary: str,
    tools_used: list[str] | None = None,
    *,
    max_len: int = 800,
) -> str:
    """Return a bounded, plain-text task outcome for graph/semantic indexing.

    Avoids embedding raw logs, stack traces, or unbounded tool output.
    """
    tools = tools_used or []
    parts = [
        f"Goal: {goal}",
        f"Outcome: {summary}",
    ]
    if tools:
        parts.append(f"Tools used: {', '.join(str(t) for t in tools)}")
    text = "\n".join(parts)
    if len(text) > max_len:
        text = text[:max_len].rsplit("\n", 1)[0] if "\n" in text[:max_len] else text[:max_len]
        text = text.rstrip() + "…"
    return text


def _summarize_result(
    llm,
    goal: str,
    result: str,
    tools_used: list[str] | None = None,
    *,
    max_len: int = 400,
    fallback_len: int = 300,
) -> str:
    """Ask the LLM for a concise task summary; fall back to bounded truncation.

    The prompt asks for 2-3 sentences covering the goal, outcome, key tools, and
    any unresolved issue. If the LLM call fails, returns empty string, or exceeds
    the bound, a deterministic fallback is returned instead.
    """
    tools = tools_used or []
    tools_clause = f" Key tools used: {', '.join(str(t) for t in tools)}." if tools else ""
    prompt = (
        "Summarize the completed task in 2-3 sentences. Include the goal, the "
        "outcome, the key tools used, and any unresolved issue. Avoid logs, stack "
        "traces, raw output, and pleasantries.\n\n"
        f"Goal: {goal}\n"
        f"Result: {result[:2000]}{tools_clause}"
    )
    try:
        summary = llm.chat([{"role": "user", "content": prompt}])
        if not isinstance(summary, str):
            logger.debug("Finish-path summarization returned non-string: %r", type(summary).__name__)
            summary = ""
        else:
            summary = summary.strip()
    except Exception as exc:
        logger.debug("Finish-path summarization failed: %s", exc)
        summary = ""
    if not summary or len(summary) > max_len * 3:
        # Deterministic fallback: head+tail of the raw result plus goal.
        half = fallback_len // 2
        if len(result) <= fallback_len:
            fallback = result
        else:
            head = result[:half].rsplit("\n", 1)[0]
            tail = result[-half:].split("\n", 1)[-1]
            fallback = f"{head}\n…\n{tail}"
        summary = f"Goal: {goal}. Outcome: {fallback}"
        if len(summary) > max_len:
            summary = summary[:max_len].rstrip() + "…"
    return summary


def extract_tools_used(working_steps: list[dict]) -> list[str]:
    """Return non-empty tool names from working-memory steps, in order.

    Filters ``working.steps`` to ``action == "tool"`` entries and extracts the
    ``tool`` field, dropping empties. Centralizes the pattern previously
    duplicated across ``react_loop`` and ``AgentController.reset_task``.
    """
    return [
        s["details"].get("tool", "")
        for s in working_steps
        if s.get("action") == "tool"
    ]


def save_task_outcome(
    *,
    results: "ResultsMemory | None",
    graph_memory_writer: Any,
    goal: str,
    summary: str,
    tools_used: list[str],
) -> None:
    """Persist a finished task's outcome to ResultsMemory and graph memory.

    Writes the bounded *summary* to *results* (if present) and enqueues a
    ``_task_outcome_text`` episode to *graph_memory_writer* (if present).
    Both writes are best-effort: a graph-memory failure is logged and never
    raised, so a finish path cannot be derailed by the optional graph store.
    """
    if results is not None:
        results.add_result(goal=goal, summary=summary, tools_used=tools_used)
    if graph_memory_writer is not None:
        try:
            graph_memory_writer.enqueue(
                _task_outcome_text(goal=goal, summary=summary, tools_used=tools_used),
                source="task_outcome",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Graph memory task outcome enqueue failed: %s", exc)


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

    def add_result(self, goal: str, summary: str, tools_used: list | None = None) -> str:
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
                    score = cosine_similarity(query_vec, vec)
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
        """Write to disk atomically with exponential-backoff retry. Caller must hold _lock."""
        _atomic_save_json(self.path, self._data, attempts=attempts, base_delay=base_delay)
