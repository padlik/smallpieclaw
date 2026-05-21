"""
context_manager.py
------------------
Context-window management for the ReAct loop.

Provides token estimation and context compaction: when the running token count
approaches the configured limit, the middle portion of the conversation history
is summarised by the LLM to keep the context window within bounds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prompt_builder import estimate_messages_tokens

if TYPE_CHECKING:
    from llm_client import LLMClient

logger = logging.getLogger(__name__)


def maybe_compact(
    messages: list[dict],
    system: str,
    ctx_max_tokens: int,
    log_prefix: str,
    llm: "LLMClient",
) -> list[dict]:
    """Return a (possibly compacted) copy of *messages*.

    If the estimated token count of *messages* plus *system* exceeds 85 % of
    *ctx_max_tokens*, the middle portion of the conversation is summarised by
    *llm* and replaced with a single compaction summary message.

    The first message (user goal) and the two most recent messages are always
    preserved verbatim so the LLM retains full context for its next step.
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
    last = messages[-2:]

    middle_text = "\n".join(
        f"[{m['role']}]: {m['content'][:500]}" for m in middle
    )

    try:
        summary = llm.chat([{
            "role": "user",
            "content": (
                "Summarize these intermediate agent steps concisely in bullet points "
                "(preserve tool names, key outputs, and decisions):\n\n" + middle_text
            ),
        }])
    except Exception as exc:
        logger.error("Compaction LLM call failed: %s", exc)
        return messages

    compacted = first + [
        {"role": "user", "content": f"[Compacted context — earlier steps summary]\n{summary}"}
    ] + last

    new_total = estimate_messages_tokens(compacted, system)
    logger.info("%sCompacted context: %d → ~%d tokens", log_prefix, total, new_total)
    return compacted
