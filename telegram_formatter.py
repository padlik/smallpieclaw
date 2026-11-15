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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from model_registry import bounded_selector_evidence

if TYPE_CHECKING:
    from model_registry import CatalogView, ModelRecord, ProviderState

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

def format_jobs_list(jobs: list, redactor: Callable[[str], str] | None = None) -> str:
    """Render a list of job dicts (from scheduler.list_jobs()) as HTML.

    *redactor*, when given, masks secrets in saved model selectors before
    they are bounded and HTML-escaped.
    """
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
        lines.append(
            "   Model: <code>"
            f"{html.escape(bounded_selector_evidence(job.get('model'), redact=redactor))}</code>"
        )
        if job.get("preserve_context"):
            lines.append("   🧠 Context: preserved between runs")
        if job.get("last_error"):
            lines.append(f"   ⚠️ Last error: {html.escape(str(job['last_error'])[:120])}")
        if job.get("config_error"):
            error = str(job["config_error"])
            if redactor is not None:
                error = redactor(error)
            lines.append(f"   ⚠️ config_error: {html.escape(error[:400])}")
        lines.append(f"   {task_label}: {task_display}\n")
    lines.append(
        "<i>Tip: /jobs reload · /jobs remove &lt;tag&gt; · /jobs pause &lt;tag&gt; · /jobs resume &lt;tag&gt;\n"
        "Read-only: /jobs history &lt;tag&gt; · /jobs result &lt;execution-id&gt;</i>"
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


# ---------------------------------------------------------------------------
# Scheduled run-result presentation
# ---------------------------------------------------------------------------

#: Telegram's limit on a message's text *after* entity parsing.
TELEGRAM_RENDERED_LIMIT = 4096

#: Telegram's limit on inline-button ``callback_data``, in bytes.
CALLBACK_DATA_LIMIT = 64

#: Prefix of the callback data carried by a result notice's Details button.
DETAILS_CALLBACK_PREFIX = "jobdetails:"

_ANY_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class ResultNotice:
    """A formatted run-result notice.

    ``details_run_id`` is non-empty only when the full result is held behind a
    Details control (the notice itself is compact); it is the retained
    execution ID the control must resolve.
    """

    text: str
    details_run_id: str = ""


def rendered_length(html_text: str) -> int:
    """Return the length Telegram counts for ``html_text`` after entity parsing."""
    # Counts code points; Telegram may count UTF-16 units, so non-BMP characters
    # (e.g. emoji) can weigh more there. Fit checks are kept on code points.
    return len(html.unescape(_ANY_TAG_RE.sub("", html_text)))


def bound_display_text(text: str, limit: int = 100) -> str:
    """Return ``text`` ellipsized to at most ``limit`` characters (for summary fields)."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def ensure_notice_fits(notice: ResultNotice) -> ResultNotice:
    """Return ``notice`` if it fits Telegram's rendered limit, else a short fallback.

    The fallback keeps the Details execution (if any) so the retained result
    stays reachable.
    """
    if rendered_length(notice.text) <= TELEGRAM_RENDERED_LIMIT:
        return notice
    if not notice.details_run_id:
        return ResultNotice(
            "⚠️ A scheduled job notice was too long to display; "
            "see job history for the outcome."
        )
    safe_id = html.escape(notice.details_run_id)
    return ResultNotice(
        "⚠️ A scheduled job notice was too long to display.\n"
        f"Execution ID: <code>{safe_id}</code>\n"
        f"Tap Details or send <code>/jobs result {safe_id}</code> to get it.",
        details_run_id=notice.details_run_id,
    )


def format_result_notice(summary_html: str, result: str, run_id: str = "") -> ResultNotice:
    """Format a run result as an expandable quote, or a compact notice if too long.

    ``summary_html`` is trusted Telegram HTML (the caller escapes its parts);
    ``result`` is untrusted plain text and is always escaped.  The summary stays
    outside an ``<blockquote expandable>`` holding the result.  Fit is decided
    on the *rendered* length, so HTML escaping never widens the allowance.  When
    the message would exceed Telegram's limit, only the summary and the
    execution ID are shown; the complete result stays in retained history.
    """
    if not result:
        return ResultNotice(summary_html)
    expandable = (
        f"{summary_html}\n\n"
        f"<blockquote expandable>{html.escape(result)}</blockquote>"
    )
    if rendered_length(expandable) <= TELEGRAM_RENDERED_LIMIT:
        return ResultNotice(expandable)
    size = f"{len(result):,} characters"
    if not run_id:
        return ResultNotice(
            f"{summary_html}\n\nThe result ({size}) is too long to display and "
            "is not available in job history."
        )
    safe_id = html.escape(run_id)
    return ResultNotice(
        f"{summary_html}\n\nThe result ({size}) is too long to display here.\n"
        f"Execution ID: <code>{safe_id}</code>\n"
        f"Tap Details or send <code>/jobs result {safe_id}</code> to get it.",
        details_run_id=run_id,
    )


def run_result_summary(tag: str, status: str, error_type: str, next_run: str) -> str:
    """Return the Telegram-HTML outcome summary for a scheduled run, with the tag bounded."""
    safe_tag = html.escape(bound_display_text(tag))
    if status == "failed":
        return (
            f"<b>⚠️ Scheduled job failed: {safe_tag}</b>\n"
            f"Error type: {html.escape(error_type)}\n"
            f"Next run: {html.escape(next_run)}"
        )
    if status == "cancelled":
        return f"<b>🛑 Scheduled job cancelled: {safe_tag}</b>"
    return f"<b>✅ Scheduled job finished: {safe_tag}</b>"


def build_run_result_notice(
    tag: str, status: str, result: str, error_type: str, next_run: str, run_id: str,
) -> ResultNotice:
    """Build the run-result notice for a scheduled run's terminal outcome.

    The summary stays outside an expandable quote holding the result; a result
    too long for one message is replaced by the execution ID and a Details button.
    """
    summary = run_result_summary(tag, status, error_type, next_run)
    if status == "cancelled":
        return ResultNotice(summary)
    return format_result_notice(summary, result, run_id)


def build_unretained_notice(
    tag: str, status: str, error_type: str, next_run: str,
) -> ResultNotice:
    """Build the notice for a run whose outcome could not be saved to job history.

    It carries the real outcome summary plus an honest note that the result was
    not retained; it has no Details control because no record exists.
    """
    summary = run_result_summary(tag, status, error_type, next_run)
    return ResultNotice(
        f"{summary}\n\nThe result was not retained in job history. "
        "Details are unavailable."
    )


def format_result_document(record: dict) -> bytes:
    """Render a retained execution record as a complete UTF-8 text document.

    The result is appended verbatim after a short plain-text header; it is
    neither escaped nor truncated.
    """
    header = (
        f"Execution ID: {record.get('run_id', '')}\n"
        f"Job: {record.get('tag', '')}\n"
        f"Status: {record.get('status', '')}\n"
        f"Finished: {record.get('ts', '')}\n\n"
    )
    return (header + (record.get("result") or "")).encode("utf-8")


def details_callback_data(run_id: str) -> str:
    """Return Details-button callback data for ``run_id``.

    Raises ``ValueError`` if the data would exceed Telegram's 64-byte limit.
    """
    data = f"{DETAILS_CALLBACK_PREFIX}{run_id}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(
            f"Details callback data for {run_id!r} exceeds {CALLBACK_DATA_LIMIT} bytes"
        )
    return data


# ---------------------------------------------------------------------------
# Provider model catalog (read-only) — task 8.1
# ---------------------------------------------------------------------------

#: Callback data for the /models refresh button. Fixed and opaque: it never
#: carries a model identity, so it needs no expiry/revalidation (task 8.2).
MODELS_REFRESH_CALLBACK = "models_refresh"

#: Entries actually rendered before the list is capped with an overflow note.
_MODELS_DISPLAY_CAP = 20

#: Per-field text bound applied before HTML-escaping, so one long provider or
#: model value can never push the whole message over Telegram's limit.
_CATALOG_FIELD_BOUND = 200

_SELECTION_STATUS_LABELS = {
    "active": "active",
    "no_selection": "no selection",
    "saved_selection_unavailable": "saved selection unavailable",
}


def _bound_catalog_text(value: str, limit: int = _CATALOG_FIELD_BOUND) -> str:
    """Bound raw text before escaping so truncation never splits an entity."""
    if len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _capability_label(value: bool | None) -> str:
    """Tri-state label: never conflate "unsupported" with "not yet known"."""
    if value is True:
        return "known-yes"
    if value is False:
        return "known-no"
    return "unknown"


def _catalog_field_line(field_name: str, value_text: str) -> str:
    """One verbose "<field>: <value>" line of provider-reported display metadata."""
    return f"  {html.escape(field_name)}: {html.escape(value_text)}"


def _format_catalog_model(record: "ModelRecord", active_model_id: str | None) -> list[str]:
    """One multi-line, fully-labeled block — used while it still fits the budget."""
    active_mark = "✅" if record.id == active_model_id else "⬜"
    metadata = record.metadata
    return [
        f"{active_mark} <code>{html.escape(_bound_catalog_text(record.id))}</code> — "
        f"{html.escape(_bound_catalog_text(record.display_name))}",
        _catalog_field_line(
            "context_tokens",
            "unknown" if metadata.context_tokens is None else str(metadata.context_tokens),
        ),
        _catalog_field_line(
            "input_tokens",
            "unknown" if metadata.input_tokens is None else str(metadata.input_tokens),
        ),
        _catalog_field_line(
            "output_tokens",
            "unknown" if metadata.output_tokens is None else str(metadata.output_tokens),
        ),
        _catalog_field_line("vision", _capability_label(metadata.vision)),
        _catalog_field_line("tool_calling", _capability_label(metadata.tool_calling)),
        _catalog_field_line("reasoning", _capability_label(metadata.reasoning)),
    ]


#: Legend for the dense one-line-per-model layout used once the fully
#: labeled layout cannot fit every ~20 capped entries in one message.
_COMPACT_LEGEND = (
    "<i>y=known-yes n=known-no ?=unknown · provider-reported, advisory only</i>"
)


def _compact_numeric(value: int | None) -> str:
    return "?" if value is None else str(value)


def _compact_capability(value: bool | None) -> str:
    if value is True:
        return "y"
    if value is False:
        return "n"
    return "?"


def _format_catalog_model_compact(record: "ModelRecord", active_model_id: str | None) -> str:
    """One dense line carrying every field's literal name, value, and source.

    Values are abbreviated (y/n/?) but every field *name* stays literal, so
    completeness checks never mistake a compacted row for a half-rendered one.
    """
    active_mark = "✅" if record.id == active_model_id else "⬜"
    metadata = record.metadata
    fields = " ".join([
        f"context_tokens:{_compact_numeric(metadata.context_tokens)}",
        f"input_tokens:{_compact_numeric(metadata.input_tokens)}",
        f"output_tokens:{_compact_numeric(metadata.output_tokens)}",
        f"vision:{_compact_capability(metadata.vision)}",
        f"tool_calling:{_compact_capability(metadata.tool_calling)}",
        f"reasoning:{_compact_capability(metadata.reasoning)}",
    ])
    return (
        f"{active_mark} <code>{html.escape(_bound_catalog_text(record.id))}</code> "
        f"{html.escape(_bound_catalog_text(record.display_name))} {fields}"
    )


_SHOWN_PLACEHOLDER = "{SHOWN}"


def _catalog_header_lines(
    view: "CatalogView", *, refresh_failed: bool, refresh_not_saved: bool,
    cache_notice: str | None = None, selection_notice: str | None = None,
) -> list[str]:
    """Title, refresh-outcome warning, and selection lines.

    `refresh_failed` and `refresh_not_saved` are mutually distinct and never
    collapsed into one generic flag: an unexpected exception escaping the
    registry boundary is not the same honest state as a refresh that ran but
    could not be persisted, and neither implies every provider failed — each
    provider's own fresh/stale line (built separately, and budgeted on its
    own) already carries that detail. These lines are always small and
    provider/model-count independent, so they never compete for the budget
    consumed by the — potentially large — provider and model sections.
    """
    lines = ["🤖 <b>Model Catalog</b> (provider-discovered)\n"]
    if refresh_failed:
        lines.append("⚠️ <i>Refresh failed unexpectedly — showing the last known catalog.</i>\n")
    elif refresh_not_saved:
        lines.append("⚠️ <i>Refresh was not saved to the model cache — showing this session's results only.</i>\n")
    # Standing file-state notices (fixed, content-free registry sentences) follow the
    # transient refresh outcome: the saved-selection notice, then the cache notice.
    for notice in (selection_notice, cache_notice):
        if notice:
            lines.append(f"⚠️ <i>{html.escape(notice)}</i>\n")

    selection_label = _SELECTION_STATUS_LABELS.get(view.selection_status, view.selection_status)
    if view.selection_status == "active":
        # An active selector equal to the saved one is "saved"; otherwise it is the
        # in-memory automatic choice, which is never written to the preference.
        saved = view.remembered_model_id == view.active_model_id
        selection_label += " (saved)" if saved else " (automatic, unsaved)"
    lines.append(f"Selection: {html.escape(selection_label)}")
    shown_id = view.active_model_id or view.remembered_model_id
    if shown_id:
        kind = "Active" if view.active_model_id else "Saved"
        lines.append(f"{kind}: <code>{html.escape(_bound_catalog_text(shown_id))}</code>")
    return lines


def _provider_line(state: "ProviderState") -> str:
    """One provider's bounded, escaped freshness line."""
    status_word = "fresh" if state.status == "fresh" else "stale"
    detail = ""
    if state.status != "fresh" and state.sanitized_last_error_code:
        detail = f" ({html.escape(state.sanitized_last_error_code)})"
    return f"  {html.escape(_bound_catalog_text(state.provider_id))}: {status_word}{detail}"


