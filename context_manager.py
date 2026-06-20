"""
context_manager.py
------------------
Context-window management for the ReAct loop.

Provides token estimation and context compaction: when the running token count
approaches the configured limit, the middle portion of the conversation history
is summarised by the LLM to keep the context window within bounds.

Compaction is content-aware: tool-result messages (shell/file output) are
trimmed head+tail so both the command/header and the trailing error region
survive, while model prose is bounded with a simple head cap. On LLM failure a
deterministic fallback summary is produced instead of returning the oversized
original messages, and the most recent (verbatim) messages are capped so a
single huge tool result cannot overflow the real model context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prompt_builder import estimate_messages_tokens

if TYPE_CHECKING:
    from llm_client import LLMClient

logger = logging.getLogger(__name__)

# Per-message caps (characters) used when rendering the middle history for the
# compaction prompt.
_ASSISTANT_CAP = 800        # model action/prose: intent is at the start
_RESULT_HEAD = 1200         # tool result: keep header (command/path/success)
_RESULT_TAIL = 1200         # tool result: keep tail (errors/final output)

# Cap (characters) for the most recent, verbatim-preserved messages so a single
# huge tool result cannot blow the context even after compaction.
_RECENT_HEAD = 3000
_RECENT_TAIL = 3000

# Hard cap (characters) on the compaction summary itself.
_SUMMARY_CAP = 6000

# Floor (characters) below which deterministic budget-tightening stops shrinking
# content — going further would discard information without meaningfully helping.
_SHRINK_FLOOR = 400


def _build_compacted(
    first: list[dict],
    summary: str,
    last: list[dict],
    summary_cap: int,
    recent_cap: int,
    first_cap: int,
) -> list[dict]:
    """Assemble the compacted message list under the given character caps."""
    bounded_summary = _truncate_head_tail(summary, summary_cap // 2, summary_cap // 2)
    head = [_cap_string_message(m, first_cap) for m in first]
    tail = [_cap_string_message(m, recent_cap) for m in last]
    return head + [
        {"role": "user", "content": f"[Compacted context — earlier steps summary]\n{bounded_summary}"}
    ] + tail


def _cap_string_message(m: dict, cap: int) -> dict:
    """Return a copy of *m* with string content head+tail trimmed to ~*cap* chars."""
    content = m.get("content")
    if isinstance(content, str) and len(content) > cap:
        capped = dict(m)
        capped["content"] = _truncate_head_tail(content, cap // 2, cap // 2)
        return capped
    return m


def _truncate_head(text: str, cap: int) -> str:
    """Keep the first *cap* characters with an omission marker."""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…[{len(text) - cap} chars omitted]"


def _truncate_head_tail(text: str, head: int, tail: int) -> str:
    """Keep *head* leading and *tail* trailing characters with a marker between."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars omitted]…\n{text[-tail:]}"


def _format_middle_message(m: dict) -> str:
    """Render one middle message for the compaction prompt, content-aware."""
    role = m.get("role", "?")
    content = str(m.get("content") or "")
    if role == "assistant":
        body = _truncate_head(content, _ASSISTANT_CAP)
    else:
        # User/tool-result messages: preserve both ends so a "failed/succeeded"
        # header and the trailing error/output region both survive.
        body = _truncate_head_tail(content, _RESULT_HEAD, _RESULT_TAIL)
    return f"[{role}]: {body}"


def _cap_recent_message(m: dict) -> dict:
    """Return a copy of *m* with oversized string content head+tail trimmed."""
    content = m.get("content")
    if isinstance(content, str) and len(content) > _RECENT_HEAD + _RECENT_TAIL:
        capped = dict(m)
        capped["content"] = _truncate_head_tail(content, _RECENT_HEAD, _RECENT_TAIL)
        return capped
    return m


def _fallback_summary(middle: list[dict]) -> str:
    """Deterministic, bounded summary used when the compaction LLM is unavailable."""
    rendered = "\n".join(_format_middle_message(m) for m in middle)
    return _truncate_head_tail(rendered, _SUMMARY_CAP // 2, _SUMMARY_CAP // 2)


def maybe_compact(
    messages: list[dict],
    system: str,
    ctx_max_tokens: int,
    log_prefix: str,
    llm: "LLMClient",
) -> list[dict]:
    """Return a (possibly compacted) copy of *messages*.

    If the estimated token count of *messages* plus *system* exceeds 85 % of
    *ctx_max_tokens*, the middle portion of the conversation is summarised and
    replaced with a single compaction summary message.

    The first message (user goal) and the two most recent messages are
    preserved, but very large recent tool-result content is capped head+tail so
    it cannot overflow the real model context. If the compaction LLM call fails,
    a deterministic fallback summary is returned rather than the un-compacted
    oversized messages.
    """
    total = estimate_messages_tokens(messages, system)
    threshold = int(ctx_max_tokens * 0.85)
    if total <= threshold:
        return messages
    if len(messages) <= 3:
        return messages

    logger.warning(
        "%sContext size ~%d tokens exceeds threshold %d — compacting…",
        log_prefix, total, threshold,
    )

    first = messages[:1]
    middle = messages[1:-2]
    # The two most recent messages are preserved; _build_compacted caps oversized
    # content head+tail so a single huge tool result cannot overflow the context.
    last = messages[-2:]

    middle_text = "\n".join(_format_middle_message(m) for m in middle)

    summary: str
    try:
        summary = llm.chat([{
            "role": "user",
            "content": (
                "Summarize these intermediate agent steps concisely in bullet points "
                "(preserve tool names, key outputs, errors, and decisions):\n\n" + middle_text
            ),
        }])
        if not (summary or "").strip():
            raise ValueError("empty compaction summary")
    except Exception as exc:
        logger.error("Compaction LLM call failed: %s — using deterministic fallback", exc)
        summary = _fallback_summary(middle)

    # Keep the summary itself bounded.
    compacted = _build_compacted(
        first, summary, last,
        summary_cap=_SUMMARY_CAP, recent_cap=_RECENT_HEAD + _RECENT_TAIL,
        first_cap=_RECENT_HEAD + _RECENT_TAIL,
    )

    new_total = estimate_messages_tokens(compacted, system)

    # Deterministic budget-tightening: if the compacted context still exceeds the
    # threshold (e.g. an enormous first message or recent tool result), shrink the
    # summary, the preserved goal, and the recent messages progressively until it
    # fits or a sensible floor is reached. This guarantees compaction never hands
    # the model an over-budget context.
    if new_total > threshold:
        summary_cap = _SUMMARY_CAP
        recent_cap = _RECENT_HEAD + _RECENT_TAIL
        first_cap = _RECENT_HEAD + _RECENT_TAIL
        while new_total > threshold and (
            summary_cap > _SHRINK_FLOOR or recent_cap > _SHRINK_FLOOR or first_cap > _SHRINK_FLOOR
        ):
            summary_cap = max(_SHRINK_FLOOR, summary_cap // 2)
            recent_cap = max(_SHRINK_FLOOR, recent_cap // 2)
            first_cap = max(_SHRINK_FLOOR, first_cap // 2)
            compacted = _build_compacted(
                first, summary, last,
                summary_cap=summary_cap, recent_cap=recent_cap, first_cap=first_cap,
            )
            new_total = estimate_messages_tokens(compacted, system)
        logger.warning(
            "%sCompacted context still large; applied deterministic tightening "
            "(summary≤%d, recent≤%d, goal≤%d chars) → ~%d tokens",
            log_prefix, summary_cap, recent_cap, first_cap, new_total,
        )

    logger.info("%sCompacted context: %d → ~%d tokens", log_prefix, total, new_total)
    return compacted
