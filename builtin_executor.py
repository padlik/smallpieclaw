"""
builtin_executor.py
-------------------
Always-available built-in tools: shell, file_read, file_write.

These tools are injected into every agent run regardless of what is in the
tools/ or tools_generated/ directories. The agent is instructed to prefer
built-in tools before creating new ones.

Dangerous operations (destructive commands, sensitive file access, any write)
require explicit user confirmation before execution. When confirmation is
needed, execute() returns {"requires_confirmation": True, "token": ..., ...}
and the caller is expected to call confirm(token) or cancel(token) after the
user responds.

Error classification (Phase 3: Agent Recovery):
    * Transient (retryable): tool_timeout, network_error, syntax_error.
    * Planning (no retry, needs alternative approach): wrong_model_for_task,
      fundamentally_wrong_approach, impossible_with_current_tools.
    * Fatal (no retry, environment/fix required): permission_denied,
      file_not_found, command_not_found.

Result dicts produced by built-in tools include the following recovery fields:
    - error_type: kebab-case error identifier (empty string on success).
    - recoverable: whether the error might succeed on retry (False on success).
    - suggestion: human-readable recovery hint (empty string on success).
"""

from __future__ import annotations

import codecs
import difflib
import html as _html_mod
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import structlog

import agent_logging

slog = agent_logging.get_logger(__name__)


class _SupportsClose(Protocol):
    """Protocol for file-like objects that support close()."""

    def close(self) -> None: ...


class _SupportsWriteClose(Protocol):
    """Protocol for file-like objects that support write() and close()."""

    def write(self, text: str, /) -> int: ...
    def close(self) -> None: ...


logger = logging.getLogger(__name__)

_CONTEXT_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _validate_context_key(context_key: str) -> str:
    """Validate a sub-agent context key before using it as a file stem."""
    if not isinstance(context_key, str) or not _CONTEXT_KEY_RE.fullmatch(context_key):
        raise ValueError(
            "context_key must be 1-128 chars: letters, digits, underscore, dash, or dot; "
            "it must start with a letter or digit"
        )
    if context_key in {".", ".."}:
        raise ValueError("context_key cannot be '.' or '..'")
    return context_key


def _context_path(context_key: str, data_dir: str) -> str:
    """Return the absolute context path, rejecting path traversal."""
    safe_key = _validate_context_key(context_key)
    ctx_dir = os.path.abspath(os.path.join(data_dir, "job_contexts"))
    path = os.path.abspath(os.path.join(ctx_dir, f"{safe_key}.json"))
    if os.path.commonpath([ctx_dir, path]) != ctx_dir:
        raise ValueError("context_key resolves outside job_contexts")
    return path


def _truncate_output(text: str, limit: int) -> str:
    """Truncate *text* to at most *limit* chars, keeping the tail.

    Tail semantics are intentional: for build, test, and script output the
    useful information (errors, results, summaries) almost always appears near
    the end.  When truncation occurs a clear marker is prepended so the LLM
    knows data was omitted.
    """
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    omitted = len(text) - limit
    return f"[...{omitted} chars omitted, showing last {limit} chars...]\n{kept}"


def _truncate_tail(tail: str, total_chars: int, limit: int) -> str:
    """Build truncated output from a rolling tail when total stream size is known.

    Use instead of _truncate_output when the caller only kept a rolling
    *tail* in memory (not the full stream) but knows the *total_chars* written.
    When total_chars <= limit the tail *is* the full output and is returned
    as-is.  Otherwise a correct omission count is prepended.
    """
    if total_chars <= limit:
        return tail
    omitted = total_chars - limit
    kept = tail[-limit:]
    return f"[...{omitted} chars omitted, showing last {limit} chars...]\n{kept}"


# ---------------------------------------------------------------------------
# Dangerous / sensitive pattern detection
# ---------------------------------------------------------------------------

_DANGEROUS_SHELL_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+-[^\s]*r[^\s]*\s+/", "recursive removal from /"),
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"\bdd\b.*\bof=", "raw device write with dd"),
    (r"\bmkfs\b", "filesystem format with mkfs"),
    (r">\s*/dev/(?!null)", "redirect to device node"),
    (r"\bchmod\s+777\b", "chmod 777"),
    (r"\bcurl\b.*\|\s*(?:ba)?sh\b", "curl pipe to shell"),
    (r"\bwget\b.*\|\s*(?:ba)?sh\b", "wget pipe to shell"),
    (r">\s*/etc/", "write to /etc/"),
    (r">\s*/boot/", "write to /boot/"),
    (r"\bsudo\s+su\b", "sudo su"),
    (r":\(\)\{.*:\|:&\}", "fork bomb"),
    (r"/dev/tcp/", "TCP reverse shell"),
    (r"\bnc\s+-e\b", "netcat reverse shell"),
    # Writing to or executing from tools_generated/ is equivalent to creating/running a tool
    # and must go through the same operator confirmation gate as create_tool.
    (r"tools_generated/", "write/execute in tools_generated/ (same as tool creation — requires operator approval)"),
]

_SENSITIVE_PATH_PATTERNS: list[str] = [
    r"/etc/passwd",
    r"/etc/shadow",
    r"/etc/sudoers",
    r"\.ssh/id_",
    r"\.ssh/authorized_keys",
    r"id_rsa",
    r"id_ecdsa",
    r"id_ed25519",
    r"\.pem$",
    r"\.key$",
    r"\.secret",
    r"config\.toml$",
    r"\.env$",
    r"secrets\.",
]


def _is_dangerous_shell(command: str) -> tuple[bool, str]:
    """Return (is_dangerous, reason). Check command against known dangerous patterns."""
    for pattern, reason in _DANGEROUS_SHELL_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True, reason
    return False, ""


def _is_sensitive_path(path: str) -> tuple[bool, str]:
    """Return (is_sensitive, reason). Check path against sensitive file patterns."""
    for pattern in _SENSITIVE_PATH_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return True, f"matches sensitive pattern: {pattern}"
    return False, ""


# ---------------------------------------------------------------------------
# log_query helpers (structured-log introspection built-in)
# ---------------------------------------------------------------------------
_WARNING_LEVEL_NUM: int = logging.WARNING
# Option C default view: with no explicit level/event_type filter, surface the
# high-signal lifecycle events below (plus anything WARNING+); routine per-step
# bookkeeping (STEP_*) is omitted unless it is itself WARNING+.
_LOG_QUERY_DEFAULT_INCLUDE_EVENTS: frozenset[str] = frozenset(
    {"TOOL_START", "TOOL_END", "LLM_CALL"}
)
# Bounds for the log_query built-in: cap disk I/O, parse work, and per-field
# size so a mid-loop introspection call cannot blow the context/token budget.
_LOG_QUERY_TAIL_BYTES: int = 1_000_000
_LOG_QUERY_MAX_SCAN_LINES: int = 5000
_LOG_QUERY_FIELD_MAXLEN: int = 500


def _log_level_to_num(level: object) -> int:
    """Map a level NAME or number to its numeric value (unknown/blank -> 0).

    Accepts the lowercase level names emitted by structlog's ``add_log_level``
    (e.g. ``"info"``) as well as standard uppercase names; comparison is
    case-insensitive. Non-numeric/unknown levels sort below every real level.
    """
    if isinstance(level, bool):
        return 0
    if isinstance(level, (int, float)):
        return int(level)
    if not level:
        return 0
    num = logging.getLevelName(str(level).upper())
    return num if isinstance(num, int) else 0


def _log_query_default_keep(rec: dict) -> bool:
    """Option C default-view predicate for a single structured log record.

    Keep the record if it is WARNING+ or a high-signal lifecycle event
    (TOOL_START/TOOL_END/LLM_CALL); routine STEP_* events are dropped unless
    they are themselves WARNING+.
    """
    level_num = _log_level_to_num(rec.get("level"))
    event_type = str(rec.get("event_type", ""))
    is_warn = level_num >= _WARNING_LEVEL_NUM
    return is_warn or event_type in _LOG_QUERY_DEFAULT_INCLUDE_EVENTS


