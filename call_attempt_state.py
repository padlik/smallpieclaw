"""Bounded outbound-attempt accounting for one logical model request."""

from __future__ import annotations

import math
from typing import Any, Literal

RequestMode = Literal["native", "json"]

_SERIALIZED_FIELDS = frozenset(
    {
        "max_retries",
        "retry_delay",
        "mode",
        "outbound_attempts",
        "automatic_retries_used",
        "manual_retries_used",
        "manual_retry_authorized",
        "partial_output",
        "effect",
    }
)


class CallAttemptStateError(ValueError):
    """A safe, categorized invalid or exhausted attempt-state operation."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class CallAttemptState:
    """Track automatic and explicit retries for one logical request.

    The class records attempts only; it does not perform provider calls, classify
    exceptions, sleep, or create states for subsequent accepted actions.
    """

    __slots__ = (
        "_max_retries",
        "_retry_delay",
        "_mode",
        "_outbound_attempts",
        "_automatic_retries_used",
        "_manual_retries_used",
        "_manual_retry_authorized",
        "_partial_output",
        "_effect",
    )

    def __init__(
        self,
        max_retries: int = 4,
        retry_delay: float = 2.0,
        mode: RequestMode = "native",
    ) -> None:
        """Create a fresh request budget, validating caller-supplied settings."""
        self._validate_configuration(max_retries, retry_delay, mode)
        self._max_retries = max_retries
        self._retry_delay = float(retry_delay)
        self._mode = mode
        self._outbound_attempts = 0
        self._automatic_retries_used = 0
        self._manual_retries_used = 0
        self._manual_retry_authorized = False
        self._partial_output = False
        self._effect = False

    @staticmethod
    def _validate_configuration(
        max_retries: Any, retry_delay: Any, mode: Any
    ) -> None:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise CallAttemptStateError(
                "invalid_state", "max_retries must be a nonnegative integer"
            )
        if (
            isinstance(retry_delay, bool)
            or not isinstance(retry_delay, (int, float))
            or retry_delay < 0
        ):
            raise CallAttemptStateError(
                "invalid_state", "retry_delay must be a finite nonnegative number"
            )
        try:
            finite_delay = math.isfinite(float(retry_delay))
        except (OverflowError, ValueError):
            finite_delay = False
        if not finite_delay:
            raise CallAttemptStateError(
                "invalid_state", "retry_delay must be a finite nonnegative number"
            )
        if mode not in ("native", "json"):
            raise CallAttemptStateError("invalid_state", "mode must be native or json")

    @staticmethod
    def _validate_mode(mode: Any) -> RequestMode:
        if mode not in ("native", "json"):
            raise CallAttemptStateError("invalid_state", "mode must be native or json")
        return mode

    @property
    def outbound_attempts(self) -> int:
        """Return the number of actual outbound calls started."""
        return self._outbound_attempts

    @property
    def automatic_retries_used(self) -> int:
        """Return automatic retries started after the first outbound call."""
        return self._automatic_retries_used

    @property
    def manual_retries_used(self) -> int:
        """Return explicit operator-directed continuation calls started."""
        return self._manual_retries_used

    @property
    def remaining_retries(self) -> int:
        """Return unused automatic retry allowance."""
        return self._max_retries - self._automatic_retries_used

    @property
    def mode(self) -> RequestMode:
        """Return the most recently selected request form."""
        return self._mode

    @property
    def partial_output(self) -> bool:
        """Whether the current outbound call emitted partial output."""
        return self._partial_output

    @property
    def effect(self) -> bool:
        """Whether the current outbound call caused an effect."""
        return self._effect

    @property
    def manual_retry_authorized(self) -> bool:
        """Whether one explicit continuation call is currently authorized."""
        return self._manual_retry_authorized

    def begin_outbound_attempt(
        self, mode: RequestMode | None = None, *, manual: bool = False
    ) -> None:
        """Record one outbound call, refusing any unbudgeted continuation."""
        if not isinstance(manual, bool):
            raise CallAttemptStateError("invalid_state", "manual must be a boolean")
        selected_mode = self._mode if mode is None else self._validate_mode(mode)

        if self._outbound_attempts == 0:
            if manual:
                raise CallAttemptStateError(
                    "manual_retry_not_authorized",
                    "a manual retry cannot replace the initial outbound attempt",
                )
        elif manual:
            if not self._manual_retry_authorized:
                raise CallAttemptStateError(
                    "manual_retry_not_authorized",
                    "manual continuation has not been authorized",
                )
            self._manual_retry_authorized = False
            self._manual_retries_used += 1
        else:
            if not self.can_retry_automatically():
                reason = (
                    "partial output" if self._partial_output else
                    "effect" if self._effect else "retry budget exhausted"
                )
                raise CallAttemptStateError("retry_exhausted", reason)
            self._automatic_retries_used += 1

        self._mode = selected_mode
        self._outbound_attempts += 1
        self._partial_output = False
        self._effect = False

    def can_retry_automatically(self) -> bool:
        """Return whether another automatic call is both safe and budgeted."""
        return (
            self._outbound_attempts > 0
            and self._automatic_retries_used < self._max_retries
            and not self._partial_output
            and not self._effect
        )

    def select_mode(self, mode: RequestMode) -> None:
        """Select a local request representation without consuming an attempt."""
        self._mode = self._validate_mode(mode)

    def next_retry_delay(self) -> float:
        """Return the next exponential delay, or a safe exhaustion error."""
        if self._partial_output or self._effect:
            reason = "partial output" if self._partial_output else "effect"
            raise CallAttemptStateError("retry_exhausted", f"automatic retry blocked by {reason}")
        if not self.can_retry_automatically():
            raise CallAttemptStateError("retry_exhausted", "automatic retry budget exhausted")
        return self._retry_delay * (2 ** self._automatic_retries_used)

    def authorize_manual_retry(self) -> None:
        """Authorize one explicit operator continuation, without budget refill."""
        if self._outbound_attempts == 0:
            raise CallAttemptStateError(
                "manual_retry_not_authorized", "cannot authorize a retry before an outbound attempt"
            )
        if self._manual_retry_authorized:
            raise CallAttemptStateError(
                "manual_retry_already_authorized", "manual continuation is already authorized"
            )
        self._manual_retry_authorized = True

    def mark_partial_output(self) -> None:
        """Mark partial output on the currently active outbound call."""
        if self._outbound_attempts == 0:
            raise CallAttemptStateError("invalid_state", "no outbound attempt has started")
        self._partial_output = True

    def mark_effect(self) -> None:
        """Mark an effect caused by the currently active outbound call."""
        if self._outbound_attempts == 0:
            raise CallAttemptStateError("invalid_state", "no outbound attempt has started")
        self._effect = True

    def to_dict(self) -> dict[str, Any]:
        """Return only the validated primitive state needed for a checkpoint."""
        return {
            "max_retries": self._max_retries,
            "retry_delay": self._retry_delay,
            "mode": self._mode,
            "outbound_attempts": self._outbound_attempts,
            "automatic_retries_used": self._automatic_retries_used,
            "manual_retries_used": self._manual_retries_used,
            "manual_retry_authorized": self._manual_retry_authorized,
            "partial_output": self._partial_output,
            "effect": self._effect,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallAttemptState:
        """Restore a complete, internally consistent checkpoint state."""
        if not isinstance(data, dict) or set(data) != _SERIALIZED_FIELDS:
            raise CallAttemptStateError(
                "invalid_checkpoint",
                "attempt checkpoint has missing or unknown fields",
            )
        try:
            cls._validate_configuration(data["max_retries"], data["retry_delay"], data["mode"])
            for field_name in ("outbound_attempts", "automatic_retries_used", "manual_retries_used"):
                value = data[field_name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise CallAttemptStateError(
                        "invalid_checkpoint", f"{field_name} must be a nonnegative integer"
                    )
            for field_name in ("manual_retry_authorized", "partial_output", "effect"):
                if not isinstance(data[field_name], bool):
                    raise CallAttemptStateError(
                        "invalid_checkpoint", f"{field_name} must be a boolean"
                    )
            outbound = data["outbound_attempts"]
            automatic = data["automatic_retries_used"]
            manual = data["manual_retries_used"]
            if automatic > data["max_retries"]:
                raise CallAttemptStateError("invalid_checkpoint", "retry counter exceeds its budget")
            if outbound == 0 and (automatic != 0 or manual != 0):
                raise CallAttemptStateError(
                    "invalid_checkpoint", "retry counters require an initial outbound attempt"
                )
            if outbound > 0 and outbound != 1 + automatic + manual:
                raise CallAttemptStateError("invalid_checkpoint", "attempt counters are inconsistent")
            if data["manual_retry_authorized"] and outbound == 0:
                raise CallAttemptStateError("invalid_checkpoint", "manual authorization is inconsistent")
            if outbound == 0 and (data["partial_output"] or data["effect"]):
                raise CallAttemptStateError("invalid_checkpoint", "current-call flags are inconsistent")
            state = cls(
                max_retries=data["max_retries"],
                retry_delay=data["retry_delay"],
                mode=data["mode"],
            )
        except CallAttemptStateError as exc:
            if exc.category == "invalid_checkpoint":
                raise
            raise CallAttemptStateError("invalid_checkpoint", "attempt checkpoint is invalid") from None
        except (TypeError, ValueError, OverflowError):
            raise CallAttemptStateError("invalid_checkpoint", "attempt checkpoint is invalid") from None

        state._outbound_attempts = outbound
        state._automatic_retries_used = automatic
        state._manual_retries_used = manual
        state._manual_retry_authorized = data["manual_retry_authorized"]
        state._partial_output = data["partial_output"]
        state._effect = data["effect"]
        return state
