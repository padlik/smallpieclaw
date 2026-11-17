"""
react_loop.py
-------------
Standalone ReAct loop extracted from AgentController.

The loop receives a ReactContext dataclass that holds all dependencies and
mutable state. This decouples the iteration logic from the controller class.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import TYPE_CHECKING, Callable, Optional

import httpx

import agent_logging
from builtin_tools.schemas import (
    build_tool_definitions,
    builtin_tool_names,
)
from confirmation import ConfirmationManager
from context_manager import (
    CompactionFailedError,
    _active_model,
    maybe_compact,
    validate_native_history,
)
from context_monitor import (
    ContextMonitor,
    ContextSnapshot,
    group_tool_defs_by_server,
)
from interfaces import ToolCall
from call_attempt_state import CallAttemptState
from checkpoint_store import CheckpointRetireStatus
from llm_client import CheckpointRetirementBlocked, LLMClient, LLMCancelledError, LLMContextOverflowError, LLMEmptyResponseError, LLMError, LLMPermanentError, _encode_images
from prompt_builder import estimate_messages_tokens, estimate_tokens
from memory_store import _summarize_result, extract_tools_used, save_task_outcome
from outcome_utils import (
    consume_execution,
    effective_execution,
    fail_outcome,
    with_execution,
)
from prompt_loader import build_system_prompt as _build_system_prompt
from trace_context import new_trace_id

from tool_formatting import (
    _compact_args_repr,
    _tool_icon,
    fmt_tool_brief,
    fmt_tool_call,
    format_tool_result,
)

# Backward-compat re-exports: these helpers moved to tool_formatting.py;
# tests and telegram_interface still import them from react_loop.
from tool_formatting import (  # noqa: F401
    _strip_shell_wrapper,
    _truncate_brief,
    fmt_tool_result_progress,
)
from native_turns import _append_native_tool_result, _linearize_native_turns

if TYPE_CHECKING:  # pragma: no cover - typing only
    from model_registry import CatalogView

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

# Effectively-unlimited step sentinel. The step gate was removed (doom-loop
# detection replaced it); this value now only initialises _LoopState.max_steps,
# which is retained for the checkpoint schema and _run_plan compat and is never
# enforced as a limit.
_EFFECTIVELY_UNLIMITED_STEPS = 10_000_000
_RE_PROMPT = "__re_prompt__"


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

    Per-layer indicator coverage: the ``"needs authorization"`` indicator catches
    the wrapper-level ``call_tool`` fast-fail ("MCP server '<name>' needs
    authorization. Run /mcp auth <name>."). The non-interactive redirect abort
    text ("...token expired, needs manual authorization") is covered by the
    pre-existing ``"token expired"`` indicator, because ``"needs authorization"``
    is not a substring of that message. Keep these two paths distinct.
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
    indicators = ("401", "unauthorized", "token expired", "refresh failed", "invalid token", "access denied", "needs authorization")
    return any(ind in error for ind in indicators)


def _handle_mcp_auth_failure(ctx: ReactContext, tool_name: str, outcome: dict) -> dict:
    """Transition the owning MCP server to needs_auth and return a helpful error."""
    server_name = ""
    if ctx.mcp_manager is not None:
        server_name = ctx.mcp_manager.server_name_for_tool(tool_name)  # type: ignore[attr-defined]
        ctx.mcp_manager.mark_needs_auth(server_name)  # type: ignore[attr-defined]
    display_name = server_name or "unknown"
    translated = {
        "success": False,
        "output": "",
        "error": (
            f"MCP token expired for server '{display_name}'. "
            f"Run `/mcp auth {display_name}` to re-authenticate."
        ),
        "exit_code": -1,
    }
    prior = effective_execution(outcome)
    return translated if prior == "unknown" else with_execution(translated, prior)


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
    execution_disposition: str = "unknown"  # local proof of execution; never serialized


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
    compaction_threshold_tokens: int = 60_000
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
    checkpoint_owner: object = None
    disk_recovery_available: bool = True
    main_send_started: bool = False

    # Immutable identity for result ownership during one interactive run. It is
    # deliberately independent of trace_id because resumed runs reuse traces.
    turn_id: str = field(default_factory=new_trace_id)

    # Immutable operator prompt identity for this run. This travels with
    # on-demand child admission rather than being inferred from executor state.
    prompt_id: Optional[str] = None

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

    # Milestone notifications — fire-and-forget Telegram pings sent by the main
    # agent at every `step_notify_interval` completed steps. 0 disables;
    # sub-agent contexts leave milestone_notify_fn unset (None) by construction.
    step_notify_interval: int = 0
    milestone_notify_fn: Optional[Callable[[int], None]] = None

    # Tool trace callback — called after each tool dispatch with a ToolTrace record
    on_tool_trace: Optional[Callable] = None  # Optional[Callable[[ToolTrace], None]]

    # Graph memory — Optional[GraphMemoryStore]; None when feature is disabled
    graph_memory: Optional[object] = None

    # Graph memory writer — Optional[GraphMemoryWriter]; for enqueuing new messages
    graph_memory_writer: Optional[object] = None

    # Max graph context entries to inject per turn
    graph_memory_max_entries: int = 10

    # Strategy memory — Optional[StrategyMemory]; None when disabled/unconfigured
    strategy_memory: Optional[object] = None



    # Creativity mode for prompt assembly — passed through to prompt_loader
    creativity_mode: str = "default"
    # Maximum iterations allowed during/after a plan execution
    plan_max_iterations: int = 50
    # Minutes of inactivity before injecting a soft "still working?" prompt
    inactivity_warn_minutes: int = 15
    max_retries: int = 4
    retry_delay: float = 2.0
    retry_wait: Optional[Callable[[threading.Event, float], bool]] = None

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


    # Presentation-only sanitized catalog view (never the authoritative run
    # pin). A replacement applies at the next prompt/definition build.
    model_catalog_view: CatalogView | None = None
    # View captured when _tool_defs was last built; a differing current view
    # invalidates the cached definitions and their token grouping.
    _tool_defs_view: CatalogView | None = None

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


def _exec_vision_query(ctx: ReactContext, args: dict) -> dict:
    """Execute a vision_query built-in: ask the LLM to analyse a local image file."""
    from path_policy import PathVerdict

    path = str(args.get("path", "")).strip()
    question = args.get("question", "What is in this image?")
    if not path:
        return with_execution({
            "success": False, "output": "",
            "error": "vision_query: 'path' argument is required.",
            "_guard_refusal": True,
        }, "refused")

    # ADR-0026 path-policy gate: vision_query reads a file, so apply the same
    # tier classification as file_read. This gate was ported from the legacy
    # trusted_zone_checker + _is_sensitive_path overlay when that overlay was
    # deleted in Wave 4.
    real_path = os.path.realpath(os.path.expanduser(path))
    builtin = ctx.builtin_executor
    if builtin is None:
        return with_execution({
            "success": False, "output": "",
            "error": "vision_query: confirmation infrastructure not available.",
            "_guard_refusal": True,
        }, "refused")

    policy = getattr(builtin, "path_policy", None)
    if policy is None:
        logger.error("PathPolicy not wired — refusing vision_query")
        return with_execution({
            "success": False,
            "output": "",
            "error": "vision_query: path policy not configured — refusing file operation",
            "error_type": "prohibited_path",
            "recoverable": False,
            "suggestion": "Restart the agent with a valid configuration so the path policy is initialised.",
        }, "refused")

    verdict, mode_or_reason = policy.classify(real_path, "read")
    if verdict == PathVerdict.PROHIBITED:
        return with_execution({
            "success": False,
            "output": "",
            "error": f"vision_query: permission denied: prohibited path ({mode_or_reason})",
            "error_type": "prohibited_path",
            "recoverable": False,
            "suggestion": "Use an allowed directory for image files.",
        }, "refused")

    if verdict == PathVerdict.UNRECOGNISED and getattr(builtin, "scheduled", False) is True:
        # Scheduled dispatch (ScheduledToolDispatch) never borrows grants or
        # reaches the interactive confirmation path.
        from builtin_executor import scheduled_approval_denied

        return scheduled_approval_denied("vision_query")

    if verdict == PathVerdict.UNRECOGNISED:
        # A standing operator grant may promote this UNRECOGNISED path to allowed.
        # The Telegram Confirm callback adds the grant before resuming the agent,
        # so a re-invoked vision_query after approval lands here and proceeds.
        if ctx.confirmation is not None:
            scope_owner: Optional[str] = None
            scope_fn = getattr(builtin, "_scope_owner_from_caller_tag", None)
            if scope_fn is not None:
                scope_owner = scope_fn(ctx.depth, ctx.caller_tag)
            grant_dir = os.path.dirname(real_path)
            if ctx.confirmation.check_grant("vision_query", grant_dir, scope_owner=scope_owner):
                verdict = PathVerdict.ALLOWED
                mode_or_reason = "r"

    if verdict == PathVerdict.UNRECOGNISED:
        desc = f"Vision query: <code>{path}</code>"
        if real_path != path:
            desc += f"\n(→ <code>{real_path}</code>)"
        desc += (
            f"\n📦 Scope: Confirm grants <code>{os.path.dirname(real_path)}</code> "
            "(all files under it) for this prompt; Till /reset grants it for the whole session"
        )
        desc += "\n⚠️ Reason for confirmation: Unrecognised zone"
        return builtin._requires_confirmation(
            "vision_query", args, desc,
            caller_depth=ctx.depth,
            caller_tag=ctx.caller_tag,
            zone_path=real_path,
        )

    encoded = _encode_images([path])

    if not encoded:
        return with_execution({
            "success": False, "output": "",
            "error": f"vision_query: could not read or encode image at '{path}'. "
                     "Check that the path is correct and the file exists.",
        }, "executed")
    messages = [{"role": "user", "content": question, "images": [path]}]
    try:
        answer = ctx.llm.chat(messages, system=None)
    except LLMCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return with_execution({"success": False, "output": "", "error": f"vision_query LLM call failed: {exc}"}, "executed")
    return with_execution({"success": True, "output": answer, "error": ""}, "executed")


def _run_vision_query(
    ctx: ReactContext,
    args: dict,
    progress_cb: Callable[[str], None],
) -> dict:
    """Execute a vision_query, prompting the operator when required.

    If ``_exec_vision_query`` stages a confirmation (UNRECOGNISED path), this
    helper blocks for operator approval, then re-runs with the staged frozen
    args.  The Telegram Confirm callback adds a standing grant for the parent
    directory before resuming the agent, so the second pass is grant-covered
    and proceeds.  On denial the staged entry is discarded and a cancelled
    outcome is returned.

    For sub-agents (depth>=1) the executor's headless bridge returns a
    ``vision_reexecute`` sentinel carrying the frozen args; this helper
    re-runs vision_query itself and refuses if the grant did not take effect.
    """

    def _take_token(token: str) -> None:
        """Silently remove a staged token without emitting a cancellation log."""
        builtin = ctx.builtin_executor
        if builtin is not None:
            builtin._pending_confirmations.take(token)

    def _refusal_outcome() -> dict:
        """Fail-closed outcome when re-run could not resolve the grant."""
        return with_execution({
            "success": False,
            "output": "",
            "error": (
                "vision_query: confirmation did not produce a usable grant; "
                "refusing to re-prompt."
            ),
            "error_type": "confirmation_failed",
            "recoverable": False,
        }, "refused")

    def _guard_re_run(outcome: dict, old_token: str | None = None) -> dict:
        """Leak guard: a re-run must not stage another confirmation cycle."""
        if outcome.get("requires_confirmation") or outcome.get("vision_reexecute"):
            if old_token:
                _take_token(old_token)
            if outcome.get("token"):
                _take_token(outcome["token"])
            return _refusal_outcome()
        return outcome

    outcome = _exec_vision_query(ctx, args)

    # Depth >= 1: the headless bridge already obtained approval and returns a
    # re-execute sentinel.  Re-run once; the grant now covers the parent dir.
    if outcome.get("vision_reexecute"):
        return _guard_re_run(_exec_vision_query(ctx, outcome["args"]))

    if not outcome.get("requires_confirmation"):
        return outcome
    if ctx.confirmation is None:
        # Fail-closed staged dict when the confirmation infrastructure is absent.
        return outcome

    token = outcome["token"]
    description = outcome.get("description", "vision_query")
    confirmed = ctx.confirmation.request_confirmation(
        token, "vision_query", description, progress_cb,
    )
    if confirmed:
        progress_cb(f"✅ Confirmed — executing `vision_query`\n{fmt_tool_call('vision_query', args)}")
        # Re-run with the staged frozen args, now covered by the operator grant.
        builtin = ctx.builtin_executor
        staged_args = args
        if builtin is not None:
            staged = builtin._pending.get(token)
            if staged is not None:
                staged_args = staged[1]
        outcome = _guard_re_run(_exec_vision_query(ctx, staged_args), token)
        # Execution completed (success or non-confirmation failure): silently
        # discard the staged token so we do not emit a spurious cancellation log.
        _take_token(token)
    else:
        if ctx.builtin_executor is not None:
            ctx.builtin_executor.cancel(token)
        outcome = fail_outcome(
            "Operation cancelled by the operator. "
            "Do not retry this operation via any other tool or method. "
            "Respond with a finish action now.",
        )
        outcome["_operator_cancelled"] = True
        outcome = with_execution(outcome, "cancelled")
        progress_cb("❌ Cancelled by operator — stopping task.")
    return outcome


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_BUILTIN_NAMES = frozenset({
    "shell", "file_read", "file_write", "schedule",
    "spawn_agent", "get_agent_result", "memory_write",
})

_JSON_FAIL_LIMIT = 3
# Consecutive identical tool-failure threshold for doom-loop self-termination.
# Mirrors _JSON_FAIL_LIMIT: the loop aborts with a user-readable message when
# the same (tool, error) pair repeats this many times in a row.
_DOOM_LOOP_LIMIT = 3
_ABSOLUTE_PLAN_CEILING = 200


@dataclass
class _LoopState:
    """Mutable per-run loop state."""
    messages: list[dict]
    goal_idx: int
    max_steps: int
    step: int = 0
    json_fail_streak: int = 0
    attempt_state: CallAttemptState | None = None
    action_accepted: bool = False
    operator_cancelled: bool = False
    # Set when an automatic compaction attempt fails; skips further automatic attempts
    # for this run only. Run state: a new run constructs a new _LoopState, and a resumed
    # run does not persist it (like the doom-loop counters below).
    auto_compaction_disabled: bool = False
    last_action_time: float = field(default_factory=time.time)
    warned_inactivity: bool = False
    # Doom-loop detection (T2): last failure key + consecutive repeat count.
    # Deliberately NOT persisted in checkpoints — a resumed run starts fresh.
    _last_tool_fail_key: str = ""
    _tool_fail_repeat: int = 0
    # Milestone notification double-fire guard (T4): the step number last
    # notified, so non-advancing iterations do not send duplicate pings.
    _last_notified_step: int = -1


@dataclass
class _Turn:
    """Result of one LLM call."""
    tool_calls: list          # non-empty → native path
    raw: str                  # LLM text output
    text_from_native: bool
    early_return: Optional[str]            # cancelled or error; if set, return immediately
    linearized_messages: Optional[list] = None  # set when messages were linearized for fallback


@dataclass
class _StepResult:
    """Control result returned by one iteration of the ReAct step loop."""
    should_continue: bool = False
    early_return: Optional[str] = None


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
    detail = type(exc).__name__

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
    if isinstance(exc, LLMContextOverflowError):
        return LLMErrorInfo("context", "📏 Context too long", False, detail)
    if isinstance(exc, LLMEmptyResponseError):
        return LLMErrorInfo("empty", "📭 Model returned no content", True, detail)
    if isinstance(exc, LLMPermanentError):
        return LLMErrorInfo("permanent", "❌ Provider rejected the request", False, detail)
    return LLMErrorInfo("unknown", "❌ LLM error: unexpected provider failure", True, detail)


def _get_user_goal(state: _LoopState) -> str:
    """Return the current user goal from its preserved history anchor."""
    if 0 <= state.goal_idx < len(state.messages):
        msg = state.messages[state.goal_idx]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"]
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

    attempt = state.attempt_state
    checkpoint: dict | None = None
    ctx.disk_recovery_available = False
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
            "model_id": ctx.llm.llm_cfg.get("id", ctx.llm.llm_cfg.get("model", "?")),
            "format_version": 2,
            "attempt_state": attempt.to_dict() if attempt is not None else None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_info": {
                "type": error_info.type,
                "message": error_info.message,
                "retryable": error_info.retryable,
                "detail": error_info.detail,
            },
        }
        if ctx.checkpoint_owner is None:
            saved = ctx.checkpoint_store.save(ctx.trace_id, checkpoint)
        else:
            saved = ctx.checkpoint_store.save(
                ctx.trace_id, checkpoint, owner=ctx.checkpoint_owner
            )
        ctx.disk_recovery_available = bool(saved) and (
            ctx.checkpoint_owner is None
            or not hasattr(ctx.checkpoint_store, "recovery_available")
            or bool(ctx.checkpoint_store.recovery_available(ctx.trace_id, ctx.checkpoint_owner))
        )

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
    prompt_message = error_info_json
    if not ctx.disk_recovery_available:
        progress(
            "⚠️ Disk recovery is unavailable because the checkpoint could not be saved. "
            "A retry can continue in this session, but a process restart will not restore this request."
        )
    response = ctx.confirmation.request_retry(
        token, prompt_message, progress, timeout_seconds=ctx.retry_timeout_seconds
    )

    # Step 3: Handle response
    if response == "retry":
        if attempt is not None and attempt.outbound_attempts:
            if not attempt.manual_retry_authorized:
                attempt.authorize_manual_retry()
            if checkpoint is not None and ctx.checkpoint_store is not None:
                checkpoint["attempt_state"] = attempt.to_dict()
                if ctx.checkpoint_owner is None:
                    saved = ctx.checkpoint_store.save(ctx.trace_id, checkpoint)
                else:
                    saved = ctx.checkpoint_store.save(
                        ctx.trace_id, checkpoint, owner=ctx.checkpoint_owner
                    )
                ctx.disk_recovery_available = bool(saved) and (
                    ctx.checkpoint_owner is None
                    or not hasattr(ctx.checkpoint_store, "recovery_available")
                    or bool(ctx.checkpoint_store.recovery_available(ctx.trace_id, ctx.checkpoint_owner))
                )
                if not ctx.disk_recovery_available:
                    logger.warning("Retry continuation has no verified disk recovery checkpoint")
                    progress(
                        "⚠️ The retry authorization could not be saved for restart recovery. "
                        "This continuation remains available only while this run stays active."
                    )
        return None  # loop continues, re-calls LLM with same state
    if response == "cancel":
        if ctx.checkpoint_store is not None and ctx.checkpoint_enabled:
            try:
                if ctx.checkpoint_owner is None:
                    ctx.checkpoint_store.delete(ctx.trace_id)
                else:
                    ctx.checkpoint_store.delete(ctx.trace_id, owner=ctx.checkpoint_owner)
            except (OSError, RuntimeError):
                logger.warning("Checkpoint cleanup was blocked; preserving recovery data")
        return f"❌ {error_info.message}"
    # timeout
    return f"❌ {error_info.message}"


def _get_tool_registry_for_grouping(ctx: ReactContext):
    """Return the tool registry to use for server grouping, or None."""
    try:
        return ctx.tool_index.registry
    except AttributeError:
        return None


def _ownerless_tool_defs_by_server(ctx: ReactContext, builtin_names: set[str]) -> dict[str, int]:
    """Count the complete definition array without any ownership registry.

    Used when the registry lookup fails: built-in/pseudo names stay ``builtin``,
    the rest ``unknown``, registered MCP servers are still seeded. If even that
    fails the whole array is counted as one ``unknown`` group; an empty array is
    a legitimate zero. A count is never skipped, truncated or turned into a
    rejection.
    """
    try:
        return group_tool_defs_by_server(ctx._tool_defs, None, ctx.mcp_manager, builtin_names)
    except Exception:  # noqa: BLE001 — a count must always be produced
        definitions = ctx._tool_defs or []
        if not definitions:
            return {"builtin": 0}
        return {"unknown": estimate_tokens(json.dumps(definitions, default=str))}


def _tool_defs_by_server_for_context(ctx: ReactContext) -> dict[str, int]:
    """Group current tool definitions by server and return per-server token counts.

    An ownership-lookup failure degrades to ownerless grouping of the same complete
    schema array, so the compaction decision and the snapshot never see zero for
    existing tools. Only the exception type is logged (messages may carry secrets).
    """
    builtin_names = builtin_tool_names()
    try:
        return group_tool_defs_by_server(
            ctx._tool_defs,
            _get_tool_registry_for_grouping(ctx),
            ctx.mcp_manager,
            builtin_names,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tool-def ownership grouping failed (%s); counting the complete definitions "
            "without ownership lookup", type(exc).__name__,
        )
        return _ownerless_tool_defs_by_server(ctx, builtin_names)


def _publish_context_snapshot(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    tool_defs_by_server: dict[str, int] | None = None,
) -> None:
    """Build and publish a ContextSnapshot from current turn data.

    *tool_defs_by_server* may be supplied when the caller has already computed
    it (e.g. the main ReAct loop); otherwise it is computed fresh. The tool
    total is always the sum of the per-server estimates, the same figure the
    compaction decision uses. Token estimation mirrors
    :func:`context_manager.maybe_compact`: the active model name drives the
    encoder selection and the system prompt is folded into the messages total.
    """
    if ctx.context_monitor is None:
        return

    _model = ctx.llm.llm_cfg.get("model")
    _total_with_system = estimate_messages_tokens(state.messages, system, model=_model)
    system_prompt_tokens = estimate_tokens(system, model=_model)
    chat_history_tokens = max(0, _total_with_system - system_prompt_tokens)
    if tool_defs_by_server is None:
        tool_defs_by_server = _tool_defs_by_server_for_context(ctx)
    # Copy: the per-run grouping cache must never be a live alias of a published snapshot.
    tool_defs_by_server = dict(tool_defs_by_server)
    tool_defs_tokens = sum(tool_defs_by_server.values())
    model_id = ctx.llm.llm_cfg.get("id")

    snapshot = ContextSnapshot(
        system_prompt_tokens=system_prompt_tokens,
        chat_history_tokens=chat_history_tokens,
        tool_defs_tokens=tool_defs_tokens,
        tool_defs_by_server=tool_defs_by_server,
        compaction_threshold=ctx.compaction_threshold_tokens,
        is_live=True,
        turn=state.step,
        model_id=model_id if isinstance(model_id, str) else "",
    )
    ctx.context_monitor.publish(snapshot)


_VIEW_FROM_CONTEXT = object()


def _assemble_system_prompt(
    ctx: ReactContext,
    user_goal: str,
    *,
    model_catalog_view: object = _VIEW_FROM_CONTEXT,
) -> str:
    """Assemble the full system prompt string for a run.

    Scheduled-job history is never injected: it is job-owned, not operator
    context (isolate-job-execution, design decision 2). The request presents
    ``model_catalog_view`` when given, otherwise ``ctx.model_catalog_view``.
    """
    view = ctx.model_catalog_view if model_catalog_view is _VIEW_FROM_CONTEXT else model_catalog_view
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
    # Absent view keeps the legacy builder call shape exactly.
    view_kwargs = {} if view is None else {"model_catalog_view": view}

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
        graph_context_section=_graph_context_section,
        strategies_section=strategies_section,
        # Suppress ResultsMemory recall when graph memory already supplied
        # semantic context this turn — avoids redundant/overlapping recall.
        results_top_k=0 if _graph_context_section else 2,
        parent_context_section=parent_context_section,
        mode=_sub_agent_prompt_variant or ctx.creativity_mode,
        **view_kwargs,
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


def _ensure_tool_defs(
    ctx: ReactContext,
    *,
    model_catalog_view: object = _VIEW_FROM_CONTEXT,
) -> None:
    """Lazily build _tool_defs if not populated for the requested catalog view.

    ``model_catalog_view`` is the view captured for the request (an explicit
    ``None`` included); by default ``ctx.model_catalog_view`` is read. Cached definitions are rebuilt when that view differs from the
    view they were built with; a rebuild also drops the dependent per-run token
    grouping cache (the definition revision is a digest of ``_tool_defs`` and
    follows it).
    """
    view = ctx.model_catalog_view if model_catalog_view is _VIEW_FROM_CONTEXT else model_catalog_view
    built_view = ctx._tool_defs_view
    if ctx._tool_defs is not None and (built_view is view or built_view == view):
        return
    # Absent view keeps the legacy builder call shape exactly.
    view_kwargs = {} if view is None else {"model_catalog_view": view}
    try:
        tool_defs = build_tool_definitions(
            mcp_manager=ctx.mcp_manager, **view_kwargs,
        )
    except Exception as _btd_exc:  # noqa: BLE001
        # The exception text may carry provider/MCP credentials; log only its type.
        logger.warning(
            "build_tool_definitions failed (%s) — MCP tools skipped", type(_btd_exc).__name__,
        )
        tool_defs = build_tool_definitions(mcp_manager=None, **view_kwargs)
    ctx._tool_defs = tool_defs
    ctx._tool_defs_view = view
    ctx._tool_defs_by_server = None
    ctx._tool_defs_tokens = 0


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
        "LLM returned non-JSON (step %d, streak %d, ~%d chars):\n--- BEGIN ---\n%s\n--- END ---",
        state.step, state.json_fail_streak, len(turn.raw), turn.raw[:1000],
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


def _definition_revision(ctx: ReactContext) -> str:
    """Return the stable digest of the tool definitions sent with a request."""
    return hashlib.sha256(
        json.dumps(ctx._tool_defs or [], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Return whether a failure is a transient transport or status error."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False


def _summarize_on_pin(ctx: ReactContext, prompt: str) -> str:
    """Summarize on the pinned model with its own bounded attempt state.

    The summary is sent without any size preview or fit check. Waits are
    cancel-responsive and cancellation propagates; the main request's attempt
    state and pending checkpoint are never touched.
    """
    messages = [{"role": "user", "content": prompt}]
    attempt = CallAttemptState(ctx.max_retries, ctx.retry_delay)
    wait = ctx.retry_wait or (lambda event, seconds: event.wait(seconds))
    while True:
        if ctx.cancel_event.is_set():
            raise LLMCancelledError("cancelled")
        try:
            bind_cancel = getattr(ctx.llm, "bind_cancel_event", None)
            with (
                bind_cancel(ctx.cancel_event) if callable(bind_cancel) else nullcontext(),
                ctx.llm.bind_attempt(attempt, manual=attempt.manual_retry_authorized),
            ):
                text = ctx.llm.chat(messages)
        except LLMCancelledError:
            raise
        except (LLMError, LLMEmptyResponseError, httpx.HTTPError) as exc:
            if ctx.cancel_event.is_set():  # a cancel racing the failure is a cancel
                raise LLMCancelledError("cancelled") from None
            if not _is_transient_llm_error(exc) or not attempt.can_retry_automatically():
                raise
            if wait(ctx.cancel_event, attempt.next_retry_delay()) or ctx.cancel_event.is_set():
                raise LLMCancelledError("cancelled") from None
        else:
            if ctx.cancel_event.is_set():  # a reply (even blank) after a cancel is a cancel
                raise LLMCancelledError("cancelled")
            return text


def _prepare_request(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    tool_defs_tokens: int,
) -> None:
    """Compact before a send when the estimated input exceeds the threshold.

    The estimate is the system prompt, the full chat history (including image
    allowances) and the complete current tool definitions
    (``tool_defs_tokens``, the sum of the per-server estimates). It is compared
    only with ``agent.compaction_threshold_tokens``; cached model limits, output
    caps and previews take no part, and nothing here rejects a send for size.
    Malformed native tool-call history still raises before any provider I/O.
    """
    if state.auto_compaction_disabled:
        # Only compaction is skipped: the source history is still validated before any send
        # (the enabled path validates it inside maybe_compact).
        validate_native_history(state.messages)
        return
    summarize = lambda prompt: _summarize_on_pin(ctx, prompt)  # noqa: E731
    total = estimate_messages_tokens(
        state.messages, system, model=_active_model(ctx.llm),
    ) + tool_defs_tokens
    try:
        compacted, goal_idx = maybe_compact(
            state.messages, system, ctx.llm,
            goal_idx=state.goal_idx, input_tokens=total,
            threshold=ctx.compaction_threshold_tokens, summarize=summarize,
        )
    except CompactionFailedError as failure:
        if ctx.cancel_event.is_set():
            raise LLMCancelledError("cancelled") from None
        # The original history is sent; further automatic attempts are off for this run.
        state.auto_compaction_disabled = True
        logger.warning(
            "Automatic compaction failed (%s, %s): the original history is sent "
            "unchanged and automatic compaction is disabled for the rest of this run",
            failure.reason, failure.cause_type,
        )
        return
    if compacted is not state.messages:
        if ctx.cancel_event.is_set():  # cancelled before publication: publish nothing
            raise LLMCancelledError("cancelled")
        state.messages, state.goal_idx = compacted, goal_idx


def _request_turn(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    progress: Callable[[str], None],
    supports_native_fallback: bool = False,
) -> _Turn:
    """Perform bounded same-request retries and return the accepted model turn."""
    raw = ""
    tool_calls: list[ToolCall] = []
    text_from_native = False
    linearized_messages = None
    attempt = state.attempt_state
    if attempt is None:
        attempt = state.attempt_state = CallAttemptState(ctx.max_retries, ctx.retry_delay)
    native = bool(ctx._tool_defs and supports_native_fallback and attempt.mode == "native")
    manual_call = False
    empty_count = 0

    def retire_before_send() -> None:
        """Retire an admitted snapshot only after preparation, at the send gate."""
        if ctx.cancel_event.is_set():
            raise LLMCancelledError("cancelled")
        retirement = ctx.checkpoint_store.retire_pending(ctx.trace_id, ctx.checkpoint_owner)
        if retirement.status is CheckpointRetireStatus.BLOCKED:
            ctx.disk_recovery_available = False
            raise CheckpointRetirementBlocked(retirement.message)
        ctx.disk_recovery_available = False
        ctx.main_send_started = True

    while True:
        if ctx.cancel_event.is_set():
            return _Turn([], "", False, "[Cancelled]")
        # Scripted clients keep the legacy pre-call gate; real clients retire
        # at their transport send gate after wire/schema preparation succeeds.
        if ctx.checkpoint_store is not None and ctx.checkpoint_owner is not None and not isinstance(ctx.llm, LLMClient):
            retirement = ctx.checkpoint_store.retire_pending(
                ctx.trace_id, ctx.checkpoint_owner
            )
            if retirement.status is CheckpointRetireStatus.BLOCKED:
                ctx.disk_recovery_available = False
                return _Turn([], "", False, retirement.message)
            ctx.disk_recovery_available = False
            ctx.main_send_started = True
        manual_call = attempt.manual_retry_authorized
        bind = getattr(ctx.llm, "bind_attempt", None)
        has_transport_binding = callable(bind)
        definition_revision = _definition_revision(ctx)
        before_send = (
            {"before_send": retire_before_send}
            if isinstance(ctx.llm, LLMClient)
            and ctx.checkpoint_store is not None
            and ctx.checkpoint_owner is not None else {}
        )
        binding = (
            bind(attempt, manual=manual_call, definition_revision=definition_revision, **before_send)
            if has_transport_binding else nullcontext()
        )

        try:
            with binding:
                if native:
                    attempts_before = attempt.outbound_attempts
                    try:
                        response = ctx.llm.chat_with_tools_fallback(
                            state.messages, tools=ctx._tool_defs, system=system,
                            progress_cb=progress,
                        )
                    except NotImplementedError:
                        if attempt.outbound_attempts != attempts_before:
                            raise
                        # Native unsupported is a local conversion only; the real
                        # client leaves the counter unchanged for this path.
                        attempt.select_mode("json")
                        native = False
                        linearized_messages = _linearize_native_turns(state.messages)
                        continue
                    if response.is_tool_call and response.tool_calls:
                        tool_calls = response.tool_calls
                    elif response.text:
                        raw = response.text
                        text_from_native = True
                    else:
                        raise LLMEmptyResponseError("empty response")
                    return _Turn(tool_calls, raw, text_from_native, None, linearized_messages)
                if linearized_messages is None:
                    linearized_messages = _linearize_native_turns(state.messages)
                raw = ctx.llm.chat_with_fallback(
                    linearized_messages, system=system, progress_cb=progress, json_mode=True,
                )
            if raw.strip():
                return _Turn([], raw, False, None, linearized_messages)
            empty_count += 1
            if empty_count <= 2 and attempt.can_retry_automatically():
                progress(f"⏳ Empty LLM response, retrying ({empty_count}/2)…")
                delay = attempt.next_retry_delay()
                wait = ctx.retry_wait or (lambda event, seconds: event.wait(seconds))
                if wait(ctx.cancel_event, delay) or ctx.cancel_event.is_set():
                    return _Turn([], "", False, "[Cancelled]")
                continue
            raise LLMEmptyResponseError("empty response")
        except LLMCancelledError:
            logger.info("Agent LLM call cancelled at step %d", state.step)
            return _Turn([], "", False, "[Cancelled]")
        except CheckpointRetirementBlocked as exc:
            return _Turn([], "", False, str(exc))
        except NotImplementedError:
            # A native implementation that reports unsupported after sending is
            # not eligible for protocol conversion.
            raise
        except (LLMError, httpx.HTTPError, LLMEmptyResponseError) as exc:
            if ctx.cancel_event.is_set():
                # Cancellation tears down the transport; that error is not a model failure.
                return _Turn([], "", False, "[Cancelled]")
            info = _classify_llm_error(exc)
            transient = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
            if isinstance(exc, httpx.HTTPStatusError):
                transient = exc.response.status_code in {408, 429, 500, 502, 503, 504}
            retryable_control = isinstance(exc, LLMEmptyResponseError)
            if retryable_control:
                empty_count += 1
            if (
                (transient or retryable_control)
                and (not retryable_control or empty_count <= 2)
                and attempt.can_retry_automatically()
            ):
                if retryable_control:
                    progress(f"⏳ Empty LLM response, retrying ({empty_count}/2)…")
                delay = attempt.next_retry_delay()
                wait = ctx.retry_wait or (lambda event, seconds: event.wait(seconds))
                if wait(ctx.cancel_event, delay) or ctx.cancel_event.is_set():
                    return _Turn([], "", False, "[Cancelled]")
                continue
            logger.warning("LLM request failed at step %d: %s", state.step, info.type)
            result = _handle_llm_error(ctx, state, info, progress, _get_user_goal(state))
            if result is None:
                continue
            return _Turn([], "", False, result)
        except Exception as exc:
            if ctx.cancel_event.is_set():
                return _Turn([], "", False, "[Cancelled]")
            info = _classify_llm_error(exc)
            result = _handle_llm_error(ctx, state, info, progress, _get_user_goal(state))
            if result is None:
                continue
            return _Turn([], "", False, result)


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
        supervisor = getattr(ctx.builtin_executor, "_supervisor", None)
        if ctx.depth == 0 and supervisor is not None:
            children = supervisor.unaccounted_children(ctx.turn_id)
            if children:
                details = ", ".join(
                    f"{agent_id} ({status})" for agent_id, status in children
                )
                sink(
                    "Cannot finish while turn-owned children are unaccounted: "
                    f"{details}. Call get_agent_result for completed children, "
                    "wait for running children, or call cancel_agent."
                )
                return None
        result = _finish_run(ctx, action_obj, user_goal)
        state.action_accepted = True
        return result

    if action == "tool":
        tool_name = action_obj.get("tool", "")
        raw_args = action_obj.get("args", {})
        args = _coerce_args(raw_args)
        if args is not raw_args:
            action_obj = {**action_obj, "args": args}
        _t0 = time.time()
        # vision_query needs LLM access — call directly, never through _dispatch_tool,
        # but still honour operator confirmation for UNRECOGNISED paths.
        if tool_name == "vision_query":
            outcome = _run_vision_query(ctx, args, progress)
        else:
            outcome = _dispatch_tool(ctx, action_obj, progress)
        _duration_ms = (time.time() - _t0) * 1000
        outcome, disposition = consume_execution(outcome)
        _emit_tool_trace(
            ctx, tool_name, args, success=outcome.get("success", False),
            duration_ms=_duration_ms,
            error=outcome.get("error", "") if not outcome.get("success", False) else "",
            execution_disposition=disposition,
        )
        if ctx.working:
            ctx.working.add_step("tool", {"tool": tool_name, "args": args, "success": outcome.get("success", False)})
        if outcome.get("send_file"):
            path_b64 = base64.b64encode(outcome["send_file"].encode()).decode()
            caption_b64 = base64.b64encode(outcome.get("caption", "").encode()).decode()
            progress(f"__FILE__:{path_b64}:{caption_b64}")
        tool_result = format_tool_result(tool_name, outcome)
        _end_status = "ok" if outcome.get("success") else "fail"
        progress(f"__TOOL_END__:{_end_status}:{tool_name}\n{fmt_tool_result_progress(tool_name, args, outcome)}")
        sink(tool_result)
        state.action_accepted = True

        if tool_name in ("get_agent_result", "wait_for_any_agent"):
            status = outcome.get("status")
            agent_id = outcome.get("agent_id")
            supervisor = getattr(ctx.builtin_executor, "_supervisor", None)
            if (
                ctx.depth == 0
                and supervisor is not None
                and status in ("done", "failed", "cancelled")
                and agent_id
            ):
                supervisor.mark_result_retrieved(ctx.turn_id, str(agent_id))

        # Doom-loop detection (T2): self-terminate when the same tool fails
        # with the same error repeatedly. Runs at the vision_query/normal-tool
        # merge point — the only place the outcome dict is in scope. Any
        # success (or a different failure key) resets the streak.
        if outcome.get("success") is False:
            error_text = (outcome.get("error", "") or "").strip()
            key = f"{tool_name}:{error_text[:200]}"
            if state._last_tool_fail_key == key:
                state._tool_fail_repeat += 1
            else:
                state._tool_fail_repeat = 1
            state._last_tool_fail_key = key
            if state._tool_fail_repeat >= _DOOM_LOOP_LIMIT:
                logger.error(
                    "Doom loop: tool '%s' failed identically %d times — aborting",
                    tool_name, state._tool_fail_repeat,
                )
                abort_msg = (
                    f"❌ Agent stuck: `{tool_name}` failed with the same error "
                    f"{state._tool_fail_repeat} times in a row "
                    "— stopping to avoid wasting budget. "
                    f"Last error: {error_text[:200]}"
                )
                progress(abort_msg)
                return abort_msg
        else:
            state._tool_fail_repeat = 0
            state._last_tool_fail_key = ""

        if outcome.get("_operator_cancelled") or ctx.cancel_event.is_set():
            state.operator_cancelled = True
        return None

    if action == "plan":
        if getattr(ctx.builtin_executor, "scheduled", False) is True:
            # Plan steps are built as plan-step agents on the shared executor,
            # outside the scheduled tool policy — refuse before any step exists.
            logger.warning("%splan refused for scheduled run", ctx.log_prefix)
            sink(
                "Error: plan is not available for scheduled jobs. "
                'Run the steps directly as individual {"action": "tool"} calls, '
                "one at a time."
            )
            return None
        plan_data = action_obj.get("plan", {})
        plan_disposition: list[str] = ["unknown"]
        result_msg, new_max_steps, _ = _run_plan(
            ctx, plan_data, state.max_steps, progress,
            on_disposition=plan_disposition.append,
        )
        if new_max_steps is not None:
            state.max_steps = max(state.step, min(int(new_max_steps), _ABSOLUTE_PLAN_CEILING))
        sink(result_msg)
        state.action_accepted = True
        if ctx.cancel_event.is_set():
            state.operator_cancelled = True
        return None

    logger.warning("Unknown action '%s' from LLM", action)
    sink(f'Unknown action "{action}". Use "tool", "plan", or "finish".')
    return None


def _run_single_step(
    ctx: ReactContext,
    state: _LoopState,
    system: str,
    user_goal: str,
    run_start: float,
    progress: Callable[[str], None],
    supports_native_fallback: bool,
) -> _StepResult:
    """Execute one ReAct step and return a control result for the outer loop.

    Responsibilities folded in here: cancellation checks, inactivity warnings,
    step counting/logging, tool-definition grouping, context compaction, the LLM
    turn request, action extraction/dispatch, and per-step context-snapshot
    publication. The helper mutates ``state`` (and possibly ``state.max_steps``
    when a plan action is dispatched; that mutation is not a gate — the loop no
    longer enforces a step limit).
    """
    if ctx.cancel_event.is_set():
        logger.warning("cancelled at step %d", state.step)
        return _StepResult(early_return="[Cancelled]")

    if not state.warned_inactivity and state.step > 1:
        warn_minutes = ctx.inactivity_warn_minutes
        if warn_minutes and (time.time() - state.last_action_time) > (warn_minutes * 60):
            state.warned_inactivity = True
            minutes = round((time.time() - state.last_action_time) / 60)
            state.messages.append({
                "role": "user",
                "content": (
                    f"You've been running for {minutes} minutes without finishing. "
                    "Are you still working? If you're done, use finish."
                ),
            })
            progress(f"⏳ Inactivity prompt after {minutes}m…")
            return _StepResult(should_continue=True)

    state.step += 1
    state.last_action_time = time.time()

    active_model = ctx.llm.llm_cfg.get("model", "?")
    agent_logging.log_event(
        agent_logging.LogEvent.STEP_BEGIN, "step begin",
        level=logging.INFO, logger=slog, step=state.step,
        model=active_model,
    )

    if ctx.on_step:
        try:
            ctx.on_step(state.step)
        except Exception:
            logger.debug("on_step callback failed", exc_info=True)
    progress(f"⚙️ Thinking… (step {state.step})")

    if ctx._tool_defs_by_server is None:
        try:
            ctx._tool_defs_by_server = _tool_defs_by_server_for_context(ctx)
            ctx._tool_defs_tokens = sum(ctx._tool_defs_by_server.values())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tool-def grouping failed (%s); counting the complete definitions "
                "without ownership lookup", type(exc).__name__,
            )
            ctx._tool_defs_by_server = _ownerless_tool_defs_by_server(ctx, set())
            ctx._tool_defs_tokens = sum(ctx._tool_defs_by_server.values())
    _tool_defs_by_server = ctx._tool_defs_by_server
    _tool_defs_tokens = ctx._tool_defs_tokens
    if state.attempt_state is None:
        state.attempt_state = CallAttemptState(ctx.max_retries, ctx.retry_delay)
    while True:
        try:
            _prepare_request(ctx, state, system, _tool_defs_tokens)
            break
        except LLMCancelledError:
            return _StepResult(early_return="[Cancelled]")
        except (LLMError, httpx.HTTPError) as exc:
            logger.warning("Request preparation failed at step %d: %s", state.step, _classify_llm_error(exc).type)
            result = _handle_llm_error(
                ctx, state, _classify_llm_error(exc), progress, _get_user_goal(state),
            )
            if result is not None:
                return _StepResult(early_return=result)

    turn = _request_turn(
        ctx, state, system, progress,
        supports_native_fallback=supports_native_fallback,
    )
    if turn.early_return is not None:
        return _StepResult(early_return=turn.early_return)
    if turn.linearized_messages is not None:
        state.messages = turn.linearized_messages

    try:
        action_obj, sink = _action_from_turn(turn, state)
    except ValueError:
        err_msg = _handle_non_json(state, turn)
        if err_msg == _RE_PROMPT:
            return _StepResult(should_continue=True)
        progress(err_msg)
        return _StepResult(early_return=err_msg)

    state.action_accepted = False
    final = _dispatch_action(
        ctx, action_obj, sink, state, user_goal, run_start, progress,
    )
    if final is not None:
        if state.action_accepted:
            state.attempt_state = None
        try:
            _publish_context_snapshot(
                ctx, state, system,
                tool_defs_by_server=_tool_defs_by_server,
            )
        except Exception:
            logger.warning("context snapshot publication failed", exc_info=True)
        return _StepResult(early_return=final)

    # Dispatch sets this marker only after accepting a valid action and recording
    # its result. Corrective refusals such as finish-veto keep the request alive.
    if state.action_accepted:
        state.attempt_state = None

    if state.operator_cancelled:
        return _StepResult(should_continue=False)

    agent_logging.log_event(
        agent_logging.LogEvent.STEP_END, "step end",
        level=logging.INFO, logger=slog, step=state.step,
    )
    try:
        _publish_context_snapshot(
            ctx, state, system,
            tool_defs_by_server=_tool_defs_by_server,
        )
    except Exception:
        logger.warning("context snapshot publication failed", exc_info=True)
    return _StepResult(should_continue=True)


def react_loop(
    ctx: ReactContext,
    user_goal: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    images: Optional[list[str]] = None,
    initial_state: Optional[_LoopState] = None,
) -> str:
    """Execute the ReAct loop while guaranteeing release of locally acquired ownership."""
    acquired_owner = False
    if (
        ctx.checkpoint_store is not None
        and ctx.checkpoint_owner is None
        and ctx.trace_id
        and hasattr(ctx.checkpoint_store, "acquire_owner")
        and (ctx.checkpoint_enabled or initial_state is not None)
    ):
        ctx.checkpoint_owner = ctx.checkpoint_store.acquire_owner(ctx.trace_id)
        if ctx.checkpoint_owner is None:
            return "❌ A run for this checkpoint trace is already active. Try again after it finishes."
        acquired_owner = True
    try:
        return _react_loop_owned(
            ctx, user_goal, progress_callback, images, initial_state
        )
    finally:
        if acquired_owner:
            ctx.checkpoint_owner.release()
            ctx.checkpoint_owner = None


def _register_turn(ctx: ReactContext) -> None:
    """Register the depth-0 turn with the sub-agent supervisor, if any."""
    supervisor = getattr(ctx.builtin_executor, "_supervisor", None)
    if ctx.depth == 0 and supervisor is not None:
        supervisor.register_active_turn(ctx.turn_id)


def _react_loop_owned(
    ctx: ReactContext,
    user_goal: str,
    progress_callback: Optional[Callable[[str], None]],
    images: Optional[list[str]],
    initial_state: Optional[_LoopState],
) -> str:
    """Run the loop; the public wrapper owns all setup and cleanup exceptions."""
    run_start = time.time()
    _ctx_tokens = agent_logging.bind_run_context(trace=ctx.trace_id, agent=ctx.label)
    active_model = ctx.llm.llm_cfg.get("model", "?")
    agent_logging.log_event(
        agent_logging.LogEvent.RUN_BEGIN, "run begin", level=logging.INFO, logger=slog,
        model=active_model, goal=user_goal[:80],
    )

    def _progress(msg: str):
        if progress_callback:
            progress_callback(msg)
        logger.debug("Agent progress: %s", msg)

    state = None
    cancelled_before_request = False
    try:
        if ctx.owns_cancel_event:
            ctx.cancel_event.clear()

        _supports_native_fallback = hasattr(ctx.llm, "chat_with_tools_fallback") and callable(
            ctx.llm.chat_with_tools_fallback
        )

        _register_turn(ctx)
        if ctx.cancel_event.is_set():
            cancelled_before_request = True
            return "⚠️ Task stopped by operator."
        ctx._tool_defs = None
        system = messages = goal_idx = None  # assigned below
        # One presentation view per request bundle: a replacement made by a
        # collaborator during assembly applies at the next run.
        request_view = ctx.model_catalog_view

        if ctx.working:
            ctx.working.start_task(user_goal)

        system = _assemble_system_prompt(
            ctx, user_goal, model_catalog_view=request_view
        )
        if ctx.cancel_event.is_set():
            cancelled_before_request = True
            return "⚠️ Task stopped by operator."
        messages, goal_idx = _init_messages(ctx, user_goal, images)
        ctx.memory.record_event(f"User request: {user_goal[:100]}")

        if ctx.graph_memory_writer is not None:
            try:
                ctx.graph_memory_writer.enqueue(user_goal, source="chat")
            except Exception as _gw_exc:  # noqa: BLE001
                logger.debug("Graph memory enqueue failed: %s", _gw_exc)

        _ensure_tool_defs(ctx, model_catalog_view=request_view)

        if initial_state is not None:
            state = initial_state
        else:
            # D1: the step gate is removed; max_steps is retained only for the
            # checkpoint schema and _run_plan compat, so it starts effectively
            # unlimited for every agent type.
            state = _LoopState(
                messages=messages, goal_idx=goal_idx,
                max_steps=_EFFECTIVELY_UNLIMITED_STEPS,
            )

        while True:
            result = _run_single_step(
                ctx, state, system, user_goal, run_start, _progress,
                supports_native_fallback=_supports_native_fallback,
            )
            if result.early_return is not None:
                return result.early_return
            if not result.should_continue:
                break

            # Milestone notification (T4): fire-and-forget ping at each
            # `step_notify_interval` completed steps. Only the main agent has
            # milestone_notify_fn wired; sub-agents default to None. The
            # _last_notified_step guard prevents double-fire on iterations
            # that return without advancing state.step (inactivity warnings).
            if (ctx.step_notify_interval
                    and ctx.milestone_notify_fn
                    and state.step > 0
                    and state.step != state._last_notified_step
                    and state.step % ctx.step_notify_interval == 0):
                try:
                    ctx.milestone_notify_fn(state.step)
                    state._last_notified_step = state.step
                except Exception:
                    logger.debug("milestone_notify failed", exc_info=True)

        # Only the operator-cancel path reaches this return (should_continue=False).
        ctx.memory.record_event("Task cancelled by operator")
        return "⚠️ Task stopped by operator."
    finally:
        if (
            ctx.cancel_event.is_set()
            and not cancelled_before_request
            and ctx.checkpoint_store is not None
            and (ctx.checkpoint_enabled or ctx.checkpoint_owner is not None)
            and (ctx.checkpoint_owner is None or ctx.main_send_started)
        ):
            try:
                if ctx.checkpoint_owner is None:
                    ctx.checkpoint_store.delete(ctx.trace_id)
                else:
                    ctx.checkpoint_store.delete(ctx.trace_id, owner=ctx.checkpoint_owner)
            except (OSError, RuntimeError):
                logger.warning("Cancelled-run checkpoint cleanup was blocked; preserving recovery data")
        # Canonical turn-drain boundary: the depth-0 turn cancels and awaits its
        # owned on-demand children here, publishing drain progress. Grants and
        # staged tokens are cleared by the caller only after this returns, and
        # the confirmation coordinator stays wired throughout so confirmation
        # callbacks remain processable. AgentController.run repeats the drain
        # as an idempotent fallback (see there); it must stay a no-op after this.
        supervisor = getattr(ctx.builtin_executor, "_supervisor", None)
        if ctx.depth == 0 and supervisor is not None:
            try:
                draining = supervisor.draining_children(ctx.turn_id)
                if draining:
                    try:
                        _progress(
                            "__DRAINING__:"
                            + ",".join(agent_id for agent_id, _status in draining)
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("draining progress publication failed", exc_info=True)
                try:
                    drain_errors = supervisor.drain_owned_children(
                        ctx.turn_id, cancel=True
                    )
                except Exception as drain_exc:  # noqa: BLE001
                    drain_errors = [f"turn drain failed: {drain_exc}"]
                for drain_error in drain_errors:
                    logger.error("sub-agent lifecycle cleanup failed: %s", drain_error)
                    try:
                        _progress(f"__DRAIN_ERROR__:{drain_error}")
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "drain error progress publication failed", exc_info=True
                        )
            finally:
                supervisor.unregister_active_turn(ctx.turn_id)
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
            model=ctx.llm.llm_cfg.get("model", "?"),
            steps=state.step if state is not None else 0,
            dur_ms=int((time.time() - run_start) * 1000),
        )
        agent_logging.reset_run_context(_ctx_tokens)


# ---------------------------------------------------------------------------
# Tool dispatch helpers
# ---------------------------------------------------------------------------




def _emit_tool_trace(
    ctx: ReactContext,
    tool_name: str,
    args: dict,
    *,
    success: bool,
    duration_ms: float,
    error: str = "",
    execution_disposition: str = "unknown",
) -> None:
    """Emit a tool trace event if a handler is registered."""
    if ctx.on_tool_trace is not None:
        ctx.on_tool_trace(ToolTrace(
            tool_name=tool_name,
            args_repr=_compact_args_repr(tool_name, args),
            success=success,
            duration_ms=round(duration_ms, 1),
            error=error,
            execution_disposition=execution_disposition,
        ))


def _run_plan(
    ctx: ReactContext,
    plan_data: dict,
    max_steps: int,
    progress: Callable[[str], None],
    *,
    on_disposition: Optional[Callable[[str], None]] = None,
) -> tuple[str, int, bool]:
    """Execute a plan. Returns (result_message, new_max_steps, success).

    ``on_disposition`` receives the plan's derived execution disposition
    (never the plan's success flag) after the private markers are stripped.
    """
    from execution_plan import (  # noqa: PLC0415
        ExecutionPlan,
        PlanExecutor,
        PlanStep,
        reject_generation_controls,
    )
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
    _trace_disposition = "unknown"
    progress("📋 Executing plan…")
    logger.info("plan | description: %s", plan_data.get("description", "")[:60])
    try:
        reject_generation_controls(plan_data)
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
        plan_result, plan_disposition = consume_execution(
            executor.execute(plan, ctx, progress_cb=progress)
        )
        _trace_disposition = plan_disposition
        if on_disposition is not None:
            on_disposition(plan_disposition)
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
                     error="" if plan_success else "plan execution failed",
                     execution_disposition=_trace_disposition)
    if ctx.working:
        ctx.working.add_step("plan", {"description": plan_data.get("description", ""), "success": plan_success})

    return result_msg, max_steps, plan_success


def _finish_run(
    ctx: ReactContext,
    action_obj: dict,
    user_goal: str,
) -> str:
    """Handle a finish action: coerce result, summarize, persist outcome. Returns the final result string."""
    result = action_obj.get("result", "Done.")
    if not isinstance(result, str):
        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)
        else:
            result = str(result) if result else "Done."
    ctx.memory.record_event(f"Agent finished: {result[:80]}")
    if ctx.short_term:
        ctx.short_term.add("user", user_goal)
        ctx.short_term.add("assistant", result)
    summary = ""
    tools_used: list[str] = []
    if ctx.depth == 0 and ctx.results and ctx.working and ctx.working.has_content():
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

    # vision_query handled directly (needs LLM access), with confirmation support.
    if tool_name == "vision_query":
        return _run_vision_query(ctx, args, _progress)

    # Built-in tools
    if ctx.builtin_executor and ctx.builtin_executor.is_builtin(tool_name):
        if tool_name in {
            "spawn_agent",
            "get_agent_result",
            "wait_for_any_agent",
            "cancel_agent",
        }:
            args = {**args, "_operator_turn_id": ctx.turn_id}
        if tool_name == "spawn_agent":
            args = {
                **args,
                "_operator_prompt_id": ctx.prompt_id,
            }
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
                outcome = with_execution(outcome, "cancelled")
                _progress("❌ Cancelled by operator — stopping task.")
        return outcome

    # MCP tools — wrap dispatch with TOOL_* lifecycle events so MCP calls are
    # visible to log_query. They bypass both executors (which emit these events
    # themselves), so without this wrapping MCP tools never appear as TOOL_*
    # events. Field conventions match the builtin/tool executors.
    # MCP dispatch performs no interactive confirmation (auth-failure
    # translation only), so a scheduled-origin call can never reach the
    # operator's confirmation channel — denial is structural, not a check.
    if ctx.mcp_manager and ctx.mcp_manager.has_tool(tool_name):
        with _tool_span(tool_name, mcp_auth=(ctx, tool_name)) as span:
            span.record(ctx.mcp_manager.call_tool(tool_name, args))
        # .outcome carries the auth-translated dict, not the raw call result.
        return span.outcome

    # Unknown tool — no hand-written tools exist anymore
    outcome = fail_outcome(f"Tool '{tool_name}' is not a built-in tool, MCP tool, or vision_query.")
    outcome["_guard_refusal"] = True
    return with_execution(outcome, "refused")
