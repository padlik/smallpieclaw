"""Unit tests for ``builtin_tools/text_utils.py`` helpers.

Covers truncation and the shared vault-secret redaction core.
"""

from __future__ import annotations

import pytest

from builtin_tools.text_utils import (
    _truncate_output,
    _truncate_tail,
    redact_vault_secrets,
)


def test_truncate_output_keeps_tail_and_reports_omission():
    text = "1234567890"
    assert _truncate_output(text, 10) == text
    truncated = _truncate_output(text, 5)
    assert truncated.endswith("67890")
    assert "5 chars omitted" in truncated


def test_truncate_tail_when_total_exceeds_limit():
    tail = "abcde"
    result = _truncate_tail(tail, total_chars=100, limit=5)
    assert result.endswith("abcde")
    assert "95 chars omitted" in result


def test_truncate_tail_returns_full_when_total_fits():
    tail = "short"
    assert _truncate_tail(tail, total_chars=5, limit=10) == tail


def test_redact_empty_secrets_is_noop():
    assert redact_vault_secrets("hello", []) == "hello"


def test_redact_skips_empty_string_entries():
    # Isolates the empty-skip property: "world" must still be redacted even
    # though empty entries precede it (an unguarded "" would interleave the
    # marker between every character instead).
    assert redact_vault_secrets("hello world", ["", "world"]) == "hello [REDACTED]"


def test_redact_replaces_multiple_secrets():
    text = "alpha beta gamma"
    assert redact_vault_secrets(text, ["beta", "gamma"]) == "alpha [REDACTED] [REDACTED]"


def test_redact_is_idempotent():
    once = redact_vault_secrets("my secret", ["secret"])
    assert once == "my [REDACTED]"
    twice = redact_vault_secrets(once, ["secret"])
    assert twice == once


def test_redact_preserves_text_without_secrets():
    text = "no confidential data here"
    assert redact_vault_secrets(text, ["secret"]) == text


_QUOTED_SECRET = 'pa"ss\\word99'


def test_redact_removes_json_escaped_form_of_secret():
    import json

    text = json.dumps({"auth": _QUOTED_SECRET})

    redacted = redact_vault_secrets(text, [_QUOTED_SECRET])

    assert json.loads(redacted)["auth"] != _QUOTED_SECRET
    assert json.dumps(_QUOTED_SECRET)[1:-1] not in redacted


def test_redact_structure_scrubs_dict_keys():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({_QUOTED_SECRET: 1, "ok": [_QUOTED_SECRET]}, [_QUOTED_SECRET])

    assert _QUOTED_SECRET not in repr(redacted)
    assert redacted == {"[REDACTED]": 1, "ok": ["[REDACTED]"]}


def test_secret_equal_to_placeholder_word_is_stable_across_passes():
    once = redact_vault_secrets("token REDACTED here", ["REDACTED"])
    twice = redact_vault_secrets(once, ["REDACTED"])

    assert once == "token [REDACTED] here"
    assert twice == once


def test_secret_overlapping_placeholder_text_does_not_corrupt_existing_placeholder():
    text = "a [REDACTED] b [REDACTED]_1 c"

    assert redact_vault_secrets(text, ["RED", "ACTED]", "[REDACTED"]) == text


def test_secret_containing_placeholder_text_is_still_redacted():
    assert redact_vault_secrets("x[REDACTED]y leaked", ["x[REDACTED]y"]) == "[REDACTED] leaked"


def test_redaction_is_idempotent_across_needle_sets():
    text = 'plain pa"ss\\word99 REDACTED [REDACTED] ***REDACTED*** id'
    for secrets in (
        [_QUOTED_SECRET],
        ["REDACTED"],
        ["REDACTED", _QUOTED_SECRET, "id"],
        ["ACTED", "***", "RED"],
    ):
        once = redact_vault_secrets(text, secrets)
        assert redact_vault_secrets(once, secrets) == once


def test_redact_structure_idempotent_with_ingestion_placeholder():
    from builtin_tools.text_utils import redact_structure

    value = {"k": "***REDACTED*** and REDACTED"}
    once = redact_structure(value, ["REDACTED"])

    assert once == {"k": "***REDACTED*** and [REDACTED]"}
    assert redact_structure(once, ["REDACTED"]) == once


def test_redact_structure_can_preserve_top_level_keys():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure(
        {"id": 1, "msg": "id", "nested": {"id": 2}}, ["id"], preserve_top_level_keys=True
    )

    assert redacted == {"id": 1, "msg": "[REDACTED]", "nested": {"[REDACTED]": 2}}


def test_redact_structure_colliding_keys_get_deterministic_suffix():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"aaaaaa": 1, "bbbbbb": 2, "[REDACTED]_1": 3}, ["aaaaaa", "bbbbbb"])

    assert redacted == {"[REDACTED]": 1, "[REDACTED]_2": 2, "[REDACTED]_1": 3}


def test_secret_extending_a_bracket_placeholder_is_redacted():
    out = redact_vault_secrets("x [REDACTED]live-token-42 y", ["[REDACTED]live-token-42"])

    assert out == "x [REDACTED] y"


def test_secret_extending_the_ingestion_placeholder_is_redacted():
    out = redact_vault_secrets(
        "x ***REDACTED***live-token-42 y", ["***REDACTED***live-token-42"]
    )

    assert out == "x [REDACTED] y"


def test_secret_starting_inside_a_placeholder_and_extending_past_it_is_redacted():
    out = redact_vault_secrets("a [REDACTED]live b", ["ED]live"])

    assert "live" not in out


def test_secret_wholly_inside_a_placeholder_leaves_the_placeholder_intact():
    assert redact_vault_secrets("a [REDACTED] b", ["REDACTED"]) == "a [REDACTED] b"


