"""Tests for telegram_formatter.py — HTML sanitization, message splitting, md_to_html."""

from __future__ import annotations

from telegram_formatter import format_jobs_list, md_to_html, sanitize_html, split_message


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
