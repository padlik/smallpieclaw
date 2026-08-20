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
        if job.get("preserve_context"):
            lines.append("   🧠 Context: preserved between runs")
        if job.get("last_error"):
            lines.append(f"   ⚠️ Last error: {html.escape(str(job['last_error'])[:120])}")
        lines.append(f"   {task_label}: {task_display}\n")
    lines.append(
        "<i>Tip: /jobs reload · /jobs remove &lt;tag&gt; · /jobs pause &lt;tag&gt; · /jobs resume &lt;tag&gt;</i>"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML converter
# ---------------------------------------------------------------------------

def md_to_html(text: str) -> str:
    """
    Convert a Markdown-flavoured string to Telegram HTML (ParseMode.HTML).

    Handles:
      - Fenced code blocks  ```lang\\ncode\\n```  →  <pre><code>…</code></pre>
      - Inline code         `code`                →  <code>…</code>
      - Bold                **text**              →  <b>…</b>
      - Italic              *text*  or _text_     →  <i>…</i>
      - Strikethrough       ~~text~~              →  <s>…</s>
      - Markdown links      [text](url)           →  <a href="url">text</a>
      - Bare URLs           https://…             →  <a href="url">url</a>

    All prose is HTML-escaped so that <, >, & never break the parser.
    Code block contents are also HTML-escaped so that shell/Python snippets
    with <, >, & display correctly inside <pre><code>.
    URLs are extracted before HTML-escaping so that underscores and ampersands
    in query parameters are never misinterpreted as italic/bold markers.
    """
    # ---- Step 1: extract fenced code blocks to protect them ----
    placeholders: list[str] = []

    def _extract_fence(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = html.escape(m.group(2))
        lang_attr = f' class="language-{html.escape(lang)}"' if lang else ""
        block = f"<pre><code{lang_attr}>{code}</code></pre>"
        placeholders.append(block)
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _extract_fence, text, flags=re.DOTALL)

    # ---- Step 2: extract inline code spans ----
    def _extract_inline(m: re.Match) -> str:
        code = html.escape(m.group(1))
        placeholders.append(f"<code>{code}</code>")
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _extract_inline, text)

    # ---- Step 2.5: extract URLs before html.escape / markdown processing ----
    def _extract_md_link(m: re.Match) -> str:
        label = html.escape(m.group(1))
        esc_url = html.escape(m.group(2))
        placeholders.append(f'<a href="{esc_url}">{label}</a>')
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)', _extract_md_link, text)

    # Bare URLs: wrap in <a> so underscores/& in query params are never
    # touched by the italic regex.
    def _extract_bare_url(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:!?)'\"")
        esc_url = html.escape(url)
        placeholders.append(f'<a href="{esc_url}">{esc_url}</a>')
        tail = m.group(0)[len(url):]
        return f"\x00BLOCK{len(placeholders) - 1}\x00{tail}"

    text = re.sub(r'https?://[^\s<>"\'`\x00]+', _extract_bare_url, text)

    # ---- Step 3: HTML-escape the remaining prose ----
    text = html.escape(text)

    # ---- Step 4: apply inline formatting to prose ----
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"\*(?!\*)(.+?)(?<!\*)\*", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # ---- Step 5: reinsert extracted blocks ----
    for i, block in enumerate(placeholders):
        text = text.replace(f"\x00BLOCK{i}\x00", block)

    return sanitize_html(text)
