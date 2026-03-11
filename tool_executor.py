"""
tool_executor.py — Safe execution of registered tool scripts.

Safety rules:
  - only registered tools may run
  - execution capped at TOOL_TIMEOUT_SEC
  - stdout+stderr captured and truncated
  - working directory set to tools dir
"""

from __future__ import annotations
import logging
import subprocess
import sys
from pathlib import Path

import config
from tool_registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    pass


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def run(self, tool_name: str, args: dict | None = None) -> str:
        """
        Execute a registered tool and return its combined output.
        args: optional key=value pairs passed as environment variables.
        """
        tool = self._registry.get(tool_name)
        if tool is None:
            raise ExecutionError(f"Tool '{tool_name}' is not registered.")

        # Verify the path is within an allowed directory
        resolved = tool.path.resolve()
        allowed = {
            config.TOOLS_DIR.resolve(),
            config.TOOLS_GEN_DIR.resolve(),
        }
        if not any(str(resolved).startswith(str(d)) for d in allowed):
            raise ExecutionError(f"Tool path '{resolved}' is outside allowed directories.")

        cmd = self._build_command(tool)
        env = self._build_env(args or {})

        logger.info("Executing tool '%s': %s", tool_name, " ".join(str(c) for c in cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.TOOL_TIMEOUT_SEC,
                cwd=str(tool.path.parent),
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise ExecutionError(
                f"Tool '{tool_name}' exceeded timeout of {config.TOOL_TIMEOUT_SEC}s."
            )
        except FileNotFoundError as e:
            raise ExecutionError(f"Interpreter not found for '{tool_name}': {e}")

        combined = (proc.stdout or "") + (proc.stderr or "")
        combined = combined.strip()

        if len(combined) > config.TOOL_OUTPUT_MAX:
            combined = combined[: config.TOOL_OUTPUT_MAX] + "\n… [output truncated]"

        if proc.returncode != 0:
            return f"[exit {proc.returncode}]\n{combined}" if combined else f"[exit {proc.returncode}]"
        return combined if combined else "[no output]"

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_command(tool: Tool) -> list[str]:
        if tool.extension == ".sh":
            return ["bash", str(tool.path)]
        elif tool.extension == ".py":
            return [sys.executable, str(tool.path)]
        else:
            raise ExecutionError(f"Unsupported tool type: {tool.extension}")

    @staticmethod
    def _build_env(args: dict) -> dict:
        import os
        env = os.environ.copy()
        for k, v in args.items():
            env[str(k).upper()] = str(v)
        return env
