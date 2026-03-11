import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any

import config

config_instance = config.config

class ToolExecutionError(Exception):
    pass

def _normalize_args(args: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for k, v in args.items():
        out[str(k)] = str(v)
    return out

def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from tool_registry import registry
    meta = registry.get_tool(name)
    if not meta:
        raise ToolExecutionError(f"Tool not found: {name}")
    path = Path(meta["path"])
    if not path.exists():
        raise ToolExecutionError(f"Tool file missing: {path}")
    # Restrict execution to tools directories
    base = Path(config_instance.TOOLS_DIR).resolve()
    gen = Path(config_instance.TOOLS_GENERATED_DIR).resolve()
    resolved = path.resolve()
    if not (resolved.is_relative_to(base) or resolved.is_relative_to(gen)):
        raise ToolExecutionError("Tool path not allowed")

    # Prepare environment
    env = os.environ.copy()
    env["AGENT_TOOL_ARGS"] = json.dumps(_normalize_args(args))
    try:
        cmd = [sys.executable, str(path)] if meta["type"] == "py" else [str(path)]
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=config_instance.TOOL_TIMEOUT_SECONDS,
        )
        stdout = proc.stdout.decode("utf-8", errors="ignore")
        stderr = proc.stderr.decode("utf-8", errors="ignore")
        # truncate output if too large
        out_bytes = stdout.encode("utf-8")
        if len(out_bytes) > config_instance.MAX_OUTPUT_BYTES:
            stdout = out_bytes[:config_instance.MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n...[truncated]"
        err_bytes = stderr.encode("utf-8")
        if len(err_bytes) > config_instance.MAX_OUTPUT_BYTES:
            stderr = err_bytes[:config_instance.MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n...[truncated]"
        return {
            "name": name,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr
        }
    except subprocess.TimeoutExpired as e:
        raise ToolExecutionError(f"Tool timed out: {name}") from e
    except Exception as e:
        raise ToolExecutionError(f"Tool execution failed: {e}") from e

