"""
tool_executor.py
----------------
Safely executes registered tools via subprocess.
Enforces timeout, output size limits, and allowlist checks.
"""

import logging
import os
import subprocess
import sys
from typing import Optional

from tool_registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)


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
        """
        tool = self.registry.get(tool_name)
        if tool is None:
            return self._error(f"Tool '{tool_name}' is not registered.")

        # Extra path-safety: ensure the resolved path is inside an allowed dir
        if not self._path_is_safe(tool):
            return self._error(f"Tool path '{tool.path}' is outside allowed directories.")

        cmd = self._build_command(tool, args or {})
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
