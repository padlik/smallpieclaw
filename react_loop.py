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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import TYPE_CHECKING, Callable, Optional

import httpx

if TYPE_CHECKING:
    from builtin_tools.access_control import TrustedZoneChecker

import agent_logging
from builtin_tools.schemas import (
    build_tool_definitions,
    builtin_tool_names,
)
from confirmation import ConfirmationManager
from context_manager import maybe_compact, resolve_compaction_threshold
from context_monitor import (
    ContextMonitor,
    ContextSnapshot,
    compute_danger_level,
    compute_headroom_real,
    group_tool_defs_by_server,
)
from interfaces import ToolCall
from llm_client import LLMClient, LLMCancelledError, LLMEmptyResponseError, LLMError, LLMPermanentError, _encode_images
from memory_store import _summarize_result, extract_tools_used, save_task_outcome
from outcome_utils import fail_outcome
from prompt_loader import build_system_prompt as _build_system_prompt

logger = logging.getLogger(__name__)
slog = agent_logging.get_logger(__name__)

# Case-insensitive fragments that indicate an LLM context-window / context-length
# overflow when present in an HTTP 400/413 response body.
_CONTEXT_OVERFLOW_INDICATORS: frozenset[str] = frozenset(
    {
        "context_length",
        "context window",
        "maximum context",
        "prompt is too long",
        "context length exceeded",
        "token limit",
        "exceeds the maximum number of tokens",
        "input token count",
        "maximum number of tokens allowed",
    }
)

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

# Safety ceiling used when the operator chooses an "unlimited" step extension.
# It is not a real unlimited loop; it is large enough to be effectively
# unlimited while still protecting against runaway execution.
_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000
_RE_PROMPT = "__re_prompt__"


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, _DEFAULT_TOOL_ICON)


def _coerce_args(args: object) -> dict:
    """Coerce list args into an integer-key dict; pass dicts through unchanged.

    Bare strings are wrapped as ``{"_raw": s}`` so downstream tool dispatch
    preserves the payload (mirrors the defense-in-depth in ``_dispatch_tool``).
    """
    if isinstance(args, list):
        return {str(i): v for i, v in enumerate(args)}
    if isinstance(args, str):
        return {"_raw": args}
    if not isinstance(args, dict):
        return {}
    return args


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
    payload = ctx._context_payload
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

    # Checkpoint store for LLM error recovery — Optional[CheckpointStore]
    checkpoint_store: Optional[object] = None
    # Whether disk checkpoints are written (config: llm_error_handling.checkpoint_enabled)
    checkpoint_enabled: bool = True
    # Retry prompt timeout in seconds (config: llm_error_handling.retry_timeout_seconds)
    retry_timeout_seconds: int = 120

    # Native tool calling — cached tool definitions built once at loop start
    _tool_defs: Optional[list[dict]] = None

    # Cached grouping of _tool_defs by server, computed once per run when first
    # needed and reused every subsequent turn.
    _tool_defs_by_server: dict[str, int] | None = None
    _tool_defs_tokens: int = 0

    # Shared, lock-free context-window profiler; injected from AgentController/main.py.
    context_monitor: ContextMonitor | None = None

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


_BRIEF_MAX = 35


