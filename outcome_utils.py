"""outcome_utils.py
-----------------
Shared helpers for constructing tool and step outcome dictionaries.

These utilities centralise common result shapes so they do not drift between
call sites.
"""

from __future__ import annotations


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
