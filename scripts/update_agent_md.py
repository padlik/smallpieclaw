#!/usr/bin/env python3
"""
scripts/update_agent_md.py
--------------------------
Update the 'Last updated:' line in AGENT.md with the current UTC timestamp
and short git hash. Called by the pre-commit hook before each commit so that
AGENT.md always reflects when it was last reviewed.

Safe to run repeatedly — idempotent.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_MD = _REPO_ROOT / "AGENT.md"


def _short_hash() -> str:
    """Return the short HEAD git hash, or 'unknown' if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def update() -> bool:
    """Update the Last updated line. Returns True if the file was changed."""
    if not _AGENT_MD.exists():
        print(f"AGENT.md not found at {_AGENT_MD}", file=sys.stderr)
        return False

    content = _AGENT_MD.read_text(encoding="utf-8")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    git_hash = _short_hash()
    new_line = f"> **Last updated:** {timestamp} ({git_hash})"

    updated = re.sub(
        r"^> \*\*Last updated:\*\*.*$",
        new_line,
        content,
        flags=re.MULTILINE,
    )

    if updated == content:
        return False  # nothing changed

    _AGENT_MD.write_text(updated, encoding="utf-8")
    print(f"Updated AGENT.md: {new_line}")
    return True


if __name__ == "__main__":
    changed = update()
    sys.exit(0)
