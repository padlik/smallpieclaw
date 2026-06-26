"""
strategy_memory.py
------------------
Persist learned task-type-to-approach strategies.

The agent uses ``StrategyMemory`` to remember which approaches work well for
specific kinds of tasks. Strategies are keyed by ``(task_type, approach)`` and
can be decayed over time, archived when confidence falls too low, and formatted
for injection into prompts.

Public helpers:
  - ``classify_task_type`` — heuristic mapping from a user goal to a kebab-case
    task type.
  - ``format_strategies_for_prompt`` — render selected strategies as prompt text.
  - ``extract_strategy`` — ask an LLM to synthesise a strategy from a task
    outcome, intended for fire-and-forget use on a background thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_JSON_FILE_ERRORS = (OSError, json.JSONDecodeError)


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


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


@dataclass
class Strategy:
    """A learned strategy mapping a task type to an approach.

    Attributes:
        task_type: Kebab-case task type (e.g. ``"pdf-to-text"``).
        approach: Human-readable approach description.
        confidence: 0-1 confidence score.
        success_count: Number of recorded successes.
        failure_count: Number of recorded failures.
        last_used: ISO 8601 timestamp of last use.
        created_at: ISO 8601 timestamp of creation.
    """

    task_type: str
    approach: str
    confidence: float
    success_count: int
    failure_count: int
    last_used: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Strategy":
        """Reconstruct a ``Strategy`` from a dictionary."""
        return cls(
            task_type=str(data.get("task_type", "")),
            approach=str(data.get("approach", "")),
            confidence=float(data.get("confidence", 0.0)),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            last_used=str(data.get("last_used", _now_iso())),
            created_at=str(data.get("created_at", _now_iso())),
        )


class StrategyMemory:
    """Persistent store for learned task-type-to-approach strategies.

    Strategies are stored in ``data/strategies.json`` and optionally mirrored to
    graph memory when ``graph_memory`` is provided. All mutations are protected by
    a re-entrant lock so the store can be shared safely between the main agent
    and background threads.
    """

    def __init__(self, data_dir: str = "data", graph_memory=None) -> None:
        """Initialise the strategy memory.

        Args:
            data_dir: Directory used for ``strategies.json`` persistence.
            graph_memory: Optional graph memory store/adapter. If present, strategies
                are also written to it on add/update.
        """
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "strategies.json")
        self.graph_memory = graph_memory
        self._strategies: list[Strategy] = []
        self._lock = threading.RLock()
        self._load()

    def add(self, strategy: Strategy) -> None:
        """Add or update a strategy (upsert by task_type + approach).

        If an existing strategy matches both ``task_type`` and ``approach``, it is
        updated in place. On update, ``success_count`` and ``failure_count`` are
        merged with the incoming values, ``confidence`` is recomputed from the
        merged history, and ``last_used`` is refreshed. ``created_at`` is preserved.
        """
        with self._lock:
            for existing in self._strategies:
                if existing.task_type == strategy.task_type and existing.approach == strategy.approach:
                    existing.success_count += max(0, strategy.success_count)
                    existing.failure_count += max(0, strategy.failure_count)
                    total = existing.success_count + existing.failure_count
                    if total > 0:
                        existing.confidence = existing.success_count / total
                    else:
                        existing.confidence = strategy.confidence
                    existing.confidence = max(0.0, min(1.0, existing.confidence))
                    existing.last_used = strategy.last_used or _now_iso()
                    break
            else:
                self._strategies.append(strategy)

            self._save_with_retry()
            self._sync_to_graph(strategy)

    def get(self, task_type: str) -> list[Strategy]:
        """Return all non-archived strategies for *task_type*, sorted by confidence desc."""
        with self._lock:
            matches = [s for s in self._strategies if s.task_type == task_type and not self._is_archived(s)]
            return sorted(matches, key=lambda s: s.confidence, reverse=True)

    def decay_all(self) -> None:
        """Apply 30-day half-life decay to every strategy.

        The decay formula is ``confidence *= 0.9 ** (days_since_last_used / 30)``.
        ``last_used`` is refreshed to the current time after decay so repeated
        decays are idempotent for a single session. Strategies with a confidence
        below 0 are clamped to 0.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            updated = False
            for strategy in self._strategies:
                try:
                    last_used = datetime.fromisoformat(strategy.last_used)
                    if last_used.tzinfo is None:
                        last_used = last_used.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError) as exc:
                    logger.warning("Strategy %r has invalid last_used %r: %s", strategy.task_type, strategy.last_used, exc)
                    continue
                days_since = (now - last_used).total_seconds() / 86400.0
                if days_since > 0:
                    strategy.confidence *= 0.9 ** (days_since / 30.0)
                    strategy.confidence = max(0.0, min(1.0, strategy.confidence))
                    updated = True
                strategy.last_used = _now_iso()
            if updated:
                self._save_with_retry()

    def archive_low_confidence(self, threshold: float = 0.2) -> None:
        """Mark strategies below *threshold* confidence as archived.

        Archived strategies are hidden from ``get`` and ``get_top_k`` but are
        retained on disk for bookkeeping and future inspection.
        """
        with self._lock:
            archived_any = False
            for strategy in self._strategies:
                if strategy.confidence < threshold and not self._is_archived(strategy):
                    self._set_archived(strategy, True)
                    archived_any = True
            if archived_any:
                self._save_with_retry()

    def get_top_k(self, task_type: str, k: int = 2) -> list[Strategy]:
        """Return up to *k* best strategies for *task_type*, handling near ties.

        The top *k* strategies are selected by confidence. If additional strategies
        have confidence within 0.1 of the k-th selected strategy, they are also
        included so callers can decide which applies. The returned list is sorted by
        confidence descending.
        """
        strategies = self.get(task_type)
        if not strategies:
            return []
        kth_confidence = strategies[min(k, len(strategies)) - 1].confidence
        cutoff = kth_confidence - 0.1
        selected = [s for s in strategies if s.confidence >= cutoff]
        return selected

    def save(self) -> None:
        """Persist current strategies to ``data/strategies.json``."""
        with self._lock:
            self._save_with_retry()

    def load(self) -> None:
        """Load strategies from ``data/strategies.json``."""
        with self._lock:
            self._load()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_archived(self, strategy: Strategy) -> bool:
        """Check whether a strategy carries the archived flag."""
        return getattr(strategy, "_archived", False) is True

    def _set_archived(self, strategy: Strategy, archived: bool) -> None:
        """Set or clear the archived flag on a strategy."""
        object.__setattr__(strategy, "_archived", archived)

    def _sync_to_graph(self, strategy: Strategy) -> None:
        """Best-effort mirror a strategy to graph memory if enabled."""
        if self.graph_memory is None:
            return
        try:
            text = (
                f"Task type: {strategy.task_type}. Approach: {strategy.approach}. "
                f"Confidence: {strategy.confidence:.2f}. "
                f"Successes: {strategy.success_count}, Failures: {strategy.failure_count}."
            )
            if hasattr(self.graph_memory, "store"):
                self.graph_memory.store(text, source="strategy_memory")
            elif hasattr(self.graph_memory, "enqueue"):
                self.graph_memory.enqueue(text, source="strategy_memory")
            elif hasattr(self.graph_memory, "add"):
                self.graph_memory.add(text, source="strategy_memory")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Graph memory strategy sync failed: %s", exc)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._strategies = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except _JSON_FILE_ERRORS as exc:
            logger.warning("Could not load strategies from %s: %s — starting fresh", self.path, exc)
            self._strategies = []
            return

        if isinstance(raw, dict):
            # Archive flag is stored outside the dataclass fields to keep
            # Strategy serialisable with asdict().
            archived = raw.get("archived", {})
            entries = raw.get("strategies", [])
        else:
            archived = {}
            entries = raw if isinstance(raw, list) else []

        strategies: list[Strategy] = []
        for item in entries:
            try:
                strategy = Strategy.from_dict(item)
                key = f"{strategy.task_type}::{strategy.approach}"
                if archived.get(key, False):
                    self._set_archived(strategy, True)
                strategies.append(strategy)
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping malformed strategy entry: %s", exc)
        self._strategies = strategies
        logger.debug("Loaded %d strategy(ies) from %s", len(self._strategies), self.path)

    def _save_with_retry(self, attempts: int = 3, base_delay: float = 0.05) -> None:
        """Write to disk atomically with exponential-backoff retry. Caller must hold _lock."""
        archived: dict[str, bool] = {}
        serialisable: list[dict[str, Any]] = []
        for strategy in self._strategies:
            key = f"{strategy.task_type}::{strategy.approach}"
            if self._is_archived(strategy):
                archived[key] = True
            serialisable.append(strategy.to_dict())
        payload = {"strategies": serialisable, "archived": archived}
        _atomic_save_json(self.path, payload, attempts=attempts, base_delay=base_delay)


