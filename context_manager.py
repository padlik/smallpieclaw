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
    from llm_client import LLMClient  # noqa

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


def _active_model(llm: "LLMClient") -> str | None:
    """Best-effort active model name for tokenizer-aware estimation.

    Returns None on any failure so estimation falls back to the heuristic; this
    keeps mocked/partial LLM objects in tests working without a real config.
    """
    try:
        return llm.llm_cfg.get("model")
    except Exception:  # noqa: BLE001
        return None


def resolve_compaction_threshold(llm_cfg: dict, ctx_max_tokens: int) -> tuple[int, int]:
    """Compute the effective context window and compaction threshold.

    Args:
        llm_cfg: Active model configuration dict. May contain ``context_window``
            and ``max_tokens`` keys.
        ctx_max_tokens: Agent-level context-window limit, used as a fallback when
            the model does not specify its own ``context_window``.

    Returns:
        A ``(effective_window, compaction_threshold)`` tuple. The threshold
        reserves the model's completion-token budget, applies an 85% margin,
        and clamps to a 256-token floor:

        ``threshold = max(int((effective_window - max_tokens) * 0.85), 256)``.
    """
    cfg = llm_cfg if isinstance(llm_cfg, dict) else {}
    effective = ctx_max_tokens
    if cfg.get("context_window"):
        effective = cfg["context_window"]
    raw_max_tokens = cfg.get("max_tokens")
    max_tokens = 1024 if raw_max_tokens is None else raw_max_tokens
    threshold = max(int((effective - max_tokens) * 0.85), 256)
    return effective, threshold


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