def _truncate_brief(text: str, limit: int = _BRIEF_MAX) -> str:
    """Truncate to limit chars, appending … if truncated."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _strip_shell_wrapper(cmd: str) -> str:
    """Strip common shell wrapper patterns from a command string.

    Strips, in order:
    1. ``sh/bash/zsh -c "..."`` wrappers
    2. Leading ``cd dir && `` prefix
    3. Leading ``export VAR=value && `` prefix
    """
    match = re.match(r'^(sh|bash|zsh)\s+-c\s+"(.+)"$', cmd)
    if match:
        cmd = match.group(2)
    cmd = re.sub(r"^cd\s+\S+\s+&&\s+", "", cmd)
    cmd = re.sub(r"^export\s+\w+=\S+\s+&&\s+", "", cmd)
    return cmd


def fmt_tool_brief(tool_name: str, args: dict, is_mcp: bool = False, server_name: str = "") -> str:
    """Format a short one-line brief of what a tool is doing, for the compact panel.

    Extracts the semantically meaningful argument per tool family. Secrets are
    protected by showing keys only, never values. Truncated to ~35 chars.
    Appends ``[MCP:{server_name}]`` when ``is_mcp`` is True.
    """
    core = ""

    if tool_name in {"file_read", "file_send", "vision_query"}:
        core = f"{tool_name} {os.path.basename(args.get('path', '?'))}"

    elif tool_name == "file_diff":
        path_a = args.get("path_a", "?")
        path_b = args.get("path_b", "?")
        core = f"{tool_name} {os.path.basename(path_a)} ↔ {os.path.basename(path_b)}"

    elif tool_name == "file_patch":
        path = args.get("path", "?")
        old_str = args.get("old_str", "") or ""
        new_str = args.get("new_str", "") or ""
        core = f"{tool_name} {os.path.basename(path)} +{len(new_str.splitlines())} -{len(old_str.splitlines())}"

    elif tool_name == "file_write":
        path = args.get("path", "?")
        content = args.get("content", "") or ""
        core = f"{tool_name} {os.path.basename(path)} ({len(content)})"

    elif tool_name == "shell":
        cmd = args.get("command", "") or ""
        stripped = _strip_shell_wrapper(cmd)
        core = f'{tool_name} "{_truncate_brief(stripped)}"'

    elif tool_name == "spawn_agent":
        task = args.get("task", "")
        core = f'{tool_name} "{_truncate_brief(task, 30)}"'

    elif tool_name == "schedule":
        action = args.get("action", "")
        tag = args.get("tag", "")
        cron = args.get("cron", "")
        if action == "list":
            core = f"{tool_name} list"
        elif action == "add":
            core = f'{tool_name} add "{_truncate_brief(tag, 30)}" {cron}'
        else:
            core = f'{tool_name} {action} "{_truncate_brief(tag, 30)}"'

    elif tool_name in {"get_agent_result", "cancel_agent"}:
        core = f"{tool_name} {args.get('agent_id', '')}"

    elif tool_name == "wait_for_any_agent":
        agent_ids = args.get("agent_ids", []) or []
        if len(agent_ids) > 2:
            core = f"{tool_name} [{len(agent_ids)} agents]"
        else:
            core = f"{tool_name} {', '.join(str(a) for a in agent_ids)}"

    elif tool_name == "memory_write":
        action = args.get("action", "")
        key = args.get("key", "")
        core = f'{tool_name} {action} "{_truncate_brief(key, 30)}"'

    elif tool_name == "memory_graph_search":
        query = args.get("query", "")
        core = f'{tool_name} "{_truncate_brief(query, 30)}"'

    elif tool_name == "memory_graph_store":
        content = args.get("content", "")
        core = f'{tool_name} "{_truncate_brief(content, 30)}"'

    elif tool_name == "log_query":
        text = args.get("text", "")
        core = f'{tool_name} "{_truncate_brief(text, 30)}"'

    elif tool_name == "secret_get":
        core = f"{tool_name} {args.get('key', '')}"

    elif tool_name == "shell_env_set":
        core = f"{tool_name} {args.get('key', '')}"

    elif tool_name in {"shell_env_unset", "shell_env_get"}:
        core = f"{tool_name} {args.get('key', '')}"

    elif tool_name == "shell_env_list":
        core = f"{tool_name} list env vars"

    else:
        # Keys only, never values — protects secrets for MCP/unknown tools.
        if args:
            core = f"{tool_name} ({', '.join(str(k) for k in args)})"
        else:
            core = tool_name

    core = core.replace("\n", " ").replace("\r", " ")
    brief = _truncate_brief(core)
    if is_mcp:
        brief = f"{brief} [MCP:{server_name}]"
    return brief


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
    checker = ctx.trusted_zone_checker
    needs_confirm = False
    reason = ""
    if checker is not None:
        builtin = ctx.builtin_executor
        request_grants = (
            builtin.grant_tracker.snapshot()
            if builtin is not None and builtin.grant_tracker is not None
            else frozenset()
        )
        zone = checker.classify(
            path, operation="read",
            request_grants=request_grants,
        )
        sensitive, zone_reason = _is_sensitive_path(real_path)
        if zone == ZoneClassification.UNRECOGNISED or sensitive:
            needs_confirm = True
            reason = zone_reason if sensitive else "Unrecognised zone"
    else:
        # Checker unwired: degrade to sensitive-only gate (same as file_read).
        logger.error("Zone: trusted_zone_checker not wired — falling back to sensitive-only gate for vision_query")
        sensitive, zone_reason = _is_sensitive_path(real_path)
        if sensitive:
            needs_confirm = True
            reason = zone_reason

    if needs_confirm:
        builtin = ctx.builtin_executor
        if builtin is None:
            return {
                "success": False, "output": "",
                "error": "vision_query: confirmation infrastructure not available.",
            }
        desc = f"Vision query: <code>{path}</code>"
        if real_path != path:
            desc += f"\n(→ <code>{real_path}</code>)"
        desc += f"\n⚠️ Reason for confirmation: {reason}"
        confirm_kwargs: dict = {
            "caller_depth": ctx.depth,
            "caller_tag": ctx.caller_tag,
        }
        # Only pass zone_path when the checker classified the path as
        # UNRECOGNISED — not on the checker-unwired sensitive-only fallback.
        if checker is not None:
            confirm_kwargs["zone_path"] = real_path
        return builtin._requires_confirmation(
            "vision_query", args, desc, **confirm_kwargs,
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


@dataclass
class LLMErrorInfo:
    """Classified LLM error information for retry/recovery."""
    type: str        # timeout, connection, rate_limit, empty, context, permanent, unknown
    message: str     # user-facing message
    retryable: bool
    detail: str      # raw exception details for logging


def _classify_llm_error(exc: Exception) -> LLMErrorInfo:
    """Classify an LLM exception into a typed error info for retry/recovery.

    LLMCancelledError is NOT classified here — it propagates before classification.
    """
    detail = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, httpx.TimeoutException):
        return LLMErrorInfo("timeout", "⏱️ Request timed out", True, detail)
    if isinstance(exc, httpx.ConnectError):
        return LLMErrorInfo("connection", "🔌 Connection failed", True, detail)
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return LLMErrorInfo("rate_limit", "🚫 Rate limit reached", True, detail)
        if exc.response.status_code in (400, 413):
            try:
                body = exc.response.text.lower()
            except Exception:
                body = ""
            if any(indicator in body for indicator in _CONTEXT_OVERFLOW_INDICATORS):
                return LLMErrorInfo("context", "📏 Context too long", False, detail)
        return LLMErrorInfo("unknown", "❌ LLM error", True, detail)
    if isinstance(exc, LLMEmptyResponseError):
        return LLMErrorInfo("empty", "📭 Model returned no content", True, detail)
    if isinstance(exc, LLMPermanentError):
        return LLMErrorInfo("permanent", f"❌ {exc}", False, detail)
    return LLMErrorInfo("unknown", f"❌ LLM error: {exc}", True, detail)


def _get_user_goal(state: _LoopState) -> str:
    """Extract the user's goal from the first user message in state."""
    for msg in state.messages:
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def _handle_llm_error(
    ctx: ReactContext,
    state: _LoopState,
    error_info: LLMErrorInfo,
    progress: Callable[[str], None],
    user_goal: str,
) -> Optional[str]:
    """Handle an LLM error: write checkpoint, prompt user for retry/cancel.

    Returns None to retry (loop continues), or an error string to return.
    """
    import json as _json
    import secrets as _secrets

    # Step 1: Write checkpoint if enabled
    if ctx.checkpoint_store is not None and ctx.checkpoint_enabled:
        checkpoint = {
            "trace_id": ctx.trace_id,
            "user_goal": user_goal,
            "messages": state.messages,
            "step": state.step,
            "goal_idx": state.goal_idx,
            "max_steps": state.max_steps,
            "json_fail_streak": state.json_fail_streak,
            "model": ctx.llm.llm_cfg.get("model", "?"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_info": {
                "type": error_info.type,
                "message": error_info.message,
                "retryable": error_info.retryable,
                "detail": error_info.detail,
            },
        }
        ctx.checkpoint_store.save(ctx.trace_id, checkpoint)

    # Step 2: Prompt user for retry/cancel
    token = _secrets.token_hex(4)
    error_info_json = _json.dumps({
        "type": error_info.type,
        "message": error_info.message,
        "retryable": error_info.retryable,
        "detail": error_info.detail[:200],
        "model": ctx.llm.llm_cfg.get("model", "?"),
        "step": state.step,
        "max_steps": state.max_steps,
        "tool_results_count": sum(
            1 for m in state.messages
            if m.get("role") == "tool"
            or m.get("role") == "user"
            and "tool '" in m.get("content", "").lower()
        ),
    })
    response = ctx.confirmation.request_retry(
        token, error_info_json, progress, timeout_seconds=ctx.retry_timeout_seconds
    )

    # Step 3: Handle response
    if response == "retry":
        return None  # loop continues, re-calls LLM with same state
    if response == "cancel":
        if ctx.checkpoint_store is not None and ctx.checkpoint_enabled:
            ctx.checkpoint_store.delete(ctx.trace_id)
        return f"❌ {error_info.message}"
    # timeout
    return f"❌ {error_info.message}"


