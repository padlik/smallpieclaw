"""
memory.py — Lightweight persistent memory backed by memory.json.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)


class Memory:
    def __init__(self):
        self._data: dict = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if config.MEMORY_FILE.exists():
            try:
                self._data = json.loads(
                    config.MEMORY_FILE.read_text(encoding="utf-8")
                )
                logger.debug("Memory loaded: %d keys", len(self._data))
            except Exception as e:
                logger.warning("Could not load memory: %s", e)
                self._data = {}

    def save(self) -> None:
        try:
            config.MEMORY_FILE.write_text(
                json.dumps(self._data, indent=2, default=str), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Could not save memory: %s", e)

    # ── access ─────────────────────────────────────────────────────────────────

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    def update(self, data: dict) -> None:
        self._data.update(data)
        self.save()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self.save()

    def all(self) -> dict:
        return dict(self._data)

    def to_prompt_snippet(self) -> str:
        """Return a compact string suitable for injection into prompts."""
        if not self._data:
            return ""
        lines = ["[Memory]"]
        for k, v in self._data.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)
