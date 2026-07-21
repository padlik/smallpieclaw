"""
prompt_registry.py
------------------
Process-singleton registry mapping a monotonic "Prompt #N" to a user-initiated
agent run.

The registry persists append-only JSONL records to ``data/prompts.jsonl`` so
prompt IDs and their sub-agent associations survive process restarts. It is
observed by the sub-agent supervisor (to record spawned agents against the active
prompt) and queried by Telegram commands / log introspection tools.

Thread-safety: all shared state is protected by a single ``threading.Lock``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PromptRecord:
    """In-memory representation of one prompt/run."""

    prompt_id: int
    trace_id: str
    text: str
    started_at: float
    ended_at: Optional[float] = None
    status: str = "running"
    sub_agent_ids: list[str] = field(default_factory=list)


class PromptRegistry:
    """Monotonic prompt-ID registry with append-only JSONL persistence."""

    def __init__(self, data_dir: str = "data") -> None:
        """Create the registry, ensuring the data directory exists and replaying
        any existing ``prompts.jsonl`` file to restore in-memory state."""
        self._lock = threading.Lock()
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, "prompts.jsonl")
        os.makedirs(data_dir, exist_ok=True)
        self._records: dict[int, PromptRecord] = {}
        self._next_id = 1
        self._trace_to_id: dict[str, int] = {}
        if os.path.exists(self._file_path):
            self._replay()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _append_line(self, line: dict) -> None:
        """Append a single JSON line to the log file (thread-safe caller)."""
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _replay(self) -> None:
        """Replay the JSONL log to rebuild in-memory records.

        For each ``prompt_id`` the last line seen wins for mutable fields.
        The next assigned ID will be ``max(prompt_id) + 1``."""
        max_id = 0
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        line = json.loads(raw_line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed prompts.jsonl line: %s", raw_line[:120])
                        continue
                    prompt_id = line.get("prompt_id")
                    if prompt_id is None or not isinstance(prompt_id, int):
                        continue
                    if prompt_id > max_id:
                        max_id = prompt_id

                    # Start record
                    if "action" not in line:
                        record = PromptRecord(
                            prompt_id=prompt_id,
                            trace_id=line.get("trace_id", ""),
                            text=line.get("text", ""),
                            started_at=line.get("started_at", 0.0),
                            ended_at=line.get("ended_at"),
                            status=line.get("status", "running"),
                            sub_agent_ids=list(line.get("sub_agent_ids", [])),
                        )
                        self._records[prompt_id] = record
                        self._trace_to_id[record.trace_id] = prompt_id
                        continue

                    # Update record
                    record = self._records.get(prompt_id)
                    if record is None:
                        continue
                    action = line.get("action")
                    if action == "add_sub_agent":
                        agent_id = line.get("agent_id")
                        if agent_id and agent_id not in record.sub_agent_ids:
                            record.sub_agent_ids.append(agent_id)
                    elif action == "finish":
                        record.ended_at = line.get("ended_at")
                        record.status = line.get("status", "done")
                        record.sub_agent_ids = list(line.get("sub_agent_ids", record.sub_agent_ids))
        except OSError as exc:
            logger.warning("Could not replay %s: %s", self._file_path, exc)
        self._next_id = max_id + 1 if max_id else 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, trace_id: str, text: str) -> PromptRecord:
        """Start tracking a new prompt run.

        Truncates *text* to the first 200 characters. Appends a start record to
        the JSONL log and returns the created ``PromptRecord``.
        """
        with self._lock:
            prompt_id = self._next_id
            self._next_id += 1
            started_at = time.time()
            truncated = text[:200]
            record = PromptRecord(
                prompt_id=prompt_id,
                trace_id=trace_id,
                text=truncated,
                started_at=started_at,
            )
            self._records[prompt_id] = record
            self._trace_to_id[trace_id] = prompt_id
            self._append_line({
                "prompt_id": prompt_id,
                "trace_id": trace_id,
                "text": truncated,
                "started_at": started_at,
                "status": "running",
                "sub_agent_ids": [],
            })
            logger.info("Prompt %d started (trace=%s)", prompt_id, trace_id)
            return record

    def finish(self, prompt_id: int, status: str) -> None:
        """Finalize a prompt run.

        *status* should be one of ``"done"``, ``"failed"``, or ``"cancelled"``.
        Appends a finalization record with the current timestamp and the full
        ``sub_agent_ids`` list.
        """
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                logger.warning("finish called for unknown prompt_id %d", prompt_id)
                return
            ended_at = time.time()
            record.ended_at = ended_at
            record.status = status
            self._append_line({
                "prompt_id": prompt_id,
                "action": "finish",
                "ended_at": ended_at,
                "status": status,
                "sub_agent_ids": list(record.sub_agent_ids),
            })
            logger.info("Prompt %d finished (status=%s)", prompt_id, status)

    def add_sub_agent(self, prompt_id: int, agent_id: str) -> None:
        """Record a spawned sub-agent against the originating prompt."""
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                logger.warning("add_sub_agent called for unknown prompt_id %d", prompt_id)
                return
            if agent_id not in record.sub_agent_ids:
                record.sub_agent_ids.append(agent_id)
            self._append_line({
                "prompt_id": prompt_id,
                "action": "add_sub_agent",
                "agent_id": agent_id,
            })
            logger.info("Prompt %d recorded sub-agent %s", prompt_id, agent_id)

    def get(self, prompt_id: int) -> Optional[PromptRecord]:
        """Return the prompt record by ID, or ``None`` if unknown."""
        with self._lock:
            return self._records.get(prompt_id)

    def by_trace(self, trace_id: str) -> Optional[PromptRecord]:
        """Return the prompt record associated with *trace_id*, or ``None``."""
        with self._lock:
            prompt_id = self._trace_to_id.get(trace_id)
            if prompt_id is None:
                return None
            return self._records.get(prompt_id)

    def list_recent(self, n: int = 20) -> list[PromptRecord]:
        """Return the most recent *n* prompt records, most recent first."""
        with self._lock:
            ids = sorted(self._records.keys(), reverse=True)
            return [self._records[i] for i in ids[:n]]
