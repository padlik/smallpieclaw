import os
import stat
import re
from pathlib import Path
from typing import Optional

import config

config_instance = config.config

FORBIDDEN_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bmv\s+/"),  # naive safety against moving system dirs
    re.compile(r"\bdd\s+of\b"),
    re.compile(r">\s*/etc/"),
    re.compile(r">\s*/sys/"),
    re.compile(r">\s*/dev/"),
]

def _is_code_safe(code: str) -> (bool, Optional[str]):
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(code):
            return False, f"Forbidden pattern detected: {pat.pattern}"
    return True, None

def create_tool(name: str, language: str, code: str, description: str = "") -> dict:
    if language not in ("bash", "python"):
        raise ValueError("Only 'bash' or 'python' are supported")
    safe, msg = _is_code_safe(code)
    if not safe:
        raise ValueError(f"Code failed safety validation: {msg}")
    if len(code.encode("utf-8")) > 64 * 1024:
        raise ValueError("Code too large")

    # Ensure shebang
    if language == "bash":
        if not code.lstrip().startswith("#!"):
            code = "#!/bin/bash\n" + code
    else:
        if not code.lstrip().startswith("#!"):
            code = "#!/usr/bin/env python3\n" + code

    # Write file
    out_dir = Path(config_instance.TOOLS_GENERATED_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = out_dir / f"{name}.{language}"
    filename.write_text(code, encoding="utf-8")
    os.chmod(filename, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)

    meta = {
        "name": name,
        "description": description.strip() if description else f"User-generated {language} tool: {name}",
        "path": str(filename),
        "type": language,
        "executable": True
    }
    return meta

