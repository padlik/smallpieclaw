"""Sub-agent conversation-context persistence (job_contexts/<key>.json).

Leaf module: the ``memory_store`` import is kept function-local (as in the
original ``builtin_executor``) to avoid an import cycle. No imports back into
``builtin_executor`` or any handler module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

_CONTEXT_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

# Scheduled-origin preserved context lives under job_contexts/scheduled/. Keys
# in that namespace contain "/", which ``_validate_context_key`` rejects, so a
# model-chosen on-demand ``context_key`` can never name (read or overwrite) one.
# Legacy policy: pre-isolation scheduled context was stored flat as
# job_contexts/<normalized-tag>.json. It is deliberately never imported: flat
# names are ambiguous (on-demand children and colliding tags shared them), so
# scheduled jobs start fresh here and the flat files stay untouched for
# on-demand readers.
SCHEDULED_CONTEXT_PREFIX = "scheduled/"
_SCHEDULED_STEM_RE = re.compile(r"[a-z0-9_]{0,64}-[0-9a-f]{16}\Z")


def scheduled_context_key(tag: str) -> str:
    """Return the origin-scoped persistence key for scheduled job *tag*.

    The digest is taken over the raw tag, so distinct tags that normalise to the
    same readable stem still get distinct storage.
    """
    stem = re.sub(r"[^a-z0-9_]+", "_", tag.strip().lower()).strip("_")[:64].rstrip("_") or "job"
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:16]
    return f"{SCHEDULED_CONTEXT_PREFIX}{stem}-{digest}"


def _validate_context_key(context_key: str) -> str:
    """Validate a sub-agent context key before using it as a file stem."""
    if not isinstance(context_key, str) or not _CONTEXT_KEY_RE.fullmatch(context_key):
        raise ValueError(
            "context_key must be 1-128 chars: letters, digits, underscore, dash, or dot; "
            "it must start with a letter or digit"
        )
    if context_key in {".", ".."}:
        raise ValueError("context_key cannot be '.' or '..'")
    return context_key


def _context_path(context_key: str, data_dir: str) -> str:
    """Return the absolute context path, rejecting path traversal."""
    base_dir = os.path.abspath(os.path.join(data_dir, "job_contexts"))
    if isinstance(context_key, str) and context_key.startswith(SCHEDULED_CONTEXT_PREFIX):
        safe_key = context_key[len(SCHEDULED_CONTEXT_PREFIX):]
        if not _SCHEDULED_STEM_RE.fullmatch(safe_key):
            raise ValueError("malformed scheduled context key")
        ctx_dir = os.path.join(base_dir, "scheduled")
    else:
        safe_key = _validate_context_key(context_key)
        ctx_dir = base_dir
    path = os.path.abspath(os.path.join(ctx_dir, f"{safe_key}.json"))
    if os.path.commonpath([ctx_dir, path]) != ctx_dir:
        raise ValueError("context_key resolves outside job_contexts")
    return path


def _save_context(context_key: str, short_term, data_dir: str) -> None:
    """Persist ShortTermMemory to data/job_contexts/<key>.json atomically.

    Delegates to ``memory_store._atomic_save_json`` (temp file + ``os.replace``)
    so an interrupted write cannot corrupt or truncate an existing context
    file. Unlike the memory-store callers, a context-save failure is logged
    and swallowed — a sub-agent finish path must not be derailed by a context
    persistence error.
    """
    from memory_store import _atomic_save_json

    path = _context_path(context_key, data_dir)
    try:
        _atomic_save_json(path, short_term.to_dict(), attempts=1)
    except OSError:
        logger.warning("Failed to save context for %s", context_key, exc_info=True)


def _load_context(context_key: str, data_dir: str, max_turns: int = 50):
    """Load ShortTermMemory from data/job_contexts/<key>.json. Returns fresh on error."""
    import json as _json
    from memory_store import ShortTermMemory

    path = _context_path(context_key, data_dir)
    if not os.path.exists(path):
        return ShortTermMemory(max_turns=max_turns)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return ShortTermMemory.from_dict(data, max_turns=max_turns)
    except (OSError, _json.JSONDecodeError):
        logger.warning("Context file corrupted for %s — starting fresh", context_key, exc_info=True)
        return ShortTermMemory(max_turns=max_turns)
