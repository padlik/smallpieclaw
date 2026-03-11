"""
security.py — User authentication via allow-list or PIN pairing.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)


class Security:
    def __init__(self):
        self._paired: set[int] = set()
        self._load_paired()

    # ── persistence ────────────────────────────────────────────────────────────

    def _load_paired(self) -> None:
        if config.PAIRED_USERS_FILE.exists():
            try:
                data = json.loads(config.PAIRED_USERS_FILE.read_text(encoding="utf-8"))
                self._paired = set(data.get("users", []))
                logger.info("Paired users loaded: %s", self._paired)
            except Exception as e:
                logger.warning("Could not load paired users: %s", e)

    def _save_paired(self) -> None:
        try:
            config.PAIRED_USERS_FILE.write_text(
                json.dumps({"users": list(self._paired)}, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Could not save paired users: %s", e)

    # ── public API ─────────────────────────────────────────────────────────────

    def is_allowed(self, user_id: int) -> bool:
        """Return True if this user may interact with the bot."""
        # Hard allow-list takes priority
        if config.ALLOWED_USER_IDS:
            return user_id in config.ALLOWED_USER_IDS
        # Pairing mode
        return user_id in self._paired

    def try_pair(self, user_id: int, pin: str) -> bool:
        """Attempt to pair a user with the given PIN. Returns True on success."""
        if config.ALLOWED_USER_IDS:
            # Allow-list mode: pairing is not applicable
            return False
        if pin.strip() == config.PAIRING_PIN:
            self._paired.add(user_id)
            self._save_paired()
            logger.info("User %d paired successfully.", user_id)
            return True
        logger.warning("User %d supplied wrong pairing PIN.", user_id)
        return False

    def unpair(self, user_id: int) -> bool:
        if user_id in self._paired:
            self._paired.discard(user_id)
            self._save_paired()
            return True
        return False

    @property
    def using_allowlist(self) -> bool:
        return bool(config.ALLOWED_USER_IDS)
