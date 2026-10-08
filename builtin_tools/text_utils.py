"""Output truncation and redaction helpers for built-in tools.

Stateless leaf module: no imports back into ``builtin_executor`` or any handler
module, so it is safe to import eagerly.
"""

from __future__ import annotations

import itertools
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

# Distinct decoded texts (the original included) the search may admit and
# examine. Admission fails closed: the moment a new distinct text would be the
# (MAX_DECODE_STATES + 1)th, the text is declared undecidable. Candidates are
# built lazily, so that one overflow candidate is constructed and then
# discarded unexamined (never stored, never matched); a search that settles
# within the cap passes honestly. Honest text reaches a handful of states;
# branching adversarial text (independent percent-run readings, interleaved
# grammars) hits this cap.
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


class _Undecidable(Exception):
    """Raised while generating readings that cannot be enumerated safely."""


def _utf8_sequence_length(raw: bytes, start: int) -> int:
    """Return the length of the valid multi-byte UTF-8 character at *start*, or 0."""
    for length in (2, 3, 4):
        chunk = raw[start : start + length]
        if len(chunk) == length:
            try:
                if len(chunk.decode("utf-8")) == 1:
                    return length
            except UnicodeDecodeError:
                continue
    return 0


def _percent_run_slots(
    run: str, relevant: "frozenset[str] | None"
) -> list[tuple[str, ...]]:
    """Split one ``%XX`` *run* into slots, each a tuple of its alternative readings.

    ASCII bytes and bytes that are not part of a valid UTF-8 character are
    always read as Latin-1 (one alternative). Each valid multi-byte UTF-8
    character is read independently as that character or as its bytes taken
    as Latin-1 (``%C3%A9`` is ``é`` or ``Ã©``), so a secret assembled from
    differently encoded parts is among the readings even when the parts are
    adjacent in one run.

    The two alternatives differ only in the non-ASCII characters they produce,
    so with *relevant* (the characters that occur in any needle) an alternative
    none of whose characters is relevant cannot take part in a needle match
    that the other misses: it is dropped when the other can, and the choice
    collapses to the UTF-8 reading when neither can. Percent-decoded non-ASCII
    characters never feed a later decoding layer, so this loses no match while
    keeping honest multilingual URLs to a single reading per character.

    Standard decoders (``urllib.parse.unquote``) turn invalid UTF-8 bytes into
    U+FFFD, a reading this function does not enumerate. Rather than emulate
    its byte-grouping rules, a run holding invalid UTF-8 bytes raises
    :class:`_Undecidable` (fail closed) whenever U+FFFD occurs in *relevant*,
    i.e. when some secret contains it; otherwise no needle can match that
    reading and the Latin-1 reading above is complete.
    """
    raw = bytes.fromhex(run.replace("%", ""))
    replacement_matters = relevant is not None and "\ufffd" in relevant
    slots: list[tuple[str, ...]] = []
    index = 0
    while index < len(raw):
        length = _utf8_sequence_length(raw, index) if raw[index] >= 0x80 else 0
        if length == 0:
            if replacement_matters and raw[index] >= 0x80:
                raise _Undecidable
            slots.append((raw[index : index + 1].decode("latin-1"),))
            index += 1
            continue
        chunk = raw[index : index + length]
        readings = (chunk.decode("utf-8"), chunk.decode("latin-1"))
        if relevant is not None:
            alive = tuple(r for r in readings if any(c in relevant for c in r))
            readings = alive or readings[:1]
        slots.append(readings)
        index += length
    return slots


def _percent_candidates(text: str, relevant: "frozenset[str] | None") -> Iterator[str]:
    """Yield every URL-decoded reading of *text* one layer deep, never growing it.

    Every multi-byte UTF-8 character inside a ``%XX`` run is decoded
    independently as UTF-8 or Latin-1 (:func:`_percent_run_slots`), and ``+``
    is read both as itself and as a space (form encoding). The cross product
    is generated lazily, all-UTF-8 first, so the caller can stop at its state
    budget instead of materializing it. Malformed escapes (``%zz``, a trailing
    ``%``) are left untouched.
    """
    if "%" not in text and "+" not in text:
        return
    parts: list[tuple[bool, tuple[str, ...]]] = []
    cursor = 0
    for match in _PERCENT_RUN_RE.finditer(text):
        parts.append((True, (text[cursor : match.start()],)))
        parts.extend((False, slot) for slot in _percent_run_slots(match.group(), relevant))
        cursor = match.end()
    parts.append((True, (text[cursor:],)))
    for plus_as_space in (False, True) if "+" in text else (False,):
        slots: list[tuple[str, ...]] = []
        for is_literal, alternatives in parts:
            if is_literal and plus_as_space:
                alternatives = (alternatives[0].replace("+", " "),)
            if slots and len(slots[-1]) == 1 and len(alternatives) == 1:
                slots[-1] = (slots[-1][0] + alternatives[0],)
            else:
                slots.append(alternatives)
        for combo in itertools.product(*slots):
            yield "".join(combo)


