"""
react_loop.py
-------------
Standalone ReAct loop extracted from AgentController.

The loop receives a ReactContext dataclass that holds all dependencies and
mutable state. This decouples the iteration logic from the controller class.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from builtin_tools.access_control import TrustedZoneChecker

import agent_logging
from builtin_tools.schemas import build_tool_definitions
from confirmation import ConfirmationManager
from context_manager import maybe_compact
from interfaces import ToolCall
from llm_client import LLMClient, LLMCancelledError, LLMError, LLMPermanentError, _encode_images
from memory_store import _summarize_result, extract_tools_used, save_task_outcome
from outcome_utils import fail_outcome
from prompt_loader import build_system_prompt as _build_system_prompt

logger = logging.getLogger(__name__)
slog = agent_logging.get_logger(__name__)

_TOOL_ICONS: dict[str, str] = {
    "shell":            "🖥️",
    "file_read":        "📄",
    "file_write":       "✏️",
    "file_append":      "✏️",
    "spawn_agent":      "🤖",
    "get_agent_result": "🤖",
    "memory_write":     "🧠",
    "memory_read":      "🧠",
    "web_fetch":        "🌐",
    "http_request":     "🌐",
    "vision_query":     "👁️",
}
_DEFAULT_TOOL_ICON = "🔧"


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, _DEFAULT_TOOL_ICON)


def _is_mcp_auth_failure(ctx: ReactContext, tool_name: str, outcome: dict) -> bool:
    """Detect clear MCP authentication/authorization failures in tool outcome.

    Only triggers for servers that have OAuth configured — a non-OAuth server
    returning '401' in its error text is a normal tool error, not an auth failure.
    """
    if not isinstance(outcome, dict) or outcome.get("success") is not False:
        return False
    # Only check for auth failures on OAuth-configured servers
    if ctx.mcp_manager is None:
        return False
    server_name = ctx.mcp_manager.server_name_for_tool(tool_name)  # type: ignore[attr-defined]
    if not server_name:
        return False
    if not ctx.mcp_manager.server_has_oauth(server_name):  # type: ignore[attr-defined]
        return False
    error = (outcome.get("error", "") or "").lower()
    indicators = ("401", "unauthorized", "token expired", "refresh failed", "invalid token", "access denied")
    return any(ind in error for ind in indicators)


def _handle_mcp_auth_failure(ctx: ReactContext, tool_name: str, outcome: dict) -> dict:
    """Transition the owning MCP server to needs_auth and return a helpful error."""
    server_name = ""
    if ctx.mcp_manager is not None:
        server_name = ctx.mcp_manager.server_name_for_tool(tool_name)  # type: ignore[attr-defined]
        ctx.mcp_manager.mark_needs_auth(server_name)  # type: ignore[attr-defined]
    display_name = server_name or "unknown"
    return {
        "success": False,
        "output": "",
        "error": (
            f"MCP token expired for server '{display_name}'. "
            f"Run `/mcp auth {display_name}` to re-authenticate."
        ),
        "exit_code": -1,
    }


def _format_parent_context(ctx: ReactContext) -> str:
    """Format a PARENT CONTEXT section for sub-agents from stored payload.

    For main agents (depth 0) this always returns an empty string. For
    sub-agents the payload was set on the AgentController by SubAgentRunner
    before the run started; if present it is rendered as a short markdown
    section.
    """
    if ctx.depth == 0:
        return ""
    payload = getattr(ctx, "_context_payload", None)
    if not payload:
        return ""
    try:
        truncated = _truncate_context_payload(payload, max_chars=2000)
        lines = ["PARENT CONTEXT (injected by parent agent):"]
        for key, value in truncated.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _truncate_context_payload(payload: dict, max_chars: int = 2000) -> dict:
    """Truncate context payload values to fit within max_chars while preserving all keys."""
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= max_chars:
        return payload
    keys = list(payload.keys())
    header = "PARENT CONTEXT (injected by parent agent):"
    # Allocate budget per key, reserving space for header and key formatting.
    budget = max_chars - len(header) - sum(len(k) + 4 for k in keys)
    per_key = max(30, budget // max(1, len(keys)))
    result = {}
    for k, v in payload.items():
        text = str(v)
        if len(text) > per_key:
            text = text[: per_key - 3].rstrip() + "..."
        result[k] = text
    return result


# ---------------------------------------------------------------------------
# Context object — bundles all dependencies for the loop
# ---------------------------------------------------------------------------


@dataclass
class ToolTrace:
    """Record of a single tool call made during agent execution."""
    tool_name: str
    args_repr: str      # compact summary of args, never raw file contents
    success: bool
    duration_ms: float
    error: str = ""     # only populated when success=False
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReactContext:
    """All state and dependencies the ReAct loop needs."""

    # Core services
    llm: LLMClient
    tool_index: object          # ToolIndex
    memory: object              # MemoryStore
    builtin_executor: object    # Optional BuiltinExecutor
    mcp_manager: object         # Optional MCPManager
    skill_registry: object      # Optional SkillRegistry

    # Configuration
    max_iterations: int = 8
    max_subagents: int = 6
    top_tools: int = 3
    ctx_max_tokens: int = 90_000
    tmp_dir: str = "/tmp/agent"
    downloads_dir: str = "downloads"
    workspace_dir: str = "~/Documents"
    log_file: str = "agent.log"
    log_backup_count: int = 30
    depth: int = 0
    label: str = "main"

    # Request-scoped trace ID for correlating one run across LLM, tool, and
    # confirmation log lines. Defaulted so test/standalone constructors that omit
    # it still work; AgentController.run() assigns a fresh ID per run.
    trace_id: str = ""

    # Memory layers
    short_term: object = None   # Optional ShortTermMemory
    working: object = None      # Optional WorkingMemory
    results: object = None      # Optional ResultsMemory

    # Cancellation
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # Whether this loop owns its cancel_event. When False (event is shared,
    # e.g. a stop signal forwarded into sub-agents), react_loop
    # must NOT clear it at startup — doing so would erase a stop request that
    # arrived just before/while the sub-agent began running.
    owns_cancel_event: bool = True

    # Step callback
    on_step: Optional[Callable[[int], None]] = None

    # Tool trace callback — called after each tool dispatch with a ToolTrace record
    on_tool_trace: Optional[Callable] = None  # Optional[Callable[[ToolTrace], None]]

    # Scheduled job history — called to get a formatted string for the system prompt
    job_history_fn: Optional[Callable[[], str]] = None

    # Graph memory — Optional[GraphMemoryStore]; None when feature is disabled
    graph_memory: Optional[object] = None

    # Graph memory writer — Optional[GraphMemoryWriter]; for enqueuing new messages
    graph_memory_writer: Optional[object] = None

    # Max graph context entries to inject per turn
    graph_memory_max_entries: int = 10

    # Strategy memory — Optional[StrategyMemory]; None when disabled/unconfigured
    strategy_memory: Optional[object] = None

    # Zone-based file access control — Optional to allow existing tests/sub-agents to construct
    # ReactContext without wiring; production paths always inject this from main.py.
    trusted_zone_checker: Optional["TrustedZoneChecker"] = None


    # Creativity mode for prompt assembly — passed through to prompt_loader
    creativity_mode: str = "default"
    # Maximum iterations allowed during/after a plan execution
    plan_max_iterations: int = 50
    # Minutes of inactivity before injecting a soft "still working?" prompt
    inactivity_warn_minutes: int = 15

    # Sub-agent context sharing (set by AgentController.run for sub-agents).
    # _context_payload is the parent context dict injected as PARENT CONTEXT;
    # _prompt_variant selects the sub-agent prompt variant ("sub-agent").
    _context_payload: Optional[dict] = None
    _prompt_variant: Optional[str] = None

    # Confirmation coordination (shared with AgentController and Telegram)
    confirmation: ConfirmationManager = field(default_factory=ConfirmationManager)

    # Native tool calling — cached tool definitions built once at loop start
    _tool_defs: Optional[list[dict]] = None

    @property
    def log_prefix(self) -> str:
        if self.trace_id:
            return f"[{self.label} {self.trace_id}] "
        return f"[{self.label}] "

    @property
    def caller_tag(self) -> str:
        """Label + trace ID for downstream log tags (executors wrap in brackets)."""
        if self.trace_id:
            return f"{self.label} {self.trace_id}"
        return self.label


# ---------------------------------------------------------------------------
# Static helpers (previously AgentController static methods)
# ---------------------------------------------------------------------------


def extract_json_candidates(text: str) -> list[str]:
    """
    Brace-counting extractor: returns all balanced {…} substrings found in text.
    Handles multiple JSON objects in a single response and prose-wrapped objects.
    """
    candidates = []
    depth = 0
    start = -1
    in_string = False
    escape_next = False
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(text[start:i + 1])
                start = -1
    return candidates


def parse_json(text: str) -> Optional[dict]:
    """Extract and parse the first valid JSON action object found in the text."""
    text = text.strip()
    if not text:
        return None

    # 1. Try the whole text as-is
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences then try again
    fence_match = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 3. Brace-counting extractor
    candidates = extract_json_candidates(text)
    first_valid: Optional[dict] = None
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if not isinstance(obj, dict):
                continue
            if first_valid is None:
                first_valid = obj
            if "action" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    if first_valid is not None:
        return first_valid

    return None


def fmt_tool_call(tool_name: str, args: dict) -> str:
    """Format a tool call as a compact, readable string for progress display."""
    if tool_name == "shell":
        cmd = args.get("command", "")
        return f"```\n$ {cmd}\n```"
    if tool_name == "file_read":
        return f"```\nread: {args.get('path', '?')}\n```"
    if tool_name == "file_write":
        path = args.get("path", "?")
        size = len(args.get("content", ""))
        return f"```\nwrite: {path} ({size} bytes)\n```"
    try:
        arg_str = json.dumps(args, ensure_ascii=False)
    except Exception:
        arg_str = str(args)
    if len(arg_str) > 200:
        arg_str = arg_str[:197] + "…"
    return f"```\n{arg_str}\n```" if arg_str and arg_str != "{}" else ""


def fmt_tool_result_progress(tool_name: str, args: dict, outcome: dict) -> str:
    """Format a tool result as a short progress update."""
    call = fmt_tool_call(tool_name, args)
    log_note = ""
    if outcome.get("full_log_path"):
        log_note = f"\n📄 full log: `{outcome['full_log_path']}`"
    if outcome.get("success", False):
        out = (outcome.get("output") or "").strip()
        # Include stderr even on success (warnings, compiler diagnostics, etc.)
        err = (outcome.get("error") or "").strip()
        combined = "\n".join(filter(None, [out, ("--- stderr ---\n" + err) if err else ""]))
        if combined:
            lines = combined.splitlines()
            # Tail semantics: show the last 8 lines (errors/results appear at the end)
            if len(lines) > 8:
                preview = "…\n" + "\n".join(lines[-8:])
            else:
                preview = "\n".join(lines)
            if len(preview) > 400:
                preview = "…" + preview[-399:]
            return f"{_tool_icon(tool_name)} **{tool_name}** ✅\n{call}\n```\n{preview}\n```{log_note}"
        return f"{_tool_icon(tool_name)} **{tool_name}** ✅\n{call}\n_(no output)_{log_note}"
    else:
        err = (outcome.get("error") or outcome.get("output") or "failed").strip()
        if len(err) > 300:
            # Tail semantics for errors too
            err = "…" + err[-297:]
        return f"{_tool_icon(tool_name)} **{tool_name}** ❌\n{call}\n```\n{err}\n```{log_note}"


def format_tool_result(tool_name: str, outcome: dict) -> str:
    """Format a tool result as a message for the LLM."""
    if outcome.get("success", False):
        output = outcome.get("output") or "(no output)"
        # Include stderr even for successful commands; warnings/diagnostics matter.
        stderr = (outcome.get("error") or "").strip()
        if stderr:
            return f"Tool '{tool_name}' succeeded:\n{output}\nstderr:\n{stderr}"
        return f"Tool '{tool_name}' succeeded:\n{output}"
    else:
        parts = [f"Tool '{tool_name}' failed (exit {outcome.get('exit_code', '?')})."]
        if outcome.get("error"):
            parts.append(f"stderr: {outcome['error']}")
        if outcome.get("output"):
            parts.append(f"stdout: {outcome['output']}")
        # Surface structured recovery metadata so the (sub-)agent can echo it back
        # in its result. PlanExecutor relies on these fields to decide retries.
        if outcome.get("error_type"):
            parts.append(f"error_type: {outcome['error_type']}")
            parts.append(f"recoverable: {bool(outcome.get('recoverable', False))}")
            if outcome.get("suggestion"):
                parts.append(f"suggestion: {outcome['suggestion']}")
        return "\n".join(parts)


def _exec_vision_query(ctx: ReactContext, args: dict) -> dict:
    """Execute a vision_query built-in: ask the LLM to analyse a local image file."""
    from builtin_tools.access_control import ZoneClassification
    from builtin_tools.patterns import _is_sensitive_path

    path = str(args.get("path", "")).strip()
    question = args.get("question", "What is in this image?")
    if not path:
        return {"success": False, "output": "", "error": "vision_query: 'path' argument is required."}

    # Zone gate: vision_query reads a file, so apply the same gate as file_read.
    real_path = os.path.realpath(os.path.expanduser(path))
    checker = getattr(ctx, "trusted_zone_checker", None)
    if checker is not None:
        builtin = getattr(ctx, "builtin_executor", None)
        request_grants = (
            builtin.grant_tracker.snapshot()
            if builtin is not None and builtin.grant_tracker is not None
            else frozenset()
        )
        zone = checker.classify(
            path, operation="read",
            request_grants=request_grants,
        )
        sensitive, reason = _is_sensitive_path(real_path)
        if zone == ZoneClassification.UNRECOGNISED or sensitive:
            builtin = getattr(ctx, "builtin_executor", None)
            if builtin is None:
                return {
                    "success": False, "output": "",
                    "error": "vision_query: confirmation infrastructure not available.",
                }
            desc = f"Vision query: <code>{path}</code>"
            if real_path != path:
                desc += f"\n(→ <code>{real_path}</code>)"
            if sensitive:
                desc += f"\n⚠️ Reason: {reason}"
            return builtin._requires_confirmation(
                "vision_query", args, desc,
                caller_depth=ctx.depth, caller_tag=ctx.caller_tag,
                zone_path=real_path,
            )
    else:
        # Checker unwired: degrade to sensitive-only gate (same as file_read).
        logger.error("Zone: trusted_zone_checker not wired — falling back to sensitive-only gate for vision_query")
        sensitive, reason = _is_sensitive_path(real_path)
        if sensitive:
            builtin = getattr(ctx, "builtin_executor", None)
            if builtin is None:
                return {
                    "success": False, "output": "",
                    "error": "vision_query: confirmation infrastructure not available.",
                }
            desc = f"Vision query: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
            if real_path != path:
                desc += f"\n(→ <code>{real_path}</code>)"
            return builtin._requires_confirmation(
                "vision_query", args, desc,
                caller_depth=ctx.depth, caller_tag=ctx.caller_tag,
            )

    encoded = _encode_images([path])
    if not encoded:
        return {
            "success": False, "output": "",
            "error": f"vision_query: could not read or encode image at '{path}'. "
                     "Check that the path is correct and the file exists.",
        }
    messages = [{"role": "user", "content": question, "images": [path]}]
    try:
        answer = ctx.llm.chat(messages, system=None)
    except LLMCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "output": "", "error": f"vision_query LLM call failed: {exc}"}
    return {"success": True, "output": answer, "error": ""}


def _append_native_tool_result(messages: list[dict], tc: ToolCall, content: str) -> None:
    """Append the assistant tool-call turn and its matching tool-result message.

    Native multi-turn dispatch requires the OpenAI wire shape: an assistant
    message carrying the ``tool_calls`` entry, immediately followed by a ``tool``
    message keyed by the same ``tool_call_id``. Centralising this keeps every
    intercept site (standard tool, plan, vision_query) identical.

    Guards two provider-rejection cases: an empty ``tc.id`` (some models omit it)
    is replaced with a generated ``call_<hex>`` id so the assistant and tool
    turns stay linked, and a non-string ``content`` is coerced to ``""`` because
    OpenAI 400s on ``content: null`` in a ``role:"tool"`` message.
    """
    call_id = tc.id or f"call_{secrets.token_hex(4)}"
    if not isinstance(content, str):
        content = ""
    messages.append({"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
    }]})
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


def _linearize_native_turns(messages: list[dict]) -> list[dict]:
    """Flatten native tool-calling turns into plain text for the json_mode path.

    Native multi-turn dispatch writes OpenAI wire-format messages into the shared
    ``messages`` list (see ``_append_native_tool_result``): an assistant message
    carrying ``tool_calls`` with ``content: None``, immediately followed by a
    ``tool`` message keyed by ``tool_call_id``. The provider ``chat`` backends
    used by the json_mode fallback only preserve ``role`` and ``content``,
    dropping ``tool_calls`` and
    ``tool_call_id``. Sending those stripped messages produces malformed payloads
    (an assistant with ``content: null`` and no ``tool_calls``, an orphan
    ``role: "tool"`` with no ``tool_call_id``) that providers reject with a 400,
    aborting the run.

    This returns a *new* list in which native-format turns are converted to plain
    text the json_mode builders can serialize safely:

    - Assistant messages with ``tool_calls`` become
      ``{"role": "assistant", "content": "Called tool: <name>(<args_summary>)"}``.
    - ``tool`` messages become ``{"role": "user", "content": <tool_result>}``.

    All other messages pass through unchanged. Conversion is 1:1, so the message
    count is preserved and any goal-index anchor into the list stays valid. It is
    also idempotent: already-linearized (plain) messages have no native fields and
    pass through untouched.
    """
    linearized: list[dict] = []
    for m in messages:
        role = m.get("role")
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            parts = []
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = func.get("name", "tool")
                raw_args = func.get("arguments", "")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
                parts.append(f"{name}({_compact_args_repr(name, parsed_args)})")
            linearized.append({
                "role": "assistant",
                "content": "Called tool: " + ", ".join(parts),
            })
        elif role == "tool":
            linearized.append({
                "role": "user",
                "content": m.get("content") or "",
            })
        else:
            linearized.append(m)
    return linearized


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_BUILTIN_NAMES = frozenset({
    "shell", "file_read", "file_write", "schedule",
    "spawn_agent", "get_agent_result", "memory_write",
})

_JSON_FAIL_LIMIT = 3
_ABSOLUTE_PLAN_CEILING = 200


@dataclass
class _LoopState:
    """Mutable per-run loop state."""
    messages: list[dict]
    goal_idx: int
    max_steps: int
    step: int = 0
    json_fail_streak: int = 0
    operator_cancelled: bool = False
    last_action_time: float = field(default_factory=time.time)
    warned_inactivity: bool = False


@dataclass
class _Turn:
    """Result of one LLM call."""
    tool_calls: list          # non-empty → native path
    raw: str                  # LLM text output
    text_from_native: bool
    early_return: Optional[str]            # cancelled or error; if set, return immediately
    linearized_messages: Optional[list] = None  # set when messages were linearized for fallback


def _assemble_system_prompt(ctx: ReactContext, user_goal: str) -> str:
    """Assemble the full system prompt string for a run."""
    pfx = ""
    _job_history_section = ""
    if ctx.job_history_fn:
        try:
            _job_history_section = ctx.job_history_fn() or ""
        except Exception as _jh_exc:
            logger.warning("%sFailed to get job history: %s", pfx, _jh_exc)

    _graph_context_section = ""
    if ctx.graph_memory is not None:
        try:
            _graph_context_section = (
                ctx.graph_memory.format_for_prompt(
                    user_goal, max_entries=ctx.graph_memory_max_entries
                ) or ""
            )
            if _graph_context_section:
                logger.info("%sGraph memory context injected", pfx)
            else:
                logger.debug("%sGraph memory context: no relevant data found", pfx)
        except Exception as _gm_exc:
            logger.debug("%sGraph memory context failed: %s", pfx, _gm_exc)

    strategies_section = ""
    if ctx.strategy_memory is not None:
        from strategy_memory import classify_task_type, format_strategies_for_prompt
        task_type = classify_task_type(user_goal)
        strategies = ctx.strategy_memory.get_top_k(task_type, k=2)
        if strategies:
            strategies_section = format_strategies_for_prompt(strategies)

    parent_context_section = _format_parent_context(ctx)
    _sub_agent_prompt_variant = getattr(ctx, "_prompt_variant", None) if ctx.depth >= 1 else None

    system, _ = _build_system_prompt(
        tool_index=ctx.tool_index,
        memory=ctx.memory,
        results=ctx.results,
        skill_registry=ctx.skill_registry,
        llm=ctx.llm,
        tmp_dir=ctx.tmp_dir,
        downloads_dir=ctx.downloads_dir,
        workspace_dir=ctx.workspace_dir,
        log_file=ctx.log_file,
        log_backup_count=ctx.log_backup_count,
        top_tools=ctx.top_tools,
        user_goal=user_goal,
        job_history_section=_job_history_section,
        graph_context_section=_graph_context_section,
        strategies_section=strategies_section,
        # Suppress ResultsMemory recall when graph memory already supplied
        # semantic context this turn — avoids redundant/overlapping recall.
        results_top_k=0 if _graph_context_section else 2,
        parent_context_section=parent_context_section,
        mode=_sub_agent_prompt_variant or ctx.creativity_mode,
    )
    return system


def _init_messages(
    ctx: ReactContext,
    user_goal: str,
    images: Optional[list[str]],
) -> tuple[list[dict], int]:
    """Build the initial messages list and return (messages, goal_idx)."""
    pfx = ""
    first_msg: dict = {"role": "user", "content": user_goal}
    if images:
        first_msg["images"] = images
        logger.info("%s%d image(s) attached to request", pfx, len(images))

    messages: list[dict] = []
    if ctx.short_term:
        messages.extend(ctx.short_term.get_messages())
    # Record the index of the current goal before appending it so that
    # maybe_compact can pin the goal as the preserved anchor rather than
    # treating messages[0] (stale short-term history) as the goal.
    goal_idx: int = len(messages)
    messages.append(first_msg)
    return messages, goal_idx


def _ensure_tool_defs(ctx: ReactContext) -> None:
    """Lazily build _tool_defs if not already populated."""
    pfx = ""
    if ctx._tool_defs is None:
        try:
            ctx._tool_defs = build_tool_definitions(
                mcp_manager=ctx.mcp_manager,
            )
        except Exception as _btd_exc:  # noqa: BLE001
            logger.warning(
                "%sbuild_tool_definitions failed: %s — MCP tools skipped",
                pfx, _btd_exc,
            )
            ctx._tool_defs = build_tool_definitions(mcp_manager=None)


def _normalize_shorthand_action(action_obj: dict) -> dict:
    """Normalize shorthand action keys to canonical form. Returns the (possibly mutated) dict."""
    pfx = ""
    action = action_obj.get("action", "")
    if action not in _BUILTIN_NAMES:
        return action_obj
    logger.warning("%sLLM used shorthand action '%s' — normalizing to tool call", pfx, action)
    if "args" in action_obj:
        shorthand_args = action_obj["args"]
        if isinstance(shorthand_args, str):
            try:
                parsed = json.loads(shorthand_args)
                if isinstance(parsed, (dict, list)):
                    shorthand_args = parsed
                else:
                    logger.warning(
                        "Shorthand action '%s' args parsed to non-dict type %s — wrapping in _raw",
                        action, type(parsed).__name__,
                    )
                    shorthand_args = {"_raw": shorthand_args}
            except (ValueError, TypeError):
                logger.warning(
                    "Shorthand action '%s' args is a non-JSON string — wrapping in _raw: %s",
                    action, shorthand_args[:200],
                )
                shorthand_args = {"_raw": shorthand_args}
    else:
        shorthand_args = {k: v for k, v in action_obj.items() if k != "action"}
    return {"action": "tool", "tool": action, "args": shorthand_args}


def _request_turn(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    progress: Callable[[str], None],
) -> _Turn:
    """Make one LLM call and return the structured turn result."""
    pfx = ""
    raw = ""
    tool_calls: list[ToolCall] = []
    native_attempted = False
    text_from_native = False
    linearized_messages = None
    _MAX_EMPTY_RETRIES = 2

    _supports_native = hasattr(ctx.llm, "chat_with_tools_fallback") and callable(
        ctx.llm.chat_with_tools_fallback
    )
    if ctx._tool_defs and _supports_native:
        try:
            response = ctx.llm.chat_with_tools_fallback(
                state.messages, tools=ctx._tool_defs, system=system, progress_cb=progress,
            )
            native_attempted = True
            if response.is_tool_call and response.tool_calls:
                tool_calls = response.tool_calls
            elif response.text:
                raw = response.text
                text_from_native = True
        except NotImplementedError:
            logger.debug("%sNative tool calling not supported by provider — falling back to json_mode", pfx)
        except LLMPermanentError:
            raise
        except LLMError:
            logger.warning("%sNative tool calling failed (LLMError) — falling back to json_mode", pfx)
        except Exception as exc:
            logger.warning(
                "%sNative tool calling unexpected error: %s — falling back to json_mode",
                pfx, exc,
            )

    if not raw and not native_attempted:
        linearized_messages = _linearize_native_turns(state.messages)
        for attempt in range(1 + _MAX_EMPTY_RETRIES):
            try:
                raw = ctx.llm.chat_with_fallback(
                    linearized_messages, system=system, progress_cb=progress, json_mode=True,
                )
            except LLMCancelledError:
                logger.info("Agent LLM call cancelled at step %d/%d", state.step, state.max_steps)
                return _Turn([], "", False, "[Cancelled]")
            except Exception as exc:
                err = f"❌ LLM error: {type(exc).__name__}: {exc}"
                progress(err)
                return _Turn([], "", False, err)
            if raw.strip():
                break
            if attempt < _MAX_EMPTY_RETRIES:
                logger.warning(
                    "%sLLM returned empty response (step %d/%d), retrying (%d/%d)…",
                    pfx, state.step, state.max_steps, attempt + 1, _MAX_EMPTY_RETRIES,
                )
                progress(f"⏳ Empty LLM response, retrying ({attempt + 1}/{_MAX_EMPTY_RETRIES})…")

    return _Turn(tool_calls, raw, text_from_native, None, linearized_messages)


def _result_sink(
    state: _LoopState,
    tc: Optional[ToolCall] = None,
) -> Callable[[str], None]:
    # Lambdas capture `state` (the dataclass), not `state.messages` directly.
    # This means they read state.messages at *call* time, so they correctly
    # append to the new list after any maybe_compact or linearization reassignment.
    """Return a function that appends a tool result to messages in the correct format."""
    if tc is not None:
        return lambda content: _append_native_tool_result(state.messages, tc, content)
    return lambda content: state.messages.append({"role": "user", "content": content})


def _dispatch_action(
    ctx: ReactContext,
    action_obj: dict,
    sink: Callable[[str], None],
    state: _LoopState,
    user_goal: str,
    run_start: float,
    progress: Callable[[str], None],
) -> Optional[str]:
    """Dispatch one action. Returns final result string on finish; None otherwise.

    May mutate state.max_steps when dispatching a plan action (to give the agent
    room to complete the plan steps).
    """
    action = action_obj.get("action", "")

    if action == "finish":
        return _finish_run(ctx, action_obj, user_goal, state.step, run_start)

    if action == "tool":
        tool_name = action_obj.get("tool", "")
        args = action_obj.get("args", {})
        if isinstance(args, list):
            args = {str(i): v for i, v in enumerate(args)}
            action_obj = {**action_obj, "args": args}
        _t0 = time.time()
        # vision_query needs LLM access — call directly, never through _dispatch_tool
        if tool_name == "vision_query":
            outcome = _exec_vision_query(ctx, args)
        else:
            outcome = _dispatch_tool(ctx, action_obj, progress)
        _duration_ms = (time.time() - _t0) * 1000
        _emit_tool_trace(
            ctx, tool_name, args, success=outcome.get("success", False),
            duration_ms=_duration_ms,
            error=outcome.get("error", "") if not outcome.get("success", False) else "",
        )
        if ctx.working:
            ctx.working.add_step("tool", {"tool": tool_name, "args": args, "success": outcome.get("success", False)})
        if outcome.get("send_file"):
            path_b64 = base64.b64encode(outcome["send_file"].encode()).decode()
            caption_b64 = base64.b64encode(outcome.get("caption", "").encode()).decode()
            progress(f"__FILE__:{path_b64}:{caption_b64}")
        tool_result = format_tool_result(tool_name, outcome)
        if outcome.get("success", False):
            logger.info("Tool '%s' result: success=True", tool_name)
        else:
            logger.warning(
                "Tool '%s' result: success=False | error=%s | args=%s",
                tool_name, outcome.get("error", ""),
                {k: str(v)[:120] for k, v in args.items()},
            )
        progress(fmt_tool_result_progress(tool_name, args, outcome))
        sink(tool_result)
        if outcome.get("_operator_cancelled") or ctx.cancel_event.is_set():
            state.operator_cancelled = True
        return None

    if action == "plan":
        plan_data = action_obj.get("plan", {})
        result_msg, new_max_steps, _ = _run_plan(ctx, plan_data, state.max_steps, progress)
        if new_max_steps is not None:
            state.max_steps = max(state.step, min(int(new_max_steps), _ABSOLUTE_PLAN_CEILING))
        sink(result_msg)
        if ctx.cancel_event.is_set():
            state.operator_cancelled = True
        return None

    logger.warning("Unknown action '%s' from LLM", action)
    sink(f'Unknown action "{action}". Use "tool", "plan", or "finish".')
    return None


def react_loop(
    ctx: ReactContext,
    user_goal: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    images: Optional[list[str]] = None,
) -> str:
    """Execute the ReAct loop: LLM → parse → dispatch → repeat. Returns the final answer string."""
    run_start = time.time()
    pfx = ""
    _ctx_tokens = agent_logging.bind_run_context(trace=ctx.trace_id, agent=ctx.label)
    agent_logging.log_event(agent_logging.LogEvent.RUN_BEGIN, "run begin", level=logging.INFO, logger=slog)

    def _progress(msg: str):
        if progress_callback:
            progress_callback(msg)
        logger.debug("%sAgent progress: %s", pfx, msg)

    try:
        ctx._tool_defs = None
        if ctx.owns_cancel_event:
            ctx.cancel_event.clear()

        active_model = ctx.llm.llm_cfg.get("model", "?")
        logger.info("%sstart | model: %s | goal: %s", pfx, active_model, user_goal[:80])

        if ctx.working:
            ctx.working.start_task(user_goal)

        system = _assemble_system_prompt(ctx, user_goal)
        messages, goal_idx = _init_messages(ctx, user_goal, images)
        ctx.memory.record_event(f"User request: {user_goal[:100]}")

        if ctx.graph_memory_writer is not None:
            try:
                ctx.graph_memory_writer.enqueue(user_goal, source="chat")
            except Exception as _gw_exc:  # noqa: BLE001
                logger.debug("%sGraph memory enqueue failed: %s", pfx, _gw_exc)

        _ensure_tool_defs(ctx)

        state = _LoopState(messages=messages, goal_idx=goal_idx, max_steps=ctx.max_iterations)

        while True:
            while state.step < state.max_steps:
                if ctx.cancel_event.is_set():
                    logger.warning("%scancelled at step %d/%d", pfx, state.step, state.max_steps)
                    return "[Cancelled]"

                if not state.warned_inactivity and state.step > 1:
                    warn_minutes = getattr(ctx, "inactivity_warn_minutes", 0)
                    if warn_minutes and (time.time() - state.last_action_time) > (warn_minutes * 60):
                        state.warned_inactivity = True
                        minutes = round((time.time() - state.last_action_time) / 60)
                        state.messages.append({
                            "role": "user",
                            "content": f"You've been running for {minutes} minutes without finishing. Are you still working? If you're done, use finish.",
                        })
                        _progress(f"⏳ Inactivity prompt after {minutes}m…")
                        continue

                state.step += 1
                state.last_action_time = time.time()

                agent_logging.log_event(
                    agent_logging.LogEvent.STEP_BEGIN, "step begin",
                    level=logging.INFO, logger=slog, step=state.step,
                )

                if ctx.on_step:
                    try:
                        ctx.on_step(state.step)
                    except Exception:
                        pass
                active_model = ctx.llm.llm_cfg.get("model", "?")
                logger.info("%sstep %d/%d | model: %s", pfx, state.step, state.max_steps, active_model)
                _progress(f"⚙️ Thinking… (step {state.step})")

                state.messages, state.goal_idx = maybe_compact(
                    state.messages, system, ctx.ctx_max_tokens, ctx.llm, goal_idx=state.goal_idx,
                )

                turn = _request_turn(ctx, state, system, _progress)
                if turn.early_return is not None:
                    return turn.early_return
                if turn.linearized_messages is not None:
                    state.messages = turn.linearized_messages

                if turn.tool_calls:
                    tc = turn.tool_calls[0]
                    if tc.name == "plan":
                        action_obj = {"action": "plan", "plan": tc.arguments}
                    elif tc.name == "finish":
                        action_obj = {"action": "finish", "result": (tc.arguments or {}).get("result", "Done.")}
                    else:
                        action_obj = {"action": "tool", "tool": tc.name, "args": tc.arguments}
                    sink = _result_sink(state, tc)
                else:
                    action_obj = parse_json(turn.raw)
                    if action_obj is None and turn.text_from_native:
                        logger.warning(
                            "%sNative path: model returned prose (no tool_calls) — treating as finish. "
                            "In json_mode this would re-prompt; with native tool calling the run ends here.",
                            pfx,
                        )
                        action_obj = {"action": "finish", "result": turn.raw}
                    if action_obj is None:
                        state.json_fail_streak += 1
                        logger.warning(
                            "%sLLM returned non-JSON (step %d/%d, streak %d, ~%d chars):\n--- BEGIN ---\n%s\n--- END ---",
                            pfx, state.step, state.max_steps, state.json_fail_streak,
                            len(turn.raw), turn.raw[:1000],
                        )
                        if state.json_fail_streak >= _JSON_FAIL_LIMIT:
                            logger.error(
                                "%sNon-JSON streak reached %d — aborting with protocol error",
                                pfx, state.json_fail_streak,
                            )
                            err_msg = (
                                f"❌ Agent protocol error: model returned non-JSON "
                                f"{state.json_fail_streak} times in a row. "
                                f"Last response (truncated to 500 chars): {turn.raw[:500]}"
                            )
                            _progress(err_msg)
                            return err_msg
                        state.messages.append({"role": "assistant", "content": turn.raw})
                        state.messages.append({
                            "role": "user",
                            "content": (
                                'ERROR: Your response was not valid JSON. '
                                'You MUST respond with ONLY a raw JSON object — no markdown, '
                                'no prose, no ```json fences. Example: '
                                '{"action": "tool", "tool": "shell", "args": {"command": "df -h"}}'
                            ),
                        })
                        continue
                    state.json_fail_streak = 0
                    state.messages.append({"role": "assistant", "content": turn.raw})
                    action_obj = _normalize_shorthand_action(action_obj)
                    sink = _result_sink(state)

                final = _dispatch_action(
                    ctx, action_obj, sink, state, user_goal, run_start, _progress,
                )
                if final is not None:
                    return final
                if state.operator_cancelled:
                    break
                agent_logging.log_event(
                    agent_logging.LogEvent.STEP_END, "step end",
                    level=logging.INFO, logger=slog, step=state.step,
                )

            if state.operator_cancelled:
                ctx.memory.record_event("Task cancelled by operator")
                return "⚠️ Task stopped by operator."

            ext_response = ctx.confirmation.request_extension(state.max_steps, _progress)
            if ext_response == "unlimited":
                state.max_steps = 10_000_000
                logger.info("%sAgent steps set to unlimited by user", pfx)
                _progress("♾️ Running until done (unlimited steps)…")
                continue
            elif ext_response == "yes":
                state.max_steps += 10
                logger.info("%sAgent steps extended to %d by user", pfx, state.max_steps)
                _progress(f"⏩ Extended — continuing to step {state.max_steps}…")
                continue
            break

        ctx.memory.record_event("Agent hit max iterations")
        return "⚠️ Agent reached maximum steps. Operation cancelled."
    finally:
        agent_logging.log_event(
            agent_logging.LogEvent.RUN_END,
            "run end",
            level=logging.INFO,
            logger=slog,
            dur_ms=int((time.time() - run_start) * 1000),
        )
        agent_logging.reset_run_context(_ctx_tokens)


# ---------------------------------------------------------------------------
# Tool dispatch helpers
# ---------------------------------------------------------------------------


def _compact_args_repr(tool_name: str, args: dict, max_len: int = 200) -> str:
    """Build a compact, single-line summary of tool arguments (never raw file contents)."""
    skip_keys = {"code", "content", "text", "body", "data"}
    parts = []
    for k, v in args.items():
        if k in skip_keys:
            parts.append(f"{k}=<{len(str(v))}chars>")
        else:
            s = str(v)
            parts.append(f"{k}={s[:60]}{'…' if len(s) > 60 else ''}")
    summary = ", ".join(parts)
    return summary[:max_len] + ("…" if len(summary) > max_len else "")


def _emit_tool_trace(
    ctx: ReactContext,
    tool_name: str,
    args: dict,
    *,
    success: bool,
    duration_ms: float,
    error: str = "",
) -> None:
    """Emit a tool trace event if a handler is registered."""
    if ctx.on_tool_trace is not None:
        ctx.on_tool_trace(ToolTrace(
            tool_name=tool_name,
            args_repr=_compact_args_repr(tool_name, args),
            success=success,
            duration_ms=round(duration_ms, 1),
            error=error,
        ))


def _run_plan(
    ctx: ReactContext,
    plan_data: dict,
    max_steps: int,
    progress: Callable[[str], None],
) -> tuple[str, int, bool]:
    """Execute a plan. Returns (result_message, new_max_steps, success)."""
    from execution_plan import ExecutionPlan, PlanExecutor, PlanStep  # noqa: PLC0415

    pfx = ""
    old_max_steps = max_steps
    plan_limit = getattr(ctx, "plan_max_iterations", 0) or (ctx.max_iterations + 20)
    if plan_limit > _ABSOLUTE_PLAN_CEILING:
        plan_limit = _ABSOLUTE_PLAN_CEILING
        logger.warning("%splan | clamped plan_max_iterations to %d", pfx, _ABSOLUTE_PLAN_CEILING)
    if plan_limit > max_steps:
        max_steps = plan_limit
        logger.info("%splan | raised max_steps from %d to %d", pfx, old_max_steps, max_steps)

    _t0 = time.time()
    plan_success = True
    progress("📋 Executing plan…")
    logger.info("%splan | description: %s", pfx, plan_data.get("description", "")[:60])
    try:
        steps_raw = plan_data.get("steps", [])
        steps = [
            PlanStep(
                id=s["id"],
                tool=s["tool"],
                args=s.get("args", {}),
                depends_on=s.get("depends_on", []),
                description=s.get("description", ""),
            )
            for s in steps_raw
        ]
        plan = ExecutionPlan(
            description=plan_data.get("description", ""),
            steps=steps,
            timeout=plan_data.get("timeout", 300),
        )
        executor = PlanExecutor(max_concurrent=ctx.max_subagents)
        plan_result = executor.execute(plan, ctx, progress_cb=progress)
        logger.info("%splan | completed | results: %d", pfx, len(plan_result.get("results", {})))
        if max_steps > old_max_steps:
            max_steps = old_max_steps + 10
            if max_steps > _ABSOLUTE_PLAN_CEILING:
                max_steps = _ABSOLUTE_PLAN_CEILING
            logger.info("%splan | adjusted max_steps to %d after completion", pfx, max_steps)
        result_msg = f"Plan execution results:\n{json.dumps(plan_result, ensure_ascii=False, indent=2)}"
    except Exception as exc:
        plan_success = False
        result_msg = f"Plan execution failed: {type(exc).__name__}: {exc}"
        logger.error("%s%s", pfx, result_msg)

    _duration_ms = (time.time() - _t0) * 1000
    _emit_tool_trace(ctx, "plan", plan_data, success=plan_success, duration_ms=_duration_ms,
                     error="" if plan_success else "plan execution failed")
    if ctx.working:
        ctx.working.add_step("plan", {"description": plan_data.get("description", ""), "success": plan_success})

    return result_msg, max_steps, plan_success


def _finish_run(
    ctx: ReactContext,
    action_obj: dict,
    user_goal: str,
    step: int,
    run_start: float,
) -> str:
    """Handle a finish action: coerce result, summarize, persist outcome. Returns the final result string."""
    pfx = ""
    result = action_obj.get("result", "Done.")
    if not isinstance(result, str):
        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)
        else:
            result = str(result) if result else "Done."
    elapsed = time.time() - run_start
    active_model = ctx.llm.llm_cfg.get("model", "?")
    logger.info("%sfinish | model: %s | steps: %d | elapsed: %.1fs", pfx, active_model, step, elapsed)
    ctx.memory.record_event(f"Agent finished: {result[:80]}")
    if ctx.short_term:
        ctx.short_term.add("user", user_goal)
        ctx.short_term.add("assistant", result)
    summary = ""
    tools_used: list[str] = []
    if ctx.results and ctx.working and ctx.working.has_content():
        tools_used = list(filter(None, extract_tools_used(ctx.working.steps)))
        summary = _summarize_result(
            ctx.llm,
            goal=user_goal,
            result=result,
            tools_used=tools_used,
        )
        save_task_outcome(
            results=ctx.results,
            graph_memory_writer=ctx.graph_memory_writer,
            goal=user_goal,
            summary=summary,
            tools_used=tools_used,
        )
    # Fire-and-forget strategy extraction on background thread
    if ctx.strategy_memory is not None:
        try:
            from strategy_memory import extract_strategy  # noqa: PLC0415

            def _extract_and_store() -> None:
                """Extract strategy and store it in StrategyMemory."""
                _outcome = {
                    "success": True,
                    "summary": summary[:500] if summary else result[:500],
                    "tools": tools_used,
                }
                strategy = extract_strategy(ctx.llm, user_goal, _outcome)
                if strategy is not None:
                    ctx.strategy_memory.add(strategy)

            _thread = threading.Thread(
                target=_extract_and_store,
                daemon=True,
            )
            _thread.start()
            logger.debug("%sStrategy extraction thread started", pfx)
        except Exception as _se_exc:  # noqa: BLE001
            logger.debug("%sStrategy extraction start failed: %s", pfx, _se_exc)
    if ctx.working:
        ctx.working.clear()
    return result


def _dispatch_tool(
    ctx: ReactContext,
    action_obj: dict,
    _progress: Callable[[str], None],
) -> dict:
    """Execute a tool action and return the outcome dict.

    Adds '_operator_cancelled' key to outcome if the user cancelled.
    """
    pfx = ""  # run identity is now supplied by structlog contextvars (see agent_logging); avoid double-prefixing
    tool_name = action_obj.get("tool", "")
    args = action_obj.get("args", {})
    if isinstance(args, str):
        # Defense-in-depth: bare string args crash .items()/.get() downstream.
        # _normalize_shorthand_action should have wrapped this already.
        args = {"_raw": args}
    if isinstance(args, list):
        args = {str(i): v for i, v in enumerate(args)}

    _progress(f"{_tool_icon(tool_name)} Running tool: `{tool_name}`\n{fmt_tool_call(tool_name, args)}")

    # vision_query handled directly (needs LLM access)
    if tool_name == "vision_query":
        return _exec_vision_query(ctx, args)

    # Built-in tools
    if ctx.builtin_executor and ctx.builtin_executor.is_builtin(tool_name):
        # For shell tool with streaming enabled, create a live-chunk callback.
        chunk_callback: Optional[Callable[[str], None]] = None
        if (tool_name == "shell"
                and getattr(ctx.builtin_executor, "_shell_streaming", False)):
            # Keep only a bounded rolling tail (last few lines) rather than the
            # full output history — avoids O(n²) re-joins and unbounded memory
            # growth on high-output commands. We only ever display the tail.
            _tail_buf: list[str] = [""]

            def _on_chunk(chunk: str) -> None:
                merged = _tail_buf[0] + chunk
                # Retain only the last 8 line-segments, and cap total length to
                # guard against a single very long line with no newlines.
                lines = merged.rsplit("\n", 8)
                tail = "\n".join(lines[-8:])[-2000:]
                _tail_buf[0] = tail
                # Emit a special-prefixed progress message so the UI handler can
                # update the live tail without adding new panel steps.
                _progress(f"__SHELL_CHUNK__:{tail}")

            chunk_callback = _on_chunk

        outcome = ctx.builtin_executor.execute(tool_name, args, caller_depth=ctx.depth, caller_tag=ctx.caller_tag,
                                                chunk_callback=chunk_callback, trace_id=ctx.trace_id)

        if outcome.get("requires_confirmation"):
            token = outcome["token"]
            description = outcome.get("description", tool_name)

            if tool_name in ctx.confirmation.auto_approve_tools:
                logger.info(
                    "%sAuto-approving '%s' (operator approved all %s)",
                    pfx, tool_name, tool_name,
                )
                outcome = ctx.builtin_executor.confirm(token, chunk_callback=chunk_callback)
                _progress(f"✅ Auto-approved `{tool_name}` (approve-all active)")
            else:
                result_confirmed = ctx.confirmation.request_confirmation(
                    token, tool_name, description, _progress,
                )

                if result_confirmed:
                    outcome = ctx.builtin_executor.confirm(token, chunk_callback=chunk_callback)
                    _progress(f"✅ Confirmed — executing `{tool_name}`\n{fmt_tool_call(tool_name, args)}")
                else:
                    ctx.builtin_executor.cancel(token)
                    outcome = fail_outcome(
                        "Operation cancelled by the operator. "
                        "Do not retry this operation via any other tool or method. "
                        "Respond with a finish action now.",
                    )
                    outcome["_operator_cancelled"] = True
                    _progress("❌ Cancelled by operator — stopping task.")
        return outcome

    # MCP tools — wrap dispatch with TOOL_* lifecycle events so MCP calls are
    # visible to log_query. They bypass both executors (which emit these events
    # themselves), so without this wrapping MCP tools never appear as TOOL_*
    # events. Field conventions match the builtin/tool executors.
    if ctx.mcp_manager and ctx.mcp_manager.has_tool(tool_name):
        _mcp_start = time.perf_counter()
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_START,
            f"tool start: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
        )
        try:
            outcome = ctx.mcp_manager.call_tool(tool_name, args)
        except Exception as exc:
            dur_ms = int((time.perf_counter() - _mcp_start) * 1000)
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_FAILED,
                f"tool failed: {tool_name}",
                level=logging.ERROR,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=-1,
                err=str(exc),
            )
            raise
        dur_ms = int((time.perf_counter() - _mcp_start) * 1000)
        if _is_mcp_auth_failure(ctx, tool_name, outcome):
            outcome = _handle_mcp_auth_failure(ctx, tool_name, outcome)
        if isinstance(outcome, dict) and outcome.get("success"):
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_END,
                f"tool end: {tool_name}",
                level=logging.INFO,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=0,
            )
        else:
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_FAILED,
                f"tool failed: {tool_name}",
                level=logging.ERROR,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=-1,
                err=(outcome.get("error", "") if isinstance(outcome, dict) else "") or "",
            )
        return outcome

    # Unknown tool — no hand-written tools exist anymore
    return fail_outcome(f"Tool '{tool_name}' is not a built-in tool, MCP tool, or vision_query.")