def _get_tool_registry_for_grouping(ctx: ReactContext):
    """Return the tool registry to use for server grouping, or None."""
    try:
        return ctx.tool_index.registry
    except AttributeError:
        return None


def _tool_defs_by_server_for_context(ctx: ReactContext) -> dict[str, int]:
    """Group current tool definitions by server and return per-server token counts."""
    builtin_names = builtin_tool_names()
    return group_tool_defs_by_server(
        ctx._tool_defs,
        _get_tool_registry_for_grouping(ctx),
        ctx.mcp_manager,
        builtin_names,
    )


def _publish_context_snapshot(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    tool_defs_by_server: dict[str, int] | None = None,
    tool_defs_tokens: int | None = None,
) -> None:
    """Build and publish a ContextSnapshot from current turn data.

    *tool_defs_by_server* and *tool_defs_tokens* may be supplied when the caller
    has already computed them (e.g. the main ReAct loop). When omitted, the
    helper computes them fresh. Token estimation mirrors
    :func:`context_manager.maybe_compact`: the active model name drives the
    encoder selection and the system prompt is folded into the messages total.
    """
    if ctx.context_monitor is None:
        return

    from prompt_builder import estimate_tokens, estimate_messages_tokens

    _model = ctx.llm.llm_cfg.get("model")
    _total_with_system = estimate_messages_tokens(state.messages, system, model=_model)
    system_prompt_tokens = estimate_tokens(system, model=_model)
    chat_history_tokens = max(0, _total_with_system - system_prompt_tokens)
    if tool_defs_by_server is None:
        tool_defs_by_server = _tool_defs_by_server_for_context(ctx)
    if tool_defs_tokens is None:
        tool_defs_tokens = sum(tool_defs_by_server.values())
    effective_window, compaction_threshold = resolve_compaction_threshold(
        ctx.llm.llm_cfg, ctx.ctx_max_tokens,
    )
    completion_reserve = ctx.llm.llm_cfg.get("max_tokens", 1024)
    headroom_nominal = compaction_threshold - system_prompt_tokens - chat_history_tokens
    headroom_real = compute_headroom_real(
        compaction_threshold, system_prompt_tokens, chat_history_tokens, tool_defs_tokens,
    )
    total = system_prompt_tokens + chat_history_tokens + tool_defs_tokens
    danger_level = compute_danger_level(total, compaction_threshold)

    snapshot = ContextSnapshot(
        system_prompt_tokens=system_prompt_tokens,
        chat_history_tokens=chat_history_tokens,
        tool_defs_tokens=tool_defs_tokens,
        tool_defs_by_server=tool_defs_by_server,
        completion_reserve=completion_reserve,
        effective_window=effective_window,
        compaction_threshold=compaction_threshold,
        headroom_nominal=headroom_nominal,
        headroom_real=headroom_real,
        danger_level=danger_level,
        is_live=True,
        turn=state.step,
    )
    ctx.context_monitor.publish(snapshot)


