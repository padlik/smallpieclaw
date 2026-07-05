"""
tool_executor.py
----------------
Safely executes registered tools via subprocess.
Enforces timeout, output size limits, and allowlist checks.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Optional

import agent_logging
from tool_registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)
slog = agent_logging.get_logger(__name__)


class ToolExecutor:
    """
    Runs tools from the registry inside a subprocess sandbox.

    Safety guarantees:
    - Only tools present in the ToolRegistry are executed.
    - Paths are resolved and verified to reside inside allowed directories.
    - Execution is time-limited.
    - Output is truncated to avoid memory exhaustion.
    """

    def __init__(self, registry: ToolRegistry, timeout: int = 10, max_output: int = 4000):
        self.registry = registry
        self.timeout = timeout
        self.max_output = max_output

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, tool_name: str, args: Optional[dict] = None) -> dict:
        """
        Execute a named tool and return a result dict:
          {
            "success": bool,
            "output":  str,   # stdout (truncated if needed)
            "error":   str,   # stderr or exception message
            "exit_code": int
          }

        Wraps the subprocess execution with TOOL_START/TOOL_END/TOOL_FAILED
        lifecycle events (and ERROR on an unexpected exception). Logging is
        purely additive: the result-dict contract and exception propagation are
        unchanged.
        """
        start = time.perf_counter()
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_START,
            f"tool start: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
        )
        try:
            result = self._execute_impl(tool_name, args)
        except Exception as exc:
            dur_ms = int((time.perf_counter() - start) * 1000)
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
            raise
        dur_ms = int((time.perf_counter() - start) * 1000)
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
        return result

    def _execute_impl(self, tool_name: str, args: Optional[dict] = None) -> dict:
        """Run the named tool in a subprocess and return the raw result dict."""
        tool = self.registry.get(tool_name)
        if tool is None:
            known = ", ".join(sorted(self.registry._registry.keys())) or "none"
            logger.warning("Tool '%s' is not registered. Known tools: %s", tool_name, known)
            return self._error(
                f"Tool '{tool_name}' is not registered. "
                f"NOTE: names mentioned inside SKILL.md files are capability descriptions, "
                f"not callable tools — do not try to call them directly. "
                f"Only call tools from BUILT-IN TOOLS (shell, file_read, file_write, schedule) "
                f"or AVAILABLE TOOLS. Currently registered tools: [{known}]"
            )

        # Extra path-safety: ensure the resolved path is inside an allowed dir
        if not self._path_is_safe(tool):
            return self._error(f"Tool path '{tool.path}' is outside allowed directories.")

        cmd = self._build_command(tool, args if isinstance(args, dict) else {})
        logger.info("Executing tool '%s': %s", tool_name, " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=os.path.dirname(tool.path),
            )
        except subprocess.TimeoutExpired:
            return self._error(f"Tool '{tool_name}' timed out after {self.timeout}s.")
        except Exception as exc:
            return self._error(f"Failed to run tool '{tool_name}': {exc}")

        stdout = self._truncate(proc.stdout)
        stderr = self._truncate(proc.stderr)

        return {
            "success": proc.returncode == 0,
            "output": stdout,
            "error": stderr,
            "exit_code": proc.returncode,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_command(self, tool: Tool, args: dict) -> list[str]:
        """Build the subprocess command list."""
        if tool.language == "bash":
            cmd = ["bash", tool.path]
        else:
            cmd = [sys.executable, tool.path]

        # Append extra args as key=value pairs (simple convention)
        for k, v in args.items():
            cmd.append(f"{k}={v}")

        return cmd

    def _path_is_safe(self, tool: Tool) -> bool:
        """
        Verify the tool's resolved absolute path starts with one of the
        registered tool directories. Prevents path-traversal attacks.
        """
        real = os.path.realpath(tool.path)
        for directory in self.registry.tools_dirs:
            allowed = os.path.realpath(directory)
            if real.startswith(allowed + os.sep) or real == allowed:
                return True
        return False

    def _truncate(self, text: str) -> str:
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n[...truncated at {self.max_output} chars]"
        return text

    @staticmethod
    def _error(msg: str) -> dict:
        logger.warning(msg)
        return {"success": False, "output": "", "error": msg, "exit_code": -1}
