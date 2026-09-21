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