def test_secret_exactly_equal_to_a_placeholder_is_indistinguishable_from_it():
    assert redact_vault_secrets("a [REDACTED] b", ["[REDACTED]"]) == "a [REDACTED] b"


def test_extending_secrets_are_idempotent_across_passes():
    text = "[REDACTED]live-token-42 ***REDACTED***live-token-42 [REDACTED]"
    secrets = ["[REDACTED]live-token-42", "***REDACTED***live-token-42", "REDACTED"]

    once = redact_vault_secrets(text, secrets)

    assert "live-token-42" not in once
    assert redact_vault_secrets(once, secrets) == once


# ---------------------------------------------------------------------------
# Repeated JSON escaping: shared depth strategy, fail-closed beyond coverage
# ---------------------------------------------------------------------------

_ESC_SECRET = 'ab12"ba21'


def _nest(text: str, layers: int) -> str:
    import json

    for _ in range(layers):
        text = json.dumps(text)
    return text


def _serialization_layers(value, depth: int):
    """Compact JSON of *value*, then json.dumps applied 0..depth more times."""
    import json

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    yield text
    for _ in range(depth):
        text = json.dumps(text, ensure_ascii=False)
        yield text


def test_redacted_structure_yields_no_needle_at_any_serialization_depth():
    from builtin_tools.text_utils import (
        MAX_ESCAPE_DEPTH,
        build_redaction_needles,
        redact_structure,
    )

    needles = build_redaction_needles([_ESC_SECRET])
    for layers in range(0, MAX_ESCAPE_DEPTH + 1):
        record = {"msg": _nest(f"token={_ESC_SECRET}", layers), "n": 1}
        redacted = redact_structure(record, [_ESC_SECRET], preserve_top_level_keys=True)
        for text in _serialization_layers(redacted, MAX_ESCAPE_DEPTH):
            assert not any(n in text for n in needles), (layers, text)


def test_nested_leaf_keeps_surrounding_text_after_redaction():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure(
        {"msg": _nest(f"token={_ESC_SECRET}", 3)}, [_ESC_SECRET]
    )

    assert "token=[REDACTED]" in redacted["msg"]


def test_leaf_nested_beyond_coverage_is_replaced_whole():
    from builtin_tools.text_utils import MAX_ESCAPE_DEPTH, redact_structure

    leaf = _nest(f"token={_ESC_SECRET}", MAX_ESCAPE_DEPTH + 1)

    assert redact_structure({"msg": leaf}, [_ESC_SECRET]) == {"msg": "[REDACTED]"}


def test_deep_backslash_run_is_kept_when_no_secret_needs_escaping():
    from builtin_tools.text_utils import redact_structure

    value = {"msg": "\\" * 500}

    assert redact_structure(value, ["plainsecret1"]) == value


def test_fail_closed_replacement_is_idempotent():
    from builtin_tools.text_utils import MAX_ESCAPE_DEPTH, redact_structure

    leaf = _nest(f"token={_ESC_SECRET}", MAX_ESCAPE_DEPTH + 2)
    once = redact_structure({"msg": leaf}, [_ESC_SECRET])

    assert redact_structure(once, [_ESC_SECRET]) == once


# ---------------------------------------------------------------------------
# Decode-then-match: every valid JSON spelling of a credential is caught
# ---------------------------------------------------------------------------


def _unicode_layer(text: str) -> str:
    """One JSON string layer spelling backslashes and quotes as \\uXXXX escapes."""
    return '"' + text.replace("\\", "\\u005c").replace('"', "\\u0022") + '"'


def _unicode_nest(text: str, layers: int) -> str:
    for _ in range(layers):
        text = _unicode_layer(text)
    return text


def _json_layers(text: str):
    """Yield *text* and every string reachable by repeated json.loads() decoding."""
    import json

    for _ in range(64):
        yield text
        try:
            decoded = json.loads(text)
        except ValueError:
            return
        if isinstance(decoded, dict):
            for value in decoded.values():
                if isinstance(value, str):
                    yield from _json_layers(value)
            return
        if not isinstance(decoded, str):
            return
        text = decoded


def _unescaped_passes(text: str):
    """Yield *text* then repeated whole-text JSON-escape decodes until stable."""
    import re

    simple = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def sub(m):
        return chr(int(m.group(1), 16)) if m.group(1) else simple[m.group(2)]

    pattern = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
    for _ in range(64):
        yield text
        decoded = pattern.sub(sub, text)
        if decoded == text:
            return
        text = decoded


@pytest.mark.parametrize("layers", [1, 2, 3, 7, 8])
def test_unicode_escaped_layers_leave_no_credential_in_structure(layers):
    from builtin_tools.text_utils import redact_structure

    leaf = _unicode_nest(f"token={_ESC_SECRET}", layers)

    redacted = redact_structure({"msg": leaf}, [_ESC_SECRET], preserve_top_level_keys=True)

    for level in _json_layers(redacted["msg"]):
        assert _ESC_SECRET not in level


@pytest.mark.parametrize("layers", [1, 2, 3, 7, 8])
def test_unicode_escaped_layers_in_dict_key_leave_no_credential(layers):
    from builtin_tools.text_utils import redact_structure

    key = _unicode_nest(f"token={_ESC_SECRET}", layers)

    redacted = redact_structure({"outer": {key: 1}}, [_ESC_SECRET])

    (new_key,) = redacted["outer"]
    for level in _json_layers(new_key):
        assert _ESC_SECRET not in level


def test_unicode_escaped_leaf_is_replaced_whole():
    from builtin_tools.text_utils import redact_structure

    leaf = _unicode_nest(f"token={_ESC_SECRET}", 2)

    assert redact_structure({"msg": leaf}, [_ESC_SECRET]) == {"msg": "[REDACTED]"}