def _assemble_system_prompt(ctx: ReactContext, user_goal: str) -> str:
    """Assemble the full system prompt string for a run."""
    _job_history_section = ""
    if ctx.job_history_fn:
        try:
            _job_history_section = ctx.job_history_fn() or ""
        except Exception as _jh_exc:
            logger.warning("Failed to get job history: %s", _jh_exc)

    _graph_context_section = ""
    if ctx.graph_memory is not None:
        try:
            _graph_context_section = (
                ctx.graph_memory.format_for_prompt(
                    user_goal, max_entries=ctx.graph_memory_max_entries
                ) or ""
            )
            if _graph_context_section:
                logger.info("Graph memory context injected")
            else:
                logger.debug("Graph memory context: no relevant data found")
        except Exception as _gm_exc:
            logger.debug("Graph memory context failed: %s", _gm_exc)

    strategies_section = ""
    if ctx.strategy_memory is not None:
        from strategy_memory import classify_task_type, format_strategies_for_prompt
        task_type = classify_task_type(user_goal)
        strategies = ctx.strategy_memory.get_top_k(task_type, k=2)
        if strategies:
            strategies_section = format_strategies_for_prompt(strategies)

    parent_context_section = _format_parent_context(ctx)
    _sub_agent_prompt_variant = ctx._prompt_variant if ctx.depth >= 1 else None

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
    first_msg: dict = {"role": "user", "content": user_goal}
    if images:
        first_msg["images"] = images
        logger.info("%d image(s) attached to request", len(images))

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
    if ctx._tool_defs is None:
        try:
            ctx._tool_defs = build_tool_definitions(
                mcp_manager=ctx.mcp_manager,
            )
        except Exception as _btd_exc:  # noqa: BLE001
            logger.warning("build_tool_definitions failed: %s — MCP tools skipped", _btd_exc, )
            ctx._tool_defs = build_tool_definitions(mcp_manager=None)


def _normalize_shorthand_action(action_obj: dict) -> dict:
    """Normalize shorthand action keys to canonical form. Returns the (possibly mutated) dict."""
    action = action_obj.get("action", "")
    if action not in _BUILTIN_NAMES:
        return action_obj
    logger.warning("LLM used shorthand action '%s' — normalizing to tool call", action)
    if "args" in action_obj:
        shorthand_args = action_obj["args"]
        if isinstance(shorthand_args, str):
            try:
                parsed = json.loads(shorthand_args)
                if isinstance(parsed, (dict, list)):
                    shorthand_args = parsed
                else:
                    logger.warning("Shorthand action '%s' args parsed to non-dict type %s — wrapping in _raw", action, type(parsed).__name__, )
                    shorthand_args = {"_raw": shorthand_args}
            except (ValueError, TypeError):
                logger.warning("Shorthand action '%s' args is a non-JSON string — wrapping in _raw: %s", action, shorthand_args[:200], )
                shorthand_args = {"_raw": shorthand_args}
    else:
        shorthand_args = {k: v for k, v in action_obj.items() if k != "action"}
    return {"action": "tool", "tool": action, "args": shorthand_args}


