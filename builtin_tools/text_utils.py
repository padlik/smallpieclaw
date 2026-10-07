"""Output truncation and redaction helpers for built-in tools.

Stateless leaf module: no imports back into ``builtin_executor`` or any handler
module, so it is safe to import eagerly.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
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

# Placeholders emitted by either redaction layer (read-time here, ingestion in
# ``agent_logging``). Existing tokens, including ``_N`` collision suffixes, are
# never re-redacted, which keeps every pass idempotent.
_PROTECTED_PLACEHOLDERS = (_REDACTED_PLACEHOLDER, "***REDACTED***")


# A secret nested in N layers of JSON string serialization appears as its
# N-times-escaped form. Redaction covers every form up to this depth; any
# deeper nesting is refused whole (see ``_exceeds_escape_coverage``).
MAX_ESCAPE_DEPTH = 6

# Every escaped form at depth > MAX_ESCAPE_DEPTH contains a backslash run at
# least this long, so such a run is the marker of uncovered nesting.
_DEEP_RUN = "\\" * 2**MAX_ESCAPE_DEPTH


def _escape_forms(secret: str, depth: int) -> set[str]:
    """Return *secret* and every JSON-string-escaped form up to *depth* layers.

    Each layer may use either ``json.dumps`` flavour (``ensure_ascii`` on or
    off), so all flavour sequences are expanded; duplicates collapse, which
    keeps secrets without escape-sensitive characters at a single form.
    """
    forms = {secret}
    frontier = {secret}
    for _ in range(depth):
        frontier = {
            json.dumps(form, ensure_ascii=ascii_flag)[1:-1]
            for form in frontier
            for ascii_flag in (True, False)
        } - forms
        forms |= frontier
    return forms


def _needles_for(secrets: "Iterable[str]", depth: int) -> tuple[str, ...]:
    needles: set[str] = set()
    for secret in secrets:
        if secret:
            needles |= _escape_forms(secret, depth)
    return tuple(sorted(needles, key=lambda n: (-len(n), n)))


def build_redaction_needles(secrets: "Iterable[str]") -> tuple[str, ...]:
    """Return literal and repeatedly JSON-escaped forms of each secret, longest first.

    The escaped forms (up to :data:`MAX_ESCAPE_DEPTH` layers, both ``json.dumps``
    flavours) catch a secret that was serialized into a JSON string, possibly
    several times, where quotes, backslashes and control characters change
    representation. Longest-first ordering keeps overlapping secrets
    deterministic.
    """
    return _needles_for(secrets, MAX_ESCAPE_DEPTH)


def build_detection_probes(secrets: "Iterable[str]") -> tuple[str, ...]:
    """Return casefolded substrings whose absence proves a stored record is untouched.

    A stored ``search_text`` is the casefolded compact JSON of the record, so a
    leaf holding a redactable form at depth *d* appears there at depth *d* + 1.
    The probes therefore cover one layer more than redaction does, plus the
    doubled backslash run that marks nesting beyond redaction's coverage. A
    record matching none of them cannot be changed by redaction.
    """
    needles = _needles_for(secrets, MAX_ESCAPE_DEPTH + 1)
    probes = list(dict.fromkeys(n.casefold() for n in needles))
    if any("\\" in n for n in needles):
        probes.append(_DEEP_RUN * 2)
    return tuple(probes)


def _exceeds_escape_coverage(text: str, needles: tuple[str, ...]) -> bool:
    """Return True when *text* may hold a secret nested deeper than the needles cover.

    Only meaningful when some needle differs from its secret (contains a
    backslash); otherwise nesting depth cannot disguise any secret.
    """
    return _DEEP_RUN in text and any("\\" in n for n in needles)


@lru_cache(maxsize=64)
def _redaction_patterns(
    needles: tuple[str, ...], placeholder: str
) -> "tuple[re.Pattern[str], re.Pattern[str]]":
    """Compile the placeholder-token scanner and the needle scanner."""
    tokens = "|".join(
        re.escape(p) + r"(?:_\d+)?"
        for p in dict.fromkeys((*_PROTECTED_PLACEHOLDERS, placeholder))
    )
    return re.compile(tokens), re.compile("|".join(map(re.escape, needles)))


def _redact_string(
    text: str,
    needles: tuple[str, ...],
    placeholder: str,
    fail_closed: bool = False,
) -> str:
    """Replace every needle occurrence in *text* that is not inside a placeholder.

    A match lying entirely within an existing placeholder token is left alone
    (keeps passes idempotent); a secret that extends beyond a token boundary
    is a real secret and is redacted whole. A secret exactly equal to a
    placeholder is therefore indistinguishable from it and stays as is.

    With *fail_closed*, a string showing nesting deeper than the needles cover
    is replaced whole by *placeholder*: partial redaction there could leave the
    secret recoverable after further decoding.
    """
    if not needles:
        return text
    if fail_closed and _exceeds_escape_coverage(text, needles):
        return placeholder
    token_re, needle_re = _redaction_patterns(needles, placeholder)
    spans = [m.span() for m in token_re.finditer(text)]
    out: list[str] = []
    copied = cursor = 0
    while (match := needle_re.search(text, cursor)) is not None:
        start, end = match.span()
        if any(s <= start and end <= e for s, e in spans):
            cursor = start + 1  # contained: keep scanning, a longer secret may start inside
            continue
        out.append(text[copied:start])
        out.append(placeholder)
        copied = cursor = end
    out.append(text[copied:])
    return "".join(out)


def redact_vault_secrets(
    text: str,
    secrets: "Iterable[str]",
    placeholder: str = _REDACTED_PLACEHOLDER,
) -> str:
    """Replace every non-empty entry of *secrets* in *text* with *placeholder*.

    Shared redaction core for retained tool artifacts and log records: the
    shell oversized output path, the log_detail/log_query read-time pass and
    dump, and the structlog ingestion redactor all route through this single
    routine so no path's redaction can silently weaken relative to another.
    Each secret is matched both literally and in its JSON-escaped form, so a
    secret serialized into a JSON string cannot leak on decode. Existing
    placeholder tokens are never re-redacted, so the pass is idempotent.
    """
    return _redact_string(text, build_redaction_needles(secrets), placeholder)


def redact_structure(
    value: Any,
    secrets: "Iterable[str]",
    placeholder: str = _REDACTED_PLACEHOLDER,
    preserve_top_level_keys: bool = False,
) -> Any:
    """Recursively redact secrets from strings AND dict keys of *value*.

    Container shapes (dict/list/tuple) are preserved; non-string leaves pass
    through. Scrubbing happens before serialization so that no serialized form
    of a secret can be produced from the redacted structure. With
    *preserve_top_level_keys* the keys of a top-level dict are kept verbatim
    (their values are still redacted) so fixed record-schema fields such as
    ``id`` or ``trace`` survive short secrets; nested keys are always scrubbed.
    """
    needles = build_redaction_needles(secrets)
    if preserve_top_level_keys and isinstance(value, dict):
        return {k: redact_with_needles(v, needles, placeholder) for k, v in value.items()}
    return redact_with_needles(value, needles, placeholder)


def redact_with_needles(value: Any, needles: tuple[str, ...], placeholder: str) -> Any:
    """Redact *value* using needles precomputed by :func:`build_redaction_needles`.

    Dict keys that redact to the same string stay distinct: the first keeps
    *placeholder*, later ones get ``<placeholder>_1``, ``_2``, ... in dict
    order, skipping any name already used by an unchanged key.
    """
    if isinstance(value, str):
        return _redact_string(value, needles, placeholder, fail_closed=True)
    if isinstance(value, dict):
        return _redact_dict(value, needles, placeholder)
    if isinstance(value, list):
        return [redact_with_needles(v, needles, placeholder) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_with_needles(v, needles, placeholder) for v in value)
    return value


def _redact_dict(value: dict, needles: tuple[str, ...], placeholder: str) -> dict:
    """Redact a dict's keys and values, keeping colliding redacted keys distinct."""
    new_keys = {
        k: (
            _redact_string(k, needles, placeholder, fail_closed=True)
            if isinstance(k, str)
            else k
        )
        for k in value
    }
    used: set = {k for k, nk in new_keys.items() if nk == k}
    result: dict = {}
    for key, item in value.items():
        new_key = new_keys[key]
        if new_key != key:
            candidate, n = new_key, 0
            while candidate in used:
                n += 1
                candidate = f"{new_key}_{n}"
            used.add(candidate)
            new_key = candidate
        result[new_key] = redact_with_needles(item, needles, placeholder)
    return result
