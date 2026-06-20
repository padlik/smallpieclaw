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
    # Cap oversized recent messages that are preserved verbatim.
    last = [_cap_recent_message(m) for m in messages[-2:]]

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
    summary = _truncate_head_tail(summary, _SUMMARY_CAP // 2, _SUMMARY_CAP // 2)

    compacted = first + [
        {"role": "user", "content": f"[Compacted context — earlier steps summary]\n{summary}"}
    ] + last

    new_total = estimate_messages_tokens(compacted, system)
    logger.info("%sCompacted context: %d → ~%d tokens", log_prefix, total, new_total)
    return compacted
