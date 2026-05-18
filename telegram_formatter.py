"""
telegram_formatter.py
---------------------
Pure formatting utilities for Telegram HTML messages.

All functions are stateless and side-effect-free — they accept strings and
return strings. No Telegram API calls, no bot state.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

# ---------------------------------------------------------------------------
# HTML tag sanitisation
# ---------------------------------------------------------------------------

# Telegram HTML only recognises these tags; anything else is rejected.
TELEGRAM_TAGS = frozenset({"b", "i", "s", "u", "code", "pre", "a", "blockquote"})

# Self-contained pattern that matches any opening or closing tag we care about.
_TAG_RE = re.compile(r"<(/?)(\w+)(\s[^>]*)?>", re.DOTALL)


def sanitize_html(text: str) -> str:
    """Ensure all Telegram-HTML tags are properly balanced.

    Walks *text* character-by-character via a regex tag scanner and:
    - keeps every opening tag in ``TELEGRAM_TAGS``, pushing it onto a stack
    - keeps every closing tag only when it matches the current top of the stack
      (drops unmatched / misnested close tags instead of forwarding them)
    - after the full string is consumed, appends synthetic close tags for any
      tags that were opened but never closed (in reverse order)

    Tags outside ``TELEGRAM_TAGS`` (e.g. ``<div>``) are passed through
    unchanged because they were either already HTML-escaped prose that slipped
    through, or placeholders — altering them would corrupt code blocks.

    Inputs that are already valid pass through with zero mutations.

    Examples::

        >>> sanitize_html("<b>hello</b>")
        '<b>hello</b>'
        >>> sanitize_html("<b>unclosed")
        '<b>unclosed</b>'
        >>> sanitize_html("foo <b>bar</b> <i>baz")
        'foo <b>bar</b> <i>baz</i>'
        >>> sanitize_html("<b><i>ok</i></b>")
        '<b><i>ok</i></b>'
    """
    stack: list[str] = []
    result: list[str] = []
    pos = 0

    for m in _TAG_RE.finditer(text):
        # Append everything between previous match end and this tag
        result.append(text[pos:m.start()])
        pos = m.end()

        is_close = bool(m.group(1))
        tag = m.group(2).lower()
        attrs = m.group(3) or ""

        if tag not in TELEGRAM_TAGS:
            # Not a Telegram formatting tag — pass through verbatim
            result.append(m.group(0))
            continue

        if not is_close:
            stack.append(tag)
            result.append(f"<{tag}{attrs}>")
        else:
            if stack and stack[-1] == tag:
                stack.pop()
                result.append(f"</{tag}>")
            # else: drop the unmatched / misnested close tag

    # Append any trailing text after the last tag
    result.append(text[pos:])

    # Close any still-open tags (innermost first)
    for tag in reversed(stack):
        result.append(f"</{tag}>")

    return "".join(result)


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

# Maximum extra chars sanitize_html may append (one </tag> per tracked tag)
_MAX_TAG_OVERHEAD = 48


def split_message(text: str, limit: int = 4000) -> list[str]:
    """
    Split text into chunks of at most `limit` characters.

    Tries to split at paragraph boundaries (\\n\\n), then line boundaries (\\n),
    then word boundaries, to avoid cutting mid-sentence or mid-HTML-tag.

    Each chunk is passed through ``sanitize_html`` to close any HTML tags that
    were opened in the chunk but not yet closed (e.g. ``<b>`` split across a
    chunk boundary), preventing Telegram API "can't parse entities" errors.

    ``sanitize_html`` can append synthetic close tags after the split point,
    inflating the chunk length.  To guarantee the final chunk never exceeds
    ``limit``, we split against ``effective`` = ``limit`` minus the worst-case
    close-tag overhead (all 8 tracked tags open at once: ~46 chars → 48 buffer).
    """
    effective = limit - _MAX_TAG_OVERHEAD

    if len(text) <= effective:
        return [sanitize_html(text)]

    parts: list[str] = []
    while len(text) > effective:
        chunk = text[:effective]
        # Try to split at a paragraph break
        split_at = chunk.rfind("\n\n")
        if split_at > effective // 2:
            parts.append(sanitize_html(text[:split_at].rstrip()))
            text = text[split_at:].lstrip("\n")
            continue
        # Try to split at a line break
        split_at = chunk.rfind("\n")
        if split_at > effective // 2:
            parts.append(sanitize_html(text[:split_at].rstrip()))
            text = text[split_at:].lstrip("\n")
            continue
        # Try to split at a word boundary
        split_at = chunk.rfind(" ")
        if split_at > effective // 2:
            parts.append(sanitize_html(text[:split_at].rstrip()))
            text = text[split_at:].lstrip(" ")
            continue
        # Hard split — no good boundary found
        parts.append(sanitize_html(text[:effective]))
        text = text[effective:]

    if text:
        parts.append(sanitize_html(text))
    return parts


# ---------------------------------------------------------------------------
# Jobs list formatting
# ---------------------------------------------------------------------------

def format_jobs_list(jobs: list) -> str:
    """Render a list of job dicts (from scheduler.list_jobs()) as HTML."""
    if not jobs:
        return "No scheduled jobs configured."
    lines = [f"📅 <b>Scheduled Jobs</b> ({len(jobs)} total)\n"]
    for job in jobs:
        is_running = job.get("is_running", False)
        if is_running:
            icon = "🔄"
        elif job["enabled"]:
            icon = "✅"
        else:
            icon = "⏸"
        last_run = job.get("last_run") or "never"
        next_run = job.get("next_run")
        stype = job.get("schedule_type", "cron")
        task_label = "🔔 Message" if stype == "once" else "📝 Task"
        task_text = job.get("task", "")
        task_display = html.escape(task_text[:300] + ("…" if len(task_text) > 300 else ""))
        tag_line = f"{icon} <code>{html.escape(job['tag'])}</code>"
        if is_running:
            tag_line += " <i>[running]</i>"
        lines.append(tag_line)
        lines.append(f"   Schedule: {html.escape(job['schedule'])}")
        lines.append(f"   Last run: {html.escape(str(last_run))}")
        if next_run:
            try:
                nr = datetime.fromisoformat(next_run).strftime("%Y-%m-%d %H:%M")
            except Exception:
                nr = next_run
            lines.append(f"   Next run: {html.escape(nr)}")
        if job.get("model"):
            lines.append(f"   Model: <code>{html.escape(job['model'])}</code>")
        if job.get("fallback_models"):
            fb_str = ", ".join(f"<code>{html.escape(m)}</code>" for m in job["fallback_models"])
            lines.append(f"   Fallbacks: {fb_str}")
        elif job.get("fallback_models") == []:
            lines.append("   Fallbacks: <i>disabled</i>")
        if job.get("preserve_context"):
            lines.append("   🧠 Context: preserved between runs")
        if job.get("last_error"):
            lines.append(f"   ⚠️ Last error: {html.escape(str(job['last_error'])[:120])}")
        lines.append(f"   {task_label}: {task_display}\n")
    lines.append(
        "<i>Tip: /jobs reload · /jobs remove &lt;tag&gt; · /jobs pause &lt;tag&gt; · /jobs resume &lt;tag&gt;</i>"
    )
    return "\n".join(lines)