def _providers_header_worst_case(total: int) -> str:
    """Upper-bound header length used only to reserve budget before fitting."""
    if not total:
        return "<b>Providers</b> (none configured)"
    return f"<b>Providers</b> ({total} shown of {total} total)"


def _providers_header_line(total: int, shown: int) -> str:
    """The real provider-section header, computed only after fitting.

    Stays plain when every provider fit (no reason to clutter an unremarkable
    catalog with a count); states "N shown of M total" only when providers
    were actually omitted, so the omission is never silent.
    """
    if not total:
        return "<b>Providers</b> (none configured)"
    if shown < total:
        return f"<b>Providers</b> ({shown} shown of {total} total)"
    return "<b>Providers</b>"


_MODELS_ADVISORY = "(advisory, provider-reported)"
_EMPTY_CACHE_GUIDANCE = "No models are cached yet. Tap 🔄 Refresh, then choose one with its button."


def _models_header_template(total: int) -> str:
    """Model-section header: always advisory-labelled; empty cache adds static guidance."""
    if not total:
        return f"<b>Models</b> {_MODELS_ADVISORY} (none discovered)\n{_EMPTY_CACHE_GUIDANCE}"
    return f"<b>Models</b> {_MODELS_ADVISORY} ({_SHOWN_PLACEHOLDER} shown of {total} total)"