def test_unicode_escape_spelling_of_plain_secret_is_redacted():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": "token=\\u0061b12ba21"}, ["ab12ba21"])

    assert redacted == {"msg": "[REDACTED]"}


def test_embedded_unicode_escaped_json_fragment_is_redacted():
    from builtin_tools.text_utils import redact_structure

    msg = 'request failed: {"token": "ab12\\u0022ba21"}'

    redacted = redact_structure({"msg": msg}, [_ESC_SECRET])

    for level in _unescaped_passes(redacted["msg"]):
        assert _ESC_SECRET not in level


def test_unicode_escaped_credential_without_escaping_secret_is_redacted():
    from builtin_tools.text_utils import redact_structure

    leaf = _unicode_nest("token=plainsecret1", 3)

    redacted = redact_structure({"msg": leaf}, ["plainsecret1"])

    for level in _json_layers(redacted["msg"]):
        assert "plainsecret1" not in level


@pytest.mark.parametrize("layers", [1, 3, 7, 8])
def test_seven_layer_json_dumps_record_artifact_has_no_recoverable_credential(layers):
    import json

    from builtin_tools.text_utils import redact_vault_secrets

    text = json.dumps({"event": "login", "msg": f"token={_ESC_SECRET}"})
    for _ in range(layers):
        text = json.dumps(text)

    redacted = redact_vault_secrets(text, [_ESC_SECRET])

    for level in _json_layers(redacted):
        assert _ESC_SECRET not in level


@pytest.mark.parametrize("layers", [1, 2, 7, 8])
def test_unicode_escaped_artifact_has_no_recoverable_credential(layers):
    from builtin_tools.text_utils import redact_vault_secrets

    text = _unicode_nest(f"token={_ESC_SECRET}", layers)

    redacted = redact_vault_secrets(text, [_ESC_SECRET])

    for level in _json_layers(redacted):
        assert _ESC_SECRET not in level


def test_decode_budget_exhaustion_fails_closed():
    from builtin_tools.text_utils import redact_structure

    leaf = _unicode_nest("harmless text", 60)

    assert redact_structure({"msg": leaf}, [_ESC_SECRET]) == {"msg": "[REDACTED]"}


def test_clean_nested_text_is_untouched_when_decode_finds_nothing():
    from builtin_tools.text_utils import redact_structure

    value = {"msg": _unicode_nest("harmless text", 3), "path": "C:\\temp\\new"}

    assert redact_structure(value, [_ESC_SECRET]) == value


def test_only_the_offending_line_is_replaced_in_multiline_text():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "first ok\n" + _unicode_layer(f"token={_ESC_SECRET}") + "\nlast ok"

    assert redact_vault_secrets(text, [_ESC_SECRET]) == "first ok\n[REDACTED]\nlast ok"


def test_secret_spelled_across_raw_newline_and_escape_replaces_whole_text():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "keep\nab\n\\u0063d\nkeep"

    redacted = redact_vault_secrets(text, ["ab\ncd"])

    assert redacted == "[REDACTED]"


def test_decode_fail_closed_replacement_is_idempotent():
    from builtin_tools.text_utils import redact_structure

    leaf = _unicode_nest(f"token={_ESC_SECRET}", 7)
    once = redact_structure({"msg": leaf}, [_ESC_SECRET])

    assert redact_structure(once, [_ESC_SECRET]) == once


def test_surrogate_pair_spelling_of_secret_is_redacted():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": "pw\\uD83D\\uDE00x"}, ["pw\U0001f600x"])

    assert redacted == {"msg": "[REDACTED]"}


def _all_unicode_layer(text: str) -> str:
    """One JSON string layer spelling every character as \\uXXXX."""
    units = text.encode("utf-16-be")
    return '"' + "".join(f"\\u{units[i] << 8 | units[i + 1]:04x}" for i in range(0, len(units), 2)) + '"'


def _random_secret(rng) -> str:
    alphabet = 'abcXYZ0129-_ "\\\u00e9\u4e2d\n\t\U0001f600\u00c3\u00a9'
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(6, 14)))


def _random_leaf(rng, secret: str) -> str:
    import json

    layers = (
        lambda t: json.dumps(t),
        lambda t: json.dumps(t, ensure_ascii=False),
        _unicode_layer,
        _all_unicode_layer,
    )
    text = f"token={secret}"
    for _ in range(rng.randint(0, 8)):
        text = rng.choice(layers)(text)
    if rng.random() < 0.3:
        text = f"request failed: {text} (retrying)"
    return text


def test_randomized_mixed_spellings_never_leave_a_recoverable_credential():
    import random

    from builtin_tools.text_utils import redact_structure, redact_vault_secrets

    rng = random.Random(20261008)
    for case in range(300):
        secret = _random_secret(rng)
        leaf = _random_leaf(rng, secret)
        value = {"msg": leaf, "nested": [{"deep": leaf}], leaf: 1}

        redacted = redact_structure(value, [secret], preserve_top_level_keys=True)
        artifact = redact_vault_secrets(leaf, [secret])

        texts = [redacted["msg"], redacted["nested"][0]["deep"], artifact]
        texts += [k for k in redacted if k not in value]
        for text in texts:
            for json_level in _json_layers(text):
                for level in _unescaped_passes(json_level):
                    assert secret not in level, (case, secret, leaf)
        assert redact_structure(redacted, [secret], preserve_top_level_keys=True) == redacted


# ---------------------------------------------------------------------------
# Python escape grammars and URL percent-encoding
# ---------------------------------------------------------------------------