# ---------------------------------------------------------------------------
# Task classification heuristic
# ---------------------------------------------------------------------------


def classify_task_type(user_goal: str) -> str:
    """Map a user goal to a kebab-case task type via simple keyword matching.

    Rules (first match wins):
        - Contains ``pdf`` → ``pdf-processing``
        - Contains ``docker`` or ``container`` → ``container-management``
        - Contains ``backup`` → ``backup-task``
        - Contains ``scan`` or ``ocr`` → ``ocr-task``
        - Contains ``convert`` or ``transcode`` → ``media-conversion``
        - Default: ``general-task``

    Args:
        user_goal: The natural-language user goal.

    Returns:
        A kebab-case task type string.
    """
    goal = (user_goal or "").lower()
    if "pdf" in goal:
        return "pdf-processing"
    if "docker" in goal or "container" in goal:
        return "container-management"
    if "backup" in goal:
        return "backup-task"
    if "scan" in goal or "ocr" in goal:
        return "ocr-task"
    if "convert" in goal or "transcode" in goal:
        return "media-conversion"
    return "general-task"


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def format_strategies_for_prompt(strategies: list[Strategy]) -> str:
    """Format strategies as a concise prompt string.

    Each strategy is rendered as::

        For {task_type}, {approach} (confidence: {confidence:.2f})

    If two strategies have confidence within 0.1 of each other, an additional
    note is appended: ``"evaluate which applies"``. When no strategies are
    provided, returns ``"No learned strategies for this task type."``.
    """
    if not strategies:
        return "No learned strategies for this task type."

    lines: list[str] = []
    for strategy in strategies:
        line = f"For {strategy.task_type}, {strategy.approach} (confidence: {strategy.confidence:.2f})"
        lines.append(line)

    if len(strategies) >= 2:
        top_two = sorted(strategies, key=lambda s: s.confidence, reverse=True)[:2]
        if abs(top_two[0].confidence - top_two[1].confidence) <= 0.1:
            lines.append("Note: top strategies have nearly equal confidence; evaluate which applies.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-based strategy extraction
# ---------------------------------------------------------------------------

_STRATEGY_EXTRACTION_PROMPT = """Analyze the following task and execution outcome.

Goal: {goal}
Outcome JSON: {outcome}

Extract a concise strategy in this exact JSON format:
{{
  "task_type": "kebab-case-task-type",
  "approach": "One-sentence description of the approach that worked or failed",
  "success": true,
  "lessons": "Brief lesson learned"
}}

Use "success": true if the outcome indicates the task was completed successfully,
otherwise false. Return only the JSON object, no markdown."""


def _sanitise_json_response(text: str) -> str:
    """Strip markdown fences and leading/trailing whitespace from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_strategy(llm_client, goal: str, outcome: dict) -> "Strategy | None":
    """Ask an LLM to synthesise a ``Strategy`` from a task outcome.

    This function is intended to be called on a background thread so the main
    agent is not blocked while the LLM analyses the outcome. It builds a prompt,
    calls ``llm_client.chat`` with a JSON request, and returns a ``Strategy`` if
    parsing succeeds.

    Args:
        llm_client: An object with a ``chat(messages: list[dict]) -> str`` method.
        goal: The original user goal.
        outcome: A dictionary describing the execution outcome (e.g. ``{"success":
            True, "summary": "...", "tools": [...]}``).

    Returns:
        A populated ``Strategy`` or ``None`` if extraction/parsing fails.
    """
    if not llm_client:
        logger.debug("extract_strategy called without an LLM client")
        return None

    try:
        outcome_json = json.dumps(outcome, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        logger.warning("Cannot serialise outcome for strategy extraction: %s", exc)
        return None

    prompt = _STRATEGY_EXTRACTION_PROMPT.format(goal=goal, outcome=outcome_json)
    try:
        response = llm_client.chat([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM strategy extraction failed: %s", exc)
        return None

    if not isinstance(response, str) or not response.strip():
        logger.debug("LLM strategy extraction returned empty/non-string response")
        return None

    cleaned = _sanitise_json_response(response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse strategy JSON from LLM: %s", exc)
        return None

    if not isinstance(parsed, dict):
        logger.warning("LLM strategy extraction returned non-object JSON: %s", type(parsed).__name__)
        return None

    task_type = str(parsed.get("task_type") or classify_task_type(goal))
    approach = str(parsed.get("approach", "")).strip()
    if not approach:
        logger.debug("LLM strategy extraction produced empty approach; discarding")
        return None

    success = bool(parsed.get("success", False))
    now = _now_iso()
    if success:
        success_count, failure_count = 1, 0
        confidence = 1.0
    else:
        success_count, failure_count = 0, 1
        confidence = 0.0

    return Strategy(
        task_type=task_type,
        approach=approach,
        confidence=confidence,
        success_count=success_count,
        failure_count=failure_count,
        last_used=now,
        created_at=now,
    )