def _fit_lines(lines: list[str], budget: int) -> list[str]:
    """Greedily keep whole lines within `budget`; a line is never split."""
    kept: list[str] = []
    size = 0
    for line in lines:
        added = len(line) + 1
        if size + added > max(budget, 0):
            break
        kept.append(line)
        size += added
    return kept


@dataclass(frozen=True)
class CatalogRenderResult:
    """Rendered catalog text plus the exact model ids that made the final cut.

    `shown_model_ids` is the single source of truth for "what is actually on
    screen" — callers (e.g. the Telegram command layer, to mint per-model
    selection controls) must never re-derive or guess it from the text or
    re-run the fitting algorithm themselves.
    """

    text: str
    shown_model_ids: tuple[str, ...]


def format_models_catalog(
    view: "CatalogView", *, refresh_failed: bool = False, refresh_not_saved: bool = False,
    cache_notice: str | None = None, selection_notice: str | None = None,
) -> str:
    """Render a sanitized `CatalogView` as read-only HTML (task 8.1).

    Thin wrapper over `render_models_catalog` kept for existing callers that
    only need text, not the shown-model-id list.
    """
    return render_models_catalog(
        view, refresh_failed=refresh_failed, refresh_not_saved=refresh_not_saved,
        cache_notice=cache_notice, selection_notice=selection_notice,
    ).text


