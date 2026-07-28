"""Error type registry with retry policies.

Defines a small registry that classifies built-in tool error conditions.
Each :class:`ErrorTypeInfo` record describes whether an error is recoverable
(retryable), how many retries are allowed, exponential backoff parameters, and
whether recovering from the error typically requires a more complex replan.

Error classification:

* Transient (retryable): ``tool_timeout``, ``network_error``, ``syntax_error``.
* Planning (no retry, needs alternative approach): ``wrong_model_for_task``,
  ``fundamentally_wrong_approach``, ``impossible_with_current_tools``,
  ``nsjail_error``.
* Fatal (no retry, environment/fix required): ``permission_denied``,
  ``file_not_found``, ``command_not_found``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ErrorTypeInfo:
    """Metadata for a single error type.

    Attributes:
        error_type: Kebab-case identifier, e.g. ``tool_timeout``.
        recoverable: Whether the error might succeed on retry.
        max_retries: Maximum retry attempts when ``recoverable`` is True.
        backoff_base: Base seconds for exponential backoff (multiplied by
            ``2 ** retry_count``).
        requires_complex_recovery: True when the agent should replan or ask the
            operator rather than simply retry the same action.
    """

    error_type: str
    recoverable: bool
    max_retries: int = 0
    backoff_base: float = 2.0
    requires_complex_recovery: bool = False


class ErrorTypeRegistry:
    """Registry of known error types and their retry policies."""

    def __init__(self) -> None:
        self._types: dict[str, ErrorTypeInfo] = {}
        self.register_defaults()

    def register(self, info: ErrorTypeInfo) -> None:
        """Register an :class:`ErrorTypeInfo` in the registry."""
        self._types[info.error_type] = info

    def get(self, error_type: str) -> Optional[ErrorTypeInfo]:
        """Return the :class:`ErrorTypeInfo` for *error_type*, or None."""
        return self._types.get(error_type)

    def register_defaults(self) -> None:
        """Register the built-in error types used by the agent."""
        self.register(ErrorTypeInfo("tool_timeout", True, 2, 2.0))
        self.register(ErrorTypeInfo("network_error", True, 2, 2.0))
        self.register(ErrorTypeInfo("syntax_error", True, 2, 1.0))
        self.register(ErrorTypeInfo("permission_denied", False, 0, 0.0, True))
        self.register(ErrorTypeInfo("file_not_found", False, 0, 0.0, True))
        self.register(ErrorTypeInfo("command_not_found", False, 0, 0.0))
        self.register(ErrorTypeInfo("wrong_model_for_task", False, 0, 0.0, True))
        self.register(ErrorTypeInfo("fundamentally_wrong_approach", False, 0, 0.0, True))
        self.register(ErrorTypeInfo("impossible_with_current_tools", False, 0, 0.0, True))
        self.register(ErrorTypeInfo("nsjail_error", False, 0, 0.0, True))
