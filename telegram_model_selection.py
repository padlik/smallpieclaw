"""telegram_model_selection.py
------------------------------
Opaque, bounded, in-memory selection tokens for the `/models` catalog
(task 8.2).

Scope boundary (do not expand): this module issues tokens, records what
they stand for, and resolves them back to a canonical id while enforcing
TTL expiry and user/chat binding. It never touches the registry or disk.

Owned by `TelegramInterface`, never by `ModelRegistry`: the registry's
`select()` already revalidates inventory and persists before publishing: a
selection token is purely a presentation-layer convenience for mapping an
opaque callback back to the canonical id that produced it.
"""

from __future__ import annotations

import secrets
import string
import threading
import time
from dataclasses import dataclass
from typing import Callable

#: Callback-data prefix for opaque selection tokens. Must stay well under
#: Telegram's 64-byte callback_data limit together with the token itself.
CALLBACK_PREFIX = "modelsel:"

#: Default time-to-live for an issued token, in seconds; a token whose age
#: is at or beyond this bound resolves as unknown.
DEFAULT_TTL_SECONDS = 300.0

#: Default bound on the number of outstanding issuances kept in memory.
DEFAULT_MAX_ENTRIES = 128

#: Telegram's hard limit on callback_data length, in bytes.
_MAX_CALLBACK_BYTES = 64

#: The alphabet `secrets.token_urlsafe` draws from.
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


@dataclass(frozen=True)
class SelectionIssuance:
    """What one opaque token stands for, recorded at mint time."""

    canonical_id: str
    user_id: int
    chat_id: int
    issued_at: float


class SelectionTokenCache:
    """Bounded, in-memory opaque-token store for `/models` selection controls.

    Not durable across restarts by design: an expired/evicted token simply
    resolves as unknown, and the user re-issues
    fresh ones by sending `/models` again.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._issuances: dict[str, SelectionIssuance] = {}
        self._order: list[str] = []

    def issue(self, canonical_id: str, *, user_id: int, chat_id: int) -> str:
        """Mint one opaque `modelsel:<token>` callback-data string for
        `canonical_id`, scoped to the issuing user and chat.

        Evicts the oldest outstanding issuance once `max_entries` is
        exceeded; this is a structural memory bound, not TTL enforcement.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._issuances[token] = SelectionIssuance(canonical_id, user_id, chat_id, self._clock())
            self._order.append(token)
            while len(self._order) > self._max_entries:
                oldest = self._order.pop(0)
                self._issuances.pop(oldest, None)
        return f"{CALLBACK_PREFIX}{token}"

    def resolve(self, callback_data: object, *, user_id: int, chat_id: int) -> str | None:
        """Return the canonical id behind `callback_data`, or ``None``.

        ``None`` covers every rejection alike — malformed, unknown, evicted,
        expired (age ``>= ttl``), or issued to a different user or chat — so
        callers cannot distinguish them. A valid token is not consumed, and a
        mismatched attempt never invalidates the legitimate holder's token.
        Pure in-memory lookup: no registry or filesystem access.
        """
        if not isinstance(callback_data, str) or len(callback_data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
            return None
        if not callback_data.startswith(CALLBACK_PREFIX):
            return None
        token = callback_data[len(CALLBACK_PREFIX):]
        if not token or not _TOKEN_CHARS.issuperset(token):
            return None
        with self._lock:
            issuance = self._issuances.get(token)
            if issuance is None:
                return None
            if self._clock() - issuance.issued_at >= self._ttl_seconds:
                return None
        if issuance.user_id != user_id or issuance.chat_id != chat_id:
            return None
        return issuance.canonical_id
