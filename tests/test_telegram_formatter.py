"""Tests for telegram_formatter.py — HTML sanitization, message splitting, md_to_html."""

from __future__ import annotations

import html as _html
import re as _re

import pytest

from telegram_formatter import (
    ResultNotice,
    bound_display_text,
    details_callback_data,
    ensure_notice_fits,
    format_jobs_list,
    format_result_document,
    format_result_notice,
    md_to_html,
    rendered_length,
    sanitize_html,
    split_message,
)


class TestSanitizeHtml:
    """HTML tag balancing tests."""

    def test_already_valid(self):
        assert sanitize_html("<b>hello</b>") == "<b>hello</b>"

    def test_unclosed_single_tag(self):
        assert sanitize_html("<b>unclosed") == "<b>unclosed</b>"

    def test_multiple_unclosed(self):
        result = sanitize_html("<b><i>nested")
        assert result == "<b><i>nested</i></b>"

    def test_mixed_closed_and_open(self):
        assert sanitize_html("foo <b>bar</b> <i>baz") == "foo <b>bar</b> <i>baz</i>"

    def test_properly_nested(self):
        assert sanitize_html("<b><i>ok</i></b>") == "<b><i>ok</i></b>"

    def test_misnested_close_tag_dropped(self):
        # </i> closes before <b> was closed — mismatched
        result = sanitize_html("<b><i>text</b></i>")
        # </b> doesn't match top of stack (<i>), so it's dropped
        # </i> then matches, and </b> is appended at end
        assert "</i>" in result
        assert result.endswith("</b>")

    def test_unknown_tags_passed_through(self):
        # <div> is not a Telegram tag — passed verbatim
        assert sanitize_html("<div>test</div>") == "<div>test</div>"

    def test_tag_with_attributes(self):
        result = sanitize_html('<a href="http://example.com">link')
        assert result == '<a href="http://example.com">link</a>'

    def test_empty_string(self):
        assert sanitize_html("") == ""

    def test_no_tags(self):
        assert sanitize_html("plain text & stuff") == "plain text & stuff"

    def test_code_block(self):
        result = sanitize_html("<pre>code\nhere</pre>")
        assert result == "<pre>code\nhere</pre>"

    def test_unclosed_pre_and_code(self):
        result = sanitize_html("<pre><code>some code")
        assert result == "<pre><code>some code</code></pre>"

    def test_blockquote_unclosed(self):
        result = sanitize_html("<blockquote>quoted text")
        assert result == "<blockquote>quoted text</blockquote>"

    def test_special_chars_in_text(self):
        # Special chars between tags should be preserved as-is
        result = sanitize_html("<b>hello &amp; world</b>")
        assert result == "<b>hello &amp; world</b>"

    def test_self_closing_like_syntax_ignored(self):
        # <br/> is not a Telegram tag — should pass through
        result = sanitize_html("line1<br/>line2")
        assert "line1" in result and "line2" in result

    def test_strikethrough_tag(self):
        assert sanitize_html("<s>deleted") == "<s>deleted</s>"

    def test_underline_tag(self):
        assert sanitize_html("<u>underlined") == "<u>underlined</u>"

    def test_deeply_nested(self):
        result = sanitize_html("<b><i><u><s>deep")
        assert result == "<b><i><u><s>deep</s></u></i></b>"

    def test_extra_close_tags_removed(self):
        # Extra </b> when stack is empty — should be dropped
        result = sanitize_html("text</b>more")
        assert result == "textmore"

    def test_interleaved_tags(self):
        # <b>hello<i>world</b></i> — </b> doesn't match top (<i>)
        result = sanitize_html("<b>hello<i>world</b></i>")
        # </b> is dropped (doesn't match <i>); </i> matches; </b> appended
        assert result.endswith("</b>")


