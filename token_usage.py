"""
token_usage.py — Thread-safe per-model token usage registry.

All LLMClient instances (main agent + sub-agents) record usage here.
/status reads the aggregated per-model totals for today.
"""

from __future__ import annotations

import threading
from datetime import date


class TokenUsageRegistry:
    """Aggregates token usage across all LLMClient instances, keyed by model name."""

    def __init__(self):
        self._lock = threading.Lock()
        # model_name → {"date": "YYYY-MM-DD", "prompt": int, "completion": int}
        self._usage: dict[str, dict] = {}

    def record(self, model: str, prompt: int, completion: int) -> None:
        today = date.today().isoformat()
        with self._lock:
            if model not in self._usage or self._usage[model]["date"] != today:
                self._usage[model] = {"date": today, "prompt": 0, "completion": 0}
            self._usage[model]["prompt"] += prompt
            self._usage[model]["completion"] += completion

    def get_today(self) -> dict[str, dict]:
        """Return {model: {prompt, completion, total}} for models used today."""
        today = date.today().isoformat()
        with self._lock:
            result = {}
            for model, v in self._usage.items():
                if v["date"] == today:
                    result[model] = {
                        "prompt": v["prompt"],
                        "completion": v["completion"],
                        "total": v["prompt"] + v["completion"],
                    }
            return result

    def get_today_totals(self) -> dict:
        """Return summed totals across all models for today."""
        today_data = self.get_today()
        prompt = sum(v["prompt"] for v in today_data.values())
        completion = sum(v["completion"] for v in today_data.values())
        return {"prompt": prompt, "completion": completion, "total": prompt + completion}


# Module-level singleton shared across all LLMClient instances.
_registry = TokenUsageRegistry()


def get_registry() -> TokenUsageRegistry:
    return _registry