def _handle_non_json(state: _LoopState, turn: _Turn) -> str:
    """Handle a turn that did not yield a parseable JSON action.

    Increments the consecutive non-JSON streak, logs the raw output, and either
    returns an error message that should abort the loop (when the streak reaches
    the limit) or returns the sentinel ``_RE_PROMPT`` so the caller can continue.
    """
    state.json_fail_streak += 1
    logger.warning(
        "LLM returned non-JSON (step %d/%d, streak %d, ~%d chars):\n--- BEGIN ---\n%s\n--- END ---",
        state.step, state.max_steps, state.json_fail_streak, len(turn.raw), turn.raw[:1000],
    )
    if state.json_fail_streak >= _JSON_FAIL_LIMIT:
        logger.error("Non-JSON streak reached %d — aborting with protocol error", state.json_fail_streak)
        return (
            f"❌ Agent protocol error: model returned non-JSON "
            f"{state.json_fail_streak} times in a row. "
            f"Last response (truncated to 500 chars): {turn.raw[:500]}"
        )
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
    return _RE_PROMPT


def _action_from_turn(turn: _Turn, state: _LoopState) -> tuple[dict, Callable[[str], None]]:
    """Extract the action dict and result sink from a parsed LLM turn.

    For native tool calls, maps supported tool names (plan/finish/tool) into the
    canonical action object and uses a native-format result sink. For parsed JSON
    text, normalizes shorthand actions and uses the plain-text result sink.
    """
    if turn.tool_calls:
        tc = turn.tool_calls[0]
        if tc.name == "plan":
            action_obj: dict = {"action": "plan", "plan": tc.arguments}
        elif tc.name == "finish":
            action_obj = {"action": "finish", "result": (tc.arguments or {}).get("result", "Done.")}
        else:
            action_obj = {"action": "tool", "tool": tc.name, "args": tc.arguments}
        return action_obj, _result_sink(state, tc)

    action_obj = parse_json(turn.raw)
    if action_obj is None and turn.text_from_native:
        logger.warning(
            "Native path: model returned prose (no tool_calls) — treating as finish. "
            "In json_mode this would re-prompt; with native tool calling the run ends here.",
        )
        action_obj = {"action": "finish", "result": turn.raw}
    if action_obj is None:
        raise ValueError("non-json")  # caller should run _handle_non_json
    state.json_fail_streak = 0
    state.messages.append({"role": "assistant", "content": turn.raw})
    action_obj = _normalize_shorthand_action(action_obj)
    return action_obj, _result_sink(state)