class TestSplitMessage:
    """Message splitting tests."""

    def test_short_message_not_split(self):
        parts = split_message("short")
        assert len(parts) == 1
        assert parts[0] == "short"

    def test_split_at_paragraph(self):
        text = "A" * 2000 + "\n\n" + "B" * 2000
        parts = split_message(text, limit=4000)
        assert len(parts) == 2

    def test_split_at_newline(self):
        text = "A" * 2000 + "\n" + "B" * 2000
        parts = split_message(text, limit=4000)
        assert len(parts) == 2

    def test_hard_split_no_boundaries(self):
        text = "A" * 8000  # no newlines, no spaces
        parts = split_message(text, limit=4000)
        assert len(parts) >= 2
        for part in parts:
            assert len(part) <= 4000

    def test_html_tags_balanced_after_split(self):
        text = "<b>" + "x" * 5000 + "</b>"
        parts = split_message(text, limit=4000)
        assert len(parts) >= 2
        # First part should have <b> closed
        assert parts[0].count("</b>") >= 1 or parts[0].endswith("</b>")

    def test_split_respects_limit(self):
        text = ("hello world " * 500).strip()
        parts = split_message(text, limit=200)
        for part in parts:
            assert len(part) <= 200

    def test_empty_string(self):
        parts = split_message("")
        assert parts == [""]

    def test_sanitization_applied(self):
        text = "<b>unclosed"
        parts = split_message(text)
        assert parts == ["<b>unclosed</b>"]


class TestFormatJobsList:
    """Jobs list formatting tests."""

    def test_empty_list(self):
        assert format_jobs_list([]) == "No scheduled jobs configured."

    def test_single_job(self):
        jobs = [{
            "tag": "health",
            "enabled": True,
            "schedule": "0 */4 * * *",
            "task": "Check health",
            "schedule_type": "cron",
        }]
        result = format_jobs_list(jobs)
        assert "health" in result
        assert "1 total" in result
        assert "✅" in result

    def test_disabled_job(self):
        jobs = [{"tag": "x", "enabled": False, "schedule": "daily", "task": "t", "schedule_type": "cron"}]
        result = format_jobs_list(jobs)
        assert "⏸" in result

    def test_running_job(self):
        jobs = [{"tag": "x", "enabled": True, "is_running": True, "schedule": "hourly", "task": "t", "schedule_type": "cron"}]
        result = format_jobs_list(jobs)
        assert "🔄" in result
        assert "[running]" in result


class TestMdToHtml:
    """Markdown to Telegram HTML conversion."""

    def test_bold(self):
        assert "<b>hello</b>" in md_to_html("**hello**")

    def test_italic_star(self):
        assert "<i>world</i>" in md_to_html("*world*")

    def test_italic_underscore(self):
        assert "<i>text</i>" in md_to_html("_text_")

    def test_strikethrough(self):
        assert "<s>old</s>" in md_to_html("~~old~~")

    def test_inline_code(self):
        assert "<code>foo</code>" in md_to_html("`foo`")

    def test_fenced_code_block(self):
        result = md_to_html("```python\nprint('hi')\n```")
        assert "<pre><code" in result
        assert "print(&#x27;hi&#x27;)" in result

    def test_html_entities_escaped(self):
        result = md_to_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_markdown_link(self):
        result = md_to_html("[click](https://example.com)")
        assert '<a href="https://example.com">click</a>' in result

    def test_bare_url(self):
        result = md_to_html("Visit https://example.com today")
        assert '<a href="https://example.com">' in result

    def test_snake_case_not_italicized(self):
        result = md_to_html("use some_function_name here")
        assert "<i>" not in result

    def test_dunder_not_bolded(self):
        result = md_to_html("call __init__ method")
        # __text__ is intentionally not converted to bold
        assert "<b>" not in result

    def test_code_block_preserves_content(self):
        result = md_to_html("```\nif x < 3 && y > 0:\n```")
        assert "&lt;" in result
        assert "&amp;&amp;" in result


# ---------------------------------------------------------------------------
# Scheduled result presentation: expandable, compact, and document shapes
# ---------------------------------------------------------------------------

_RUN_ID = "run-" + "ab12cd34" * 4
_SUMMARY = "<b>✅ Scheduled job finished: nightly</b>"


class TestRenderedLength:
    def test_tags_are_not_counted(self):
        assert rendered_length("<b>hi</b>") == 2

    def test_entities_count_as_one_character(self):
        assert rendered_length("a &lt; b &amp; c") == len("a < b & c")

    def test_blockquote_expandable_tag_is_not_counted(self):
        assert rendered_length("<blockquote expandable>xy</blockquote>") == 2


