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
            "task (str, REQUIRED for add — the natural-language goal or reminder text; "
            "for once/reminder jobs this is the message delivered to the user, e.g. "
            "'Remind the user to check the tennis scores'), "
            "schedule_type (str: daily|interval|once), "
            "time (HH:MM for daily), run_at (HH:MM for once), "
            "hours (int), minutes (int), notify (bool, default true). "
            "Always provide a non-empty task when adding any job."
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

    def __init__(self, default_timeout: int = 30, max_output: int = 4000, scheduler=None):
        self.default_timeout = default_timeout
        self.max_output = max_output
        self.scheduler = scheduler  # Optional[Scheduler] — for the schedule built-in
        # pending: token -> (tool_name, args)
        self._pending: dict[str, tuple[str, dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_TOOLS

    def all_tools(self) -> list[BuiltinTool]:
        return list(BUILTIN_TOOLS.values())

    def execute(self, tool_name: str, args: Optional[dict] = None) -> dict:
        """
        Execute a built-in tool. Returns standard result dict, or a
        requires_confirmation dict if the operation needs user approval.
        """
        args = args or {}
        if tool_name == "shell":
            return self._exec_shell(args)
        elif tool_name == "file_read":
            return self._exec_file_read(args)
        elif tool_name == "file_write":
            return self._exec_file_write(args)
        elif tool_name == "file_send":
            return self._exec_file_send(args)
        elif tool_name == "schedule":
            return self._exec_schedule(args)
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

    def _requires_confirmation(self, tool_name: str, args: dict, description: str) -> dict:
        token = secrets.token_hex(12)
        self._pending[token] = (tool_name, args)
        logger.info("Built-in '%s' requires confirmation, token=%s", tool_name, token[:8])
        return {
            "requires_confirmation": True,
            "token": token,
            "description": description,
        }

    def _run(self, tool_name: str, args: dict) -> dict:
        """Actually execute without any confirmation check."""
        if tool_name == "shell":
            return self._run_shell(args)
        elif tool_name == "file_read":
            return self._run_file_read(args)
        elif tool_name == "file_write":
            return self._run_file_write(args)
        return {"success": False, "output": "", "error": "Unknown built-in", "exit_code": -1}

    # ---- shell ----

    def _exec_shell(self, args: dict) -> dict:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "No command provided.", "exit_code": -1}

        dangerous, reason = _is_dangerous_shell(command)
        if dangerous:
            desc = f"Run shell command: <code>{command}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._requires_confirmation("shell", args, desc)

        return self._run_shell(args)

    def _run_shell(self, args: dict) -> dict:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self.default_timeout))
        logger.info("Built-in shell executing: %s", command[:120])
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

    def _exec_file_read(self, args: dict) -> dict:
        path = str(args.get("path", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        sensitive, reason = _is_sensitive_path(path)
        if sensitive:
            desc = f"Read file: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._requires_confirmation("file_read", args, desc)

        return self._run_file_read(args)

    def _run_file_read(self, args: dict) -> dict:
        path = str(args.get("path", "")).strip()
        max_bytes = int(args.get("max_bytes", 50_000))
        offset = int(args.get("offset", 0))
        logger.info("Built-in file_read: %s (offset=%d, max=%d)", path, offset, max_bytes)
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

    def _exec_file_write(self, args: dict) -> dict:
        path = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "w"))
        if mode not in ("w", "a"):
            mode = "w"
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        action = "append to" if mode == "a" else "overwrite"
        desc = f"{action.capitalize()} file: <code>{path}</code> ({len(content)} chars)"
        return self._requires_confirmation("file_write", args, desc)

    def _run_file_write(self, args: dict) -> dict:
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
            return {"success": True, "output": f"Written {len(content)} chars to {path}.", "error": "", "exit_code": 0}
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except Exception as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

    # ---- file_send ----

    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram bot API limit)

    def _exec_file_send(self, args: dict) -> dict:
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
                schedule_type=str(args.get("schedule_type", args.get("schedule", "interval"))),
                task=str(args.get("task", "")),
                notify=bool(args.get("notify", True)),
                hours=int(args["hours"]) if args.get("hours") is not None else None,
                minutes=int(args["minutes"]) if args.get("minutes") is not None else None,
                time_str=str(args.get("time", "")) or None,
                run_at=str(args.get("run_at", "")) or None,
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