def render_models_catalog(
    view: "CatalogView", *, refresh_failed: bool = False, refresh_not_saved: bool = False,
    cache_notice: str | None = None, selection_notice: str | None = None,
) -> CatalogRenderResult:
    """Render a sanitized `CatalogView` as read-only HTML, and report which
    model ids were actually displayed (task 8.2 shared seam).

    Only whitelisted `CatalogView`/`ProviderState`/`ModelRecord` fields are
    used — never raw connections, credentials, or provider exception text.
    No canonical model id is embedded in any callback data here; this is a
    pure render — minting opaque selection tokens from `shown_model_ids` is
    the caller's responsibility, not this function's.

    Both the provider section and the model section are budgeted as part of
    one overall 4096-char message: providers are fit first (whole, escaped
    lines only — never split), any omission is stated explicitly, and model
    entries then get whatever room remains. Entries are budgeted as whole,
    fully-labeled blocks — never cut mid field — and every "shown" count is
    computed only *after* fitting, so it is always the true number of
    complete entries that made it into the message, never a pre-truncation
    cap. The fully labeled model layout is used whenever it fits; otherwise
    every capped entry switches together to a dense one-line-per-model
    layout (with a legend) that keeps every field name, value, and source
    but drops verbose wording, so ordinary catalogs can still show the full
    ~20-entry cap within one message even when many providers are present.
    """
    header_lines = _catalog_header_lines(
        view, refresh_failed=refresh_failed, refresh_not_saved=refresh_not_saved,
        cache_notice=cache_notice, selection_notice=selection_notice,
    )

    provider_total = len(view.providers)
    provider_lines_all = [_provider_line(state) for state in view.providers]
    providers_header_worst = _providers_header_worst_case(provider_total)

    model_total = len(view.models)
    candidates = view.models[:_MODELS_DISPLAY_CAP]
    models_header = _models_header_template(model_total)
    model_trailer_worst = (
        f"\n\n…+{model_total} more models available ({model_total} total)" if model_total else ""
    )

    provider_trailer_worst = (
        f"\n…+{provider_total} more providers not shown ({provider_total} total)" if provider_total else ""
    )
    scaffold = "\n".join(header_lines + ["", providers_header_worst, "", models_header])
    provider_budget = (
        TELEGRAM_RENDERED_LIMIT - len(scaffold) - len(provider_trailer_worst) - len(model_trailer_worst)
    )
    kept_providers = _fit_lines(provider_lines_all, provider_budget)
    providers_shown = len(kept_providers)

    prefix_text = "\n".join(header_lines) + "\n\n" + _providers_header_line(provider_total, providers_shown)
    if kept_providers:
        prefix_text += "\n" + "\n".join(kept_providers)
    if provider_total > providers_shown:
        prefix_text += f"\n…+{provider_total - providers_shown} more providers not shown ({provider_total} total)"
    prefix_text += "\n"

    model_budget = TELEGRAM_RENDERED_LIMIT - len(prefix_text) - len(models_header) - len(model_trailer_worst)

    verbose_blocks = [_format_catalog_model(record, view.active_model_id) for record in candidates]
    verbose_body = "\n".join(line for block in verbose_blocks for line in block)
    if (len(verbose_body) if verbose_body else 0) <= model_budget:
        shown_models = len(candidates)
        body_text = verbose_body
    else:
        compact_blocks = [[_format_catalog_model_compact(record, view.active_model_id)] for record in candidates]
        kept_blocks = _fit_blocks(compact_blocks, model_budget - (len(_COMPACT_LEGEND) + 1 if candidates else 0))
        shown_models = len(kept_blocks)
        body_lines = ([_COMPACT_LEGEND] if kept_blocks else []) + [
            line for block in kept_blocks for line in block
        ]
        body_text = "\n".join(body_lines)

    text = prefix_text + models_header.replace(_SHOWN_PLACEHOLDER, str(shown_models))
    if body_text:
        text += "\n" + body_text
    if model_total > shown_models:
        text += f"\n\n…+{model_total - shown_models} more models available ({model_total} total)"
    shown_model_ids = tuple(record.id for record in candidates[:shown_models])
    return CatalogRenderResult(text, shown_model_ids)


def _fit_blocks(blocks: list[list[str]], budget: int) -> list[list[str]]:
    """Greedily keep whole multi-line blocks within `budget`; never splits one."""
    kept: list[list[str]] = []
    size = 0
    for block in blocks:
        block_text = "\n".join(block)
        added = len(block_text) + 1
        if size + added > max(budget, 0):
            break
        kept.append(block)
        size += added
    return kept
