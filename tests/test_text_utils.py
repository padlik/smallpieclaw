"""Unit tests for ``builtin_tools/text_utils.py`` helpers.

Covers truncation and the shared vault-secret redaction core.
"""

from __future__ import annotations

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