def _decode_candidates(text: str, relevant: "frozenset[str] | None") -> Iterator[str]:
    """Yield every text one decoding layer can turn *text* into.

    JSON escapes and Python escapes give one candidate each; URL decoding
    gives one per independent reading of its percent runs
    (:func:`_percent_candidates`).
    """
    yield _unescape_json_once(text)
    yield _unescape_python_once(text)
    yield from _percent_candidates(text, relevant)


def _decodes_to_hit(
    text: str,
    hit: "Callable[[str], bool]",
    relevant: "frozenset[str] | None" = None,
) -> bool:
    """Return True when a decode of *text* satisfies *hit*, or cannot be shown clean.

    Each level applies every decoder (JSON, Python, and URL decoding with each
    multi-byte percent character read independently as UTF-8 or Latin-1, ``+``
    kept or read as a space) to every text reached so far, so any interleaving
    of the grammars and any combination of readings is a candidate; a hit in
    any candidate counts. A text already seen is not revisited. *relevant*, the
    characters occurring in any needle, lets URL decoding skip readings that no
    needle can match and makes invalid UTF-8 percent bytes undecidable when a
    needle contains U+FFFD (:func:`_percent_run_slots`).

    At most :data:`MAX_DECODE_STATES` distinct texts, the original included,
    are admitted and examined. Admission fails closed (True): the moment a new
    distinct text would exceed that cap it is rejected. Candidates are built
    lazily, so one overflow candidate is constructed and discarded unexamined,
    but the combinations of independent readings are never fully generated. A
    single text with more than that many candidate readings is likewise
    undecidable, as is a search still producing new texts after
    :data:`MAX_DECODE_PASSES` levels. A search that settles within the budget
    returns False.
    """
    seen = {(len(text), hash(text))}
    frontier = [text]
    try:
        for _ in range(MAX_DECODE_PASSES):
            reached: list[str] = []
            for current in frontier:
                for index, decoded in enumerate(_decode_candidates(current, relevant)):
                    if index >= MAX_DECODE_STATES:
                        return True
                    key = (len(decoded), hash(decoded))
                    if key in seen:
                        continue
                    if len(seen) >= MAX_DECODE_STATES:
                        return True
                    if hit(decoded):
                        return True
                    seen.add(key)
                    reached.append(decoded)
            if not reached:
                return False
            frontier = reached
    except _Undecidable:
        return True
    return True


@lru_cache(maxsize=64)
def _redaction_patterns(
    needles: tuple[str, ...], placeholder: str
) -> "tuple[re.Pattern[str], re.Pattern[str], bool, frozenset[str]]":
    """Compile the placeholder-token scanner, the needle scanner, the escape flag and needle alphabet."""
    tokens = "|".join(
        re.escape(p) + r"(?:_\d+)?"
        for p in dict.fromkeys((*_PROTECTED_PLACEHOLDERS, placeholder))
    )
    return (
        re.compile(tokens),
        re.compile("|".join(map(re.escape, needles))),
        any("\\" in n for n in needles),
        frozenset("".join(needles)),
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
       decoding that does not settle within :data:`MAX_DECODE_PASSES` and
       :data:`MAX_DECODE_STATES`, marks the text suspicious. Suspicious *lines* are replaced whole by
       *placeholder* (an encoded secret never spans a raw newline); if the
       text is still suspicious afterwards (a secret spelled across lines),
       the whole text is replaced.
    """
    if not needles:
        return text
    token_re, needle_re, escape_sensitive, alphabet = _redaction_patterns(needles, placeholder)
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
            alphabet,
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