def _per_run_percent_variants(text: str, limit: int = 256) -> list[str]:
    """Return *text* with every ``%XX`` run decoded independently as UTF-8 or Latin-1.

    The oracle's split-and-mixed-decode trick generalized: any combination of
    per-run encodings, with ``+`` read as a space or kept, capped at *limit*.
    """
    import itertools
    import re
    from urllib.parse import unquote

    parts = re.split(r"((?:%[0-9a-fA-F]{2})+)", text)
    runs = [i for i, part in enumerate(parts) if i % 2]
    variants: list[str] = []
    for plus in (False, True):
        for combo in itertools.islice(itertools.product(("utf-8", "latin-1"), repeat=len(runs)), limit):
            out = list(parts)
            for index, encoding in zip(runs, combo):
                out[index] = unquote(parts[index], encoding=encoding)
            joined = "".join(
                part.replace("+", " ") if plus and i % 2 == 0 else part
                for i, part in enumerate(out)
            )
            variants.append(joined)
    return variants


def _recoverable(secret: str, text: str, depth: int = 4) -> bool:
    """Return True when any chain of common decoders applied to *text* reveals *secret*."""
    import ast
    import codecs
    import json
    from urllib.parse import unquote, unquote_plus

    def _literal(t: str):
        value = ast.literal_eval(t)
        return value if isinstance(value, str) else json.dumps(value)

    def _py_dict_values(t: str):
        value = ast.literal_eval(t)
        return " ".join(map(str, value.values())) if isinstance(value, dict) else t

    decoders = (
        json.loads, _literal, _py_dict_values,
        lambda t: codecs.decode(t, "unicode_escape"),
        unquote, unquote_plus,
        lambda t: unquote(t, encoding="latin-1"),
        lambda t: unquote_plus(t, encoding="latin-1"),
    )
    import warnings

    seen = {text}
    frontier = [text]
    for _ in range(depth):
        nxt = []
        for current in frontier:
            if secret in current:
                return True
            for decode in decoders:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        decoded = decode(current)
                except (ValueError, SyntaxError, TypeError, UnicodeError, MemoryError):
                    continue
                if isinstance(decoded, str) and decoded not in seen:
                    seen.add(decoded)
                    nxt.append(decoded)
            for variant in _per_run_percent_variants(current):
                if variant not in seen:
                    seen.add(variant)
                    nxt.append(variant)
        frontier = nxt
    return any(secret in t for t in frontier)


_PY_SECRET = "abc'def\"ghi"
_ORACLE_X22_SECRET = 'ab12"ba21'


def test_recoverable_helper_detects_the_oracle_shapes():
    assert _recoverable(_PY_SECRET, repr({"token": _PY_SECRET}))
    assert _recoverable(_ORACLE_X22_SECRET, r"token=ab12\x22ba21")
    assert _recoverable(_ORACLE_X22_SECRET, "token=ab12%22ba21")


def test_python_repr_of_dict_with_credential_is_not_recoverable():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": repr({"token": _PY_SECRET})}, [_PY_SECRET])

    assert not _recoverable(_PY_SECRET, redacted["msg"])


def test_python_hex_escape_in_diagnostic_is_not_recoverable():
    from builtin_tools.text_utils import redact_vault_secrets

    redacted = redact_vault_secrets(r"token=ab12\x22ba21", [_ORACLE_X22_SECRET])

    assert not _recoverable(_ORACLE_X22_SECRET, redacted)


@pytest.mark.parametrize(
    "spelling",
    [r"ab12\x22ba21", r"ab12\u0022ba21", r"ab12\U00000022ba21", r"ab12\042ba21",
     r"ab12\N{QUOTATION MARK}ba21", "ab12\\\"ba21", r"ab12\'ba21"],
)
def test_every_python_escape_spelling_is_redacted(spelling):
    from builtin_tools.text_utils import redact_vault_secrets

    secret = "ab12'ba21" if spelling.endswith(r"\'ba21") else _ORACLE_X22_SECRET

    redacted = redact_vault_secrets(f"token={spelling}", [secret])

    assert "[REDACTED]" in redacted
    assert not _recoverable(secret, redacted)


def test_python_line_continuation_cannot_split_a_secret():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=ab12\\\nba21", ["ab12ba21"]) == "[REDACTED]"


def test_python_repr_of_nested_json_text_is_not_recoverable():
    import json

    from builtin_tools.text_utils import redact_structure

    inner = json.dumps({"token": _PY_SECRET})
    redacted = redact_structure({"msg": repr(inner)}, [_PY_SECRET])

    assert not _recoverable(_PY_SECRET, redacted["msg"])


def test_json_encoded_text_inside_python_repr_layers_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    layered = repr(_unicode_nest(f"token={_ESC_SECRET}", 2))

    redacted = redact_vault_secrets(layered, [_ESC_SECRET])

    assert not _recoverable(_ESC_SECRET, redacted)


def test_url_escaped_credential_is_not_recoverable():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": "token=ab12%22ba21"}, [_ORACLE_X22_SECRET])

    assert not _recoverable(_ORACLE_X22_SECRET, redacted["msg"])


def test_url_escaped_credential_in_artifact_is_not_recoverable():
    from builtin_tools.text_utils import redact_vault_secrets

    redacted = redact_vault_secrets("GET /x?token=ab12%22ba21 HTTP/1.1", [_ORACLE_X22_SECRET])

    assert redacted == "[REDACTED]"


def test_plus_encoded_space_in_url_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("q=correct+horse+battery", ["correct horse battery"]) == "[REDACTED]"


def test_percent_encoded_multibyte_credential_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=p%C3%A4ss%E4%B8%AD", ["päss中"]) == "[REDACTED]"


def test_percent_encoded_latin1_credential_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=p%E4ssword", ["pässword"]) == "[REDACTED]"


def test_url_encoded_json_escaped_credential_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "token=ab12%5Cu0022ba21"

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == "[REDACTED]"


