"""Pure presentation helpers for tool calls and results.

Extracted from ``react_loop.py``: tool icons, tool-call/brief/result
formatters, and the compact args repr used by traces. No ReAct-loop state,
no ``ReactContext`` dependency — everything takes tool_name/args/outcome
and returns display strings.

``react_loop`` re-exports the public names for backward compatibility
(tests and ``telegram_interface`` import them from there).
"""

import json
import os
import re
from typing import Callable

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


def _format_path(tool_name: str, args: dict) -> str:
    """Return a brief showing the basename of the target path."""
    return f"{tool_name} {os.path.basename(args.get('path', '?'))}"


def _format_file_diff(tool_name: str, args: dict) -> str:
    """Return a brief with both file basenames for a diff."""
    path_a = args.get("path_a", "?")
    path_b = args.get("path_b", "?")
    return f"{tool_name} {os.path.basename(path_a)} ↔ {os.path.basename(path_b)}"


def _format_file_patch(tool_name: str, args: dict) -> str:
    """Return a brief with line counts for a patch."""
    path = args.get("path", "?")
    old_str = args.get("old_str", "") or ""
    new_str = args.get("new_str", "") or ""
    return (
        f"{tool_name} {os.path.basename(path)} "
        f"+{len(new_str.splitlines())} -{len(old_str.splitlines())}"
    )


def _format_file_write(tool_name: str, args: dict) -> str:
    """Return a brief with content length for a file write."""
    path = args.get("path", "?")
    content = args.get("content", "") or ""
    return f"{tool_name} {os.path.basename(path)} ({len(content)})"


def _format_shell(tool_name: str, args: dict) -> str:
    """Return a brief with the stripped shell command quoted."""
    cmd = args.get("command", "") or ""
    stripped = _strip_shell_wrapper(cmd)
    return f'{tool_name} "{_truncate_brief(stripped)}"'


def _format_spawn_agent(tool_name: str, args: dict) -> str:
    """Return a brief with the sub-agent task truncated."""
    task = args.get("task", "")
    return f'{tool_name} "{_truncate_brief(task, 30)}"'


def _format_schedule(tool_name: str, args: dict) -> str:
    """Return a brief for schedule actions."""
    action = args.get("action", "")
    tag = args.get("tag", "")
    cron = args.get("cron", "")
    if action == "list":
        return f"{tool_name} list"
    if action == "add":
        return f'{tool_name} add "{_truncate_brief(tag, 30)}" {cron}'
    return f'{tool_name} {action} "{_truncate_brief(tag, 30)}"'


def _format_agent_id(tool_name: str, args: dict) -> str:
    """Return a brief with the target agent id."""
    return f"{tool_name} {args.get('agent_id', '')}"


def _format_wait_for_any_agent(tool_name: str, args: dict) -> str:
    """Return a brief summarising the awaited agent ids."""
    agent_ids = args.get("agent_ids", []) or []
    if len(agent_ids) > 2:
        return f"{tool_name} [{len(agent_ids)} agents]"
    return f"{tool_name} {', '.join(str(a) for a in agent_ids)}"


def _format_memory_write(tool_name: str, args: dict) -> str:
    """Return a brief with memory action and key."""
    action = args.get("action", "")
    key = args.get("key", "")
    return f'{tool_name} {action} "{_truncate_brief(key, 30)}"'


def _format_quoted_arg(tool_name: str, args: dict, arg_name: str) -> str:
    """Return a brief with a single quoted argument truncated to 30 chars."""
    value = args.get(arg_name, "")
    return f'{tool_name} "{_truncate_brief(value, 30)}"'


def _format_key(tool_name: str, args: dict) -> str:
    """Return a brief showing a key-only argument; value is never exposed."""
    return f"{tool_name} {args.get('key', '')}"


def _format_generic(tool_name: str, args: dict) -> str:
    """Fallback brief: list argument keys only, never values."""
    if args:
        return f"{tool_name} ({', '.join(str(k) for k in args)})"
    return tool_name


# Dispatch table from built-in tool name to its brief formatter.
_BRIEF_FORMATTERS: dict[str, Callable[[str, dict], str]] = {
    "file_read": _format_path,
    "file_send": _format_path,
    "vision_query": _format_path,
    "file_diff": _format_file_diff,
    "file_patch": _format_file_patch,
    "file_write": _format_file_write,
    "shell": _format_shell,
    "spawn_agent": _format_spawn_agent,
    "schedule": _format_schedule,
    "get_agent_result": _format_agent_id,
    "cancel_agent": _format_agent_id,
    "wait_for_any_agent": _format_wait_for_any_agent,
    "memory_write": _format_memory_write,
    "memory_graph_search": lambda tool_name, args: _format_quoted_arg(tool_name, args, "query"),
    "memory_graph_store": lambda tool_name, args: _format_quoted_arg(tool_name, args, "content"),
    "log_query": lambda tool_name, args: _format_quoted_arg(tool_name, args, "text"),
    "secret_get": _format_key,
    "shell_env_set": _format_key,
    "shell_env_unset": _format_key,
    "shell_env_get": _format_key,
    "shell_env_list": lambda tool_name, args: f"{tool_name} list env vars",
}


def fmt_tool_brief(tool_name: str, args: dict, is_mcp: bool = False, server_name: str = "") -> str:
    """Format a short one-line brief of what a tool is doing, for the compact panel.

    Extracts the semantically meaningful argument per tool family. Secrets are
    protected by showing keys only, never values. Truncated to ~35 chars.
    Appends ``[MCP:{server_name}]`` when ``is_mcp`` is True.
    """
    formatter = _BRIEF_FORMATTERS.get(tool_name, _format_generic)
    core = formatter(tool_name, args)

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
