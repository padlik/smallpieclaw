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

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_builder import estimate_messages_tokens
from model_registry import ModelMetadata
from providers._errors import LLMCancelledError, LLMPermanentError
from model_request_budget import (
    ModelRequestBudget,
    ModelRequestBudgetError,
    compaction_threshold,
    compute_request_budget,
)

if TYPE_CHECKING:
    from llm_client import LLMClient  # noqa

logger = logging.getLogger(__name__)

# Per-message caps (characters) used when rendering the middle history for the
# compaction prompt.
_ASSISTANT_CAP = 800        # model action/prose: intent is at the start
_RESULT_HEAD = 1200         # tool result: keep header (command/path/success)
_RESULT_TAIL = 1200         # tool result: keep tail (errors/final output)
_CALL_ARGS_CAP = 400        # tool call arguments rendered per call

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


def resolve_compaction_threshold(
    llm_cfg: dict | ModelMetadata,
    ctx_max_tokens: int,
    *,
    metadata: ModelMetadata | dict | None = None,
    request_budget: ModelRequestBudget | None = None,
) -> tuple[int, int]:
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
    if request_budget is not None:
        if not isinstance(request_budget, ModelRequestBudget):
            raise ModelRequestBudgetError("invalid_request")
        effective = (
            request_budget.combined_context
            or request_budget.input_limit
            or request_budget.hard_input_budget
        )
        # This threshold is a compaction heuristic, not approval that the
        # assembled request fits. The client must re-budget after payload edits.
        return effective, compaction_threshold(request_budget.hard_input_budget)
    catalog_metadata = metadata
    if catalog_metadata is None:
        if isinstance(llm_cfg, ModelMetadata):
            catalog_metadata = llm_cfg
        elif isinstance(cfg.get("metadata"), (ModelMetadata, dict)):
            catalog_metadata = cfg["metadata"]
    if catalog_metadata is not None:
        model_metadata = _coerce_model_metadata(catalog_metadata)
        budget = compute_request_budget(
            model_metadata,
            input_tokens=0,
            local_context_tokens=ctx_max_tokens,
        )
        effective = budget.combined_context or budget.input_limit or budget.hard_input_budget
        return effective, compaction_threshold(budget.hard_input_budget)

    effective = ctx_max_tokens
    if cfg.get("context_window"):
        effective = cfg["context_window"]
    raw_max_tokens = cfg.get("max_tokens")
    max_tokens = 1024 if raw_max_tokens is None else raw_max_tokens
    threshold = max(int((effective - max_tokens) * 0.85), 256)
    return effective, threshold


_MALFORMED_HISTORY = (
    "Conversation history contains malformed tool-call groups and cannot be "
    "sent or compacted. Start a new task."
)


def _valid_call(call: object) -> str | None:
    """Return the call ID when *call* is a supported native call entry."""
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    func = call.get("function")
    if not isinstance(call_id, str) or not call_id or not isinstance(func, dict):
        return None
    name = func.get("name")
    if not isinstance(name, str) or not name:
        return None
    if call.get("type") not in (None, "function"):
        return None
    if "arguments" not in func:
        return None
    arguments = func["arguments"]
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError, RecursionError):
            return None
        if not isinstance(parsed, (dict, list)):
            return None
    elif not isinstance(arguments, (dict, list)):
        return None
    return call_id


def validate_native_history(messages: list[dict]) -> None:
    """Reject malformed native tool-call groups with a fixed, sanitized error.

    Every assistant ``tool_calls`` message needs well-formed calls with unique
    non-empty IDs within the group, immediately followed by exactly one string-content tool
    result per ID. Orphan or mismatched results raise. Nothing is repaired.
    """
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if not isinstance(m, dict):
            raise LLMPermanentError(_MALFORMED_HISTORY)
        role = m.get("role")
        calls = m.get("tool_calls") if role == "assistant" else None
        if role == "tool":
            raise LLMPermanentError(_MALFORMED_HISTORY)
        i += 1
        if not calls:
            continue
        if not isinstance(calls, list):
            raise LLMPermanentError(_MALFORMED_HISTORY)
        expected: set[str] = set()
        for call in calls:
            call_id = _valid_call(call)
            if call_id is None or call_id in expected:
                raise LLMPermanentError(_MALFORMED_HISTORY)
            expected.add(call_id)
        got: set[str] = set()
        while i < n and isinstance(messages[i], dict) and messages[i].get("role") == "tool":
            result = messages[i]
            result_id = result.get("tool_call_id")
            if (
                not isinstance(result_id, str)
                or not result_id
                or not isinstance(result.get("content"), str)
                or result_id not in expected
                or result_id in got
            ):
                raise LLMPermanentError(_MALFORMED_HISTORY)
            got.add(result_id)
            i += 1
        if got != expected:
            raise LLMPermanentError(_MALFORMED_HISTORY)