def test_json_escaped_url_encoded_credential_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    text = '"token=ab12\\u0025\\u0032\\u0032ba21"'

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == "[REDACTED]"


def test_double_url_encoded_credential_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=ab12%2522ba21", [_ORACLE_X22_SECRET]) == "[REDACTED]"


def test_intermediate_layer_needle_is_not_lost_to_a_later_decoder():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "token=a\\u0025\\u0032\\u0030b"

    assert redact_vault_secrets(text, ["a%20b"]) == "[REDACTED]"


def test_honest_url_without_a_needle_is_untouched():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "GET /search?q=hello%20world%21&lang=en%2Dus+x%FF%FE HTTP/1.1"

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == text


def test_honest_python_repr_without_a_needle_is_untouched():
    from builtin_tools.text_utils import redact_vault_secrets

    text = repr({"path": "C:\\temp\\new", "msg": "it's \x00 fine\n"})

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == text


def test_honest_url_whose_decode_contains_a_needle_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("GET /a%2Fsecret%2Fb", ["a/secret/b"]) == "[REDACTED]"


def test_mixed_decoder_layers_beyond_budget_fail_closed():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "harmless"
    for _ in range(40):
        text = text.replace("%", "%25").replace("h", "%68")

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == "[REDACTED]"


def test_short_secret_redacts_an_ordinary_value():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("pw=ab", ["ab"]) == "pw=[REDACTED]"


def test_short_secret_replaces_a_coincidental_encoded_line():
    from builtin_tools.text_utils import redact_vault_secrets

    text = r'diagnostic {"key":"\u0061\n"}'

    assert redact_vault_secrets(text, ["a\n"]) == "[REDACTED]"


def _mixed_encoding_percent_layer(text: str) -> str:
    """Percent-encode every non-alphanumeric run, alternating UTF-8 and Latin-1 per run."""
    import itertools
    import re
    from urllib.parse import quote

    encodings = itertools.cycle(("utf-8", "latin-1"))
    return re.sub(
        r"[^A-Za-z0-9]+",
        lambda m: quote(m.group(), safe="", encoding=next(encodings), errors="replace"),
        text,
    )


def _cr_continuation_layer(text: str) -> str:
    """Quote *text* as a Python literal split by a CRLF or CR line continuation."""
    middle = len(text) // 2
    terminator = "\r\n" if len(text) % 2 else "\r"
    return "'" + text[:middle] + "\\" + terminator + text[middle:] + "'"


def _random_mixed_leaf(rng, secret: str) -> str:
    import json
    from urllib.parse import quote, quote_plus

    layers = (
        repr,
        json.dumps,
        _unicode_layer,
        lambda t: quote(t, safe=""),
        lambda t: quote_plus(t),
        lambda t: quote(t, safe="", encoding="latin-1", errors="replace"),
        lambda t: quote_plus(t, encoding="latin-1", errors="replace"),
        _cr_continuation_layer,
        _mixed_encoding_percent_layer,
    )
    text = f"token={secret}"
    for _ in range(rng.randint(1, 5)):
        text = rng.choice(layers)(text)
    if rng.random() < 0.3:
        text = f"GET /x?{text} HTTP/1.1"
    return text


def test_randomized_python_and_url_layers_never_leave_a_recoverable_credential():
    import random

    from builtin_tools.text_utils import redact_structure, redact_vault_secrets

    rng = random.Random(20261009)
    for case in range(300):
        secret = _random_secret(rng)
        leaf = _random_mixed_leaf(rng, secret)
        value = {"msg": leaf, "nested": [{"deep": leaf}], leaf: 1}

        redacted = redact_structure(value, [secret], preserve_top_level_keys=True)
        artifact = redact_vault_secrets(leaf, [secret])

        texts = [redacted["msg"], redacted["nested"][0]["deep"], artifact]
        texts += [k for k in redacted if k not in value]
        for text in texts:
            assert not _recoverable(secret, text), (case, secret, leaf, text)
        assert redact_structure(redacted, [secret], preserve_top_level_keys=True) == redacted


# ---------------------------------------------------------------------------
# Oracle round 6: malformed named escapes, CR/CRLF continuations, Latin-1 percent
# ---------------------------------------------------------------------------

_MALFORMED_NAME = r"ordinary diagnostic \N{\uD800}"


def _malformed_name_texts():
    import json

    return [
        pytest.param(_MALFORMED_NAME, id="raw"),
        pytest.param(json.dumps(_MALFORMED_NAME), id="json-layered"),
        pytest.param(json.dumps({"msg": _MALFORMED_NAME}), id="json-object"),
    ]


@pytest.mark.parametrize("text", _malformed_name_texts())
def test_malformed_named_escape_does_not_crash_the_redactor(text):
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets(text, ["ab12ba21"]) == text


@pytest.mark.parametrize("text", _malformed_name_texts())
def test_malformed_named_escape_does_not_crash_structure_redaction(text):
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": text, "k": "token=ab12ba21"}, ["ab12ba21"])

    assert redacted == {"msg": text, "k": "token=[REDACTED]"}


def test_credential_hidden_behind_a_malformed_named_escape_is_still_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    text = _MALFORMED_NAME + " token=ab12%22ba21"

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == "[REDACTED]"


@pytest.mark.parametrize("terminator", ["\r\n", "\r", "\n"])
def test_python_line_continuation_of_any_newline_style_cannot_split_a_secret(terminator):
    from builtin_tools.text_utils import redact_vault_secrets

    text = "{'token': 'ab12" + "\\" + terminator + "ba21'}"

    assert redact_vault_secrets(text, ["ab12ba21"]) == "[REDACTED]"