def _read_tail_lines(path: str, max_bytes: int, max_lines: int) -> tuple[list[str], bool]:
    """Return ``(lines, window_saturated)`` for the trailing window of *path*.

    Reads at most the final *max_bytes* and returns at most the most recent
    *max_lines*, so scanning the active log stays cheap regardless of total file
    size. When the read starts mid-file the leading partial line is dropped, so
    callers never see a truncated (unparseable) JSON record.

    ``window_saturated`` is True when the window did NOT cover the whole file —
    either the byte read began mid-file (``max_bytes`` reached) or more lines
    were present than *max_lines* (``max_lines`` reached) — so older records fell
    outside the returned tail and counts derived from it are recent-window lower
    bounds, not full-file totals.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # drop the partial first line from a mid-file seek
    window_saturated = start > 0 or len(lines) > max_lines
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines, window_saturated


def _log_query_project(rec: dict) -> dict:
    """Return a shallow copy of *rec* with over-long string values truncated.

    Caps any single field at ``_LOG_QUERY_FIELD_MAXLEN`` chars so one verbose
    record (e.g. a large ``err`` or ``msg``) cannot dominate the log_query
    output — the field-level analogue of ToolExecutor.max_output.
    """
    projected: dict = {}
    for key, value in rec.items():
        if isinstance(value, str) and len(value) > _LOG_QUERY_FIELD_MAXLEN:
            omitted = len(value) - _LOG_QUERY_FIELD_MAXLEN
            projected[key] = f"{value[:_LOG_QUERY_FIELD_MAXLEN]}…[+{omitted} chars]"
        else:
            projected[key] = value
    return projected


# ---------------------------------------------------------------------------
# Tool descriptor (compatible with ToolRegistry Tool dataclass interface)
# ---------------------------------------------------------------------------

@dataclass
class BuiltinTool:
    name: str
    description: str
    language: str = "python"
    path: str = "<builtin>"
    is_generated: bool = False


BUILTIN_TOOLS: dict[str, BuiltinTool] = {
    "secret_get": BuiltinTool(
        name="secret_get",
        description="Retrieve a value from the vault by key. Args: key (str, REQUIRED). Requires user confirmation.",
    ),
    "shell": BuiltinTool(
        name="shell",
        description="Execute a shell command on the host system. Args: command (str), timeout (int, default 30).",
    ),
    "file_read": BuiltinTool(
        name="file_read",
        description="Read a file from the filesystem. Args: path (str), max_bytes (int, default 50000), offset (int, default 0). Negative offset counts from end of file (e.g. -5000 reads last 5000 bytes, like tail).",
    ),
    "file_write": BuiltinTool(
        name="file_write",
        description="Write content to a file on the filesystem. Args: path (str), content (str), mode (str: 'w' or 'a', default 'w').",
    ),
    "file_diff": BuiltinTool(
        name="file_diff",
        description=(
            "Compare two files and return a traditional unified diff. "
            "Read-only and non-destructive. "
            "Args: path_a (str, required — first/old file), "
            "path_b (str, required — second/new file), "
            "context_lines (int, default 3 — lines of context around changes), "
            "max_bytes (int, default 200000 — per-file read cap). "
            "Returns the unified diff text, or 'Files are identical.' when there are no differences."
        ),
    ),
    "file_send": BuiltinTool(
        name="file_send",
        description=(
            "Send a local file or photo from the server to the Telegram chat. "
            "Args: path (str, required — absolute or relative path to the file), "
            "caption (str, optional — text shown below the file/photo)."
        ),
    ),
    "schedule": BuiltinTool(
        name="schedule",
        description=(
            "Manage scheduled jobs and reminders. "
            "Args: action (str: list|add|remove|pause|resume|run_now), "
            "tag (str, unique job name), "
            "task (str, REQUIRED for add — the natural-language goal or reminder text), "
            "cron (str, 5-field cron expression in local time, e.g. '0 */6 * * *' = every 6h, "
            "'0 2 * * *' = daily at 02:00, '*/30 * * * *' = every 30 min). "
            "For one-time reminders use schedule_type='once' with run_at='HH:MM'. "
            "Legacy fields hours/minutes/time are still accepted and auto-converted to cron. "
            "notify (bool, default true). "
            "model (str, optional — model identifier to use for this job's sub-agent, e.g. 'gpt-4o'). "
            "fallback_models (list[str], optional — ordered list of fallback model identifiers "
            "to try if the primary model is unavailable, e.g. ['gemini-3-flash-preview:cloud', 'gpt-4o-mini']). "
            "preserve_context (bool, default false — if true, conversation history is kept between runs). "
            "max_iterations (int, optional — override the step limit for this job; "
            "default: scheduled_max_iterations from config, 0 = unlimited). "
            "Always provide a non-empty task when adding any job."
        ),
    ),
    "spawn_agent": BuiltinTool(
        name="spawn_agent",
        description=(
            "Spawn an isolated sub-agent in the background for a long-running or model-specific task. "
            "Returns immediately with agent_id — use get_agent_result(agent_id) to retrieve the result.\n"
            "\n"
            "WRITING A GOOD TASK — sub-agents run in complete isolation (no shared context, memory, or files):\n"
            "  • State the OBJECTIVE clearly in the first sentence.\n"
            "  • Include ALL context the sub-agent needs: file paths already on disk, data already extracted,\n"
            "    language requirements, relevant facts, constraints.\n"
            "  • Specify which TOOLS to use (shell, file_read, etc.) and the order if sequence matters.\n"
            "  • Specify the exact OUTPUT required: format, language, structure, length.\n"
            "  • Do NOT rely on sub-agent improvisation — be explicit and complete.\n"
            "  • Sub-agents cannot spawn further sub-agents.\n"
            "\n"
            "Args:\n"
            "  task            (str, REQUIRED) — self-contained instructions for the sub-agent.\n"
            "                  Must be named 'task', NOT 'prompt', 'goal', or 'description'.\n"
            "  model           (str, optional) — model id from AVAILABLE MODELS (default: background_model).\n"
            "  response_format (str, optional) — 'text' (default) | 'json' | 'file'.\n"
            "                  json → sub-agent must return a single valid JSON object.\n"
            "                  file → sub-agent writes output to a file and returns the absolute path.\n"
            "  context_payload (dict, optional) — parent context to inject into the sub-agent's system prompt.\n"
            "  context_key     (str, optional) — key for persisting conversation history between calls.\n"
            "  max_tokens      (int, optional) — override maximum tokens in the sub-agent's response.\n"
            "  temperature     (float, optional) — override sampling temperature (0.0–2.0).\n"
            "  top_p           (float, optional) — override nucleus sampling probability (0.0–1.0).\n"
            "\n"
            "Example (good task — self-contained):\n"
            "{\"task\": \"Summarise the podcast transcript already saved at /tmp/piclaw/clean_transcript.txt "
            "in Russian. Use file_read to load the file. Return a structured report with three sections: "
            "Key Topics, Main Arguments, Conclusions. Plain text, maximum 800 words.\", "
            "\"model\": \"kimi-k2.5:cloud\", \"response_format\": \"text\"}"
        ),
    ),
    "get_agent_result": BuiltinTool(
        name="get_agent_result",
        description=(
            "Wait for a sub-agent to finish and retrieve its result. "
            "Blocks until the sub-agent completes or the timeout is reached. "
            "Args: agent_id (str, REQUIRED — the id returned by spawn_agent), "
            "timeout (int, optional — seconds to wait, default: configured subagent_result_timeout), "
            "cancel_on_timeout (bool, optional — if true (default), the sub-agent is automatically cancelled "
            "when the timeout expires so it does not waste tokens or send a stale notification; "
            "set to false only if you intend to call get_agent_result again for the same agent). "
            "Returns: {status: 'done'|'failed'|'cancelled'|'timeout'|'not_found', "
            "result_type: 'text'|'json'|'file', result: <output>}. "
            "Example: {\"agent_id\": \"sa-abc123\"}"
        ),
    ),
    "memory_write": BuiltinTool(
        name="memory_write",
        description=(
            "Read or write the agent's persistent memory (data/memory.json). "
            "Actions: "
            "  set    — store any value under a key: args: key (str), value (any). "
            "  append — append an item to a list key (creates the list if needed): args: key (str), value (any). "
            "  delete — remove a key: args: key (str). "
            "  get    — retrieve a single key: args: key (str). "
            "Use 'append' on key 'notes' to add a persistent note. "
            "Examples: "
            "{\"action\":\"append\",\"key\":\"notes\",\"value\":\"Disk replaced 2025-04-01\"}, "
            "{\"action\":\"set\",\"key\":\"last_backup\",\"value\":\"2025-04-05\"}, "
            "{\"action\":\"delete\",\"key\":\"old_key\"}."
        ),
    ),
    "vision_query": BuiltinTool(
        name="vision_query",
        description=(
            "Ask the active LLM to analyse a local image file. "
            "Use this whenever the user asks about the contents of an image or photo. "
            "Args: path (str, required — absolute path to the image file on disk), "
            "question (str, required — what to ask about the image, e.g. 'Who is in this photo?'). "
            "Returns the LLM's text description/answer. "
            "Only works with vision-capable models (GPT-4o, Claude 3+, Gemini, LLaVA, etc.). "
            "Example: {\"path\": \"/home/pi/downloads/photo.jpg\", \"question\": \"What is in this image?\"}"
        ),
    ),
    "file_patch": BuiltinTool(
        name="file_patch",
        description=(
            "Make a surgical search-and-replace edit to a file. "
            "Prefer this over file_read + file_write when making small targeted changes. "
            "Args: "
            "  path       (str, required) — absolute path to the file. "
            "  old_str    (str, required) — exact text to find in the file; include enough surrounding "
            "                              context (e.g. the whole line) to be unambiguous. "
            "  new_str    (str, required) — replacement text (may be empty string to delete old_str). "
            "  occurrence (int, optional, default 1) — which occurrence to replace (1 = first); "
            "                                          0 = replace all occurrences. "
            "Returns an error (no changes made) if old_str is not found or matches more than one "
            "occurrence when occurrence=1. "
            "Always requires operator confirmation — confirmation shows a diff-style preview. "
            "Example: {\"path\": \"/etc/app/config.toml\", \"old_str\": \"port = 8080\", \"new_str\": \"port = 9090\"}"
        ),
    ),
    "memory_graph_search": BuiltinTool(
        name="memory_graph_search",
        description=(
            "Search the knowledge graph for facts, entities, people, preferences, or past events. "
            "Returns relevant entities and relationships from the graph memory. "
            "Args: query (str, required) — what to search for. "
            "Only available when graph memory is enabled ([graph_memory] enabled = true in config). "
            "ALWAYS call this before saying 'I don't have information about...' regarding past events "
            "or user preferences. "
            "Example: {\"query\": \"user preferred languages\"}"
        ),
    ),
    "memory_graph_store": BuiltinTool(
        name="memory_graph_store",
        description=(
            "Store an important fact, preference, or relationship in the knowledge graph. "
            "Use this when the user shares important facts or preferences that should be remembered. "
            "Args: "
            "  content     (str, required) — the fact or information to remember. "
            "  entity_type (str, optional) — type hint: person, tool, concept, preference, other. "
            "  user_id     (str, optional) — user identifier (default: 'agent'). "
            "Only available when graph memory is enabled ([graph_memory] enabled = true in config). "
            "Example: {\"content\": \"User prefers Python over JavaScript for automation scripts\", "
            "\"entity_type\": \"preference\"}"
        ),
    ),
    "log_query": BuiltinTool(
        name="log_query",
        description=(
            "Query the agent's own structured run log (the active JSONL sink) to inspect "
            "recent tool activity, errors, and events for the current or a specified run. "
            "Reads ONLY the recent TAIL of the active log (the most recent lines/bytes), so "
            "results reflect a recent window, not the entire run history. "
            "Read-only and non-destructive; all arguments are optional. "
            "Args: "
            "  trace       (str) — run trace id (e.g. 'r-1a2b3c4d'); defaults to the CURRENT run. "
            "                      Use '*' (or '') to search across all runs/traces. "
            "  level       (str) — minimum level NAME to include: DEBUG|INFO|WARNING|ERROR|CRITICAL. "
            "  event_type  (str) — exact event type to match: TOOL_START|TOOL_END|TOOL_FAILED|"
            "LLM_CALL|LLM_FAILED|STEP_BEGIN|STEP_END|RUN_BEGIN|RUN_END|ERROR. "
            "  tool        (str) — exact tool name to match (e.g. 'shell', 'file_read'). "
            "  since       (str) — ISO timestamp; only include records at or after this time. "
            "  limit       (int, default 50) — max records to return (the most recent are kept if more match). "
            "  text        (str) — case-insensitive substring to search across the full JSON "
            "                      representation of each record (all fields: msg, event, logger, etc.). "
            "                      Alias 'query' is also accepted. "
            "                      When text/query is given without level or event_type, the "
            "                      high-signal default view is NOT applied, so all INFO records "
            "                      (including startup messages such as 'GraphMemoryStore initialised') "
            "                      are visible. "
            "                      For natural-language / log-wide text searches, pass trace='*' "
            "                      together with text='…' to cover all runs. "
            "When neither level, event_type, nor text/query is given, a high-signal default view "
            "is returned: warnings/errors plus TOOL_START/TOOL_END/LLM_CALL events (routine "
            "STEP_* events are omitted). "
            "Returns a JSON object with 'records', 'count', 'truncated', 'total_matched', "
            "'window_saturated', and 'scanned_lines'. NOTE: the active log is shared across all "
            "traces, so 'total_matched' is a count within the scanned recent window (over "
            "'scanned_lines' lines), NOT a full-run total; when 'window_saturated' is true, older "
            "records fell outside the scanned window — narrow with 'since'/'tool'/'event_type' or "
            "treat counts as a recent-window lower bound. "
            "Examples: {\"tool\": \"shell\", \"limit\": 20}, "
            "{\"trace\": \"*\", \"text\": \"GraphMemoryStore\", \"limit\": 10}"
        ),
    ),
}


# ---------------------------------------------------------------------------
# BuiltinExecutor
# ---------------------------------------------------------------------------

class BuiltinExecutor:
    """
    Executes built-in tools with optional confirmation for dangerous operations.

    Confirmation flow:
      1. execute() detects a dangerous/sensitive operation.
      2. Returns {"requires_confirmation": True, "token": token, "description": desc}.
      3. Caller stores the token and prompts the user.
      4. On user approval:  call confirm(token) → returns the actual result dict.
      5. On user rejection: call cancel(token)  → cleans up state.
    """

    def __init__(self, default_timeout: int = 30, max_output: int = 4000, scheduler=None,
                 sub_agent_factory=None, data_dir: str = "data",
                 memory=None, max_subagents: int = 6, subagent_result_timeout: int = 300,
                 notify_html_fn=None, shell_backend: str = "subprocess",
                 shell_pty_cols: int = 220, shell_pty_rows: int = 50,
                 shell_streaming: bool = False, working=None, results=None,
                 vault_path: str = "", log_jsonl_path: str = ""):
        self.default_timeout = default_timeout
        self.max_output = max_output
        self.scheduler = scheduler  # Optional[Scheduler] — for the schedule built-in
        self._sub_agent_factory = sub_agent_factory  # Callable[[model, context_key, label, notify_fn], SubAgentRunner]
        self._data_dir = data_dir
        self._memory = memory  # Optional[MemoryStore] — for memory_write built-in
        self._working = working  # Optional[WorkingMemory] — for spawn_agent context summary
        self._results = results  # Optional[ResultsMemory] — for spawn_agent context summary
        self._max_subagents = max_subagents
        self._subagent_result_timeout = subagent_result_timeout
        self._notify_html_fn = notify_html_fn  # Optional[Callable[[str], None]] — HTML notify path
        self._vault_path = vault_path  # Path to TOML vault file for secret_get
        self._log_jsonl_path = log_jsonl_path  # Active JSONL log sink for the log_query built-in
        self._graph_memory = None   # Optional[GraphMemoryStore] — set by main.py after init
        self._graph_memory_writer = None  # Optional[GraphMemoryWriter] — set by main.py after init
        self._shell_backend = shell_backend   # "subprocess" or "pty"
        self._shell_pty_cols = shell_pty_cols
        self._shell_pty_rows = shell_pty_rows
        self._shell_streaming = shell_streaming  # forward chunks to on_chunk callback (PTY only)
        self._sub_agent_pool = ThreadPoolExecutor(
            max_workers=max_subagents, thread_name_prefix="sub-agent"
        )
        # pending: token -> (tool_name, args)
        self._pending: dict[str, tuple[str, dict]] = {}
        # Headless (sub-agent) confirmation bridge
        # token -> threading.Event  (set when the operator responds)
        self._headless_confirm_events: dict[str, object] = {}
        # token -> bool  (True = approved, False = denied)
        self._headless_confirm_results: dict[str, bool] = {}
        # Optional prompt callback: fn(token, tool_name, description, caller_tag) -> None
        # Set by main.py after TelegramInterface is created.
        self._subagent_confirm_prompt_fn: Optional[Callable[[str, str, str, str], None]] = None
        # How long (seconds) to wait for the operator to respond to a sub-agent prompt
        self._subagent_confirm_timeout: int = 120

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def shutdown(self, graceful_timeout: float = 10.0) -> None:
        """Shut down the sub-agent thread pool.

        Signals all active sub-agents to cancel, waits up to graceful_timeout
        seconds for them to finish, then forces shutdown of any stragglers.
        """
        from sub_agent_registry import get_registry as _get_registry
        registry = _get_registry()
        active = registry.list_active()
        if active:
            logger.info("Shutdown: cancelling %d active sub-agent(s)…", len(active))
            for record in active:
                record.cancel()
            # Wait briefly for cancelled agents to wind down before forcing pool shutdown
            import time as _time
            deadline = _time.monotonic() + graceful_timeout
            while _time.monotonic() < deadline:
                if not any(r.status == "running" for r in registry.list_active()):
                    break
                _time.sleep(0.25)
        self._sub_agent_pool.shutdown(wait=False, cancel_futures=True)
        logger.debug("Sub-agent pool shut down.")

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_TOOLS

    def all_tools(self) -> list[BuiltinTool]:
        return list(BUILTIN_TOOLS.values())

    def execute(self, tool_name: str, args: Optional[dict] = None, caller_depth: int = 0, caller_tag: str = "",
                chunk_callback: Optional[Callable[[str], None]] = None, trace_id: str = "") -> dict:
        """
        Execute a built-in tool. Returns standard result dict, or a
        requires_confirmation dict if the operation needs user approval.

        caller_depth is the depth of the AgentController invoking this tool
        (0 = main agent, 1 = sub-agent). Used to enforce the no-nested-spawn rule.
        caller_tag is a human-readable label for logging (e.g. "[main]", "[sa-fcf85d]").
        chunk_callback is an optional callable invoked with each output chunk during PTY
        shell execution (only when shell_streaming=True). Ignored for other tools.
        trace_id is the request-scoped trace of the invoking run; it is propagated to
        spawned sub-agents so their logs correlate with the parent request.

        Emits TOOL_START before dispatch and TOOL_END/TOOL_FAILED afterwards
        (plus ERROR on an unexpected exception). These lifecycle events wrap the
        existing dispatch without altering its result-dict contract or exception
        propagation. A deferred result (requires_confirmation) gets no completion
        event — the underlying operation has not run yet.
        """
        args = args or {}
        start = time.perf_counter()
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_START,
            f"tool start: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
        )
        try:
            result = self._dispatch(
                tool_name, args, caller_depth=caller_depth, caller_tag=caller_tag,
                chunk_callback=chunk_callback, trace_id=trace_id,
            )
        except Exception as exc:
            dur_ms = int((time.perf_counter() - start) * 1000)
            self._emit_tool_lifecycle_error(tool_name, exc, dur_ms)
            raise
        dur_ms = int((time.perf_counter() - start) * 1000)
        self._emit_tool_lifecycle_end(tool_name, result, dur_ms)
        return result

    def _emit_tool_lifecycle_end(self, tool_name: str, result: dict, dur_ms: int) -> None:
        """Emit TOOL_END on a successful result or TOOL_FAILED on an error result.

        A ``requires_confirmation`` result is a deferred operation (nothing has
        executed yet), so no completion event is emitted for it.
        """
        if isinstance(result, dict) and result.get("requires_confirmation"):
            return
        if isinstance(result, dict) and result.get("success"):
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_END,
                f"tool end: {tool_name}",
                level=logging.INFO,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=result.get("exit_code", 0),
            )
        else:
            exit_code = result.get("exit_code", -1) if isinstance(result, dict) else -1
            err = (result.get("error", "") if isinstance(result, dict) else "") or ""
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_FAILED,
                f"tool failed: {tool_name}",
                level=logging.ERROR,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=exit_code,
                err=err,
            )

    def _emit_tool_lifecycle_error(self, tool_name: str, exc: BaseException, dur_ms: int) -> None:
        """Emit ERROR + TOOL_FAILED for an unexpected exception during a tool run.

        Shared by the ``execute()`` dispatch wrapper and the confirmed-run path
        in ``confirm()`` so a raised (rather than returned-as-dict) failure still
        closes the TOOL_START span.
        """
        agent_logging.log_event(
            agent_logging.LogEvent.ERROR,
            f"tool error: {tool_name}: {exc}",
            level=logging.ERROR,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=-1,
            err=str(exc),
        )
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

    def _dispatch(self, tool_name: str, args: dict, caller_depth: int = 0, caller_tag: str = "",
                  chunk_callback: Optional[Callable[[str], None]] = None, trace_id: str = "") -> dict:
        """Route a built-in tool call to its handler (no lifecycle logging)."""
        if tool_name == "shell":
            return self._exec_shell(args, caller_depth=caller_depth, caller_tag=caller_tag,
                                    chunk_callback=chunk_callback)
        elif tool_name == "file_read":
            return self._exec_file_read(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_write":
            return self._exec_file_write(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_send":
            return self._exec_file_send(args, caller_tag=caller_tag)
        elif tool_name == "schedule":
            return self._exec_schedule(args)
        elif tool_name == "spawn_agent":
            return self._exec_spawn_agent(args, caller_depth=caller_depth, caller_tag=caller_tag,
                                          trace_id=trace_id)
        elif tool_name == "get_agent_result":
            return self._exec_get_agent_result(args, caller_tag=caller_tag)
        elif tool_name == "memory_write":
            return self._exec_memory_write(args, caller_tag=caller_tag)
        elif tool_name == "file_patch":
            return self._exec_file_patch(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_diff":
            return self._exec_file_diff(args, caller_tag=caller_tag)
        elif tool_name == "memory_graph_search":
            return self._exec_memory_graph_search(args, caller_tag=caller_tag)
        elif tool_name == "memory_graph_store":
            return self._exec_memory_graph_store(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "secret_get":
            return self._exec_secret_get(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "log_query":
            return self._exec_log_query(args, caller_depth=caller_depth, caller_tag=caller_tag)
        else:
            return {"success": False, "output": "", "error": f"Unknown built-in: {tool_name}", "exit_code": -1}

    def confirm(self, token: str, chunk_callback: Optional[Callable[[str], None]] = None,
                *, _emit_lifecycle: bool = True) -> dict:
        """Execute a previously staged dangerous operation after user confirmation.

        chunk_callback is forwarded to the shell backend so live streaming keeps
        working for commands that required confirmation before running.

        execute() deferred the TOOL_END/TOOL_FAILED when it returned
        ``requires_confirmation`` (nothing had run yet), so the confirmed run
        emits the matching completion here — with ``tool`` and ``dur_ms`` — to
        close the TOOL_START span. ``_emit_lifecycle`` is set False by the
        headless bridge, whose own execute() wrapper still owns the span and
        would otherwise double-log the completion.
        """
        entry = self._pending.pop(token, None)
        if entry is None:
            return {"success": False, "output": "", "error": "Confirmation token expired or unknown.", "exit_code": -1}
        tool_name, args = entry
        logger.info("Executing confirmed built-in '%s' (token %s)", tool_name, token[:8])
        start = time.perf_counter()
        try:
            result = self._run(tool_name, args, chunk_callback=chunk_callback)
        except Exception as exc:
            if _emit_lifecycle:
                dur_ms = int((time.perf_counter() - start) * 1000)
                self._emit_tool_lifecycle_error(tool_name, exc, dur_ms)
            raise
        if _emit_lifecycle:
            dur_ms = int((time.perf_counter() - start) * 1000)
            self._emit_tool_lifecycle_end(tool_name, result, dur_ms)
        return result

    def cancel(self, token: str) -> None:
        """Discard a pending confirmation.

        Closes the TOOL_START span left open when execute() deferred the
        operation: emits a cancelled TOOL_END (``cancelled=True``) when the
        token was still pending. An unknown/expired token is a no-op.
        """
        entry = self._pending.pop(token, None)
        if entry is None:
            return
        tool_name = entry[0]
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_END,
            f"tool cancelled: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
            cancelled=True,
        )

    def signal_headless_confirm(self, token: str, approved: bool) -> bool:
        """Signal the outcome of a headless (sub-agent) confirmation prompt.

        Returns True if the token was found and signalled, False if it was
        already expired or unknown (double-press / stale button).
        Called from the Telegram cb_subagent_confirm callback.
        """
        event = self._headless_confirm_events.pop(token, None)
        if event is None:
            return False  # expired or already resolved
        self._headless_confirm_results[token] = approved
        event.set()  # type: ignore[attr-defined]
        return True


    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _requires_confirmation(self, tool_name: str, args: dict, description: str,
                               caller_depth: int = 0, caller_tag: str = "") -> dict:
        # In headless mode (sub-agents, caller_depth >= 1):
        #   shell/dangerous → always deny (too risky to run destructive commands unattended)
        #   file_read/sensitive, file_write, file_patch → require operator confirmation via Telegram
        if caller_depth >= 1:
            if tool_name == "shell":
                command = args.get("command", "")
                logger.warning(
                    "Headless sub-agent: dangerous shell command blocked (requires confirmation): %s",
                    command[:120],
                )
                return {
                    "success": False,
                    "output": "",
                    "error": (
                        f"Command blocked in headless mode (would require confirmation): {command[:200]}\n"
                        "Tip: use a safer alternative, or break the command into non-destructive steps."
                    ),
                    "exit_code": -1,
                }
            # Non-shell (sensitive file_read, file_write, file_patch): bridge to Telegram
            return self._headless_confirm_bridge(tool_name, args, description, caller_tag=caller_tag)

        token = secrets.token_hex(12)
        self._pending[token] = (tool_name, args)
        logger.info("Built-in '%s' requires confirmation, token=%s", tool_name, token[:8])
        return {
            "requires_confirmation": True,
            "token": token,
            "description": description,
        }

    def _headless_confirm_bridge(self, tool_name: str, args: dict, description: str,
                                 caller_tag: str = "") -> dict:
        """Block the sub-agent thread until the operator approves or denies via Telegram.

        If the prompt callback is not wired (bot not ready) or times out, fails closed.
        """
        import threading as _threading

        if self._subagent_confirm_prompt_fn is None:
            logger.warning(
                "Headless sub-agent: Telegram bridge not wired — blocking %s (fail-closed)",
                tool_name,
            )
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Sub-agent sensitive operation blocked: Telegram confirmation bridge "
                    f"not available for '{tool_name}'."
                ),
                "exit_code": -1,
            }

        token = secrets.token_hex(12)
        event = _threading.Event()
        self._headless_confirm_events[token] = event
        self._pending[token] = (tool_name, args)

        logger.info(
            "Headless sub-agent: sending Telegram confirmation prompt for %s (token=%s)",
            tool_name, token[:8],
        )
        try:
            self._subagent_confirm_prompt_fn(token, tool_name, description, caller_tag)
        except Exception as exc:
            logger.error(
                "Headless sub-agent: failed to send Telegram prompt for %s: %s — blocking (fail-closed)",
                tool_name, exc,
            )
            self._headless_confirm_events.pop(token, None)
            self._pending.pop(token, None)
            return {
                "success": False,
                "output": "",
                "error": f"Sub-agent sensitive operation blocked: could not send Telegram prompt ({exc}).",
                "exit_code": -1,
            }

        answered = event.wait(timeout=self._subagent_confirm_timeout)
        if not answered:
            logger.warning(
                "Headless sub-agent: Telegram prompt timed out for %s (token=%s) — blocking",
                tool_name, token[:8],
            )
            self._headless_confirm_events.pop(token, None)
            self._pending.pop(token, None)
            self._headless_confirm_results.pop(token, None)
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Sub-agent sensitive operation timed out waiting for operator confirmation "
                    f"('{tool_name}', {self._subagent_confirm_timeout}s)."
                ),
                "exit_code": -1,
            }

        approved = self._headless_confirm_results.pop(token, False)
        if not approved:
            self._pending.pop(token, None)
            logger.info("Headless sub-agent: operator denied %s (token=%s)", tool_name, token[:8])
            return {
                "success": False,
                "output": "",
                "error": f"Sub-agent sensitive operation denied by operator ('{tool_name}').",
                "exit_code": -1,
            }

        logger.info("Headless sub-agent: operator approved %s (token=%s) — executing", tool_name, token[:8])
        # The enclosing execute() call already owns this tool's lifecycle span,
        # so suppress confirm()'s own completion event to avoid double-logging.
        return self.confirm(token, _emit_lifecycle=False)

    def _run(self, tool_name: str, args: dict, caller_tag: str = "",
             chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Actually execute without any confirmation check."""
        if tool_name == "shell":
            return self._run_shell(args, caller_tag=caller_tag, chunk_callback=chunk_callback)
        elif tool_name == "file_read":
            return self._run_file_read(args, caller_tag=caller_tag)
        elif tool_name == "file_write":
            return self._run_file_write(args, caller_tag=caller_tag)
        elif tool_name == "file_patch":
            return self._run_file_patch(args, caller_tag=caller_tag)
        elif tool_name == "memory_graph_store":
            return self._run_memory_graph_store(args, caller_tag=caller_tag)
        elif tool_name == "secret_get":
            return self._run_secret_get(args, caller_tag=caller_tag)
        return {"success": False, "output": "", "error": "Unknown built-in", "exit_code": -1}

    # ---- shell ----

    def _exec_shell(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                    chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "No command provided.", "exit_code": -1}

        dangerous, reason = _is_dangerous_shell(command)
        if dangerous:
            desc = f"Run shell command: <code>{command}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._requires_confirmation("shell", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

        return self._run_shell(args, caller_tag=caller_tag, chunk_callback=chunk_callback)

    def _run_shell(self, args: dict, caller_tag: str = "",
                   chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Dispatch to the configured shell backend (subprocess or pty)."""
        if self._shell_backend == "pty" and sys.platform != "win32":
            return self._run_shell_pty(args, caller_tag=caller_tag, chunk_callback=chunk_callback)
        return self._run_shell_subprocess(args, caller_tag=caller_tag)

    def _open_shell_log(self, caller_tag: str = "") -> tuple[Optional[_SupportsWriteClose], Optional[str]]:
        """Open a run-specific artifact log file for incremental writing.

        Returns (file_handle, absolute_path) or (None, None) on failure.
        The caller must close the file handle and call _finalize_shell_log to
        either keep or remove the file.

        Shell logs can contain sensitive command output, so the directory is
        created owner-only (0700) and the file owner-only (0600).
        """
        try:
            log_dir = os.path.join(self._data_dir, "shell_logs")
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
            # makedirs honours mode only when creating; tighten an existing dir.
            try:
                os.chmod(log_dir, 0o700)
            except OSError:
                pass
            ts = time.strftime("%Y%m%d-%H%M%S")
            fname = f"shell-{ts}-{secrets.token_hex(4)}.log"
            path = os.path.abspath(os.path.join(log_dir, fname))
            # O_EXCL guarantees we created the file; 0o600 → owner read/write only.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            fh = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
            return fh, path
        except OSError as exc:
            logger.warning("Built-in shell: cannot open artifact log: %s", exc)
            return None, None

    def _finalize_shell_log(self, fh, path: Optional[str], total_chars: int,
                            caller_tag: str = "") -> Optional[str]:
        """Close the artifact file and decide whether to keep or delete it.

        Keeps the file (and returns path) only when total_chars exceeds
        max_output.  Otherwise removes the file and returns None.
        """
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        if path is None:
            return None
        if total_chars > self.max_output:
            logger.info("Built-in shell: full output (%d chars) saved to %s",
                        total_chars, path)
            return path
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    def _run_shell_subprocess(self, args: dict, caller_tag: str = "") -> dict:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self.default_timeout))
        logger.info("Built-in shell (subprocess) executing: %s", command[:120])
        _start = time.monotonic()

        # Open artifact log for incremental writing; kept only if output is large.
        _log_fh, _artifact_path = self._open_shell_log(caller_tag)
        _tail_out = ""
        _tail_err = ""
        _total_out = 0
        _total_err = 0
        _stderr_header_written = False

        # Start the command in its own process group/session so that on timeout
        # we can kill the whole tree (the shell plus any children that inherited
        # the stdout/stderr pipes), not just the top-level shell.  Without this,
        # a leaked grandchild can keep the pipes open and block the reader threads.
        _popen_kwargs: dict = {}
        if sys.platform != "win32":
            _popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows-only
            _popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_popen_kwargs,
            )
        except OSError as exc:
            if _log_fh:
                _log_fh.close()
            if _artifact_path:
                try:
                    os.unlink(_artifact_path)
                except OSError:
                    pass
            err_text = str(exc)
            error_type = "command_not_found" if "No such file or directory" in err_text else "tool_timeout"
            suggestion = (
                "Check the command name or install the missing executable."
                if error_type == "command_not_found"
                else "Try the command again with a longer timeout."
            )
            return {
                "success": False,
                "output": "",
                "error": err_text,
                "exit_code": -1,
                "error_type": error_type,
                "recoverable": error_type == "tool_timeout",
                "suggestion": suggestion,
            }

        def _kill_tree() -> None:
            """Kill the whole process group (POSIX) or the process (Windows)."""
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    return
                except (OSError, ProcessLookupError):
                    pass
            try:
                proc.kill()
            except OSError:
                pass

        def _close_pipe(pipe) -> None:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

        def _disable_artifact_log() -> None:
            """Silently close and unlink the artifact on write failure."""
            nonlocal _log_fh, _artifact_path
            fh, path = _log_fh, _artifact_path
            _log_fh = None
            _artifact_path = None
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        def _append_stdout(text: str) -> None:
            nonlocal _tail_out, _total_out
            if not text:
                return
            _total_out += len(text)
            _tail_out = (_tail_out + text)[-self.max_output:]
            if _log_fh is not None:
                try:
                    _log_fh.write(text)
                except OSError:
                    _disable_artifact_log()

        def _append_stderr(text: str) -> None:
            nonlocal _tail_err, _total_err, _stderr_header_written
            if not text:
                return
            _total_err += len(text)
            _tail_err = (_tail_err + text)[-self.max_output:]
            if _log_fh is not None:
                try:
                    if not _stderr_header_written:
                        _log_fh.write("\n--- stderr ---\n")
                        _stderr_header_written = True
                    _log_fh.write(text)
                except OSError:
                    _disable_artifact_log()

        import select as _select

        # Per-stream incremental UTF-8 decoders keep multibyte characters that
        # straddle os.read() chunk boundaries intact (a plain chunk.decode()
        # would emit U+FFFD replacement chars for the split halves).
        streams: dict[int, tuple[object, Callable[[str], None], codecs.IncrementalDecoder]] = {}
        for _pipe, _append in ((proc.stdout, _append_stdout), (proc.stderr, _append_stderr)):
            if _pipe is None:
                continue
            try:
                os.set_blocking(_pipe.fileno(), False)
                streams[_pipe.fileno()] = (
                    _pipe, _append, codecs.getincrementaldecoder("utf-8")(errors="replace"),
                )
            except (OSError, ValueError):
                _close_pipe(_pipe)

        timed_out = False
        deadline = _start + timeout
        while streams:
            now = time.monotonic()
            if not timed_out and proc.poll() is None and now >= deadline:
                timed_out = True
                _kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break

            # If the shell has exited and no stream is immediately readable,
            # return without waiting for EOF: escaped descendants can keep pipe
            # fds open indefinitely.  This preserves data already available in
            # the pipe while avoiding reader-thread leaks/hangs.
            select_timeout = 0.05 if proc.poll() is not None else max(0.0, min(0.1, deadline - now))
            try:
                ready, _, _ = _select.select(list(streams), [], [], select_timeout)
            except (OSError, ValueError):
                break
            if not ready and proc.poll() is not None:
                break
            for fd in ready:
                pipe, append, decoder = streams.get(fd, (None, None, None))
                if pipe is None or append is None or decoder is None:
                    continue
                while True:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        append(decoder.decode(b"", final=True))
                        streams.pop(fd, None)
                        _close_pipe(pipe)
                        break
                    if not chunk:
                        append(decoder.decode(b"", final=True))
                        streams.pop(fd, None)
                        _close_pipe(pipe)
                        break
                    append(decoder.decode(chunk))

        for _pipe, _append, _decoder in list(streams.values()):
            _append(_decoder.decode(b"", final=True))
            _close_pipe(_pipe)
        streams.clear()

        if proc.poll() is None and not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        elapsed_ms = (time.monotonic() - _start) * 1000.0
        total_combined = _total_out + _total_err
        full_log_path = self._finalize_shell_log(_log_fh, _artifact_path, total_combined, caller_tag)

        # Build truncated outputs from rolling tails using correct total counts.
        output = _truncate_tail(_tail_out, _total_out, self.max_output)
        error = _truncate_tail(_tail_err, _total_err, self.max_output)

        returncode = proc.returncode if not timed_out else -1

        if not timed_out and returncode != 0 and not output.strip() and error:
            # Some commands write only to stderr (e.g. systemctl status);
            # promote stderr → output so the LLM sees the failure reason.
            output = error
            error = ""

        if full_log_path:
            notice = f"\n[full output saved to: {full_log_path} — use file_read to view it]"
            output = output + notice

        logger.info(
            "Built-in shell exit=%s stdout=%d stderr=%d chars in %.0fms",
            returncode, _total_out, _total_err, elapsed_ms,
        )
        if timed_out:
            timeout_error = f"Command timed out after {timeout}s."
            if error.strip():
                timeout_error = f"{timeout_error}\nstderr:\n{error}"
            return {
                "success": False,
                "output": output,
                "error": timeout_error,
                "exit_code": -1,
                "elapsed_ms": round(elapsed_ms),
                "full_log_path": full_log_path,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Try the command again with a longer timeout.",
            }
        # Classify non-zero exit codes from the shell.
        error_type = ""
        recoverable = False
        suggestion = ""
        if returncode != 0:
            error_lower = error.lower()
            output_lower = output.lower()
            combined = f"{error_lower}\n{output_lower}"
            if "permission denied" in combined:
                error_type = "permission_denied"
                recoverable = False
                suggestion = "Check file permissions or use sudo."
            elif "command not found" in combined or "not found" in error_lower and "file" not in error_lower:
                error_type = "command_not_found"
                recoverable = False
                suggestion = "Check the command name or install the missing executable."
            elif "no such file or directory" in combined:
                error_type = "file_not_found"
                recoverable = False
                suggestion = "Check the file path or create the missing file."
        return {
            "success": returncode == 0,
            "output": output,
            "error": error,
            "exit_code": returncode,
            "elapsed_ms": round(elapsed_ms),
            "full_log_path": full_log_path,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }

    def _run_shell_pty(self, args: dict, caller_tag: str = "",
                       chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Run shell command inside a pseudo-terminal.

        Gives the child process a real TTY so isatty()==True, enabling:
        - line-buffered (real-time) output instead of 64 KB block buffering
        - ANSI colour codes from tools like git, pytest, npm
        - progress indicators that detect a terminal
        stdout and stderr are merged by the PTY line discipline (chronological
        order preserved).  Falls back to subprocess on import error.

        When chunk_callback is provided and self._shell_streaming is True, each
        decoded text chunk is forwarded to the callback as it arrives.
        """
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self.default_timeout))
        logger.info("Built-in shell (pty) executing: %s", command[:120])
        streaming = self._shell_streaming and chunk_callback is not None

        try:
            from ptyprocess import PtyProcessUnicode  # type: ignore[import]
        except ImportError:
            logger.warning("ptyprocess not available, falling back to subprocess")
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        import select as _select
        import re as _re
        _ANSI_RE = _re.compile(r'\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07')

        try:
            proc = PtyProcessUnicode.spawn(
                ['/bin/sh', '-c', command],
                dimensions=(self._shell_pty_rows, self._shell_pty_cols),
                echo=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PTY spawn failed (%s), falling back to subprocess", exc)
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        import time as _time
        total_chars = 0
        timed_out = False
        _start = _time.monotonic()
        deadline = _start + timeout

        # Rolling tail: bounded memory regardless of how much the process emits.
        _tail = ""

        # Open artifact log for incremental writing.
        _log_fh, _artifact_path = self._open_shell_log(caller_tag)

        try:
            while proc.isalive():
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    ready, _, _ = _select.select([proc.fd], [], [], min(remaining, 0.25))
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = proc.read(4096)
                except EOFError:
                    break
                # Normalize TTY line endings and strip ANSI for clean LLM output
                chunk = chunk.replace('\r\n', '\n').replace('\r', '\n')
                chunk = _ANSI_RE.sub('', chunk)
                total_chars += len(chunk)
                _tail = (_tail + chunk)[-self.max_output:]
                if _log_fh is not None:
                    _log_fh.write(chunk)
                if streaming and chunk_callback is not None:
                    try:
                        chunk_callback(chunk)
                    except Exception:  # noqa: BLE001
                        pass

            # Drain remaining output after loop
            if not timed_out:
                for _ in range(20):
                    try:
                        r, _, _ = _select.select([proc.fd], [], [], 0.05)
                        if not r:
                            break
                        chunk = proc.read(4096).replace('\r\n', '\n').replace('\r', '\n')
                        chunk = _ANSI_RE.sub('', chunk)
                        total_chars += len(chunk)
                        _tail = (_tail + chunk)[-self.max_output:]
                        if _log_fh is not None:
                            _log_fh.write(chunk)
                        if streaming and chunk_callback is not None:
                            try:
                                chunk_callback(chunk)
                            except Exception:  # noqa: BLE001
                                pass
                    except (EOFError, OSError, ValueError):
                        break
        finally:
            if timed_out:
                # On POSIX, signal the PTY child's process group so background
                # children that ignore SIGHUP also get terminated.
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                proc.terminate(force=True)
            proc.close(force=False)

        elapsed_ms = (_time.monotonic() - _start) * 1000.0
        full_log_path = self._finalize_shell_log(_log_fh, _artifact_path, total_chars, caller_tag)

        output = _truncate_tail(_tail, total_chars, self.max_output)
        if full_log_path:
            output = output + f"\n[full output saved to: {full_log_path} — use file_read to view it]"

        exit_code = proc.exitstatus if not timed_out else -1
        # exitstatus is None if signalled; treat as failure
        if exit_code is None:
            exit_code = -1
        logger.info(
            "Built-in shell (pty) exit=%s combined=%d chars in %.0fms",
            exit_code, total_chars, elapsed_ms,
        )
        if timed_out:
            return {
                "success": False,
                "output": output,
                "error": f"Command timed out after {timeout}s.",
                "exit_code": -1,
                "elapsed_ms": round(elapsed_ms),
                "full_log_path": full_log_path,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Try the command again with a longer timeout.",
            }
        error_type = ""
        recoverable = False
        suggestion = ""
        if exit_code != 0:
            output_lower = output.lower()
            if "permission denied" in output_lower:
                error_type = "permission_denied"
                suggestion = "Check file permissions or use sudo."
            elif "command not found" in output_lower:
                error_type = "command_not_found"
                suggestion = "Check the command name or install the missing executable."
            elif "no such file or directory" in output_lower:
                error_type = "file_not_found"
                suggestion = "Check the file path or create the missing file."
        return {
            "success": exit_code == 0,
            "output": output,
            "error": "" if exit_code == 0 else output,
            "exit_code": exit_code,
            "elapsed_ms": round(elapsed_ms),
            "full_log_path": full_log_path,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }

    # ---- file_read ----

    def _exec_file_read(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        sensitive, reason = _is_sensitive_path(path)
        if sensitive:
            desc = f"Read file: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._requires_confirmation("file_read", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

        return self._run_file_read(args, caller_tag=caller_tag)

    def _run_file_read(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        max_bytes = int(args.get("max_bytes", 50_000))
        offset = int(args.get("offset", 0))
        logger.info("Built-in file_read: %s (offset=%d, max=%d)", path, offset, max_bytes)
        try:
            if not os.path.exists(path):
                return {
                    "success": False,
                    "output": "",
                    "error": f"File not found: {path}",
                    "exit_code": 1,
                    "error_type": "file_not_found",
                    "recoverable": False,
                    "suggestion": "Check the file path or create the missing file.",
                }
            size = os.path.getsize(path)
            # Negative offset = from end of file (tail semantics)
            if offset < 0:
                offset = max(0, size + offset)
            with open(path, "r", errors="replace") as f:
                if offset:
                    f.seek(offset)
                content = f.read(max_bytes)
            truncated = size > offset + max_bytes
            note = f"\n[Showing {len(content)} of {size} bytes from offset {offset}]" if truncated else ""
            return {
                "success": True,
                "output": content + note,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except PermissionError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Permission denied: {exc}",
                "exit_code": 1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Check file permissions or use sudo.",
            }
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "exit_code": 1,
                "error_type": "file_not_found" if "No such file" in str(exc) else "",
                "recoverable": False,
                "suggestion": "Check the file path or create the missing file." if "No such file" in str(exc) else "",
            }

    # ---- file_diff ----

    def _exec_file_diff(self, args: dict, caller_tag: str = "") -> dict:
        path_a = str(args.get("path_a", "")).strip()
        path_b = str(args.get("path_b", "")).strip()
        if not path_a or not path_b:
            return {
                "success": False, "output": "",
                "error": "file_diff: both 'path_a' and 'path_b' are required.",
                "exit_code": -1,
            }
        try:
            context_lines = int(args.get("context_lines", 3))
        except (TypeError, ValueError):
            return {"success": False, "output": "", "error": "file_diff: 'context_lines' must be an integer.", "exit_code": -1}
        if context_lines < 0:
            context_lines = 0
        try:
            max_bytes = int(args.get("max_bytes", 200_000))
        except (TypeError, ValueError):
            return {"success": False, "output": "", "error": "file_diff: 'max_bytes' must be an integer.", "exit_code": -1}

        logger.info("Built-in file_diff: %s <-> %s (context=%d)", path_a, path_b, context_lines)

        try:
            for p in (path_a, path_b):
                if not os.path.exists(p):
                    return {"success": False, "output": "", "error": f"File not found: {p}", "exit_code": 1}
            with open(path_a, "r", errors="replace") as f:
                a_text = f.read(max_bytes)
            with open(path_b, "r", errors="replace") as f:
                b_text = f.read(max_bytes)
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=path_a, tofile=path_b,
            n=context_lines,
        ))
        if not diff:
            return {"success": True, "output": "Files are identical.", "error": "", "exit_code": 0}
        return {"success": True, "output": "".join(diff), "error": "", "exit_code": 0}

    # ---- file_write ----

    def _exec_file_write(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "w"))
        if mode not in ("w", "a"):
            mode = "w"
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        action = "append to" if mode == "a" else "overwrite"
        desc = f"{action.capitalize()} file: <code>{path}</code> ({len(content)} chars)"
        return self._requires_confirmation("file_write", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_write(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "w"))
        if mode not in ("w", "a"):
            mode = "w"
        logger.info("Built-in file_write: %s (mode=%s, len=%d)", path, mode, len(content))
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, mode) as f:
                f.write(content)
            return {
                "success": True,
                "output": f"Written {len(content)} chars to {path}.",
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except PermissionError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Permission denied: {exc}",
                "exit_code": 1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Check file permissions or use sudo.",
            }
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "exit_code": 1,
                "error_type": "file_not_found" if "No such file" in str(exc) else "",
                "recoverable": False,
                "suggestion": "Check the file path or create the missing file." if "No such file" in str(exc) else "",
            }

    # ---- file_patch ----

    def _exec_file_patch(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        old_str = str(args.get("old_str", ""))
        new_str = str(args.get("new_str", ""))
        try:
            occurrence = int(args.get("occurrence", 1))
        except (ValueError, TypeError):
            return {"success": False, "output": "", "error": "file_patch: 'occurrence' must be an integer.", "exit_code": -1}
        if occurrence < 0:
            return {"success": False, "output": "", "error": "file_patch: 'occurrence' must be >= 0 (0 = replace all).", "exit_code": -1}

        if not path:
            return {"success": False, "output": "", "error": "file_patch: 'path' is required.", "exit_code": -1}
        if not old_str:
            return {"success": False, "output": "", "error": "file_patch: 'old_str' is required.", "exit_code": -1}
        if not os.path.exists(path):
            return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}

        # Validate the match before staging for confirmation
        try:
            with open(path, "r", errors="replace") as fh:
                content = fh.read()
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

        count = content.count(old_str)
        if count == 0:
            return {
                "success": False, "output": "",
                "error": (
                    f"file_patch: 'old_str' not found in {path}. "
                    "Make sure the text matches exactly (including whitespace and indentation). "
                    "Use file_read to inspect the file if needed."
                ),
                "exit_code": 1,
            }
        if occurrence == 1 and count > 1:
            return {
                "success": False, "output": "",
                "error": (
                    f"file_patch: 'old_str' matches {count} occurrences in {path} but occurrence=1 (ambiguous). "
                    "Include more surrounding context in 'old_str' to make it unique, "
                    "or set occurrence=0 to replace all."
                ),
                "exit_code": 1,
            }

        # Build a human-readable diff summary for the confirmation prompt
        old_lines = old_str.splitlines()
        new_lines = new_str.splitlines()
        removed = "\n".join(f"  - {ln}" for ln in old_lines[:8])
        added = "\n".join(f"  + {ln}" for ln in new_lines[:8])
        if len(old_lines) > 8:
            removed += f"\n  - … ({len(old_lines) - 8} more lines)"
        if len(new_lines) > 8:
            added += f"\n  + … ({len(new_lines) - 8} more lines)"
        replace_note = f" (replacing all {count} occurrences)" if occurrence == 0 else ""
        desc = (
            f"Patch file: <code>{path}</code>{replace_note}\n"
            f"{removed}\n{added}"
        )

        sensitive, _ = _is_sensitive_path(path)
        if sensitive:
            desc += "\n⚠️ Sensitive file"

        return self._requires_confirmation("file_patch", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_patch(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        old_str = str(args.get("old_str", ""))
        new_str = str(args.get("new_str", ""))
        try:
            occurrence = int(args.get("occurrence", 1))
        except (ValueError, TypeError):
            occurrence = 1
        logger.info("Built-in file_patch: %s (occurrence=%d)", path, occurrence)
        try:
            with open(path, "r", errors="replace") as fh:
                content = fh.read()
            if occurrence == 0:
                count = content.count(old_str)
                if count == 0:
                    return {
                        "success": False, "output": "",
                        "error": f"file_patch: 'old_str' not found in {path} at execution time.",
                        "exit_code": 1,
                    }
                patched = content.replace(old_str, new_str)
                n_replaced = count
            else:
                # Find the Nth occurrence (occurrence >= 1)
                pos = 0
                idx = -1
                for _ in range(occurrence):
                    idx = content.find(old_str, pos)
                    if idx == -1:
                        return {
                            "success": False, "output": "",
                            "error": (
                                f"file_patch: occurrence {occurrence} of 'old_str' not found in {path} "
                                "at execution time (file may have changed after validation)."
                            ),
                            "exit_code": 1,
                        }
                    pos = idx + 1
                patched = content[:idx] + new_str + content[idx + len(old_str):]
                n_replaced = 1
            with open(path, "w") as fh:
                fh.write(patched)
            return {
                "success": True,
                "output": f"Patched {path}: replaced {n_replaced} occurrence(s).",
                "error": "", "exit_code": 0,
            }
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

    # ---- file_send ----

    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram bot API limit)

    def _exec_file_send(self, args: dict, caller_tag: str = "") -> dict:
        path = os.path.expanduser(str(args.get("path", "")).strip())
        caption = str(args.get("caption", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}
        if not os.path.exists(path):
            return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}
        if not os.path.isfile(path):
            return {"success": False, "output": "", "error": f"Not a file: {path}", "exit_code": 1}
        size = os.path.getsize(path)
        if size > self._MAX_FILE_SIZE:
            return {
                "success": False, "output": "",
                "error": f"File too large ({size // 1024 // 1024} MB). Max 50 MB.", "exit_code": 1,
            }
        logger.info("Built-in file_send: %s (%d bytes)", path, size)
        return {
            "success": True,
            "output": f"Sending {os.path.basename(path)} to chat…",
            "error": "",
            "exit_code": 0,
            "send_file": path,
            "caption": caption,
        }

    # ---- schedule ----

    def _exec_schedule(self, args: dict) -> dict:
        if not self.scheduler:
            return {"success": False, "output": "", "error": "Scheduler not available.", "exit_code": -1}
        action = str(args.get("action", "list")).lower()
        tag = str(args.get("tag", "")).strip()

        if action == "list":
            jobs = self.scheduler.list_jobs()
            if not jobs:
                return {"success": True, "output": "No scheduled jobs.", "error": "", "exit_code": 0}
            lines = []
            for j in jobs:
                status = "✅" if j["enabled"] else "⏸"
                stype = j.get("schedule_type", "interval")
                task_label = "Message" if stype == "once" else "Task"
                err = f"\n   ⚠️ last error: {j['last_error'][:120]}" if j.get("last_error") else ""
                lines.append(
                    f"{status} {j['tag']} ({j['schedule']})\n"
                    f"   {task_label}: {j['task']}\n"
                    f"   Last run: {j['last_run'] or 'never'}{err}"
                )
            return {"success": True, "output": "\n".join(lines), "error": "", "exit_code": 0}

        if action == "add":
            if not tag:
                return {"success": False, "output": "", "error": "tag is required for add", "exit_code": -1}
            result = self.scheduler.add_job(
                tag=tag,
                schedule_type=str(args.get("schedule_type", args.get("schedule", "cron"))),
                task=str(args.get("task", "")),
                notify=bool(args.get("notify", True)),
                hours=int(args["hours"]) if args.get("hours") is not None else None,
                minutes=int(args["minutes"]) if args.get("minutes") is not None else None,
                time_str=str(args.get("time", "")) or None,
                run_at=str(args.get("run_at", "")) or None,
                cron=str(args.get("cron", "")) or None,
                model=str(args["model"]) if args.get("model") else None,
                fallback_models=args.get("fallback_models"),
                preserve_context=bool(args.get("preserve_context", False)),
                max_iterations=int(args["max_iterations"]) if args.get("max_iterations") is not None else None,
            )
            if result["success"]:
                return {"success": True, "output": f"Job '{tag}' added.", "error": "", "exit_code": 0}
            return {"success": False, "output": "", "error": result["error"], "exit_code": -1}

        if action == "remove":
            ok = self.scheduler.remove_job(tag)
            if ok:
                return {"success": True, "output": f"Job '{tag}' removed.", "error": "", "exit_code": 0}
            return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

        if action == "pause":
            ok = self.scheduler.pause_job(tag)
            if ok:
                return {"success": True, "output": f"Job '{tag}' paused.", "error": "", "exit_code": 0}
            return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

        if action == "resume":
            ok = self.scheduler.resume_job(tag)
            if ok:
                return {"success": True, "output": f"Job '{tag}' resumed.", "error": "", "exit_code": 0}
            return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

        if action == "run_now":
            result = self.scheduler.run_now(tag)
            if result["success"]:
                return {"success": True, "output": f"Job '{tag}' triggered.", "error": "", "exit_code": 0}
            return {"success": False, "output": "", "error": result["error"], "exit_code": -1}

        return {"success": False, "output": "", "error": f"Unknown action '{action}'. Use: list, add, remove, pause, resume, run_now", "exit_code": -1}

    # ------------------------------------------------------------------
    # spawn_agent
    # ------------------------------------------------------------------

    def _exec_spawn_agent(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                          trace_id: str = "") -> dict:
        """
        Spawn an isolated sub-agent in a background thread.

        The sub-agent runs to completion then delivers its result via
        notify_fn (Telegram) and writes to long-term memory.
        Returns immediately with {status: "spawned", agent_id: "sa-..."}.

        caller_depth is the depth of the AgentController that invoked this tool.
        Sub-agents (depth ≥ 1) are not allowed to spawn further sub-agents.
        """
        from sub_agent_registry import get_registry as get_agent_registry

        task = args.get("task", "").strip()
        # Accept common LLM aliases for the 'task' parameter
        if not task:
            for _alias in ("prompt", "goal", "description"):
                _v = args.get(_alias, "").strip()
                if _v:
                    logger.warning(
                        "spawn_agent: received '%s' instead of 'task' — treating as task (fix your prompt)", _alias
                    )
                    task = _v
                    break
        if not task:
            return {
                "success": False,
                "output": "",
                "error": "spawn_agent: 'task' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a clear task string describing what the sub-agent should do.",
            }

        # Depth guard — prevent recursive sub-agent spawning (hard error, not a silent no-op)
        if caller_depth >= 1:
            return {
                "success": False,
                "output": "",
                "error": "spawn_agent cannot be called from within a sub-agent (max nesting depth: 1).",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Do not spawn sub-agents from within a sub-agent; perform the work directly.",
            }

        if self._sub_agent_factory is None:
            return {
                "success": False,
                "output": "",
                "error": "spawn_agent: sub_agent_factory not configured.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "The agent runtime is missing sub-agent support; this cannot be recovered in-flight.",
            }

        # Concurrency cap — only count on-demand (managed) agents, not scheduler jobs
        current_managed = get_agent_registry().count_managed()
        if current_managed >= self._max_subagents:
            return {
                "success": False,
                "output": "",
                "error": (
                    f"spawn_agent: max_subagents cap reached ({current_managed}/{self._max_subagents}). "
                    "Wait for a managed sub-agent to finish or cancel one with /agents cancel managed."
                ),
                "exit_code": -1,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Wait for an existing sub-agent to finish, then retry.",
            }

        # response_format — how the sub-agent should return its result
        response_format = args.get("response_format", "text").lower()
        if response_format not in ("text", "json", "file"):
            response_format = "text"
        if response_format == "json":
            task = task + "\n\nReturn your entire answer as a single valid JSON object. Do not include any prose or markdown fences."
        elif response_format == "file":
            task = task + "\n\nWrite your output to a file and return only the absolute file path as your answer."

        model = args.get("model") or None
        context_key = args.get("context_key") or None
        if context_key:
            try:
                context_key = _validate_context_key(str(context_key))
            except ValueError as exc:
                return {
                    "success": False,
                    "output": "",
                    "error": f"spawn_agent: invalid context_key: {exc}",
                    "exit_code": -1,
                    "error_type": "permission_denied",
                    "recoverable": False,
                    "suggestion": "Use a context_key with only letters, digits, underscore, dash, or dot.",
                }

        # context_payload — parent context shared with sub-agent
        context_payload = args.get("context_payload")
        if isinstance(context_payload, str):
            try:
                context_payload = json.loads(context_payload)
            except Exception:  # noqa: BLE001
                context_payload = {"parent_note": context_payload}
        if context_payload is None:
            # Implicit context: build an automatic summary from available sources.
            from prompt_loader import build_spawn_context_summary
            context_payload = build_spawn_context_summary(
                user_goal=task,
                working=self._working,
                memory=self._memory,
                results=self._results,
                graph_memory=self._graph_memory,
            )
        if not isinstance(context_payload, dict):
            context_payload = {"parent_note": str(context_payload)}

        fallback_models = args.get("fallback_models")  # None = inherit; [] = disable
        job_tag = args.get("_job_tag") or None       # set by scheduler; used for finish callback
        label = job_tag or context_key or "on-demand"
        # Finish callback passed directly from scheduler to avoid shared-attribute race
        # when multiple jobs fire concurrently.
        _finish_cb = args.get("_finish_cb") or getattr(self, '_scheduler_finish_cb', None)
        _finish_tag = job_tag or label
        # Execution log callback — only provided by scheduler for scheduled jobs.
        _result_log_cb = args.get("_result_log_cb") if job_tag else None
        # Scheduled jobs set expandable=False so results are shown as plain text,
        # not collapsed inside an expandable blockquote.
        _expandable = args.get("expandable", True)
        # _notify=False suppresses Telegram output for silent scheduled jobs.
        _notify_result = args.get("_notify", True)

        # Build the sub-agent via factory
        max_iterations = args.get("max_iterations")  # None = use factory default (scheduled_max_iter)
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
                if max_iterations <= 0:
                    max_iterations = None  # treat 0/negative as "use default"
            except (ValueError, TypeError):
                max_iterations = None

        # Optional per-call LLM parameter overrides
        _raw_max_tokens = args.get("max_tokens")
        _raw_temperature = args.get("temperature")
        _raw_top_p = args.get("top_p")
        try:
            max_tokens_override = int(_raw_max_tokens) if _raw_max_tokens is not None else None
        except (ValueError, TypeError):
            max_tokens_override = None
        try:
            temperature_override = float(_raw_temperature) if _raw_temperature is not None else None
        except (ValueError, TypeError):
            temperature_override = None
        try:
            top_p_override = float(_raw_top_p) if _raw_top_p is not None else None
        except (ValueError, TypeError):
            top_p_override = None

        try:
            runner = self._sub_agent_factory(
                model=model,
                context_key=context_key,
                label=label,
                notify_fn=None,   # factory sets this from main notify_fn
                fallback_models=fallback_models,
                max_iterations=max_iterations,
                max_tokens=max_tokens_override,
                temperature=temperature_override,
                top_p=top_p_override,
                trace_id=trace_id or None,
                context_payload=context_payload,
            )
        except ValueError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"spawn_agent: {exc}",
                "exit_code": -1,
                "error_type": "wrong_model_for_task",
                "recoverable": False,
                "suggestion": "Choose a model that exists in the configured model list.",
            }

        from sub_agent_registry import SubAgentRecord
        import time

        record = SubAgentRecord(
            agent_id=runner.agent_id,
            label=label,
            model=runner._model_id,
            task_preview=task[:80],
            started_at=time.time(),
            source="on-demand",
            max_iterations=runner._agent.max_iterations,
            result_type=response_format,
        )
        # Share cancel_event and LLM client with the registry record so that
        # /agents cancel can immediately interrupt any in-progress HTTP request.
        record._cancel_event = runner._cancel_event
        record._llm_client = runner._llm

        # Wire iteration tracking: update registry on each step
        _agent_id = runner.agent_id
        runner._agent._on_step = lambda s: get_agent_registry().update_iteration(_agent_id, s)

        get_agent_registry().register(record)

        # Log spawn params for observability
        _fb_log = str(fallback_models) if fallback_models is not None else "inherited"
        logger.info(
            "spawn_agent: id=%s label=%s model=%s fallback=%s task=%s",
            runner.agent_id, label, runner._model_id, _fb_log, task[:100],
        )

        def _run_and_notify():
            # Convenience: use HTML notify if available (results in expandable quote blocks)
            _notify_html = self._notify_html_fn
            _context_save_attempted = False

            def _send_result_html(header_html: str, body: str) -> None:
                """Send header + body, optionally wrapped in an expandable blockquote."""
                escaped = _html_mod.escape(body)
                if _expandable:
                    msg = f"{header_html}\n<blockquote expandable>{escaped}</blockquote>"
                else:
                    msg = f"{header_html}\n\n{escaped}"
                if _notify_html:
                    _notify_html(msg)
                else:
                    runner.notify_fn(msg)

            def _save_context_before_completion() -> None:
                """Persist context before exposing completion to get_agent_result callers."""
                nonlocal _context_save_attempted
                if not context_key or _context_save_attempted:
                    return
                _context_save_attempted = True
                try:
                    _save_context(context_key, runner._short_term, self._data_dir)
                except Exception as save_exc:
                    logger.warning(
                        "spawn_agent: [%s] context save failed for %s: %s",
                        label, context_key, save_exc,
                    )

            try:
                result = runner.run(task)
                # P2 consolidation: sub-agent results are NOT auto-persisted into
                # semantic memory. JSON LongTermMemory is legacy/backfill-only and
                # auto-writing arbitrary sub-agent output risks prompt poisoning.
                # If a result should be remembered, the operator/main agent must
                # explicitly (and with confirmation) call memory_graph_store.
                if result == "[Cancelled]":
                    record.status = "cancelled"
                    record.result = "[Cancelled]"
                    _save_context_before_completion()
                    record._result_event.set()
                    elapsed = int(time.time() - record.started_at)
                    logger.info("spawn_agent: [%s] cancelled | id=%s", label, runner.agent_id)
                    # Record cancellation in job history log for scheduled jobs
                    if _result_log_cb:
                        try:
                            _result_log_cb(
                                tag=job_tag,
                                task=task,
                                result="[Cancelled]",
                                success=False,
                                elapsed_s=elapsed,
                                model=runner._model_id,
                            )
                        except Exception as log_exc:
                            logger.warning("spawn_agent: [%s] result_log_cb failed (cancelled): %s", label, log_exc)
                    # Suppress notification for agents cancelled due to get_agent_result timeout —
                    # the caller already received a timeout response and moved on.
                    if _notify_result and not record._timeout_cancelled:
                        try:
                            runner.notify_fn(
                                f"🛑 Sub-agent {runner.agent_id} cancelled\n"
                                f"Job: **{label}**\n"
                                f"Completed {record.iteration}/{record.max_iterations} iterations before stop."
                            )
                        except Exception as notify_exc:
                            logger.warning("spawn_agent: [%s] notify failed (cancelled): %s", label, notify_exc)
                else:
                    record.status = "done"
                    record.result = result
                    _save_context_before_completion()
                    record._result_event.set()
                    elapsed = int(time.time() - record.started_at)
                    logger.info(
                        "spawn_agent: [%s] done | id=%s model=%s elapsed=%ds",
                        label, runner.agent_id, runner._model_id, elapsed,
                    )
                    # Record execution result in job history log for scheduled jobs
                    if _result_log_cb:
                        try:
                            _result_log_cb(
                                tag=job_tag,
                                task=task,
                                result=result,
                                success=True,
                                elapsed_s=elapsed,
                                model=runner._model_id,
                            )
                        except Exception as log_exc:
                            logger.warning("spawn_agent: [%s] result_log_cb failed: %s", label, log_exc)
                    if _notify_result:
                        header_html = (
                            f"✅ <b>Sub-agent</b> <code>{_html_mod.escape(runner.agent_id)}</code>"
                            f" finished ({elapsed}s)\n"
                            f"<b>Job:</b> {_html_mod.escape(label)}"
                            f" | <b>Model:</b> <code>{_html_mod.escape(runner._model_id)}</code>\n"
                            f"<b>Task:</b> {_html_mod.escape(task[:120])}"
                        )
                        try:
                            _send_result_html(header_html, result)
                        except Exception as notify_exc:
                            logger.warning("spawn_agent: [%s] notify failed (success): %s", label, notify_exc)
            except Exception as exc:
                record.status = "failed"
                record.result = str(exc)
                _save_context_before_completion()
                record._result_event.set()
                elapsed = int(time.time() - record.started_at)
                logger.error(
                    "spawn_agent: [%s] failed | id=%s model=%s elapsed=%ds | %s",
                    label, runner.agent_id, runner._model_id, elapsed, exc, exc_info=True,
                )
                # Record failure in job history log for scheduled jobs
                if _result_log_cb:
                    try:
                        _result_log_cb(
                            tag=job_tag,
                            task=task,
                            result=f"Error: {exc}",
                            success=False,
                            elapsed_s=elapsed,
                            model=runner._model_id,
                        )
                    except Exception as log_exc:
                        logger.warning("spawn_agent: [%s] result_log_cb failed (error): %s", label, log_exc)
                if _notify_result:
                    header_html = (
                        f"❌ <b>Sub-agent</b> <code>{_html_mod.escape(runner.agent_id)}</code>"
                        f" failed ({elapsed}s)\n"
                        f"<b>Job:</b> {_html_mod.escape(label)}"
                        f" | <b>Model:</b> <code>{_html_mod.escape(runner._model_id)}</code>\n"
                        f"<b>Task:</b> {_html_mod.escape(task[:120])}"
                    )
                    try:
                        _send_result_html(header_html, f"Error: {exc}")
                    except Exception as notify_exc:
                        logger.warning("spawn_agent: [%s] notify failed (error): %s", label, notify_exc)
            finally:
                # Persist conversation context (if requested) regardless of
                # success/cancellation/failure so a crash mid-task does not lose
                # the sub-agent's short-term memory.
                _save_context_before_completion()
                get_agent_registry().unregister(runner.agent_id)
                runner.close()
                if _finish_cb:
                    _finish_cb(_finish_tag)

        self._sub_agent_pool.submit(_run_and_notify)

        return {
            "success": True,
            "output": (
                f"Sub-agent spawned (id: {runner.agent_id}, model: {runner._model_id}, "
                f"response_format: {response_format}). "
                f"Call get_agent_result(\"{runner.agent_id}\") to retrieve the result when needed."
            ),
            "error": "",
            "exit_code": 0,
            "agent_id": runner.agent_id,
            "response_format": response_format,
        }


    def _exec_get_agent_result(self, args: dict, caller_tag: str = "") -> dict:
        """
        Wait for a sub-agent to finish and return its result.

        Blocks until the agent's _result_event is set or timeout expires.
        """
        from sub_agent_registry import get_registry as get_agent_registry

        agent_id = args.get("agent_id", "").strip()
        if not agent_id:
            return {
                "success": False,
                "output": "",
                "error": "get_agent_result: 'agent_id' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the agent_id returned by spawn_agent.",
            }

        timeout = args.get("timeout", self._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._subagent_result_timeout

        record = get_agent_registry().get(agent_id)
        if record is None:
            return {
                "success": False,
                "output": "",
                "error": f"get_agent_result: no active sub-agent with id '{agent_id}'.",
                "exit_code": -1,
                "status": "not_found",
                "error_type": "file_not_found",
                "recoverable": False,
                "suggestion": "The agent may have already finished; check /agents or recent notifications.",
            }

        # If already finished (event already set), return immediately
        finished = record._result_event.wait(timeout=timeout)
        if not finished:
            # Auto-cancel the sub-agent unless caller explicitly opted out.
            # This prevents orphaned sub-agents from wasting tokens and sending
            # irrelevant Telegram notifications after the caller has moved on.
            cancel_on_timeout = args.get("cancel_on_timeout", True)
            if cancel_on_timeout and not record._cancel_event.is_set():
                record._timeout_cancelled = True
                record.cancel()
                logger.info(
                    "get_agent_result: timed out after %ds — auto-cancelled agent '%s'",
                    timeout, agent_id,
                )
            return {
                "success": False,
                "output": f"get_agent_result: timed out after {timeout}s waiting for agent '{agent_id}'.",
                "error": "",
                "exit_code": 0,
                "status": "timeout",
                "agent_id": agent_id,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Wait for the sub-agent to finish and call get_agent_result again.",
            }

        error_type = ""
        recoverable = False
        suggestion = ""
        if record.status == "failed":
            error_type = "wrong_model_for_task"
            recoverable = False
            suggestion = "Consider using a different model or breaking the task into smaller steps."
        return {
            "success": record.status == "done",
            "output": record.result or "",
            "error": record.result if record.status == "failed" else "",
            "exit_code": 0 if record.status == "done" else -1,
            "status": record.status,
            "result_type": record.result_type,
            "result": record.result,
            "agent_id": agent_id,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }


    def _exec_memory_write(self, args: dict, caller_tag: str = "") -> dict:
        """Read or update persistent MemoryStore (data/memory.json)."""
        if self._memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_write: MemoryStore is not available in this context.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Memory storage is disabled in this runtime; do not rely on memory_write.",
            }

        action = args.get("action", "").strip().lower()
        key = args.get("key", "").strip()

        import json as _json

        def _ok(out: str) -> dict:
            return {
                "success": True,
                "output": out,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }

        def _err(msg: str, error_type: str = "", suggestion: str = "") -> dict:
            return {
                "success": False,
                "output": "",
                "error": msg,
                "exit_code": -1,
                "error_type": error_type or "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": suggestion,
            }

        if action == "get":
            if not key:
                return _err("memory_write get: 'key' is required.")
            value = self._memory.get(key)
            return _ok(_json.dumps(value))

        if not key:
            return _err("memory_write: 'key' is required.")

        if action == "set":
            value = args.get("value")
            # Guard against LLM pre-serializing the value as a JSON string.
            # e.g. value="{\"count\":7}" → stored as {"count": 7} not a raw string.
            if isinstance(value, str):
                try:
                    parsed = _json.loads(value)
                    # Only replace if it decoded to a non-string type (object, list, number, bool, None)
                    if not isinstance(parsed, str):
                        logger.warning(
                            "memory_write set key=%s: value was a JSON string — auto-parsed to %s",
                            key, type(parsed).__name__,
                        )
                        value = parsed
                except _json.JSONDecodeError:
                    pass  # Keep original string value
            self._memory.set(key, value)
            logger.info("memory_write set: key=%s type=%s", key, type(value).__name__)
            return _ok(f"Memory key '{key}' updated.")

        elif action == "append":
            value = args.get("value")
            current = self._memory.get(key)
            if not isinstance(current, list):
                current = []
            current.append(value)
            self._memory.set(key, current)
            logger.info("memory_write append: key=%s (now %d items)", key, len(current))
            return _ok(f"Appended to '{key}' ({len(current)} items total).")

        elif action == "delete":
            self._memory.delete(key)
            logger.info("memory_write delete: key=%s", key)
            return _ok(f"Memory key '{key}' deleted.")

        else:
            return _err(
                f"memory_write: unknown action '{action}'. Valid: set, append, delete, get.",
                error_type="fundamentally_wrong_approach",
                suggestion="Use one of: set, append, delete, get.",
            )

    # ---- memory_graph_search ----

    def _exec_memory_graph_search(self, args: dict, caller_tag: str = "") -> dict:
        if self._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_search: graph memory is not enabled or not available. "
                         "Set [graph_memory] enabled = true in config.toml and install ladybug.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        query = str(args.get("query", "")).strip()
        if not query:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_search: 'query' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a non-empty query string.",
            }
        try:
            context = self._graph_memory.format_for_prompt(query)
            if not context:
                return {
                    "success": True,
                    "output": "No relevant entities or facts found in graph memory.",
                    "error": "",
                    "exit_code": 0,
                    "error_type": "",
                    "recoverable": False,
                    "suggestion": "",
                }
            logger.info("memory_graph_search: query=%s", query[:60])
            return {
                "success": True,
                "output": context,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "output": "",
                "error": f"memory_graph_search failed: {exc}",
                "exit_code": -1,
                "error_type": "network_error",
                "recoverable": True,
                "suggestion": "Retry the graph memory search; the database may be temporarily unavailable.",
            }

    # ---- memory_graph_store ----

    def _exec_memory_graph_store(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        if self._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_store: graph memory is not enabled or not available. "
                         "Set [graph_memory] enabled = true in config.toml and install ladybug.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        content = str(args.get("content", "")).strip()
        if not content:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_store: 'content' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the fact or relationship you want to store.",
            }
        # Writing to graph memory changes future recalled prompt context, so it is
        # a confirmation-requiring operation. Operator approval admits the memory
        # as "confirmed"; the model/sub-agent cannot self-approve it.
        preview = content if len(content) <= 200 else content[:200] + "…"
        desc = (
            "Store this in graph memory as a *confirmed* fact "
            "(it will influence future recalled context):\n"
            f"`{preview}`"
        )
        return self._requires_confirmation(
            "memory_graph_store", args, desc, caller_depth=caller_depth, caller_tag=caller_tag
        )

    def _run_memory_graph_store(self, args: dict, caller_tag: str = "") -> dict:
        """Execute a confirmed graph-memory store. Only reached after operator approval."""
        from graph_memory import ADMISSION_CONFIRMED, CONFIDENCE_CONFIRMED

        if self._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_store: graph memory is not enabled or not available.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        content = str(args.get("content", "")).strip()
        if not content:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_store: 'content' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the fact or relationship you want to store.",
            }
        user_id = str(args.get("user_id", "agent")).strip() or "agent"
        try:
            # Store the operator-approved note as a confirmed episode.
            ep_id = self._graph_memory.add_episode(
                content,
                user_id=user_id,
                source="manual",
                admission_status=ADMISSION_CONFIRMED,
                confidence=CONFIDENCE_CONFIRMED,
            )
            # Derived relations from background extraction remain "observed"; only
            # the explicitly approved note itself is confirmed.
            if self._graph_memory_writer is not None:
                self._graph_memory_writer.enqueue(content, user_id=user_id, source="manual")
                self._graph_memory_writer.flush()
            logger.info("memory_graph_store: stored confirmed episode %s", ep_id)
            return {
                "success": True,
                "output": f"Stored in graph memory as confirmed (episode {ep_id}). "
                          "Extraction scheduled in background.",
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "output": "",
                "error": f"memory_graph_store failed: {exc}",
                "exit_code": -1,
                "error_type": "network_error",
                "recoverable": True,
                "suggestion": "Retry the graph memory store; the database may be temporarily unavailable.",
            }

    def _exec_secret_get(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Stage a vault lookup for operator confirmation."""
        key = args.get("key", "")
        if not key:
            return {
                "success": False,
                "output": "",
                "error": "secret_get: 'key' is required.",
                "exit_code": -1,
            }
        desc = f"Look up vault key '{key}'"
        if caller_depth == 0:
            return self._requires_confirmation(
                "secret_get", args, desc, caller_depth=caller_depth, caller_tag=caller_tag
            )
        return self._headless_confirm_bridge(
            "secret_get", args, desc, caller_tag=caller_tag
        )

    def _run_secret_get(self, args: dict, caller_tag: str = "") -> dict:
        """Read a confirmed key from the TOML vault file.

        Delegates format parsing to :func:`config_schema.parse_vault_content`
        with ``require_all_strings=False`` so a non-string SIBLING key (e.g. an
        idiomatic ``[jira]`` table) no longer breaks unrelated lookups — only
        the requested key must be a string.  The returned value is always a
        string: if the requested key itself resolves to a non-string type, a
        ``config_error`` result is returned instead of the raw value.
        """
        # Local imports to avoid circular-import risk at module load time.
        from config_schema import parse_vault_content as _parse_vault_content  # noqa: PLC0415
        from exceptions import ConfigError as _ConfigError  # noqa: PLC0415

        key = args.get("key", "")
        logger.info("Built-in secret_get: key=%s", key)

        try:
            with open(self._vault_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Cannot read vault: {exc}",
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": "Check vault file path and TOML validity.",
            }

        try:
            vault = _parse_vault_content(
                content, self._vault_path, require_all_strings=False
            )
        except _ConfigError as exc:
            # Normalise ConfigError messages to begin with "Cannot read vault:"
            # so the tool API surface stays stable.
            msg = str(exc)
            if not msg.startswith("Cannot read vault"):
                msg = f"Cannot read vault: {msg}"
            return {
                "success": False,
                "output": "",
                "error": msg,
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": "Check vault file path and TOML validity.",
            }

        value = vault.get(key)
        if value is None and key not in vault:
            return {
                "success": False,
                "output": "",
                "error": f"Vault key '{key}' not found.",
                "exit_code": -1,
                "error_type": "not_found",
                "recoverable": False,
                "suggestion": "Add the key to the vault file.",
            }

        # Per-key type check: siblings may be non-string (require_all_strings=False),
        # but the value we hand back must be a string secret.
        if not isinstance(value, str):
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Vault key '{key}' is not a string secret "
                    f"(got {type(value).__name__})."
                ),
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": (
                    "Store the secret as a top-level string key "
                    '(e.g. api_key = "sk-...") in the vault file.'
                ),
            }

        # value is guaranteed to be a string by the per-key check above.
        return {
            "success": True,
            "output": value,
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }

    # ---- log_query ----

    def _exec_log_query(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Query the active JSONL log sink and return matching records.

        Read-only introspection over ``self._log_jsonl_path`` (one JSON object
        per line). Only the trailing ``_LOG_QUERY_TAIL_BYTES`` bytes / most
        recent ``_LOG_QUERY_MAX_SCAN_LINES`` lines are scanned, so a mid-loop
        call does bounded work regardless of total log size (``total_matched``
        therefore counts matches within that tail window). Supports
        trace/level/event_type/tool/since/text filters, a useful default view
        (Option C) when neither level, event_type, nor text is supplied, and
        most-recent-N truncation via ``limit``.

        The ``text`` argument (alias: ``query``) performs a case-insensitive
        substring search across the full compact JSON serialisation of each
        record so that any field — msg, event, logger, tool output, etc. — is
        searchable.  When ``text`` is provided without an explicit ``level`` or
        ``event_type``, the Option C high-signal default view is **not** applied,
        allowing routine INFO startup records (e.g. "GraphMemoryStore
        initialised at data/graph_memory (dim=1536)") to be surfaced.

        A missing or unset log path yields a well-formed EMPTY result rather
        than an error. ``caller_depth`` and ``caller_tag`` are accepted for
        dispatch symmetry with peer handlers.
        """
        # limit (most-recent-N kept); fall back to the default on bad input.
        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        if limit <= 0:
            limit = 50

        # Resolve the trace scope: an explicit arg wins, else the current run's
        # trace from contextvars; "*" or "" widens the query to all traces.
        if args.get("trace") is not None:
            trace = str(args.get("trace"))
        else:
            trace = str(structlog.contextvars.get_contextvars().get("trace", "") or "")
        all_traces = trace in ("*", "")

        level_arg = args.get("level") or ""
        event_type_arg = args.get("event_type") or ""
        tool_arg = args.get("tool") or ""
        since_arg = str(args.get("since") or "")
        # text/query: case-insensitive full-record substring search.
        # Accept "query" as an alias for "text"; "text" takes precedence.
        text_arg = str(args.get("text") or args.get("query") or "").strip()
        # Option C default view is suppressed when text/query is given so that
        # INFO-level records (e.g. startup messages) are not silently excluded.
        use_default_view = not level_arg and not event_type_arg and not text_arg
        min_level = _log_level_to_num(level_arg) if level_arg else 0
        text_lower = text_arg.lower() if text_arg else ""

        logger.info(
            "log_query: trace=%s level=%s event_type=%s tool=%s since=%s text=%s limit=%d",
            trace or "<all>", level_arg or "-", event_type_arg or "-",
            tool_arg or "-", since_arg or "-", text_arg or "-", limit,
        )

        path = self._log_jsonl_path
        if not path or not os.path.exists(path):
            return self._log_query_result([], 0, False)

        # Bounded tail read: never scan more than the trailing window even if the
        # active log has grown large within the day (before rotation).
        try:
            lines, window_saturated = _read_tail_lines(
                path, _LOG_QUERY_TAIL_BYTES, _LOG_QUERY_MAX_SCAN_LINES
            )
        except OSError as exc:
            logger.warning("log_query: cannot read log sink %s: %s", path, exc)
            return self._log_query_result([], 0, False)
        scanned_lines = len(lines)

        matched: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue  # skip malformed (non-JSON) lines gracefully
            if not isinstance(rec, dict):
                continue

            if not all_traces and str(rec.get("trace", "")) != trace:
                continue
            if since_arg and str(rec.get("ts", "")) < since_arg:
                continue
            if tool_arg and rec.get("tool") != tool_arg:
                continue
            if level_arg and _log_level_to_num(rec.get("level")) < min_level:
                continue
            if event_type_arg and rec.get("event_type") != event_type_arg:
                continue
            if use_default_view and not _log_query_default_keep(rec):
                continue
            if text_lower and text_lower not in json.dumps(rec, ensure_ascii=False).lower():
                continue

            matched.append(rec)

        total_matched = len(matched)
        truncated = total_matched > limit
        out_records = matched[-limit:] if truncated else matched
        return self._log_query_result(
            out_records, total_matched, truncated,
            window_saturated=window_saturated, scanned_lines=scanned_lines,
        )

    def _log_query_result(self, records: list, total_matched: int, truncated: bool,
                          *, window_saturated: bool = False, scanned_lines: int = 0) -> dict:
        """Render a log_query payload using the peer result-dict convention.

        Records are projected (over-long field values truncated) and only the
        most recent records whose compact serialization fits within
        ``self.max_output`` are kept — mirroring ToolExecutor.max_output so a
        mid-loop call cannot blow the context budget. The metadata keys (count,
        truncated, total_matched) are preserved; ``truncated`` also reflects any
        size cap. The newest record is always kept even if it alone is large.

        ``window_saturated``/``scanned_lines`` disclose the recent-window scope:
        ``total_matched`` counts matches only within the ``scanned_lines`` lines
        of the scanned tail, and when ``window_saturated`` is True older records
        fell outside that window (so it is a recent-window lower bound).
        """
        projected = [_log_query_project(rec) for rec in records]
        # Single pass newest→oldest: keep records until the serialized size would
        # exceed the budget (O(n); avoids re-serializing the whole list).
        kept_rev: list = []
        size = 2  # the enclosing "[]"
        for rec in reversed(projected):
            size += len(json.dumps(rec, ensure_ascii=False)) + 1  # +1 separator
            if kept_rev and size > self.max_output:
                truncated = True
                break
            kept_rev.append(rec)
        kept = list(reversed(kept_rev))
        payload = {
            "records": kept,
            "count": len(kept),
            "truncated": truncated,
            "total_matched": total_matched,
            "window_saturated": window_saturated,
            "scanned_lines": scanned_lines,
        }
        return {
            "success": True,
            "output": json.dumps(payload, ensure_ascii=False),
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }


def _save_context(context_key: str, short_term, data_dir: str) -> None:
    """Persist ShortTermMemory to data/job_contexts/<key>.json atomically.

    Delegates to ``memory_store._atomic_save_json`` (temp file + ``os.replace``)
    so an interrupted write cannot corrupt or truncate an existing context
    file. Unlike the memory-store callers, a context-save failure is logged
    and swallowed — a sub-agent finish path must not be derailed by a context
    persistence error.
    """
    from memory_store import _atomic_save_json

    path = _context_path(context_key, data_dir)
    try:
        _atomic_save_json(path, short_term.to_dict(), attempts=1)
    except OSError:
        logger.warning("Failed to save context for %s", context_key, exc_info=True)


def _load_context(context_key: str, data_dir: str, max_turns: int = 50):
    """Load ShortTermMemory from data/job_contexts/<key>.json. Returns fresh on error."""
    import json as _json
    from memory_store import ShortTermMemory

    path = _context_path(context_key, data_dir)
    if not os.path.exists(path):
        return ShortTermMemory(max_turns=max_turns)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return ShortTermMemory.from_dict(data, max_turns=max_turns)
    except (OSError, _json.JSONDecodeError):
        logger.warning("Context file corrupted for %s — starting fresh", context_key, exc_info=True)
        return ShortTermMemory(max_turns=max_turns)
