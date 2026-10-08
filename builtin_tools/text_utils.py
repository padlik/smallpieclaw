"""Output truncation and redaction helpers for built-in tools.

Stateless leaf module: no imports back into ``builtin_executor`` or any handler
module, so it is safe to import eagerly.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from typing import Any, Callable, Iterable, Iterator


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


# Surgical redaction covers a secret's literal form and its JSON-escaped forms
# up to this many serialization layers (both ``json.dumps`` flavours), so
# ordinary nested records keep their surrounding text. It is a convenience, not
# the safety net: whatever it misses (other \\uXXXX spellings, deeper layers) is
# caught by the decode check below.
MAX_ESCAPE_DEPTH = 6

# A backslash run this long is the classic signature of deep escaping; when a
# secret needs escaping at all, such a line is replaced without decoding.
_DEEP_RUN = "\\" * 2**MAX_ESCAPE_DEPTH

# Decode passes tried before a still-changing text is declared undecidable and
# treated as hiding a secret. Each pass strips one escaping layer, so honest
# text settles in a handful. Every decoder only ever keeps a text the same
# length or shortens it (``+`` -> space preserves length, escapes and ``%XX``
# runs shrink), so decoding cannot grow or oscillate; this pass cap bounds the
# length-preserving cases.
MAX_DECODE_PASSES = 32

# Distinct decoded texts explored (across all candidate readings) before
# decoding is declared undecidable; honest text reaches a handful, branching
# adversarial text hits this cap. Each text spawns at most one candidate per
# decoder, and the ambiguous percent readings (UTF-8 vs Latin-1, with or
# without ``+`` as space) contribute at most four distinct ones.
MAX_DECODE_STATES = 32

_JSON_ESCAPE_RE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
_PYTHON_ESCAPE_RE = re.compile(
    r"\\(?:(\r\n|\r|\n)|x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})"
    r"|N\{([^}\n]{1,100})\}|([0-7]{1,3})|([\\'\"abfnrtv]))"
)
_PYTHON_SIMPLE_ESCAPES = {
    "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t", "v": "\v",
}
_MAX_CODEPOINT = 0x10FFFF
_PERCENT_RUN_RE = re.compile(r"(?:%[0-9a-fA-F]{2})+")
_ENCODING_MARKER_RE = re.compile(r"[\\%+]")
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")
_SIMPLE_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


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


def build_redaction_needles(secrets: "Iterable[str]") -> tuple[str, ...]:
    """Return literal and JSON-escaped forms of each secret, longest first.

    The escaped forms (up to :data:`MAX_ESCAPE_DEPTH` layers, both ``json.dumps``
    flavours) let a secret serialized into a JSON string be redacted in place.
    Longest-first ordering keeps overlapping secrets deterministic. Results
    are cached per distinct secret set, since the expansion is costly and a
    scan redacts many records against one vault.
    """
    return _build_needles(tuple(secrets))


@lru_cache(maxsize=8)
def _build_needles(secrets: tuple[str, ...]) -> tuple[str, ...]:
    needles: set[str] = set()
    for secret in secrets:
        if secret:
            needles |= _escape_forms(secret, MAX_ESCAPE_DEPTH)
    return tuple(sorted(needles, key=lambda n: (-len(n), n)))


def _unescape_json_once(text: str) -> str:
    """Decode every JSON string escape in *text* one layer, never growing it.

    Handles every valid spelling (``\\"``, ``\\u0022``, ``\\\\``, ``\\u005c``,
    surrogate pairs, ...) because the escape grammar is decoded rather than
    enumerated. Each escape becomes a single character, so repeated decoding
    cannot inflate the text.
    """

    def _decode(match: "re.Match[str]") -> str:
        hex_digits, simple = match.groups()
        return chr(int(hex_digits, 16)) if hex_digits else _SIMPLE_ESCAPES[simple]

    decoded = _JSON_ESCAPE_RE.sub(_decode, text)
    return _SURROGATE_PAIR_RE.sub(
        lambda m: chr(0x10000 + ((ord(m.group()[0]) - 0xD800) << 10) + ord(m.group()[1]) - 0xDC00),
        decoded,
    )


def _unescape_python_once(text: str) -> str:
    """Decode every Python string escape in *text* one layer, never growing it.

    Covers what ``repr()`` and the ``unicode_escape`` codec understand:
    ``\\xHH``, ``\\uXXXX``, ``\\UXXXXXXXX``, octal, ``\\N{NAME}``, the single
    letter and quote escapes and backslash-newline continuation (LF, CRLF or
    CR). An unknown or unencodable ``\\N{...}`` name stays literal. It is a
    regular-expression decoder (no ``eval``), works on raw text without valid
    quoting, and leaves malformed or unknown escapes untouched.
    """

    def _decode(match: "re.Match[str]") -> str:
        newline, hex2, hex4, hex8, name, octal, simple = match.groups()
        if newline:
            return ""
        if name:
            try:
                return unicodedata.lookup(name)
            except (KeyError, UnicodeError):
                return match.group()
        if octal:
            return chr(int(octal, 8))
        if simple:
            return _PYTHON_SIMPLE_ESCAPES[simple]
        code = int(hex2 or hex4 or hex8, 16)
        return chr(code) if code <= _MAX_CODEPOINT else match.group()

    decoded = _PYTHON_ESCAPE_RE.sub(_decode, text)
    return _SURROGATE_PAIR_RE.sub(
        lambda m: chr(0x10000 + ((ord(m.group()[0]) - 0xD800) << 10) + ord(m.group()[1]) - 0xDC00),
        decoded,
    )


def _unescape_percent_once(text: str) -> str:
    """Decode every ``%XX`` run in *text* one layer (RFC 3986), never growing it.

    Each run of percent-escapes is decoded as UTF-8; bytes that are not valid
    UTF-8 fall back to Latin-1, so both common URL encodings are recovered.
    Malformed escapes (``%zz``, a trailing ``%``) are left untouched.
    """
    if "%" not in text:
        return text

    def _decode(match: "re.Match[str]") -> str:
        raw = bytes.fromhex(match.group().replace("%", ""))
        decoded = raw.decode("utf-8", "surrogateescape")
        return "".join(
            chr(ord(c) - 0xDC00) if "\udc80" <= c <= "\udcff" else c for c in decoded
        )

    return _PERCENT_RUN_RE.sub(_decode, text)


def _unescape_percent_latin1_once(text: str) -> str:
    """Decode every ``%XX`` run in *text* one layer, reading each byte as Latin-1.

    The alternative reading of :func:`_unescape_percent_once`: a run whose
    bytes also happen to form valid UTF-8 (``%C3%A9``) is two characters here
    but one there, and a secret may have been encoded either way.
    """
    if "%" not in text:
        return text
    return _PERCENT_RUN_RE.sub(
        lambda match: bytes.fromhex(match.group().replace("%", "")).decode("latin-1"),
        text,
    )


def _unescape_url_form_once(text: str) -> str:
    """Decode *text* like :func:`_unescape_percent_once`, also reading ``+`` as a space."""
    return _unescape_percent_once(text.replace("+", " "))


def _unescape_url_form_latin1_once(text: str) -> str:
    """Decode *text* like :func:`_unescape_percent_latin1_once`, also reading ``+`` as a space."""
    return _unescape_percent_latin1_once(text.replace("+", " "))


_DECODERS: "tuple[Callable[[str], str], ...]" = (
    _unescape_json_once,
    _unescape_python_once,
    _unescape_percent_once,
    _unescape_percent_latin1_once,
    _unescape_url_form_once,
    _unescape_url_form_latin1_once,
)


def _decodes_to_hit(text: str, hit: "Callable[[str], bool]") -> bool:
    """Return True when a decode of *text* satisfies *hit*, or never settles.

    Each level applies every decoder (JSON, Python, percent as UTF-8 and as
    Latin-1, each also form-encoded) to every text reached so far, so any
    interleaving of the grammars and any reading of an ambiguous ``%XX`` run is
    a candidate; a hit in any candidate counts. A text already seen is not
    revisited. Decoding stops once a level reaches
    nothing new. Fail closed (True) when decoding is still producing new texts
    after :data:`MAX_DECODE_PASSES` levels or has reached
    :data:`MAX_DECODE_STATES` distinct texts: such text cannot be shown clean.
    """
    seen = {(len(text), hash(text))}
    frontier = [text]
    for _ in range(MAX_DECODE_PASSES):
        reached: list[str] = []
        for current in frontier:
            for decode in _DECODERS:
                decoded = decode(current)
                key = (len(decoded), hash(decoded))
                if key in seen:
                    continue
                if hit(decoded):
                    return True
                seen.add(key)
                reached.append(decoded)
        if not reached:
            return False
        if len(seen) > MAX_DECODE_STATES:
            return True
        frontier = reached
    return True


@lru_cache(maxsize=64)
def _redaction_patterns(
    needles: tuple[str, ...], placeholder: str
) -> "tuple[re.Pattern[str], re.Pattern[str], bool]":
    """Compile the placeholder-token scanner, the needle scanner and the escape flag."""
    tokens = "|".join(
        re.escape(p) + r"(?:_\d+)?"
        for p in dict.fromkeys((*_PROTECTED_PLACEHOLDERS, placeholder))
    )
    return (
        re.compile(tokens),
        re.compile("|".join(map(re.escape, needles))),
        any("\\" in n for n in needles),
    )


def _unprotected_matches(
    text: str, token_re: "re.Pattern[str]", needle_re: "re.Pattern[str]"
) -> Iterator[tuple[int, int]]:
    """Yield each needle span of *text* that is not inside a placeholder token.

    A match lying entirely within an existing placeholder token is skipped
    (keeps passes idempotent); a secret that extends beyond a token boundary
    is a real secret. A secret exactly equal to a placeholder is therefore
    indistinguishable from it and stays as is.
    """
    spans = [m.span() for m in token_re.finditer(text)]
    cursor = 0
    while (match := needle_re.search(text, cursor)) is not None:
        start, end = match.span()
        if any(s <= start and end <= e for s, e in spans):
            cursor = start + 1  # contained: keep scanning, a longer secret may start inside
            continue
        yield start, end
        cursor = end


def _redact_string(text: str, needles: tuple[str, ...], placeholder: str) -> str:
    """Redact *text* by literal match, then fail closed on anything that decodes to a secret.

    1. Every needle occurrence outside a placeholder token is replaced in place.
    2. If the remainder holds a backslash, ``%`` or ``+``, it is decoded layer
       by layer (:func:`_decodes_to_hit`: JSON escapes, Python escapes, URL
       percent-encoding, in any interleaving); a layer containing a needle, or
       decoding that does not settle within :data:`MAX_DECODE_PASSES`, marks
       the text suspicious. Suspicious *lines* are replaced whole by
       *placeholder* (an encoded secret never spans a raw newline); if the
       text is still suspicious afterwards (a secret spelled across lines),
       the whole text is replaced.
    """
    if not needles:
        return text
    token_re, needle_re, escape_sensitive = _redaction_patterns(needles, placeholder)
    out: list[str] = []
    copied = 0
    for start, end in _unprotected_matches(text, token_re, needle_re):
        out.append(text[copied:start])
        out.append(placeholder)
        copied = end
    out.append(text[copied:])
    redacted = "".join(out)

    def suspicious(candidate: str) -> bool:
        if _ENCODING_MARKER_RE.search(candidate) is None:
            return False
        if escape_sensitive and _DEEP_RUN in candidate:
            return True
        return _decodes_to_hit(
            candidate,
            lambda layer: next(_unprotected_matches(layer, token_re, needle_re), None)
            is not None,
        )

    if not suspicious(redacted):
        return redacted
    cleaned = "\n".join(
        placeholder if suspicious(line) else line for line in redacted.split("\n")
    )
    return placeholder if suspicious(cleaned) else cleaned


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
    Each secret is matched literally and in its common JSON-escaped forms; text
    that still decodes to a secret under any JSON string-escape spelling (or
    nests deeper than the decode budget) is replaced line by line, or whole
    when the secret is spread across lines. Existing placeholder tokens are
    never re-redacted, so the pass is idempotent.
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
        return _redact_string(value, needles, placeholder)
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
            _redact_string(k, needles, placeholder)
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
