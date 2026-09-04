"""Dangerous-command and sensitive-path detection for built-in tools.

Stateless leaf module: only depends on ``re``; no imports back into
``builtin_executor`` or any handler module, so it is safe to import eagerly.
"""

from __future__ import annotations

import re

_DANGEROUS_SHELL_PATTERNS: list[tuple[str, str, str]] = [
    (r"\brm\s+-[^\s]*r[^\s]*\s+/", "recursive removal from /", "host_escape"),
    (r"\brm\s+-rf\b", "rm -rf", "project"),
    (r"\bdd\b.*\bof=", "raw device write with dd", "host_escape"),
    (r"\bmkfs\b", "filesystem format with mkfs", "host_escape"),
    (r">\s*/dev/(?!null)", "redirect to device node", "host_escape"),
    (r"\bchmod\s+777\b", "chmod 777", "project"),
    (r"\bcurl\b.*\|\s*(?:ba)?sh\b", "curl pipe to shell", "network"),
    (r"\bwget\b.*\|\s*(?:ba)?sh\b", "wget pipe to shell", "network"),
    (r">\s*/etc/", "write to /etc/", "host_escape"),
    (r">\s*/boot/", "write to /boot/", "host_escape"),
    (r"\bsudo\s+su\b", "sudo su", "policy"),
    (r":\(\)\{.*:\|:&\}", "fork bomb", "resource"),
    (r"/dev/tcp/", "TCP reverse shell", "network"),
    (r"\bnc\s+-e\b", "netcat reverse shell", "network"),
]


def _is_dangerous_shell(command: str) -> tuple[bool, str, str]:
    """Return (is_dangerous, reason, category).

    Category is one of: host_escape, network, resource, project, policy.
    Used by the configurable confirmation gate (_should_confirm) to decide
    whether to skip confirmation when nsjail sandboxing is active.
    """
    for pattern, reason, category in _DANGEROUS_SHELL_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True, reason, category
    return False, "", ""
