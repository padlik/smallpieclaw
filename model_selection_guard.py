"""Conversation selection guard for manual model switches.

After a manual switch to a smaller model, the next first request may no longer
fit. The guard records only sanitized budget identities (never clients,
credentials or run models), decides when protection is armed, and clears it
only when a request admitted on the very model that armed it is usable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal

from model_registry import ModelMetadata

GUARD_REJECTION_MESSAGE = (
    "❌ This conversation no longer fits the model you switched to, so the request "
    "was not sent and your conversation history is preserved. Select a larger "
    "model with /models, or clear the conversation with /reset."
)

GUARD_PREPARATION_FAILED_MESSAGE = (
    "❌ The request could not be prepared for the selected model, so it was not "
    "sent and your conversation history is preserved. Check the selection with "
    "/models, or clear the conversation with /reset."
)

Comparison = Literal["smaller", "larger", "equal", "unknown"]


@dataclass(frozen=True)
class SelectionBudgetIdentity:
    """Immutable, secret-free budget identity of one admitted selection."""

    model_id: str
    selection_revision: int
    adapter: str
    input_ceiling: int | None
    assumed: bool = False
    metadata: ModelMetadata = field(default_factory=ModelMetadata)
    local_context_tokens: int = 0


@dataclass(frozen=True)
class GuardTicket:
    """Admission-time capture that gates clearing on the exact same state."""

    guarded: bool
    model_id: str
    selection_revision: int
    generation: int
    token: int


def compare_identities(
    previous: SelectionBudgetIdentity | None, current: SelectionBudgetIdentity
) -> Comparison:
    """Compare input ceilings; anything unproven is ``unknown``."""
    if (
        previous is None
        or previous.input_ceiling is None
        or current.input_ceiling is None
    ):
        return "unknown"
    if current.input_ceiling < previous.input_ceiling:
        return "smaller"
    if current.input_ceiling == previous.input_ceiling:
        return "equal"
    # A larger ceiling derived from an assumption cannot prove capacity.
    if previous.assumed or current.assumed:
        return "unknown"
    return "larger"


class ConversationSelectionGuard:
    """Thread-safe guard state; the lock is never held across I/O."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: SelectionBudgetIdentity | None = None
        self._evaluated_revision = -1
        self._armed = False
        self._generation = 0
        self._token = 0

    @property
    def armed(self) -> bool:
        """Return whether the next first request must pass the staged fit check."""
        with self._lock:
            return self._armed

    def notify_manual_selection(self, identity: SelectionBudgetIdentity) -> None:
        """Record a persisted manual selection; stale or replayed revisions are ignored."""
        with self._lock:
            if identity.selection_revision <= self._evaluated_revision:
                return
            if (
                self._current is not None
                and identity.selection_revision < self._current.selection_revision
            ):
                # Older than the actually bound admission (e.g. unknown-history first admit).
                return
            outcome = compare_identities(self._current, identity)
            if outcome in ("smaller", "unknown"):
                self._armed = True
            elif outcome == "larger":
                self._armed = False
            self._current = identity
            self._evaluated_revision = identity.selection_revision
            self._token += 1

    def notify_history_restored(
        self, selection: SelectionBudgetIdentity | None = None, *, empty: bool = False
    ) -> None:
        """Record a history replacement; every replacement invalidates old tickets.

        An empty replacement clears protection. Otherwise arm conservatively unless
        the restored origin provably fits the current model.
        """
        with self._lock:
            self._generation += 1
            self._token += 1
            if empty:
                self._armed = False
                return
            if selection is None or self._current is None:
                arm = True
            else:
                arm = compare_identities(selection, self._current) in ("smaller", "unknown")
            if arm:
                self._armed = True

    def reset(self) -> None:
        """Clear the guard and invalidate every outstanding ticket."""
        with self._lock:
            self._armed = False
            self._current = None
            self._generation += 1
            self._token += 1

    def begin(self, admitted: SelectionBudgetIdentity) -> GuardTicket:
        """Capture the guard decision for a run admitted on ``admitted``."""
        with self._lock:
            current = self._current
            if self._armed and current is None:
                # Unknown-provenance history: bind protection to the first actual admission.
                self._current = current = admitted
            guarded = self._armed
            if not guarded and current is not None and (
                current.model_id != admitted.model_id
                or current.selection_revision != admitted.selection_revision
            ):
                # An explicit admission of another model never borrows the current exemption.
                guarded = compare_identities(current, admitted) in ("smaller", "unknown")
            return GuardTicket(
                guarded=guarded,
                model_id=admitted.model_id,
                selection_revision=admitted.selection_revision,
                generation=self._generation,
                token=self._token,
            )

    def complete(self, ticket: GuardTicket) -> bool:
        """Clear protection after a usable response; return whether it cleared."""
        with self._lock:
            current = self._current
            if (
                not ticket.guarded
                or not self._armed
                or current is None
                or ticket.generation != self._generation
                or ticket.token != self._token
                or ticket.model_id != current.model_id
                or ticket.selection_revision != current.selection_revision
            ):
                return False
            self._armed = False
            self._token += 1
            return True