class TestFormatResultNoticeShort:
    def test_summary_is_outside_expandable_quote_containing_result(self):
        notice = format_result_notice(_SUMMARY, "all good", _RUN_ID)
        assert notice.text.startswith(_SUMMARY)
        match = _re.search(
            r"<blockquote expandable>(.*)</blockquote>", notice.text, _re.DOTALL,
        )
        assert match is not None
        assert match.group(1) == "all good"
        assert _SUMMARY not in match.group(1)

    def test_short_result_offers_no_details(self):
        notice = format_result_notice(_SUMMARY, "all good", _RUN_ID)
        assert notice.details_run_id == ""

    def test_untrusted_result_html_is_escaped(self):
        notice = format_result_notice(_SUMMARY, "<script>x</script> & <b>y", _RUN_ID)
        assert "<script>" not in notice.text
        assert "&lt;script&gt;" in notice.text
        assert "&amp;" in notice.text

    def test_empty_result_has_no_quote(self):
        notice = format_result_notice(_SUMMARY, "", _RUN_ID)
        assert notice.text == _SUMMARY
        assert "blockquote" not in notice.text

    def test_result_exactly_at_limit_stays_expandable(self):
        room = 4096 - rendered_length(_SUMMARY + "\n\n")
        notice = format_result_notice(_SUMMARY, "x" * room, _RUN_ID)
        assert rendered_length(notice.text) == 4096
        assert "<blockquote expandable>" in notice.text
        assert notice.details_run_id == ""


class TestFormatResultNoticeOversized:
    def test_one_character_over_limit_becomes_compact(self):
        room = 4096 - rendered_length(_SUMMARY + "\n\n")
        notice = format_result_notice(_SUMMARY, "x" * (room + 1), _RUN_ID)
        assert "blockquote" not in notice.text
        assert rendered_length(notice.text) < 1000

    def test_compact_notice_names_execution_id_and_offers_details(self):
        notice = format_result_notice(_SUMMARY, "y" * 10_000, _RUN_ID)
        assert _SUMMARY in notice.text
        assert _RUN_ID in notice.text
        assert "/jobs result" in notice.text
        assert notice.details_run_id == _RUN_ID
        assert "y" * 100 not in notice.text

    def test_escaped_html_does_not_widen_the_allowance(self):
        # 3,000 "&" render as 3,000 chars (fits) even though raw HTML is 15,000.
        fits = format_result_notice(_SUMMARY, "&" * 3000, _RUN_ID)
        assert "<blockquote expandable>" in fits.text
        assert len(fits.text) > 4096
        assert rendered_length(fits.text) <= 4096
        # 4,200 "&" exceed the rendered limit and must go compact.
        over = format_result_notice(_SUMMARY, "&" * 4200, _RUN_ID)
        assert "blockquote" not in over.text
        assert over.details_run_id == _RUN_ID

    def test_header_counts_toward_the_limit(self):
        header = "<b>" + "H" * 500 + "</b>"
        notice = format_result_notice(header, "z" * 3800, _RUN_ID)
        assert "blockquote" not in notice.text

    def test_multibyte_text_is_measured_in_characters(self):
        notice = format_result_notice(_SUMMARY, "日本語" * 1000, _RUN_ID)
        assert "<blockquote expandable>" in notice.text

    def test_oversized_without_run_id_states_unavailable_and_offers_no_details(self):
        notice = format_result_notice(_SUMMARY, "y" * 10_000, "")
        assert notice.details_run_id == ""
        assert "blockquote" not in notice.text
        assert "not available" in notice.text.lower()


class TestBoundDisplayText:
    def test_short_text_is_unchanged(self):
        assert bound_display_text("nightly", 100) == "nightly"

    def test_long_text_is_ellipsized_to_the_limit(self):
        bounded = bound_display_text("t" * 4000, 100)
        assert len(bounded) == 100
        assert bounded.endswith("…")


class TestEnsureNoticeFits:
    def test_fitting_notice_is_returned_unchanged(self):
        notice = ResultNotice("<b>ok</b>")
        assert ensure_notice_fits(notice) is notice

    def test_oversized_notice_is_replaced_by_a_fitting_fallback(self):
        fallback = ensure_notice_fits(ResultNotice("<b>" + "x" * 5000 + "</b>"))
        assert rendered_length(fallback.text) <= 4096

    def test_oversized_notice_keeps_its_details_execution(self):
        fallback = ensure_notice_fits(
            ResultNotice("x" * 5000, details_run_id=_RUN_ID)
        )
        assert fallback.details_run_id == _RUN_ID
        assert _RUN_ID in fallback.text