@pytest.mark.parametrize("terminator", ["\r\n", "\r"])
def test_cr_continuation_credential_is_not_recoverable_by_literal_eval(terminator):
    from builtin_tools.text_utils import redact_structure

    text = "{'token': 'ab12" + "\\" + terminator + "ba21'}"
    assert _recoverable("ab12ba21", text)

    redacted = redact_structure({"msg": text}, ["ab12ba21"])

    assert not _recoverable("ab12ba21", redacted["msg"])


def test_honest_text_with_a_trailing_backslash_cr_and_no_needle_is_untouched():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "C:\\dir\\\r\nnext line"

    assert redact_vault_secrets(text, ["ab12ba21"]) == text


_LATIN1_SECRET = "p\u00c3\u00a9ssword"


def test_latin1_percent_encoding_that_is_also_valid_utf8_is_redacted():
    from urllib.parse import quote

    from builtin_tools.text_utils import redact_vault_secrets

    text = "token=" + quote(_LATIN1_SECRET, safe="", encoding="latin-1")
    assert text == "token=p%C3%A9ssword"

    assert redact_vault_secrets(text, [_LATIN1_SECRET]) == "[REDACTED]"


def test_latin1_percent_credential_is_not_recoverable_by_latin1_unquote():
    from urllib.parse import quote

    from builtin_tools.text_utils import redact_structure

    text = "token=" + quote(_LATIN1_SECRET, safe="", encoding="latin-1")
    assert _recoverable(_LATIN1_SECRET, text)

    redacted = redact_structure({"msg": text}, [_LATIN1_SECRET])

    assert not _recoverable(_LATIN1_SECRET, redacted["msg"])


def test_utf8_percent_reading_is_still_redacted_when_latin1_reading_differs():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=p%C3%A9ssword", ["p\u00e9ssword"]) == "[REDACTED]"


def test_latin1_percent_inside_json_inside_repr_is_redacted():
    import json
    from urllib.parse import quote

    from builtin_tools.text_utils import redact_vault_secrets

    inner = "token=" + quote(_LATIN1_SECRET, safe="", encoding="latin-1")
    layered = repr(json.dumps({"q": inner}))

    assert redact_vault_secrets(layered, [_LATIN1_SECRET]) == "[REDACTED]"


def test_latin1_form_encoded_plus_credential_is_redacted():
    from urllib.parse import quote_plus

    from builtin_tools.text_utils import redact_vault_secrets

    secret = "caf\u00c3\u00a9 au lait"
    text = "q=" + quote_plus(secret, encoding="latin-1")

    assert redact_vault_secrets(text, [secret]) == "[REDACTED]"


def test_honest_utf8_ambiguous_percent_url_is_untouched():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "GET /search?q=caf%C3%A9&lang=fr%2Dfr&x=%E4%B8%AD HTTP/1.1"

    assert redact_vault_secrets(text, [_LATIN1_SECRET]) == text


def test_percent_decode_candidates_stay_within_the_state_budget():
    from builtin_tools.text_utils import MAX_DECODE_STATES, _decodes_to_hit

    text = "q=" + "%C3%A9" * 2 + "&r=" + "%E4%B8%AD" * 2
    reached: list[str] = []

    def record(layer: str) -> bool:
        reached.append(layer)
        return False

    assert _decodes_to_hit(text, record) is False
    assert len(set(reached)) <= MAX_DECODE_STATES


# ---------------------------------------------------------------------------
# Oracle round 7: independent per-run percent readings, admission-time state cap
# ---------------------------------------------------------------------------

_SPLIT_SECRET = "prefix\u00e9-\u00c3\u00a9suffix"
_SPLIT_MSG = "token=prefix%C3%A9-%C3%A9suffix"


def _mixed_percent_text(parts: list[str], separator: str = "-") -> str:
    """Encode *parts* alternately as UTF-8 and Latin-1 percent runs joined by *separator*."""
    from urllib.parse import quote

    encodings = ("utf-8", "latin-1")
    return separator.join(
        quote(part, safe="", encoding=encodings[i % 2]) for i, part in enumerate(parts)
    )


def test_split_and_mixed_decode_of_two_ambiguous_runs_is_detected_by_the_oracle_helper():
    assert _recoverable(_SPLIT_SECRET, _SPLIT_MSG)


def test_independent_percent_run_readings_are_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets(_SPLIT_MSG, [_SPLIT_SECRET]) == "[REDACTED]"


def test_independent_percent_run_readings_are_not_recoverable_from_structure():
    from builtin_tools.text_utils import redact_structure

    redacted = redact_structure({"msg": _SPLIT_MSG}, [_SPLIT_SECRET])

    assert not _recoverable(_SPLIT_SECRET, redacted["msg"])


def test_four_alternating_utf8_and_latin1_runs_are_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    parts = ["a\u00e9", "b\u00c3\u00a9", "c\u00e9", "d\u00c3\u00a9"]
    secret = "-".join(parts)
    text = "q=" + _mixed_percent_text(parts)
    assert _recoverable(secret, text)

    assert redact_vault_secrets(text, [secret]) == "[REDACTED]"


def test_mixed_runs_whose_needle_needs_the_plus_as_space_reading_are_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    secret = "a b\u00e9 c\u00c3\u00a9"
    text = "q=a+b%C3%A9+c%C3%A9"
    assert _recoverable(secret, text)

    assert redact_vault_secrets(text, [secret]) == "[REDACTED]"


def test_honest_multilingual_url_with_many_runs_is_untouched_for_an_ascii_secret():
    from builtin_tools.text_utils import redact_vault_secrets

    text = (
        "GET /wiki/%E6%97%A5%E6%9C%AC%E8%AA%9E/caf%C3%A9?a=%C3%B1o%C3%B1o&b=%E4%B8%AD"
        "&c=%D0%9F%D1%80%D0%B8&d=%C3%BC&e=%C3%A4&f=%C3%B6 HTTP/1.1"
    )

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == text