def _request_turn(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    progress: Callable[[str], None],
    supports_native_fallback: bool = False,
) -> _Turn:
    """Make one LLM call and return the structured turn result."""
    raw = ""
    tool_calls: list[ToolCall] = []
    native_attempted = False
    text_from_native = False
    linearized_messages = None
    _MAX_EMPTY_RETRIES = 2

    if ctx._tool_defs and supports_native_fallback:
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
            logger.debug("Native tool calling not supported by provider — falling back to json_mode")
        except LLMPermanentError:
            raise
        except LLMError:
            logger.warning("Native tool calling failed (LLMError) — falling back to json_mode")
        except Exception as exc:
            logger.warning("Native tool calling unexpected error: %s — falling back to json_mode", exc, )

    if not raw and not native_attempted:
        linearized_messages = _linearize_native_turns(state.messages)
        empty_retries = 0
        while True:
            try:
                raw = ctx.llm.chat_with_fallback(
                    linearized_messages, system=system, progress_cb=progress, json_mode=True,
                )
            except LLMCancelledError:
                logger.info("Agent LLM call cancelled at step %d/%d", state.step, state.max_steps)
                return _Turn([], "", False, "[Cancelled]")
            except (LLMError, LLMEmptyResponseError, httpx.HTTPError) as exc:
                error_info = _classify_llm_error(exc)
                logger.warning("LLM error at step %d/%d: %s — %s", state.step, state.max_steps, error_info.type, error_info.detail)
                # Try to handle the error (checkpoint + retry prompt)
                result = _handle_llm_error(ctx, state, error_info, progress, _get_user_goal(state))
                if result is None:
                    # User pressed Retry — re-attempt the LLM call.
                    # This is an unbounded, human-paced retry, NOT tied to
                    # the empty-response retry counter.
                    continue
                return _Turn([], "", False, result)
            if raw.strip():
                break
            if empty_retries < _MAX_EMPTY_RETRIES:
                logger.warning("LLM returned empty response (step %d/%d), retrying (%d/%d)…", state.step, state.max_steps, empty_retries + 1, _MAX_EMPTY_RETRIES, )
                progress(f"⏳ Empty LLM response, retrying ({empty_retries + 1}/{_MAX_EMPTY_RETRIES})…")
                empty_retries += 1
                continue
            break

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
        raw_args = action_obj.get("args", {})
        args = _coerce_args(raw_args)
        if args is not raw_args:
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
            logger.warning("Tool '%s' result: success=False | error=%s | args=%s", tool_name, outcome.get("error", ""), {k: str(v)[:120] for k, v in args.items()}, )
        _end_status = "ok" if outcome.get("success") else "fail"
        progress(f"__TOOL_END__:{_end_status}:{tool_name}\n{fmt_tool_result_progress(tool_name, args, outcome)}")
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
    initial_state: Optional[_LoopState] = None,
) -> str:
    """Execute the ReAct loop: LLM → parse → dispatch → repeat. Returns the final answer string."""
    run_start = time.time()
    _ctx_tokens = agent_logging.bind_run_context(trace=ctx.trace_id, agent=ctx.label)
    agent_logging.log_event(agent_logging.LogEvent.RUN_BEGIN, "run begin", level=logging.INFO, logger=slog)

    def _progress(msg: str):
        if progress_callback:
            progress_callback(msg)
        logger.debug("Agent progress: %s", msg)

    try:
        ctx._tool_defs = None
        if ctx.owns_cancel_event:
            ctx.cancel_event.clear()

        active_model = ctx.llm.llm_cfg.get("model", "?")
        logger.info("start | model: %s | goal: %s", active_model, user_goal[:80])

        if ctx.working:
            ctx.working.start_task(user_goal)

        system = _assemble_system_prompt(ctx, user_goal)
        messages, goal_idx = _init_messages(ctx, user_goal, images)
        ctx.memory.record_event(f"User request: {user_goal[:100]}")

        if ctx.graph_memory_writer is not None:
            try:
                ctx.graph_memory_writer.enqueue(user_goal, source="chat")
            except Exception as _gw_exc:  # noqa: BLE001
                logger.debug("Graph memory enqueue failed: %s", _gw_exc)

        _ensure_tool_defs(ctx)

        _supports_native_fallback = hasattr(ctx.llm, "chat_with_tools_fallback") and callable(
            ctx.llm.chat_with_tools_fallback
        )

        if initial_state is not None:
            state = initial_state
        else:
            state = _LoopState(messages=messages, goal_idx=goal_idx, max_steps=ctx.max_iterations)

        while True:
            while state.step < state.max_steps:
                if ctx.cancel_event.is_set():
                    logger.warning("cancelled at step %d/%d", state.step, state.max_steps)
                    return "[Cancelled]"

                if not state.warned_inactivity and state.step > 1:
                    warn_minutes = ctx.inactivity_warn_minutes
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
                        logger.debug("on_step callback failed", exc_info=True)
                active_model = ctx.llm.llm_cfg.get("model", "?")
                logger.info("step %d/%d | model: %s", state.step, state.max_steps, active_model)
                _progress(f"⚙️ Thinking… (step {state.step})")

                # Per-model context window: effective limit from the active
                # model's context_window, falling back to agent.ctx_max_tokens.
                # Completion tokens (max_tokens) are reserved before the 85%
                # margin is applied inside maybe_compact().
                _effective_ctx = (
                    ctx.llm.llm_cfg.get("context_window") or ctx.ctx_max_tokens
                )
                _completion_budget = ctx.llm.llm_cfg.get("max_tokens") or 1024
                if ctx._tool_defs_by_server is None:
                    try:
                        ctx._tool_defs_by_server = _tool_defs_by_server_for_context(ctx)
                        ctx._tool_defs_tokens = sum(ctx._tool_defs_by_server.values())
                    except Exception:
                        logger.warning(
                            "tool-def grouping failed; compaction falls back to tool_defs_tokens=0",
                            exc_info=True,
                        )
                        ctx._tool_defs_by_server = {}
                        ctx._tool_defs_tokens = 0
                _tool_defs_by_server = ctx._tool_defs_by_server
                _tool_defs_tokens = ctx._tool_defs_tokens
                state.messages, state.goal_idx = maybe_compact(
                    state.messages, system, _effective_ctx, ctx.llm,
                    goal_idx=state.goal_idx,
                    model_max_tokens=_completion_budget,
                    tool_defs_tokens=_tool_defs_tokens,
                )

                turn = _request_turn(
                    ctx, state, system, _progress,
                    supports_native_fallback=_supports_native_fallback,
                )
                if turn.early_return is not None:
                    return turn.early_return
                if turn.linearized_messages is not None:
                    state.messages = turn.linearized_messages

                try:
                    action_obj, sink = _action_from_turn(turn, state)
                except ValueError:
                    err_msg = _handle_non_json(state, turn)
                    if err_msg == _RE_PROMPT:
                        continue
                    _progress(err_msg)
                    return err_msg

                final = _dispatch_action(
                    ctx, action_obj, sink, state, user_goal, run_start, _progress,
                )
                if final is not None:
                    if ctx.checkpoint_store is not None and ctx.checkpoint_enabled:
                        ctx.checkpoint_store.delete(ctx.trace_id)
                    try:
                        _publish_context_snapshot(
                            ctx, state, system,
                            tool_defs_by_server=_tool_defs_by_server,
                            tool_defs_tokens=_tool_defs_tokens,
                        )
                    except Exception:
                        logger.warning("context snapshot publication failed", exc_info=True)
                    return final
                if state.operator_cancelled:
                    break
                agent_logging.log_event(
                    agent_logging.LogEvent.STEP_END, "step end",
                    level=logging.INFO, logger=slog, step=state.step,
                )
                try:
                    _publish_context_snapshot(
                        ctx, state, system,
                        tool_defs_by_server=_tool_defs_by_server,
                        tool_defs_tokens=_tool_defs_tokens,
                    )
                except Exception:
                    logger.warning("context snapshot publication failed", exc_info=True)

            if state.operator_cancelled:
                ctx.memory.record_event("Task cancelled by operator")
                return "⚠️ Task stopped by operator."

            ext_response = ctx.confirmation.request_extension(state.max_steps, _progress)
            if ext_response == "unlimited":
                state.max_steps = _EFFECTIVELY_UNLIMITED_STEPS
                logger.info("Agent steps set to effectively unlimited by user")
                _progress("♾️ Running until done (effectively unlimited, capped at 10M for safety)…")
                continue
            elif ext_response == "yes":
                state.max_steps += 10
                logger.info("Agent steps extended to %d by user", state.max_steps)
                _progress(f"⏩ Extended — continuing to step {state.max_steps}…")
                continue
            break

        ctx.memory.record_event("Agent hit max iterations")
        return "⚠️ Agent reached maximum steps. Operation cancelled."
    finally:
        if ctx.context_monitor is not None:
            try:
                last = ctx.context_monitor.read()
                if last is not None:
                    ctx.context_monitor.publish(dataclass_replace(last, is_live=False))
            except Exception:
                logger.warning("context snapshot publication failed", exc_info=True)
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
    old_max_steps = max_steps
    plan_limit = ctx.plan_max_iterations or (ctx.max_iterations + 20)
    if plan_limit > _ABSOLUTE_PLAN_CEILING:
        plan_limit = _ABSOLUTE_PLAN_CEILING
        logger.warning("plan | clamped plan_max_iterations to %d", _ABSOLUTE_PLAN_CEILING)
    if plan_limit > max_steps:
        max_steps = plan_limit
        logger.info("plan | raised max_steps from %d to %d", old_max_steps, max_steps)

    _t0 = time.time()
    plan_success = True
    progress("📋 Executing plan…")
    logger.info("plan | description: %s", plan_data.get("description", "")[:60])
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
        logger.info("plan | completed | results: %d", len(plan_result.get("results", {})))
        if max_steps > old_max_steps:
            max_steps = old_max_steps + 10
            if max_steps > _ABSOLUTE_PLAN_CEILING:
                max_steps = _ABSOLUTE_PLAN_CEILING
            logger.info("plan | adjusted max_steps to %d after completion", max_steps)
        result_msg = f"Plan execution results:\n{json.dumps(plan_result, ensure_ascii=False, indent=2)}"
    except Exception as exc:
        plan_success = False
        result_msg = f"Plan execution failed: {type(exc).__name__}: {exc}"
        logger.error(result_msg)

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
    result = action_obj.get("result", "Done.")
    if not isinstance(result, str):
        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)
        else:
            result = str(result) if result else "Done."
    elapsed = time.time() - run_start
    active_model = ctx.llm.llm_cfg.get("model", "?")
    logger.info("finish | model: %s | steps: %d | elapsed: %.1fs", active_model, step, elapsed)
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
            logger.debug("Strategy extraction thread started")
        except Exception as _se_exc:  # noqa: BLE001
            logger.debug("Strategy extraction start failed: %s", _se_exc)
    if ctx.working:
        ctx.working.clear()
    return result


