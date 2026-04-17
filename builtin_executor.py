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
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

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
            "preserve_context (bool, default false — if true, conversation history is kept between runs). "
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
            "  context_key     (str, optional) — key for persisting conversation history between calls.\n"
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
            "timeout (int, optional — seconds to wait, default: configured subagent_result_timeout). "
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
                 memory=None, max_subagents: int = 6, subagent_result_timeout: int = 300):
        self.default_timeout = default_timeout
        self.max_output = max_output
        self.scheduler = scheduler  # Optional[Scheduler] — for the schedule built-in
        self._sub_agent_factory = sub_agent_factory  # Callable[[model, context_key, label, notify_fn], SubAgentRunner]
        self._data_dir = data_dir
        self._memory = memory  # Optional[MemoryStore] — for memory_write built-in
        self._max_subagents = max_subagents
        self._subagent_result_timeout = subagent_result_timeout
        # pending: token -> (tool_name, args)
        self._pending: dict[str, tuple[str, dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_TOOLS

    def all_tools(self) -> list[BuiltinTool]:
        return list(BUILTIN_TOOLS.values())

    def execute(self, tool_name: str, args: Optional[dict] = None, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """
        Execute a built-in tool. Returns standard result dict, or a
        requires_confirmation dict if the operation needs user approval.

        caller_depth is the depth of the AgentController invoking this tool
        (0 = main agent, 1 = sub-agent). Used to enforce the no-nested-spawn rule.
        caller_tag is a human-readable label for logging (e.g. "[main]", "[sa-fcf85d]").
        """
        args = args or {}
        if tool_name == "shell":
            return self._exec_shell(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_read":
            return self._exec_file_read(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_write":
            return self._exec_file_write(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "file_send":
            return self._exec_file_send(args, caller_tag=caller_tag)
        elif tool_name == "schedule":
            return self._exec_schedule(args)
        elif tool_name == "spawn_agent":
            return self._exec_spawn_agent(args, caller_depth=caller_depth, caller_tag=caller_tag)
        elif tool_name == "get_agent_result":
            return self._exec_get_agent_result(args, caller_tag=caller_tag)
        elif tool_name == "memory_write":
            return self._exec_memory_write(args, caller_tag=caller_tag)
        else:
            return {"success": False, "output": "", "error": f"Unknown built-in: {tool_name}", "exit_code": -1}

    def confirm(self, token: str) -> dict:
        """Execute a previously staged dangerous operation after user confirmation."""
        entry = self._pending.pop(token, None)
        if entry is None:
            return {"success": False, "output": "", "error": "Confirmation token expired or unknown.", "exit_code": -1}
        tool_name, args = entry
        logger.info("Executing confirmed built-in '%s' (token %s)", tool_name, token[:8])
        return self._run(tool_name, args)

    def cancel(self, token: str) -> None:
        """Discard a pending confirmation."""
        self._pending.pop(token, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _requires_confirmation(self, tool_name: str, args: dict, description: str,
                               caller_depth: int = 0, caller_tag: str = "") -> dict:
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        # In headless mode (sub-agents), there is no user to confirm — auto-handle:
        #   shell/dangerous → deny (too risky to run destructive commands unattended)
        #   file_read/sensitive, file_write → approve (non-destructive or expected by task)
        if caller_depth >= 1:
            if tool_name == "shell":
                command = args.get("command", "")
                logger.warning(
                    "%sHeadless sub-agent: dangerous shell command blocked (requires confirmation): %s",
                    _pfx, command[:120],
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
            else:
                # file_read sensitive or file_write — auto-approve
                logger.info(
                    "%sHeadless sub-agent: auto-approving %s (no user confirmation available)", _pfx, tool_name
                )
                return self._run(tool_name, args, caller_tag=caller_tag)

        token = secrets.token_hex(12)
        self._pending[token] = (tool_name, args)
        logger.info("%sBuilt-in '%s' requires confirmation, token=%s", _pfx, tool_name, token[:8])
        return {
            "requires_confirmation": True,
            "token": token,
            "description": description,
        }

    def _run(self, tool_name: str, args: dict, caller_tag: str = "") -> dict:
        """Actually execute without any confirmation check."""
        if tool_name == "shell":
            return self._run_shell(args, caller_tag=caller_tag)
        elif tool_name == "file_read":
            return self._run_file_read(args, caller_tag=caller_tag)
        elif tool_name == "file_write":
            return self._run_file_write(args, caller_tag=caller_tag)
        return {"success": False, "output": "", "error": "Unknown built-in", "exit_code": -1}

    # ---- shell ----

    def _exec_shell(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "No command provided.", "exit_code": -1}

        dangerous, reason = _is_dangerous_shell(command)
        if dangerous:
            desc = f"Run shell command: <code>{command}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._requires_confirmation("shell", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

        return self._run_shell(args, caller_tag=caller_tag)

    def _run_shell(self, args: dict, caller_tag: str = "") -> dict:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self.default_timeout))
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        logger.info("%sBuilt-in shell executing: %s", _pfx, command[:120])
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout or "")[:self.max_output]
            error = (proc.stderr or "")[:500]
            if proc.returncode != 0 and not output and error:
                # Some commands write only to stderr on success (e.g. systemctl status)
                output = error
                error = ""
            return {
                "success": proc.returncode == 0,
                "output": output,
                "error": error,
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "", "error": f"Command timed out after {timeout}s.", "exit_code": -1}
        except Exception as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": -1}

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
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        logger.info("%sBuilt-in file_read: %s (offset=%d, max=%d)", _pfx, path, offset, max_bytes)
        try:
            if not os.path.exists(path):
                return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}
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
            return {"success": True, "output": content + note, "error": "", "exit_code": 0}
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except Exception as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

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
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        logger.info("%sBuilt-in file_write: %s (mode=%s, len=%d)", _pfx, path, mode, len(content))
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, mode) as f:
                f.write(content)
            return {"success": True, "output": f"Written {len(content)} chars to {path}.", "error": "", "exit_code": 0}
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except Exception as exc:
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
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        logger.info("%sBuilt-in file_send: %s (%d bytes)", _pfx, path, size)
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
                preserve_context=bool(args.get("preserve_context", False)),
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

    def _exec_spawn_agent(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """
        Spawn an isolated sub-agent in a background thread.

        The sub-agent runs to completion then delivers its result via
        notify_fn (Telegram) and writes to long-term memory.
        Returns immediately with {status: "spawned", agent_id: "sa-..."}.

        caller_depth is the depth of the AgentController that invoked this tool.
        Sub-agents (depth ≥ 1) are not allowed to spawn further sub-agents.
        """
        import threading

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
            return {"success": False, "output": "", "error": "spawn_agent: 'task' is required.", "exit_code": -1}

        # Depth guard — prevent recursive sub-agent spawning (hard error, not a silent no-op)
        if caller_depth >= 1:
            return {
                "success": False, "output": "",
                "error": "spawn_agent cannot be called from within a sub-agent (max nesting depth: 1).",
                "exit_code": -1,
            }

        if self._sub_agent_factory is None:
            return {"success": False, "output": "", "error": "spawn_agent: sub_agent_factory not configured.", "exit_code": -1}

        # Concurrency cap — only count on-demand (managed) agents, not scheduler jobs
        current_managed = get_agent_registry().count_managed()
        if current_managed >= self._max_subagents:
            return {
                "success": False, "output": "",
                "error": (
                    f"spawn_agent: max_subagents cap reached ({current_managed}/{self._max_subagents}). "
                    "Wait for a managed sub-agent to finish or cancel one with /agents cancel managed."
                ),
                "exit_code": -1,
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
        fallback_models = args.get("fallback_models")  # None = inherit; [] = disable
        job_tag = args.get("_job_tag") or None       # set by scheduler; used for finish callback
        label = job_tag or context_key or "on-demand"

        # Build the sub-agent via factory
        try:
            runner = self._sub_agent_factory(
                model=model,
                context_key=context_key,
                label=label,
                notify_fn=None,   # factory sets this from main notify_fn
                fallback_models=fallback_models,
            )
        except ValueError as exc:
            return {"success": False, "output": "", "error": f"spawn_agent: {exc}", "exit_code": -1}

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
        _pfx = f"[{caller_tag}] " if caller_tag else ""
        _fb_log = str(fallback_models) if fallback_models is not None else "inherited"
        logger.info(
            "%sspawn_agent: id=%s label=%s model=%s fallback=%s task=%s",
            _pfx, runner.agent_id, label, runner._model_id, _fb_log, task[:100],
        )

        # Capture scheduler finish callback now (at spawn time) to avoid race
        # conditions when multiple jobs are spawned concurrently — each thread
        # gets its own snapshot of the callback bound to the correct job tag.
        _finish_cb = getattr(self, '_scheduler_finish_cb', None)
        _finish_tag = job_tag or label

        def _run_and_notify():
            try:
                result = runner.run(task)
                # Persist context if requested
                if context_key:
                    _save_context(context_key, runner._short_term, self._data_dir)
                # Write to long-term memory
                if runner._agent.long_term and result and result != "[Cancelled]":
                    try:
                        runner._agent.long_term.add(
                            f"[Sub-agent {label}] {result[:500]}", source="sub_agent"
                        )
                    except Exception:
                        pass
                if result == "[Cancelled]":
                    record.status = "cancelled"
                    record.result = "[Cancelled]"
                    record._result_event.set()
                    logger.info("spawn_agent: [%s] cancelled | id=%s", label, runner.agent_id)
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
                    record._result_event.set()
                    elapsed = int(time.time() - record.started_at)
                    logger.info(
                        "spawn_agent: [%s] done | id=%s model=%s elapsed=%ds",
                        label, runner.agent_id, runner._model_id, elapsed,
                    )
                    header = (
                        f"✅ Sub-agent {runner.agent_id} finished ({elapsed}s)\n"
                        f"Job: **{label}** | Model: {runner._model_id}\n"
                        f"Task: {task[:120]}"
                    )
                    try:
                        runner.notify_fn(header + "\n\n" + result)
                    except Exception as notify_exc:
                        logger.warning("spawn_agent: [%s] notify failed (success): %s", label, notify_exc)
            except Exception as exc:
                record.status = "failed"
                record.result = str(exc)
                record._result_event.set()
                elapsed = int(time.time() - record.started_at)
                logger.error(
                    "spawn_agent: [%s] failed | id=%s model=%s elapsed=%ds | %s",
                    label, runner.agent_id, runner._model_id, elapsed, exc, exc_info=True,
                )
                try:
                    runner.notify_fn(
                        f"❌ Sub-agent {runner.agent_id} failed ({elapsed}s)\n"
                        f"Job: **{label}** | Model: {runner._model_id}\n"
                        f"Task: {task[:120]}\n"
                        f"Error: {exc}"
                    )
                except Exception as notify_exc:
                    logger.warning("spawn_agent: [%s] notify failed (error): %s", label, notify_exc)
            finally:
                get_agent_registry().unregister(runner.agent_id)
                if _finish_cb:
                    _finish_cb(_finish_tag)

        t = threading.Thread(target=_run_and_notify, daemon=True, name=f"sub-agent-{label}")
        t.start()

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
            return {"success": False, "output": "", "error": "get_agent_result: 'agent_id' is required.", "exit_code": -1}

        timeout = args.get("timeout", self._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._subagent_result_timeout

        record = get_agent_registry().get(agent_id)
        if record is None:
            return {
                "success": False, "output": "",
                "error": f"get_agent_result: no active sub-agent with id '{agent_id}'.",
                "exit_code": -1,
                "status": "not_found",
            }

        # If already finished (event already set), return immediately
        finished = record._result_event.wait(timeout=timeout)
        if not finished:
            return {
                "success": False,
                "output": f"get_agent_result: timed out after {timeout}s waiting for agent '{agent_id}'.",
                "error": "",
                "exit_code": 0,
                "status": "timeout",
                "agent_id": agent_id,
            }

        return {
            "success": record.status == "done",
            "output": record.result or "",
            "error": record.result if record.status == "failed" else "",
            "exit_code": 0 if record.status == "done" else -1,
            "status": record.status,
            "result_type": record.result_type,
            "result": record.result,
            "agent_id": agent_id,
        }


    def _exec_memory_write(self, args: dict, caller_tag: str = "") -> dict:
        """Read or update persistent MemoryStore (data/memory.json)."""
        if self._memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_write: MemoryStore is not available in this context.",
                "exit_code": -1,
            }

        action = args.get("action", "").strip().lower()
        key = args.get("key", "").strip()

        if action == "get":
            if not key:
                return {"success": False, "output": "", "error": "memory_write get: 'key' is required.", "exit_code": -1}
            import json as _json
            value = self._memory.get(key)
            return {"success": True, "output": _json.dumps(value), "error": "", "exit_code": 0}

        if not key:
            return {"success": False, "output": "", "error": "memory_write: 'key' is required.", "exit_code": -1}

        if action == "set":
            value = args.get("value")
            # Guard against LLM pre-serializing the value as a JSON string.
            # e.g. value="{\"count\":7}" → stored as {"count": 7} not a raw string.
            if isinstance(value, str):
                try:
                    import json as _json
                    parsed = _json.loads(value)
                    # Only replace if it decoded to a non-string type (object, list, number, bool, None)
                    if not isinstance(parsed, str):
                        logger.warning(
                            "memory_write set key=%s: value was a JSON string — auto-parsed to %s",
                            key, type(parsed).__name__,
                        )
                        value = parsed
                except Exception:
                    pass  # Keep original string value
            self._memory.set(key, value)
            logger.info("memory_write set: key=%s type=%s", key, type(value).__name__)
            return {"success": True, "output": f"Memory key '{key}' updated.", "error": "", "exit_code": 0}

        elif action == "append":
            value = args.get("value")
            current = self._memory.get(key)
            if not isinstance(current, list):
                current = []
            current.append(value)
            self._memory.set(key, current)
            logger.info("memory_write append: key=%s (now %d items)", key, len(current))
            return {"success": True, "output": f"Appended to '{key}' ({len(current)} items total).", "error": "", "exit_code": 0}

        elif action == "delete":
            self._memory.delete(key)
            logger.info("memory_write delete: key=%s", key)
            return {"success": True, "output": f"Memory key '{key}' deleted.", "error": "", "exit_code": 0}

        else:
            return {
                "success": False, "output": "",
                "error": f"memory_write: unknown action '{action}'. Valid: set, append, delete, get.",
                "exit_code": -1,
            }


def _save_context(context_key: str, short_term, data_dir: str) -> None:
    """Persist ShortTermMemory to data/job_contexts/<key>.json."""
    import json as _json
    import os

    ctx_dir = os.path.join(data_dir, "job_contexts")
    os.makedirs(ctx_dir, exist_ok=True)
    path = os.path.join(ctx_dir, f"{context_key}.json")
    try:
        data = short_term.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to save context for %s", context_key, exc_info=True)


def _load_context(context_key: str, data_dir: str, max_turns: int = 50):
    """Load ShortTermMemory from data/job_contexts/<key>.json. Returns fresh on error."""
    import json as _json
    import os
    from memory_store import ShortTermMemory

    path = os.path.join(data_dir, "job_contexts", f"{context_key}.json")
    if not os.path.exists(path):
        return ShortTermMemory(max_turns=max_turns)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return ShortTermMemory.from_dict(data, max_turns=max_turns)
    except Exception:
        logger.warning("Context file corrupted for %s — starting fresh", context_key, exc_info=True)
        return ShortTermMemory(max_turns=max_turns)