class TestFormatResultDocument:
    def test_document_is_utf8_bytes_with_complete_result(self):
        result = ("日本語 ünïcode 🚀\n" + "line\n" * 5000)
        record = {
            "run_id": _RUN_ID, "tag": "nightly", "status": "done",
            "ts": "2026-10-04T01:00:00Z", "result": result,
        }
        data = format_result_document(record)
        assert isinstance(data, bytes)
        text = data.decode("utf-8")
        assert text.endswith(result)
        assert _RUN_ID in text
        assert "nightly" in text
        assert "done" in text

    def test_document_does_not_html_escape_the_result(self):
        record = {
            "run_id": _RUN_ID, "tag": "t", "status": "failed",
            "ts": "2026-10-04T01:00:00Z", "result": "<b>a</b> & b",
        }
        text = format_result_document(record).decode("utf-8")
        assert text.endswith("<b>a</b> & b")
        assert _html.escape("<b>") not in text

    def test_document_is_not_truncated_beyond_message_limit(self):
        record = {
            "run_id": _RUN_ID, "tag": "t", "status": "done",
            "ts": "2026-10-04T01:00:00Z", "result": "q" * 200_000,
        }
        text = format_result_document(record).decode("utf-8")
        assert text.count("q") == 200_000


class TestDetailsCallbackData:
    def test_round_trips_run_id_within_telegram_limit(self):
        data = details_callback_data(_RUN_ID)
        assert data.endswith(_RUN_ID)
        assert len(data.encode("utf-8")) <= 64

    def test_overlong_run_id_is_rejected(self):
        with pytest.raises(ValueError):
            details_callback_data("run-" + "f" * 80)


def _job_with_model(**model: object) -> dict:
    """One enabled cron job whose ``model`` entry is *model* (empty = absent)."""
    return {"tag": "nightly", "enabled": True, "schedule": "daily", "task": "t",
            "schedule_type": "cron", **model}


class TestFormatJobsListSelectorEvidence:
    """Saved selectors render through the bounded pre-pin representation (12.22)."""

    @pytest.mark.parametrize(
        "model",
        [123, 0, ["QZXlist"], {"QZXkey": "QZXvalue"}],
        ids=["int", "zero", "list", "dict"],
    )
    def test_non_string_model_renders_label_without_contents(self, model):
        result = format_jobs_list([_job_with_model(model=model)])

        assert "malformed (not a string)" in result
        assert "QZX" not in result
        assert "Model: <code>123</code>" not in result

    def test_string_model_renders_escaped_string(self):
        result = format_jobs_list([_job_with_model(model="p/<model>")])

        assert "Model: <code>p/&lt;model&gt;</code>" in result

    def test_absent_model_renders_inherit(self):
        result = format_jobs_list([_job_with_model()])

        assert "inherit" in result


_TG_SECRET = "QZXSENTINEL7f3aJOBS-token"


def _tg_redactor(text: str) -> str:
    """Fake vault redaction with a placeholder that needs HTML escaping."""
    return text.replace(_TG_SECRET, "<REDACTED>")


class TestFormatJobsListRedactor:
    """``/jobs`` passes the scheduler's redactor into the evidence helper (12.22 F1)."""

    def test_redactor_masks_secret_string_model(self):
        result = format_jobs_list(
            [_job_with_model(model=f"p/{_TG_SECRET}")], redactor=_tg_redactor,
        )

        assert "Model: <code>p/&lt;REDACTED&gt;</code>" in result
        assert _TG_SECRET[:8] not in result

    def test_without_redactor_string_model_is_unredacted(self):
        result = format_jobs_list([_job_with_model(model=f"p/{_TG_SECRET}")])

        assert f"Model: <code>p/{_TG_SECRET}</code>" in result

    def test_redactor_keeps_label_and_inherit_rendering(self):
        result = format_jobs_list(
            [_job_with_model(model=123), _job_with_model()], redactor=_tg_redactor,
        )

        assert "malformed (not a string)" in result
        assert "inherit" in result