def _emit_tool_lifecycle(
    tool_name: str,
    start: float,
    outcome_or_exc: object,
    *,
    auth_recheck: Optional[tuple[ReactContext, str]] = None,
) -> Optional[dict]:
    """Emit matching TOOL_END or TOOL_FAILED events for a completed tool call.

    ``start`` is the ``time.perf_counter()`` value captured when the tool began.
    ``outcome_or_exc`` may be the result dict returned by the tool backend or an
    exception instance that escaped the call.  The helper computes duration, emits
    lifecycle events to ``slog``, and returns the final outcome dict when an
    outcome was supplied, or ``None`` when an exception was supplied.

    For MCP calls, pass ``auth_recheck=(ctx, tool_name)`` so that the helper can
    translate an MCP auth failure into the standard error result dict before
    emitting TOOL_FAILED.
    """
    dur_ms = int((time.perf_counter() - start) * 1000)
    if isinstance(outcome_or_exc, BaseException):
        # Only TOOL_FAILED here — an additional ERROR event would double-count
        # the same failure for anything aggregating agent.jsonl by event_type.
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_FAILED,
            f"tool failed: {tool_name}",
            level=logging.ERROR,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=-1,
            err=str(outcome_or_exc),
        )
        return None

    outcome: dict = outcome_or_exc  # type: ignore[assignment]
    if auth_recheck is not None:
        mcp_ctx, mcp_tool = auth_recheck
        if _is_mcp_auth_failure(mcp_ctx, mcp_tool, outcome):
            outcome = _handle_mcp_auth_failure(mcp_ctx, mcp_tool, outcome)

    if outcome.get("success"):
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_END,
            f"tool end: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=outcome.get("exit_code", 0),
        )
    else:
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_FAILED,
            f"tool failed: {tool_name}",
            level=logging.ERROR,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=outcome.get("exit_code", -1),
            err=outcome.get("error", "") or "",
        )
    return outcome


