"""outcome_utils.py
-----------------
Shared helpers for constructing tool and step outcome dictionaries.

These utilities centralise common result shapes so they do not drift between
call sites.
"""

from __future__ import annotations

from typing import Literal, Union

ExecutionDisposition = Literal["executed", "refused", "deferred", "cancelled"]
ExecutionReading = Union[ExecutionDisposition, Literal["unknown"]]

_EXECUTION_KEY = "_execution_disposition"
_DISPOSITIONS = frozenset({"executed", "refused", "deferred", "cancelled"})
_VETO_ERROR_TYPES = frozenset(
    {"prohibited_path", "scheduled_approval_denied", "confirmation_failed"}
)


RUNTIME_FAILURE_PREFIX = "❌"


def is_runtime_failure_text(result: object) -> bool:
    """Return whether *result* is an unstructured runtime terminal failure.

    The runtime reserves a leading ``❌`` on plain terminal strings for failure
    (including terminal LLM failures). Callers apply this only after
    cancellation and explicit structured outcomes: a JSON outcome starts with
    ``{``, so an explicitly successful one whose *output* begins with ``❌``
    never matches.
    """
    return isinstance(result, str) and result.startswith(RUNTIME_FAILURE_PREFIX)


def with_execution(outcome: dict, disposition: ExecutionDisposition) -> dict:
    """Return a shallow copy of *outcome* stamped with a private execution disposition.

    Only a local semantic boundary that actually attempted (or refused/deferred/
    cancelled) the requested operation may call this; the marker carries no
    arguments, tokens, identities or error text.
    """
    if disposition not in _DISPOSITIONS:
        raise ValueError(f"invalid execution disposition: {disposition!r}")
    stamped = dict(outcome)
    stamped[_EXECUTION_KEY] = disposition
    return stamped


def read_execution(outcome: object) -> ExecutionReading:
    """Return the stamped disposition, or ``"unknown"`` when absent or invalid."""
    if not isinstance(outcome, dict):
        return "unknown"
    value = outcome.get(_EXECUTION_KEY)
    if isinstance(value, str) and value in _DISPOSITIONS:
        return value  # type: ignore[return-value]
    return "unknown"


def effective_execution(outcome: object) -> ExecutionReading:
    """Return the disposition after legacy refusal/deferral/cancellation vetoes.

    ``executed`` survives only when no refusal, deferral or cancellation signal
    is present; success flags and error strings are never execution evidence.
    """
    reading = read_execution(outcome)
    if reading == "unknown" or not isinstance(outcome, dict):
        return reading
    if outcome.get("requires_confirmation") or outcome.get("vision_reexecute"):
        return "deferred"
    if outcome.get("_operator_cancelled"):
        return "cancelled"
    if outcome.get("_guard_refusal") or outcome.get("error_type") in _VETO_ERROR_TYPES:
        return "refused"
    return reading


def strip_execution(outcome: dict) -> dict:
    """Return a shallow copy of *outcome* without the private execution marker."""
    if _EXECUTION_KEY not in outcome:
        return outcome
    return {k: v for k, v in outcome.items() if k != _EXECUTION_KEY}


def consume_execution(outcome: dict) -> tuple[dict, ExecutionReading]:
    """Split *outcome* into its marker-free copy and its effective disposition."""
    return strip_execution(outcome), effective_execution(outcome)


def fail_outcome(
    error: str,
    *,
    error_type: str = "",
    recoverable: bool = False,
    suggestion: str = "",
    exit_code: int = -1,
    output: str = "",
) -> dict:
    """Return the standard failure-outcome dictionary.

    All failure outcomes share the same schema; using this helper keeps the
    field set consistent across the codebase.

    Args:
        error: Human-readable error message (required).
        error_type: Normalised error classification, e.g. ``"tool_timeout"``.
        recoverable: Whether the caller is expected to be able to recover.
        suggestion: Hints for the LLM/user about how to recover.
        exit_code: Process-style exit code; ``-1`` for generic failure.
        output: Any partial output produced before the failure.

    Returns:
        A dict with ``success=False`` and the standard outcome fields.
    """
    return {
        "success": False,
        "output": output,
        "error": error,
        "exit_code": exit_code,
        "error_type": error_type,
        "recoverable": recoverable,
        "suggestion": suggestion,
    }