def _message_spans(messages: list[dict]) -> list[tuple[int, int]]:
    """Return half-open index spans; a tool-calling assistant and its results are one."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        end = i + 1
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {
                c.get("id") for c in m["tool_calls"] if isinstance(c, dict) and c.get("id")
            }
            while (
                end < n
                and messages[end].get("role") == "tool"
                and messages[end].get("tool_call_id") in ids
            ):
                end += 1
        spans.append((i, end))
        i = end
    return spans


def _partition_compaction(
    messages: list[dict], goal_idx: int,
) -> tuple[list[int], list[int], list[int]]:
    """Split original indices into (first, middle, last) on whole-span boundaries.

    ``last`` covers the final two messages expanded back to a span boundary;
    ``first`` is the whole span holding the goal unless that span is in
    ``last``; ``middle`` is everything else in original order.
    """
    n = len(messages)
    goal = min(max(0, goal_idx), n - 1)
    spans = _message_spans(messages)
    suffix_start = next(s for s, e in spans if s <= max(0, n - 2) < e)
    last = list(range(suffix_start, n))
    first: list[int] = []
    if goal < suffix_start:
        first = list(range(*next((s, e) for s, e in spans if s <= goal < e)))
    kept = set(first) | set(last)
    middle = [i for i in range(n) if i not in kept]
    return first, middle, last


def _assemble(
    messages: list[dict],
    first: list[int],
    middle: list[int],
    last: list[int],
    goal: int,
    summary: str,
    summary_cap: int,
    recent_cap: int,
) -> tuple[list[dict], int]:
    """Build the compacted list from original indices; the goal is never capped."""
    out: list[dict] = []
    goal_pos = 0

    def emit(i: int) -> None:
        nonlocal goal_pos
        if i == goal:
            goal_pos = len(out)
            out.append(messages[i])
        else:
            out.append(_cap_string_message(messages[i], recent_cap))

    for i in first:
        emit(i)
    if middle:
        bounded = _truncate_head_tail(summary, summary_cap // 2, summary_cap // 2)
        out.append({
            "role": "user",
            "content": f"[Compacted context — earlier steps summary]\n{bounded}",
        })
    for i in last:
        emit(i)
    return out, goal_pos


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
    content = m.get("content")
    content = content if isinstance(content, str) else ("" if content is None else str(content))
    if role == "assistant":
        body = _truncate_head(content, _ASSISTANT_CAP)
        calls = []
        for call in m.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            func = call.get("function") or {}
            args = func.get("arguments", "")
            args = args if isinstance(args, str) else json.dumps(args, default=str)
            calls.append(
                f"{func.get('name', 'tool')}({_truncate_head(args, _CALL_ARGS_CAP)}) "
                f"id={call.get('id', '?')}"
            )
        if calls:
            body = (body + "\n" if body else "") + "tool calls: " + "; ".join(calls)
    elif role == "tool":
        body = _truncate_head_tail(content, _RESULT_HEAD, _RESULT_TAIL)
        return f"[tool result id={m.get('tool_call_id', '?')}]: {body}"
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
    goal: int,
) -> tuple[list[dict], int]:
    """Cap non-goal contents of a short overbudget history; return it and its token total."""
    def capped_at(cap: int) -> list[dict]:
        return [m if i == goal else _cap_string_message(m, cap) for i, m in enumerate(messages)]

    cap = _RECENT_HEAD + _RECENT_TAIL
    capped = capped_at(cap)
    new_total = estimate_messages_tokens(capped, system, model=_active_model(llm))
    while new_total > threshold and cap > _SHRINK_FLOOR:
        cap = max(_SHRINK_FLOOR, cap // 2)
        capped = capped_at(cap)
        new_total = estimate_messages_tokens(capped, system, model=_active_model(llm))
    logger.warning(
        "Short context exceeded budget; capped message contents to ≤%d chars → ~%d tokens",
        cap, new_total,
    )
    return capped, new_total


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
    metadata: ModelMetadata | dict | None = None,
    request_budget: ModelRequestBudget | None = None,
    input_tokens: int | None = None,
    repair_target: int | None = None,
    force: bool = False,
    summarize: Callable[[str], str] | None = None,
) -> tuple[list[dict], int]:
    """Return a (possibly compacted) copy of *messages* and the goal's new index.

    Optional repair inputs: ``input_tokens`` is the complete request estimate
    (messages, system, tool definitions and framing); the part beyond the
    message estimate is held fixed. With ``force`` and a positive
    ``repair_target`` (largest input that could fit) one bounded pass is made
    toward that target even when the heuristic threshold would not fire, and no
    budget is consulted, so it also works for budgets below the 256 floor. The
    caller must re-preview the result; nothing here approves a send.
    ``summarize`` replaces the default ``llm.chat`` summary call; a
    ``LLMCancelledError`` it raises propagates, any other failure falls back to
    the deterministic summary.

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

    A supplied ``request_budget`` is the authoritative hard-input estimate used
    for the compaction threshold. This heuristic is not approval that the final
    request fits: the client must recompute its budget after any payload changes.

    The current active goal and the two most recent messages are preserved, but
    very large recent tool-result content is capped head+tail so it cannot
    overflow the real model context. If the compaction LLM call fails, a
    deterministic fallback summary is returned rather than the un-compacted
    oversized messages.

    The goal message is preserved unchanged (every field, never capped) on every
    path; its position is returned precisely. Compaction works on whole spans: an
    assistant ``tool_calls`` message and all its matching tool results are kept or
    summarized together. At the shrink floor the oldest retained non-goal span is
    demoted into a deterministically rebuilt summary.

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
    validate_native_history(messages)
    if not messages:
        return messages, goal_idx
    message_total = estimate_messages_tokens(messages, system, model=_active_model(llm))
    overhead = 0
    if input_tokens is not None:
        total = input_tokens
        overhead = max(0, input_tokens - message_total)
    else:
        total = message_total + tool_defs_tokens
    if force and type(repair_target) is int and repair_target > 0:
        threshold = repair_target
    else:
        force = False
        _, threshold = resolve_compaction_threshold(
            llm.llm_cfg, ctx_max_tokens, metadata=metadata, request_budget=request_budget,
        )
    if total <= threshold:
        # Under threshold: messages returned unchanged, so is the goal position.
        return messages, goal_idx
    # Message-level limit once the fixed non-message overhead is accounted for.
    limit = max(1, threshold - overhead)
    goal = min(max(0, goal_idx), len(messages) - 1)
    if len(messages) <= 3:
        capped, capped_total = _cap_short_overbudget_context(messages, system, limit, llm, goal)
        if capped_total <= limit:
            return capped, goal

    logger.warning(
        "Context size ~%d tokens exceeds threshold %d — compacting…",
        total, threshold,
    )

    first, middle, last = _partition_compaction(messages, goal)

    def summarize_middle() -> str:
        middle_text = "\n".join(_format_middle_message(messages[i]) for i in middle)
        summary_prompt = (
            "Summarize these intermediate agent steps concisely in bullet points "
            "(preserve tool names, key outputs, errors, and decisions):\n\n" + middle_text
        )
        try:
            if summarize is not None:
                text = summarize(summary_prompt)
            else:
                text = llm.chat([{"role": "user", "content": summary_prompt}])
            if not (text or "").strip():
                raise ValueError("empty compaction summary")
            return text
        except LLMCancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Compaction LLM call failed (%s) — using deterministic fallback",
                type(exc).__name__,
            )
            return _fallback_summary([messages[i] for i in middle])

    summary = summarize_middle() if middle else ""
    summary_cap = _SUMMARY_CAP
    recent_cap = _RECENT_HEAD + _RECENT_TAIL
    compacted, new_goal_idx = _assemble(
        messages, first, middle, last, goal, summary, summary_cap, recent_cap,
    )
    new_total = estimate_messages_tokens(compacted, system, model=_active_model(llm))

    # Deterministic tightening: shrink caps to the floor, then demote the oldest
    # retained non-goal span whole into the middle and rebuild the summary
    # without another summary request. The goal span is never demoted.
    while new_total > limit:
        if summary_cap > _SHRINK_FLOOR or recent_cap > _SHRINK_FLOOR:
            summary_cap = max(_SHRINK_FLOOR, summary_cap // 2)
            recent_cap = max(_SHRINK_FLOOR, recent_cap // 2)
        else:
            victim = next(
                ((s, e) for s, e in _message_spans(messages)
                 if s in last and not s <= goal < e),
                None,
            )
            if victim is None:
                break
            span = set(range(*victim))
            last = [i for i in last if i not in span]
            middle = sorted(set(middle) | span)
            summary = _fallback_summary([messages[i] for i in middle])
        compacted, new_goal_idx = _assemble(
            messages, first, middle, last, goal, summary, summary_cap, recent_cap,
        )
        new_total = estimate_messages_tokens(compacted, system, model=_active_model(llm))

    logger.info("Compacted context: %d → ~%d tokens", total, new_total)
    return compacted, new_goal_idx


def _coerce_model_metadata(metadata: ModelMetadata | dict) -> ModelMetadata:
    """Accept only the catalog's C/T/O fields, not human model config values."""
    if isinstance(metadata, ModelMetadata):
        return metadata
    allowed = {"context_tokens", "input_tokens", "output_tokens"}
    if not isinstance(metadata, dict) or set(metadata) - allowed:
        raise ModelRequestBudgetError("invalid_request")
    return ModelMetadata(**metadata)
