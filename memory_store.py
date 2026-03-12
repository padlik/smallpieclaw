"""
memory_store.py
---------------
Lightweight JSON-backed key-value memory store.
Used by the agent to persist facts between sessions
(e.g. known services, last backup time, user preferences).
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class MemoryStore:
    """
    Simple persistent memory backed by a JSON file.
    Thread-safe for single-process use (file rewritten on every mutation).
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._save()

    def all(self) -> dict[str, Any]:
        """Return a copy of all stored key-value pairs."""
        return dict(self._data)

    def update(self, updates: dict[str, Any]) -> None:
        """Batch-update multiple keys."""
        self._data.update(updates)
        self._save()

    def as_prompt_text(self) -> str:
        """Format memory as a short text block suitable for LLM context."""
        if not self._data:
            return "No persistent memory entries."
        lines = []
        for k, v in self._data.items():
            lines.append(f"  {k}: {json.dumps(v)}")
        return "\n".join(lines)

    def record_event(self, event: str) -> None:
        """Append a timestamped event to the event log."""
        log: list = self._data.setdefault("_event_log", [])
        log.append({"time": datetime.utcnow().isoformat(), "event": event})
        # Keep only last 50 events to avoid unbounded growth
        self._data["_event_log"] = log[-50:]
        self._save()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
                logger.debug("Memory loaded from %s (%d keys)", self.path, len(self._data))
            except Exception as exc:
                logger.warning("Could not load memory from %s: %s — starting fresh", self.path, exc)
                self._data = {}
        else:
            # Seed with useful defaults
            self._data = {
                "known_services": [],
                "last_health_check": None,
                "notes": [],
            }
            self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.error("Could not save memory: %s", exc)
