"""Session-scoped shell environment variable management tools.

Handler module: ``ShellEnvTools`` holds a back-reference to the
``BuiltinExecutor`` façade (``owner``) and mutates the ``_shell_env`` dict that
lives on the façade. These tools do not perform filesystem, network, or
subprocess operations, so they are not confirmation-gated. Variables stored here
are injected into subsequent ``shell`` invocations via nsjail ``-E`` flags.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)

_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class ShellEnvTools:
    """Session-scoped shell environment variable management tools.

    These tools manage the _shell_env dict on the BuiltinExecutor façade.
    Variables set here are injected via nsjail -E flags on subsequent shell calls.
    They are NOT confirmation-capable — no filesystem, network, or subprocess operations.
    """

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def shell_env_set(self, args: dict) -> dict:
        """Set a session-scoped shell environment variable.

        Args:
            args: Tool arguments. Must contain ``key`` and ``value`` strings.

        Returns:
            A result dict with ``success`` set to True, or False with an
            ``error`` field if the key is invalid.
        """
        key = str(args.get("key", ""))
        value = str(args.get("value", ""))
        if not _ENV_KEY_RE.match(key):
            return {
                "success": False,
                "output": "",
                "error": f"Invalid env var key {key!r}: must match [A-Za-z_][A-Za-z0-9_]*",
            }
        if any(c in value for c in ("\x00", "\n", "\r")):
            return {
                "success": False,
                "output": "",
                "error": "Null byte, newline, or carriage return in env var value is not allowed",
            }
        logger.info("Built-in shell_env_set: %s", key)
        with self._owner._shell_env_lock:
            self._owner._shell_env[key] = value
        return {"success": True, "output": f"Set {key}={value}", "error": ""}

    def shell_env_unset(self, args: dict) -> dict:
        """Remove a session-scoped shell environment variable.

        Args:
            args: Tool arguments. Must contain a ``key`` string.

        Returns:
            A result dict with ``success`` set to True.
        """
        key = str(args.get("key", ""))
        logger.info("Built-in shell_env_unset: %s", key)
        with self._owner._shell_env_lock:
            self._owner._shell_env.pop(key, None)
        return {"success": True, "output": f"Unset {key}", "error": ""}

    def shell_env_list(self, args: dict) -> dict:
        """Return a snapshot of all session-scoped shell environment variables.

        Args:
            args: Tool arguments (unused).

        Returns:
            A result dict with ``success`` set to True and an ``env`` field
            containing a shallow copy of the current session environment.
        """
        with self._owner._shell_env_lock:
            snapshot = dict(self._owner._shell_env)
        logger.info("Built-in shell_env_list: %d entries", len(snapshot))
        return {"success": True, "output": json.dumps(snapshot, ensure_ascii=False), "env": snapshot, "error": ""}

    def shell_env_get(self, args: dict) -> dict:
        """Get the value of a session-scoped shell environment variable.

        Args:
            args: Tool arguments. Must contain a ``key`` string.

        Returns:
            A result dict with ``success`` set to True and a ``value`` field.
            The value is an empty string if the key is not set.
        """
        key = str(args.get("key", ""))
        logger.info("Built-in shell_env_get: %s", key)
        with self._owner._shell_env_lock:
            value = self._owner._shell_env.get(key, "")
        return {"success": True, "output": value, "value": value, "error": ""}
