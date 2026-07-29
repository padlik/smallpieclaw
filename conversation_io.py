"""
conversation_io.py
------------------
Lightweight helpers for conversation-id persistence and conversation JSON
save/load.  Extracted from main.py to break the circular import:
  main imports AgentController, AgentController must not import main.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _xdg_state_home() -> str:
    """Return XDG_STATE_HOME, defaulting to ~/.local/state."""
    return os.environ.get(
        "XDG_STATE_HOME",
        os.path.join(os.path.expanduser("~"), ".local", "state"),
    )


def _load_or_create_conversation_id(state_dir: str, force_new: bool = False) -> str:
    """Read or generate the conversation_id from <state_dir>/conversation_id.

    Generates uuid4().hex[:12] if the file is missing or corrupted, or when
    force_new is True. Writes the file atomically (temp file + os.replace).
    """
    import uuid as _uuid

    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "conversation_id")
    existing = ""
    if not force_new and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                candidate = f.read().strip()
            if len(candidate) == 12 and all(c in "0123456789abcdef" for c in candidate):
                existing = candidate
            elif candidate:
                logger.warning(
                    "conversation_id file at %s has invalid value %r — generating new id",
                    path, candidate,
                )
        except (OSError, ValueError):
            logger.warning(
                "Failed to read conversation_id file at %s — generating new id",
                path, exc_info=True,
            )
    if existing:
        return existing
    new_id = _uuid.uuid4().hex[:12]
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_id)
    os.replace(tmp_path, path)
    return new_id


def _save_conversation(path: str, short_term) -> None:
    """Save ShortTermMemory to a conversation JSON file atomically.

    The file is tightened to 0600 after the atomic replace so chat history
    (which may contain sensitive user data) is owner-only, matching the
    session_logs file permissions.
    """
    from memory_store import _atomic_save_json

    try:
        _atomic_save_json(path, short_term.to_dict(), attempts=3)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        logger.warning("Failed to save conversation to %s", path, exc_info=True)
