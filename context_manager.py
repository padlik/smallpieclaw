"""
context_manager.py
------------------
Context-window management for the ReAct loop.

Provides token estimation and context compaction: when the running token count
approaches the configured limit, the middle portion of the conversation history
is summarised by the LLM to keep the context window within bounds.

Compaction is content-aware: only the middle history rendered in the summary
prompt is bounded; tool-result messages (shell/file output) retain both ends.
The configured-threshold path makes one attempt per request: the goal and the
last two messages (whole native call/result
spans) are never capped, only a nonempty middle is summarized, and an empty
middle changes and truncates nothing. A failed or empty summary and an invalid
candidate raise ``CompactionFailedError``: the caller keeps the original history
and disables further automatic attempts for that run. Cancellation propagates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from prompt_builder import estimate_messages_tokens
from providers._errors import (
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
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


class CompactionFailedError(Exception):
    """An automatic compaction attempt failed; the caller keeps the original history.

    Carries only fixed labels (never provider, prompt, summary or history text):
    ``reason`` is ``summary_failed``, ``empty_summary`` or ``invalid_candidate`` and
    ``cause_type`` the failing exception's class name (``"-"`` when none).
    """

    def __init__(self, reason: str, cause_type: str = "-") -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause_type = cause_type


def _active_model(llm: "LLMClient") -> str | None:
    """Best-effort active model name for tokenizer-aware estimation.

    Returns None on any failure so estimation falls back to the heuristic; this
    keeps mocked/partial LLM objects in tests working without a real config.
    """
    try:
        return llm.llm_cfg.get("model")
    except Exception:  # noqa: BLE001
        return None


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
) -> tuple[list[dict], int]:
    """Publish a whole summary while keeping goal and recent spans unchanged."""
    out: list[dict] = []
    goal_pos = 0

    def emit(i: int) -> None:
        nonlocal goal_pos
        if i == goal:
            goal_pos = len(out)
        out.append(messages[i])

    for i in first:
        emit(i)
    if middle:
        out.append({
            "role": "user",
            "content": f"[Compacted context — earlier steps summary]\n{summary}",
        })
    for i in last:
        emit(i)
    return out, goal_pos


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


def _summary_prompt(messages: list[dict], middle: list[int]) -> str:
    """Render the compaction prompt for the eligible middle."""
    middle_text = "\n".join(_format_middle_message(messages[i]) for i in middle)
    return (
        "Summarize these intermediate agent steps concisely in bullet points "
        "(preserve tool names, key outputs, errors, and decisions):\n\n" + middle_text
    )


def _summarize_automatic(
    messages: list[dict],
    middle: list[int],
    llm: "LLMClient",
    summarize: Callable[[str], str] | None,
) -> str:
    """Return the stripped, non-empty summary or raise :class:`CompactionFailedError`.

    Only provider failures (``LLMError``, ``LLMEmptyResponseError``, ``httpx.HTTPError``)
    and blank text are compaction failures. Cancellation (``LLMCancelledError``) and
    unexpected programmer errors propagate unchanged.
    """
    prompt = _summary_prompt(messages, middle)
    try:
        if summarize is not None:
            text = summarize(prompt)
        else:
            text = llm.chat([{"role": "user", "content": prompt}])
    except (LLMError, LLMEmptyResponseError, httpx.HTTPError) as exc:
        raise CompactionFailedError("summary_failed", type(exc).__name__) from exc
    if not isinstance(text, str) or not text.strip():
        raise CompactionFailedError("empty_summary")
    return text.strip()


def maybe_compact(
    messages: list[dict],
    system: str,
    llm: "LLMClient",
    *,
    threshold: int,
    goal_idx: int = 0,
    tool_defs_tokens: int = 0,
    input_tokens: int | None = None,
    summarize: Callable[[str], str] | None = None,
) -> tuple[list[dict], int]:
    """Summarize only nonempty middle history once when input exceeds threshold.

    The goal and last two messages (whole native call/result spans) remain
    unchanged. An empty middle returns the original messages without truncation.
    A failed or invalid summary raises ``CompactionFailedError`` without publishing
    a candidate; cancellation propagates. The caller owns run-scoped warning,
    disabling further automatic attempts and retention of original history.
    """
    validate_native_history(messages)
    if not messages:
        return messages, goal_idx
    message_total = estimate_messages_tokens(messages, system, model=_active_model(llm))
    if input_tokens is not None:
        total = input_tokens
        overhead = max(0, input_tokens - message_total)
    else:
        total = message_total + tool_defs_tokens
        overhead = tool_defs_tokens
    if total <= threshold:
        return messages, goal_idx
    goal = min(max(0, goal_idx), len(messages) - 1)
    first, middle, last = _partition_compaction(messages, goal)
    if not middle:
        return messages, goal_idx
    logger.warning("Context size ~%d tokens exceeds threshold %d — compacting…", total, threshold)
    summary = _summarize_automatic(messages, middle, llm, summarize)
    compacted, new_goal_idx = _assemble(messages, first, middle, last, goal, summary)
    try:
        validate_native_history(compacted)
    except LLMPermanentError as exc:
        raise CompactionFailedError("invalid_candidate", type(exc).__name__) from exc
    logger.info(
        "Compacted context: %d → ~%d tokens", total,
        estimate_messages_tokens(compacted, system, model=_active_model(llm)) + overhead,
    )
    return compacted, new_goal_idx