class _ToolSpanOutcome:
    """Outcome carrier handed to the body of a :func:`_tool_span` block.

    The body calls :meth:`record` with the tool's raw outcome dict; on exit the
    span replaces :attr:`outcome` with the post-processed dict (e.g. an MCP auth
    failure translated into an actionable error), which the caller then returns.
    """

    def __init__(self) -> None:
        self.outcome: dict = {}

    def record(self, outcome: dict) -> None:
        """Store the tool's raw outcome dict for lifecycle emission."""
        self.outcome = outcome


@contextmanager
def _tool_span(tool_name: str, *, mcp_auth: Optional[tuple[ReactContext, str]] = None):
    """Context manager emitting TOOL_START / TOOL_END / TOOL_FAILED lifecycle events.

    Yields a :class:`_ToolSpanOutcome`; the body must call ``record(outcome)`` so
    the span can emit the matching terminal event (or an exception can be raised
    out of the block).  For MCP tools pass ``mcp_auth=(ctx, tool_name)`` so that
    authentication failures are translated before the failure event is emitted —
    read the translated dict from ``.outcome`` after the block exits.
    """
    agent_logging.log_event(
        agent_logging.LogEvent.TOOL_START,
        f"tool start: {tool_name}",
        level=logging.INFO,
        logger=slog,
        tool=tool_name,
    )
    start = time.perf_counter()
    span = _ToolSpanOutcome()
    try:
        yield span
    except Exception as exc:
        _emit_tool_lifecycle(tool_name, start, exc)
        raise
    final = _emit_tool_lifecycle(tool_name, start, span.outcome, auth_recheck=mcp_auth)
    if final is not None:
        span.outcome = final


def _dispatch_tool(
    ctx: ReactContext,
    action_obj: dict,
    _progress: Callable[[str], None],
) -> dict:
    """Execute a tool action and return the outcome dict.

    Adds '_operator_cancelled' key to outcome if the user cancelled.
    """
    tool_name = action_obj.get("tool", "")
    args = action_obj.get("args", {})
    if isinstance(args, str):
        # Defense-in-depth: bare string args crash .items()/.get() downstream.
        # _normalize_shorthand_action should have wrapped this already.
        args = {"_raw": args}
    args = _coerce_args(args)

    is_mcp = bool(ctx.mcp_manager) and ctx.mcp_manager.has_tool(tool_name)
    server_name = ""
    if is_mcp:
        server_name = ctx.mcp_manager.server_name_for_tool(tool_name)  # type: ignore[union-attr]
    brief = fmt_tool_brief(tool_name, args, is_mcp=is_mcp, server_name=server_name)
    _progress(f"{_tool_icon(tool_name)} Running tool: `{tool_name}`\n{brief}")

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

        outcome = ctx.builtin_executor.execute(
            tool_name, args, caller_depth=ctx.depth, caller_tag=ctx.caller_tag,
            chunk_callback=chunk_callback, trace_id=ctx.trace_id,
        )

        if outcome.get("requires_confirmation"):
            token = outcome["token"]
            description = outcome.get("description", tool_name)

            if tool_name in ctx.confirmation.auto_approve_tools:
                logger.info("Auto-approving '%s' (operator approved all %s)", tool_name, tool_name, )
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
        with _tool_span(tool_name, mcp_auth=(ctx, tool_name)) as span:
            span.record(ctx.mcp_manager.call_tool(tool_name, args))
        # .outcome carries the auth-translated dict, not the raw call result.
        return span.outcome

    # Unknown tool — no hand-written tools exist anymore
    return fail_outcome(f"Tool '{tool_name}' is not a built-in tool, MCP tool, or vision_query.")
