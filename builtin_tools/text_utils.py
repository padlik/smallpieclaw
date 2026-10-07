"""Output truncation and redaction helpers for built-in tools.

Stateless leaf module: no imports back into ``builtin_executor`` or any handler
module, so it is safe to import eagerly.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


def _truncate_output(text: str, limit: int) -> str:
    """Truncate *text* to at most *limit* chars, keeping the tail.

    Tail semantics are intentional: for build, test, and script output the
    useful information (errors, results, summaries) almost always appears near
    the end.  When truncation occurs a clear marker is prepended so the LLM
    knows data was omitted.
    """
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    omitted = len(text) - limit
    return f"[...{omitted} chars omitted, showing last {limit} chars...]\n{kept}"


def _truncate_tail(tail: str, total_chars: int, limit: int) -> str:
    """Build truncated output from a rolling tail when total stream size is known.

    Use instead of _truncate_output when the caller only kept a rolling
    *tail* in memory (not the full stream) but knows the *total_chars* written.
    When total_chars <= limit the tail *is* the full output and is returned
    as-is.  Otherwise a correct omission count is prepended.
    """
    if total_chars <= limit:
        return tail
    omitted = total_chars - limit
    kept = tail[-limit:]
    return f"[...{omitted} chars omitted, showing last {limit} chars...]\n{kept}"


_REDACTED_PLACEHOLDER = "[REDACTED]"


def build_redaction_needles(secrets: "Iterable[str]") -> list[str]:
    """Return literal and JSON-escaped forms of each secret, longest first.

    The escaped forms (ASCII and non-ASCII ``json.dumps`` flavours) catch a
    secret that was serialized into a JSON string, where quotes, backslashes
    and control characters change representation. Longest-first ordering keeps
    overlapping secrets deterministic.
    """
    needles: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        needles.add(secret)
        needles.add(json.dumps(secret)[1:-1])
        needles.add(json.dumps(secret, ensure_ascii=False)[1:-1])
    return sorted(needles, key=lambda n: (-len(n), n))


def _redact_string(text: str, needles: "list[str]", placeholder: str) -> str:
    """Replace every needle in *text* with *placeholder*."""
    for needle in needles:
        text = text.replace(needle, placeholder)
    return text


def redact_vault_secrets(
    text: str,
    secrets: "Iterable[str]",
    placeholder: str = _REDACTED_PLACEHOLDER,
) -> str:
    """Replace every non-empty entry of *secrets* in *text* with *placeholder*.

    Shared redaction core for retained tool artifacts and log records: the
    shell oversized output path, the log_detail read-time pass and dump, and
    the structlog ingestion redactor all route through this single routine so
    no path's redaction can silently weaken relative to another. Each secret is
    matched both literally and in its JSON-escaped form, so a secret serialized
    into a JSON string cannot leak on decode. Idempotent.
    """
    return _redact_string(text, build_redaction_needles(secrets), placeholder)


def redact_structure(
    value: Any,
    secrets: "Iterable[str]",
    placeholder: str = _REDACTED_PLACEHOLDER,
) -> Any:
    """Recursively redact secrets from strings AND dict keys of *value*.

    Container shapes (dict/list/tuple) are preserved; non-string leaves pass
    through. Scrubbing happens before serialization so that no serialized form
    of a secret can be produced from the redacted structure.
    """
    return redact_with_needles(value, build_redaction_needles(secrets), placeholder)


def redact_with_needles(value: Any, needles: "list[str]", placeholder: str) -> Any:
    """Redact *value* using needles precomputed by :func:`build_redaction_needles`."""
    if isinstance(value, str):
        return _redact_string(value, needles, placeholder)
    if isinstance(value, dict):
        return {
            (_redact_string(k, needles, placeholder) if isinstance(k, str) else k):
            redact_with_needles(v, needles, placeholder)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_with_needles(v, needles, placeholder) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_with_needles(v, needles, placeholder) for v in value)
    return value