def test_honest_multilingual_url_is_untouched_for_a_non_ascii_secret():
    from builtin_tools.text_utils import redact_vault_secrets

    text = (
        "GET /wiki/%E6%97%A5%E6%9C%AC%E8%AA%9E/caf%C3%A9?a=%C3%B1o%C3%B1o&b=%E4%B8%AD"
        "&c=%D0%9F%D1%80%D0%B8&d=%C3%BC&e=%C3%A4&f=%C3%B6 HTTP/1.1"
    )

    assert redact_vault_secrets(text, [_SPLIT_SECRET]) == text


def test_ambiguous_runs_beyond_the_state_budget_are_replaced_not_passed_through():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "q=" + "-".join(["a%C3%A9"] * 6)

    assert redact_vault_secrets(text, ["Q\u00e9\u00c3\u00a9"]) == "[REDACTED]"


def test_ambiguous_runs_within_the_state_budget_are_passed_through_when_clean():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "q=" + "-".join(["a%C3%A9"] * 4)

    assert redact_vault_secrets(text, ["Q\u00e9\u00c3\u00a9"]) == text


def test_percent_decoding_never_grows_the_text_across_readings():
    from builtin_tools.text_utils import _decodes_to_hit

    text = "q=" + "-".join(["a%C3%A9+b"] * 4)
    lengths: list[int] = []

    def record(layer: str) -> bool:
        lengths.append(len(layer))
        return False

    _decodes_to_hit(text, record)

    assert max(lengths) <= len(text)


def _chain_text(distinct_states: int) -> str:
    """Return a text whose decode search reaches exactly *distinct_states* texts (root included)."""
    text = "%41"
    for _ in range(distinct_states - 2):
        text = text.replace("%", "%25")
    return text


def _states_reached(text: str) -> int:
    from builtin_tools.text_utils import _decodes_to_hit

    layers: set[str] = {text}

    def record(layer: str) -> bool:
        layers.add(layer)
        return False

    _decodes_to_hit(text, record)
    return len(layers)


def test_state_cap_admits_exactly_the_budget_of_distinct_texts(monkeypatch):
    from builtin_tools import text_utils

    monkeypatch.setattr(text_utils, "MAX_DECODE_STATES", 6)
    text = _chain_text(6)
    assert _states_reached(text) == 6

    assert text_utils.redact_vault_secrets(text, ["zzzz"]) == text


def test_state_cap_fails_closed_when_one_more_distinct_text_would_be_admitted(monkeypatch):
    from builtin_tools import text_utils

    monkeypatch.setattr(text_utils, "MAX_DECODE_STATES", 6)

    assert text_utils.redact_vault_secrets(_chain_text(7), ["zzzz"]) == "[REDACTED]"


def _count_construction_and_examination(monkeypatch, text: str) -> tuple[int, int, bool]:
    """Run the search at cap 6; return (distinct candidates constructed, examined, verdict)."""
    from builtin_tools import text_utils

    monkeypatch.setattr(text_utils, "MAX_DECODE_STATES", 6)
    constructed: set[str] = set()
    examined: set[str] = set()
    original = text_utils._decode_candidates

    def counting(current, relevant):
        for candidate in original(current, relevant):
            constructed.add(candidate)
            yield candidate

    def record(layer: str) -> bool:
        examined.add(layer)
        return False

    monkeypatch.setattr(text_utils, "_decode_candidates", counting)
    verdict = text_utils._decodes_to_hit(text, record)
    return len(constructed | {text}), len(examined | {text}), verdict


def test_settling_search_admits_and_examines_exactly_the_budget(monkeypatch):
    constructed, examined, verdict = _count_construction_and_examination(
        monkeypatch, _chain_text(6)
    )

    assert (constructed, examined, verdict) == (6, 6, False)


def test_overflow_candidate_is_constructed_then_discarded_unexamined(monkeypatch):
    constructed, examined, verdict = _count_construction_and_examination(
        monkeypatch, _chain_text(7)
    )

    assert (constructed, examined, verdict) == (7, 6, True)


def test_branching_readings_stop_at_the_budget_with_one_overflow_candidate(monkeypatch):
    constructed, examined, verdict = _count_construction_and_examination(
        monkeypatch, "q=" + "-".join(["a%C3%A9"] * 6)
    )

    assert verdict is True
    assert examined <= 6
    assert constructed <= examined + 1


def test_oracle_thirty_two_state_input_settles_at_exactly_the_budget():
    from builtin_tools.text_utils import MAX_DECODE_STATES, redact_vault_secrets

    text = r"\x25|%252B|\\u0061|\\|%5Cu0025|\\"

    assert _states_reached(text) == MAX_DECODE_STATES
    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == text


def test_oracle_thirty_two_state_input_is_replaced_when_the_budget_is_one_lower(monkeypatch):
    from builtin_tools import text_utils

    monkeypatch.setattr(text_utils, "MAX_DECODE_STATES", text_utils.MAX_DECODE_STATES - 1)
    text = r"\x25|%252B|\\u0061|\\|%5Cu0025|\\"

    assert text_utils.redact_vault_secrets(text, [_ORACLE_X22_SECRET]) == "[REDACTED]"


def test_five_ambiguous_runs_exceed_the_real_budget_and_four_do_not():
    from builtin_tools.text_utils import redact_vault_secrets

    secret = "Q\u00e9\u00c3\u00a9"
    four = "q=" + "-".join(["a%C3%A9"] * 4)
    five = "q=" + "-".join(["a%C3%A9"] * 5)

    assert redact_vault_secrets(four, [secret]) == four
    assert redact_vault_secrets(five, [secret]) == "[REDACTED]"