def _cap_short_overbudget_context(
    messages: list[dict],
    system: str,
    threshold: int,
    llm: "LLMClient",
) -> list[dict]:
    """Cap a short but already-overbudget history without adding a summary."""
    cap = _RECENT_HEAD + _RECENT_TAIL
    capped = [_cap_string_message(m, cap) for m in messages]
    new_total = estimate_messages_tokens(capped, system, model=_active_model(llm))
    while new_total > threshold and cap > _SHRINK_FLOOR:
        cap = max(_SHRINK_FLOOR, cap // 2)
        capped = [_cap_string_message(m, cap) for m in messages]
        new_total = estimate_messages_tokens(capped, system, model=_active_model(llm))
    logger.warning(
        "Short context exceeded budget; capped message contents to ≤%d chars → ~%d tokens",
        cap, new_total,
    )
    return capped


def _fallback_summary(middle: list[dict]) -> str:
    """Deterministic, bounded summary used when the compaction LLM is unavailable."""
    rendered = "\n".join(_format_middle_message(m) for m in middle)
    return _truncate_head_tail(rendered, _SUMMARY_CAP // 2, _SUMMARY_CAP // 2)


def maybe_compact(
    messages: list[dict],
    system: str,
    ctx_max_tokens: int,
    llm: "LLMClient",
    *,
    goal_idx: int = 0,
    tool_defs_tokens: int = 0,
) -> tuple[list[dict], int]:
    """Return a (possibly compacted) copy of *messages* and the goal's new index.

    If the estimated token count of *messages* plus *system* plus
    ``tool_defs_tokens`` exceeds 85 % of the effective context window (after
    reserving completion tokens), the middle portion of the conversation is
    summarised and replaced with a single compaction summary message.

    The compaction threshold is computed by :func:`resolve_compaction_threshold`
    from the active model's ``llm_cfg``; it reserves completion tokens first,
    then applies the 85% margin, finally clamping to a 256-token floor:
    ``threshold = max(int((effective - max_tokens) * 0.85), 256)``. This
    prevents the context from growing into the token budget the model needs
    for its response. ``ctx_max_tokens`` is the agent-level context-window
    limit, and the effective limit is the per-model ``context_window`` when set.

    The 256-token floor is a last-resort guard for the unvalidated raw-dict path
    where ``max_tokens >= ctx_max_tokens``; validated configs always produce a
    positive raw threshold because ``context_window > max_tokens`` is enforced at
    parse time.

    The current active goal and the two most recent messages are preserved, but
    very large recent tool-result content is capped head+tail so it cannot
    overflow the real model context. If the compaction LLM call fails, a
    deterministic fallback summary is returned rather than the un-compacted
    oversized messages.

    The goal message is preserved verbatim on every path, but its position in
    the returned list is *not* fixed: on full compaction it becomes the ``first``
    anchor at index 0 when it precedes the recent tail, or it is kept inside the
    preserved ``last`` tail (at index 2 or 3 of the length-4 compacted list) when
    it already lies within the two most recent messages. The returned index
    locates the goal precisely so callers never have to guess after compaction.

    Args:
        messages: Conversation history to (potentially) compact.
        system: System prompt string (used for token estimation only).
        ctx_max_tokens: Effective context-window limit in tokens (per-model
            ``context_window`` or ``agent.ctx_max_tokens``).
        llm: LLM client used for the compaction summary call.
        goal_idx: Index of the current active goal message within *messages*.
            When ``react_loop`` prepends short-term history before the current
            user goal, ``messages[0]`` is stale prior conversation, not the
            goal.  Passing the correct index ensures the goal is pinned into
            the ``first`` slot rather than being swept into the summarised
            middle.  Defaults to ``0`` for backward-compatibility when no
            history precedes the goal.
        tool_defs_tokens: Extra token count to include in the total, e.g. the
            space consumed by tool definitions that are sent with every model
            call. This makes compaction aware of otherwise-invisible tool-def
            overhead. Defaults to 0.

    Returns:
        A ``(compacted_messages, new_goal_idx)`` tuple where ``new_goal_idx`` is
        the index of the goal message within ``compacted_messages``. All call
        sites must unpack this tuple.
    """
    total = estimate_messages_tokens(messages, system, model=_active_model(llm)) + tool_defs_tokens
    _, threshold = resolve_compaction_threshold(llm.llm_cfg, ctx_max_tokens)
    if total <= threshold:
        # Under threshold: messages returned unchanged, so is the goal position.
        return messages, goal_idx
    if len(messages) <= 3:
        # Capping-only path preserves order and length, so the goal keeps its
        # index (clamped defensively into the valid range).
        capped = _cap_short_overbudget_context(messages, system, threshold, llm)
        return capped, min(max(0, goal_idx), len(capped) - 1)

    logger.warning(
        "Context size ~%d tokens exceeds threshold %d — compacting…",
        total, threshold,
    )

    # Clamp goal_idx so there is always at least one message in middle and two
    # in last. When the goal is very close to the end (e.g. compaction fires on
    # the first step before any ReAct messages accumulate), the goal will land
    # in `last` rather than `first`, but it is still preserved verbatim.
    _goal = min(goal_idx, max(0, len(messages) - 3))
    first = [messages[_goal]]
    # Short-term history that precedes the goal (messages[:_goal]) is fed into
    # the middle so it is summarised rather than silently dropped.
    middle = messages[:_goal] + messages[_goal + 1:-2]
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

    new_total = estimate_messages_tokens(compacted, system, model=_active_model(llm))

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
            new_total = estimate_messages_tokens(compacted, system, model=_active_model(llm))
        logger.warning(
            "Compacted context still large; applied deterministic tightening "
            "(summary≤%d, recent≤%d, goal≤%d chars) → ~%d tokens",
            summary_cap, recent_cap, first_cap, new_total,
        )

    logger.info("Compacted context: %d → ~%d tokens", total, new_total)

    # Locate the goal in the compacted list. `_build_compacted` returns
    # [first_msg, summary_msg, last[0], last[1]] (length 4). When the goal
    # preceded the recent tail it became the `first` anchor (index 0); otherwise
    # it was clamped into `last` and keeps its offset from the end of the list.
    if goal_idx <= len(messages) - 3:
        new_goal_idx = 0
    else:
        new_goal_idx = len(compacted) - (len(messages) - goal_idx)
    return compacted, new_goal_idx
