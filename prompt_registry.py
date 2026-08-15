"""
prompt_registry.py
------------------
Process-singleton registry mapping a globally-unique ULID prompt ID to a
user-initiated agent run.

The registry persists append-only JSONL records to ``data/prompts.jsonl`` so
prompt IDs and their sub-agent associations survive process restarts. It is
observed by the sub-agent supervisor (to record spawned agents against the active
prompt) and queried by Telegram commands / log introspection tools.

Prompt IDs are 26-char ULID strings (Crockford base32, 48-bit millisecond
timestamp + 80-bit random), generated inline with no external dependency. They
are globally unique and stable forever — across restarts, registry resets, and
day boundaries. Legacy integer IDs from a prior version are normalized to
``str`` on replay; callers must always pass ``str`` to ``get()``. Only new
records receive ULIDs.

Thread-safety: all shared state is protected by a single ``threading.Lock``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ULID generator (inline, no external dependency)
# ---------------------------------------------------------------------------

# Crockford base32 alphabet (excludes I, L, O, U to avoid confusion).
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """Generate a 26-char ULID string (Crockford base32).

    Layout: 6-byte (48-bit) millisecond timestamp + 10-byte (80-bit) random
    from ``secrets.token_bytes`` = 16 bytes total, Crockford base32 encoded to
    26 chars. Time-sortable lexicographically; 80 bits of entropy makes
    collision effectively impossible.
    """
    ms_timestamp = int(time.time() * 1000)
    randomness = secrets.token_bytes(10)
    raw = ms_timestamp.to_bytes(6, "big") + randomness  # 16 bytes
    # Crockford base32 encode (5 bits per char, 16 bytes -> 26 chars with 3-bit pad)
    value = int.from_bytes(raw, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


@dataclass
class PromptRecord:
    """In-memory representation of one prompt/run."""

    prompt_id: str
    trace_id: str
    text: str
    started_at: float
    ended_at: Optional[float] = None
    status: str = "running"
    sub_agent_ids: list[str] = field(default_factory=list)


@dataclass
class SearchPage:
    """Page of prompt search results.

    *results* is the sliced page of prompt records (at most *limit* items).
    *total_matched* is the full number of records matching the search filters
    before applying offset and limit.
    """

    results: list[PromptRecord]
    total_matched: int


class PromptRegistry:
    """Globally-unique ULID prompt-ID registry with append-only JSONL persistence."""

    MAX_IN_MEMORY: int = 100
    """Maximum finalized prompt records kept in memory. Running records are never evicted."""

    def __init__(self, data_dir: str = "data") -> None:
        """Create the registry, ensuring the data directory exists and replaying
        any existing ``prompts.jsonl`` file to restore in-memory state.

        On first startup (no ``prompts_archive.jsonl``), finalized records from
        the existing event log are backfilled into the archive snapshot file.
        """
        self._lock = threading.Lock()
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, "prompts.jsonl")
        self._archive_file_path = os.path.join(data_dir, "prompts_archive.jsonl")
        os.makedirs(data_dir, exist_ok=True)
        self._records: dict[str, PromptRecord] = {}
        self._trace_to_id: dict[str, str] = {}
        if os.path.exists(self._file_path):
            self._replay()
        if not os.path.exists(self._archive_file_path):
            self._backfill_archive()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _append_line(self, line: dict) -> None:
        """Append a single JSON line to the log file (thread-safe caller)."""
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _archive_snapshot(self, record: PromptRecord) -> None:
        """Append a self-contained snapshot of *record* to the archive file.

        The caller must already hold ``self._lock``, except during
        single-threaded construction in ``_backfill_archive()``; this method
        performs no locking of its own.
        """
        line = {
            "prompt_id": record.prompt_id,
            "trace_id": record.trace_id,
            "text": record.text,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "status": record.status,
            "sub_agent_ids": record.sub_agent_ids,
        }
        with open(self._archive_file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    @staticmethod
    def _record_from_archive_line(line: dict) -> Optional[PromptRecord]:
        """Reconstruct a ``PromptRecord`` from an archive JSON line.

        Returns ``None`` if the line is malformed (missing or non-string
        ``prompt_id``, non-numeric ``started_at``).
        """
        prompt_id = line.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return None
        try:
            started_at = float(line.get("started_at", 0.0))
        except (TypeError, ValueError):
            return None
        ended_at = line.get("ended_at")
        try:
            ended_at = float(ended_at) if ended_at is not None else None
        except (TypeError, ValueError):
            ended_at = None
        return PromptRecord(
            prompt_id=prompt_id,
            trace_id=line.get("trace_id", ""),
            text=line.get("text", ""),
            started_at=started_at,
            ended_at=ended_at,
            status=line.get("status", "done"),
            sub_agent_ids=list(line.get("sub_agent_ids", [])),
        )

    def _replay(self) -> None:
        """Replay the JSONL log to rebuild in-memory records.

        For each ``prompt_id`` the last line seen wins for mutable fields.
        Legacy integer IDs are normalized to ``str`` at the replay boundary;
        callers must always pass ``str`` to ``get()``. Non-int/non-str values
        (including ``bool``, ``float``, ``list``, ``dict``) are skipped. Only
        new records receive ULIDs (no counter, no ``max_id`` logic).
        """
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
                    if isinstance(prompt_id, bool) or not isinstance(prompt_id, (int, str)):
                        continue
                    prompt_id = str(prompt_id)

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

    def _backfill_archive(self) -> None:
        """Backfill the archive from the event log on first startup.

        Writes one snapshot per finalized record to the archive file. Running
        records are skipped. I/O errors are logged but do not prevent startup
        — a partial or missing archive still enables search for records that
        were written.
        """
        finalized = [r for r in self._records.values() if r.status != "running"]
        try:
            for record in finalized:
                self._archive_snapshot(record)
        except OSError as exc:
            logger.warning("Backfill archive write failed: %s", exc)
            return
        logger.info("Backfilled %d finalized records to archive", len(finalized))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, trace_id: str, text: str) -> PromptRecord:
        """Start tracking a new prompt run.

        Truncates *text* to the first 200 characters. Appends a start record to
        the JSONL log and returns the created ``PromptRecord``.
        """
        with self._lock:
            prompt_id = _generate_ulid()
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
            logger.info("Prompt %s started (trace=%s)", prompt_id, trace_id)
            self._evict_oldest()
            return record

    def finish(self, prompt_id: str, status: str) -> None:
        """Finalize a prompt run.

        *status* should be one of ``"done"``, ``"failed"``, or ``"cancelled"``.
        Appends a finalization record with the current timestamp and the full
        ``sub_agent_ids`` list.
        """
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                logger.warning("finish called for unknown prompt_id %s", prompt_id)
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
            try:
                self._archive_snapshot(record)
            except OSError as exc:
                logger.warning("Archive snapshot write failed for %s: %s", prompt_id, exc)
            logger.info("Prompt %s finished (status=%s)", prompt_id, status)

    def add_sub_agent(self, prompt_id: str, agent_id: str) -> None:
        """Record a spawned sub-agent against the originating prompt."""
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                logger.warning("add_sub_agent called for unknown prompt_id %s", prompt_id)
                return
            if agent_id not in record.sub_agent_ids:
                record.sub_agent_ids.append(agent_id)
            self._append_line({
                "prompt_id": prompt_id,
                "action": "add_sub_agent",
                "agent_id": agent_id,
            })
            logger.info("Prompt %s recorded sub-agent %s", prompt_id, agent_id)

    def get(self, prompt_id: str) -> Optional[PromptRecord]:
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

    def show(self, prompt_id: str) -> Optional[PromptRecord]:
        """Look up a prompt by ID, checking memory first then archive."""
        record = self.get(prompt_id)
        if record is not None:
            return record
        return self.find_in_archive(prompt_id)

    def _evict_oldest(self) -> None:
        """Evict the oldest finalized record when in-memory cap is exceeded.

        Called from ``start()`` when ``len(self._records) > MAX_IN_MEMORY``.
        Only finalized records are evicted; running records are never removed.
        If all records are running, no eviction occurs.
        """
        if len(self._records) <= self.MAX_IN_MEMORY:
            return
        finalized = [r for r in self._records.values() if r.status != "running"]
        if not finalized:
            return
        oldest = min(finalized, key=lambda r: r.started_at)
        del self._records[oldest.prompt_id]
        self._trace_to_id.pop(oldest.trace_id, None)
        logger.debug("Evicted prompt %s from memory", oldest.prompt_id)

    def _parse_iso(self, value: str) -> float:
        """Parse an ISO 8601 string to epoch seconds.

        Naive inputs (no timezone offset) are interpreted as UTC.
        """
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    def _build_time_filter(
        self,
        days: Optional[float],
        since: Optional[str],
        until: Optional[str],
    ) -> Optional[callable]:  # type: ignore[type-arg]
        """Return a predicate for ``started_at`` based on the supplied filters.

        ``since``/``until`` take precedence over ``days``. If no time filter
        applies, returns ``None``.
        """
        if since is not None or until is not None:
            since_epoch: Optional[float] = None
            until_epoch: Optional[float] = None
            if since is not None:
                since_epoch = self._parse_iso(since)
            if until is not None:
                until_epoch = self._parse_iso(until)

            def _absolute_filter(started_at: float) -> bool:
                if since_epoch is not None and started_at < since_epoch:
                    return False
                if until_epoch is not None and started_at > until_epoch:
                    return False
                return True

            return _absolute_filter

        if days is not None:
            cutoff = time.time() - days * 86400
            return lambda started_at: started_at >= cutoff

        return None

    def search(
        self,
        query: str = "",
        days: Optional[float] = None,
        status: Optional[str] = None,
        trace_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchPage:
        """Search in-memory records and the archive for prompts matching *query*.

        Filters narrow results by case-insensitive substring match on prompt
        text, exact ``status``/``trace_id`` matches, and absolute or relative
        time windows. Results are merged, deduplicated, sorted by
        ``started_at`` descending, and paginated.
        """
        query_lower = query.lower() if query else ""
        time_filter = self._build_time_filter(days, since, until)

        def _matches(record: PromptRecord) -> bool:
            if query_lower and query_lower not in record.text.lower():
                return False
            if status is not None and record.status != status:
                return False
            if trace_id is not None and record.trace_id != trace_id:
                return False
            if time_filter is not None and not time_filter(record.started_at):
                return False
            return True

        with self._lock:
            mem_records = list(self._records.values())

        seen_ids: set[str] = set()
        matched: list[PromptRecord] = []

        for record in mem_records:
            if _matches(record):
                matched.append(record)
                seen_ids.add(record.prompt_id)

        if os.path.exists(self._archive_file_path):
            try:
                with open(self._archive_file_path, "r", encoding="utf-8") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            line = json.loads(raw_line)
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed archive line: %s", raw_line[:120])
                            continue
                        record = self._record_from_archive_line(line)
                        if record is None or record.prompt_id in seen_ids:
                            continue
                        if _matches(record):
                            matched.append(record)
            except OSError as exc:
                logger.warning("Could not read archive %s: %s", self._archive_file_path, exc)

        matched.sort(key=lambda r: r.started_at, reverse=True)
        total_matched = len(matched)
        return SearchPage(results=matched[offset : offset + limit], total_matched=total_matched)

    def find_in_archive(self, prompt_id: str) -> Optional[PromptRecord]:
        """Find a prompt record in the archive file by prompt_id.

        Note: if a record is ever archived twice (e.g. double-finish), this
        returns the first (oldest) matching snapshot. The single-finalize
        invariant (one ``finish()`` per prompt) prevents this today.
        """
        if not os.path.exists(self._archive_file_path):
            return None
        try:
            with open(self._archive_file_path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        line = json.loads(raw_line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed archive line: %s", raw_line[:120])
                        continue
                    record = self._record_from_archive_line(line)
                    if record is not None and record.prompt_id == prompt_id:
                        return record
        except OSError as exc:
            logger.warning("Could not read archive %s: %s", self._archive_file_path, exc)
        return None

    def list_recent(self, n: int = 20) -> list[PromptRecord]:
        """Return the most recent *n* prompt records, most recent first.

        Sorted by ``started_at`` descending (not by ``prompt_id`` keys) so mixed
        legacy-int and ULID-string IDs never cause a ``TypeError``.
        """
        with self._lock:
            records = sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)
            return records[:n]