def test_randomized_independent_percent_run_readings_never_leave_a_recoverable_credential():
    import random

    from builtin_tools.text_utils import redact_structure, redact_vault_secrets

    pieces = ["\u00e9", "\u00c3\u00a9", "-", "ab", "1", "_", "\u00fc", "\u00c3\u00bc"]
    rng = random.Random(20261010)
    for case in range(300):
        secret = "".join(rng.choice(pieces) for _ in range(rng.randint(4, 9))) + "z"
        text = "token=" + secret
        for layer in rng.sample(
            [_mixed_encoding_percent_layer, repr, lambda t: t.replace(" ", "+")], rng.randint(1, 2)
        ):
            text = layer(text)
        leaf = f"GET /x?{text} HTTP/1.1" if rng.random() < 0.3 else text

        redacted = redact_structure({"msg": leaf}, [secret])["msg"]
        artifact = redact_vault_secrets(leaf, [secret])

        for out in (redacted, artifact):
            assert not _recoverable(secret, out), (case, secret, leaf, out)
        assert redact_vault_secrets(redacted, [secret]) == redacted


def test_needle_inside_a_run_that_also_holds_irrelevant_ascii_is_redacted():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("x%3D%E9%C3%BC", ["\u00e9\u00c3\u00bc"]) == "[REDACTED]"


def test_mixed_readings_inside_one_adjacent_percent_run_are_redacted():
    from urllib.parse import quote

    from builtin_tools.text_utils import redact_vault_secrets

    secret = "\u00e9\u00c3\u00a9"
    text = "token=" + quote("\u00e9", safe="") + quote("\u00c3\u00a9", safe="", encoding="latin-1")
    assert text == "token=%C3%A9%C3%A9"
    from urllib.parse import unquote

    assert unquote("%C3%A9") + unquote("%C3%A9", encoding="latin-1") == secret

    assert redact_vault_secrets(text, [secret]) == "[REDACTED]"


# ---------------------------------------------------------------------------
# Oracle round 8: invalid UTF-8 percent bytes and the U+FFFD replacement reading
# ---------------------------------------------------------------------------

_REPLACEMENT_CASES = [
    pytest.param("prefix%C0%AFsuffix", 2, id="overlong-C0AF"),
    pytest.param("prefix%FFsuffix", 1, id="byte-FF"),
    pytest.param("prefix%ED%A0%80suffix", 3, id="surrogate-bytes"),
    pytest.param("prefix%F4%90%80%80suffix", 4, id="out-of-range"),
]


def _replacement_secret(encoded: str) -> str:
    from urllib.parse import unquote

    return unquote(encoded)


@pytest.mark.parametrize(("encoded", "replacements"), _REPLACEMENT_CASES)
def test_default_unquote_replacement_reading_of_invalid_utf8_is_redacted(encoded, replacements):
    from builtin_tools.text_utils import redact_vault_secrets

    secret = _replacement_secret(encoded)
    assert secret == "prefix" + "\ufffd" * replacements + "suffix"
    text = "token=" + encoded

    assert redact_vault_secrets(text, [secret]) == "[REDACTED]"


@pytest.mark.parametrize(("encoded", "replacements"), _REPLACEMENT_CASES)
def test_replacement_reading_credential_is_not_recoverable_from_structure(encoded, replacements):
    from builtin_tools.text_utils import redact_structure

    secret = _replacement_secret(encoded)
    text = "token=" + encoded
    assert _recoverable(secret, text)

    redacted = redact_structure({"msg": text}, [secret])

    assert not _recoverable(secret, redacted["msg"])


def test_malformed_percent_bytes_pass_through_when_no_secret_has_a_replacement_character():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "GET /x?a=%C0%AF&b=%FF&c=%ED%A0%80&d=%F4%90%80%80 HTTP/1.1"

    assert redact_vault_secrets(text, [_ORACLE_X22_SECRET, "p\u00e9ssword"]) == text


def test_invalid_percent_bytes_are_still_read_as_latin1_for_a_latin1_secret():
    from builtin_tools.text_utils import redact_vault_secrets

    assert redact_vault_secrets("token=p%E4ssword", ["p\u00e4ssword"]) == "[REDACTED]"


def test_valid_utf8_percent_text_passes_when_a_secret_has_a_replacement_character():
    from builtin_tools.text_utils import redact_vault_secrets

    text = "GET /wiki/%E6%97%A5%E6%9C%AC/caf%C3%A9 HTTP/1.1"

    assert redact_vault_secrets(text, ["prefix\ufffdsuffix"]) == text


def test_randomized_malformed_utf8_percent_layers_never_leave_a_recoverable_credential():
    import random
    from urllib.parse import quote

    from builtin_tools.text_utils import redact_structure, redact_vault_secrets

    bad = [b"\xc0\xaf", b"\xff", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xe4", b"\x80"]
    rng = random.Random(20261011)
    for case in range(300):
        pieces = [rng.choice(["ab", "-", "9", "\u00e9"]) for _ in range(rng.randint(1, 3))]
        raw = b"".join(
            rng.choice(bad) if rng.random() < 0.5 else p.encode("utf-8") for p in pieces
        )
        encoded = "token=" + quote(raw, safe="")
        secret = "\ufffd".join(["pre", "post"]) if rng.random() < 0.2 else _replacement_secret(encoded)
        if "\ufffd" not in secret and rng.random() < 0.5:
            secret = secret.encode("utf-8", "replace").decode("latin-1")
        leaf = f"GET /x?{encoded} HTTP/1.1" if rng.random() < 0.3 else encoded

        redacted = redact_structure({"msg": leaf}, [secret])["msg"]
        artifact = redact_vault_secrets(leaf, [secret])

        for out in (redacted, artifact):
            assert not _recoverable(secret, out), (case, secret, leaf, out)
