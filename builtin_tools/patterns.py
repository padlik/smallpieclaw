"""Dangerous-command and sensitive-path detection for built-in tools.

Stateless leaf module: only depends on ``re``; no imports back into
``builtin_executor`` or any handler module, so it is safe to import eagerly.
"""

from __future__ import annotations

import re

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
    # Writing to or executing from tools_generated/ is equivalent to creating/running a tool
    # and must go through the same operator confirmation gate as create_tool.
    (r"tools_generated/", "write/execute in tools_generated/ (same as tool creation — requires operator approval)"),
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
