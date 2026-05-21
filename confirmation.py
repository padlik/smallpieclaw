"""
confirmation.py
---------------
Thread-safe coordination of agent confirmation requests.

The 'request_*' methods block the agent thread until the operator responds.
The 'signal_*' methods are called from external threads (Telegram, tests).

Dict mutations (single-key insert/pop) are atomic in CPython under the GIL,
so no additional lock is needed for the shared dicts.
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Marker prefixes sent to the progress callback for the Telegram UI
CONFIRM_PREFIX = "__CONFIRM__"
EXTEND_PREFIX = "__EXTEND__"
TOOL_CREATE_PREFIX = "__TOOL_CREATE__"


class ConfirmationManager:
    """Manages all pending operator confirmations for a single agent session.

    Three confirmation flows are supported:

    1. **Tool confirmation** — request_confirmation / signal_confirmation /
       signal_approve_all.  Used for shell / file_write (and any other builtin
       that returns ``requires_confirmation``).

    2. **Step extension** — request_extension / signal_extension.  Prompted
       when the agent reaches its ``max_iterations`` limit.

    3. **Tool creation** — request_tool_create / signal_tool_create.  Prompted
       when the LLM emits a ``create_tool`` action.
    """

    def __init__(self) -> None:
        # --- Confirmation ---
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}
        self.auto_approve_tools: set[str] = set()

        # --- Extension ---
        self._extend_events: dict[str, threading.Event] = {}
        self._extend_results: dict[str, str] = {}

        # --- Tool creation ---
        self._tool_create_events: dict[str, threading.Event] = {}
        self._tool_create_results: dict[str, str] = {}
        self.tool_create_pending: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Tool confirmation
    # ------------------------------------------------------------------

    def request_confirmation(
        self,
        token: str,
        tool_name: str,
        description: str,
        progress_cb: Callable[[str], None],
    ) -> bool:
        """Block the calling thread until the operator confirms or denies.

        Sends ``CONFIRM_PREFIX:token:tool_name:description`` to *progress_cb*
        so the Telegram layer can render the confirmation UI.

        Returns True if the operator confirmed, False if denied/timed-out.
        """
        event = threading.Event()
        self._confirm_events[token] = event
        self._confirm_results[token] = False
        progress_cb(f"{CONFIRM_PREFIX}:{token}:{tool_name}:{description}")
        event.wait(timeout=300)
        confirmed = self._confirm_results.pop(token, False)
        self._confirm_events.pop(token, None)
        return confirmed

    def signal_confirmation(self, token: str, confirmed: bool) -> None:
        """Called from an external thread to deliver the operator's decision.

        Sets the result and unblocks ``request_confirmation``.
        """
        logger.info(
            "signal_confirmation: token=%s confirmed=%s event_found=%s",
            token[:8], confirmed, token in self._confirm_events,
        )
        self._confirm_results[token] = confirmed
        if event := self._confirm_events.get(token):
            event.set()
        else:
            logger.warning(
                "signal_confirmation: no event for token=%s (already resolved?)", token[:8]
            )

    def signal_approve_all(self, token: str, tool_name: str) -> None:
        """Approve-all: register *tool_name* for automatic approval for this
        task, then unblock the current ``request_confirmation`` as confirmed.
        """
        logger.info("signal_approve_all: token=%s tool_name=%s", token[:8], tool_name)
        self.auto_approve_tools.add(tool_name)
        self._confirm_results[token] = True
        if event := self._confirm_events.get(token):
            event.set()
        else:
            logger.warning(
                "signal_approve_all: no event for token=%s", token[:8]
            )

    # ------------------------------------------------------------------
    # Step extension
    # ------------------------------------------------------------------

    def request_extension(
        self,
        max_steps: int,
        progress_cb: Callable[[str], None],
    ) -> str:
        """Block until the operator responds to a max-steps extension prompt.

        Sends ``EXTEND_PREFIX:token:max_steps`` to *progress_cb*.
        Returns ``'yes'``, ``'unlimited'``, or ``'no'``.
        """
        token = secrets.token_hex(4)
        event = threading.Event()
        self._extend_events[token] = event
        self._extend_results[token] = "no"
        progress_cb(f"{EXTEND_PREFIX}:{token}:{max_steps}")
        event.wait(timeout=120)
        self._extend_events.pop(token, None)
        return self._extend_results.pop(token, "no")

    def signal_extension(self, token: str, response: str) -> None:
        """Called from an external thread with the operator's extension decision.

        *response* must be ``'yes'``, ``'unlimited'``, or ``'no'``.
        """
        logger.info("signal_extension: token=%s response=%s", token[:8], response)
        self._extend_results[token] = response
        if event := self._extend_events.get(token):
            event.set()
        else:
            logger.warning("signal_extension: no event for token=%s", token[:8])

    # ------------------------------------------------------------------
    # Tool creation
    # ------------------------------------------------------------------

    def request_tool_create(
        self,
        token: str,
        tool_info: dict,
        progress_cb: Callable[[str], None],
    ) -> str:
        """Register a pending tool-create request and block until the operator responds.

        Sends ``TOOL_CREATE_PREFIX:token`` to *progress_cb* so the Telegram UI
        can fetch and display the pending tool info via ``get_pending_tool_create``.

        Returns ``'create'``, ``'run'``, or ``'cancel'``.
        """
        self.tool_create_pending[token] = tool_info
        event = threading.Event()
        self._tool_create_events[token] = event
        self._tool_create_results[token] = "cancel"
        progress_cb(f"{TOOL_CREATE_PREFIX}:{token}")
        event.wait(timeout=300)
        self._tool_create_events.pop(token, None)
        action = self._tool_create_results.pop(token, "cancel")
        self.tool_create_pending.pop(token, None)
        return action

    def get_pending_tool_create(self, token: str) -> Optional[dict]:
        """Return pending tool-create data for display in the Telegram UI."""
        return self.tool_create_pending.get(token)

    def signal_tool_create(self, token: str, action: str) -> None:
        """Called from an external thread with ``'create'``, ``'run'``, or ``'cancel'``."""
        logger.info("signal_tool_create: token=%s action=%s", token[:8], action)
        self._tool_create_results[token] = action
        if event := self._tool_create_events.get(token):
            event.set()
        else:
            logger.warning("signal_tool_create: no event for token=%s", token[:8])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear_auto_approve(self) -> None:
        """Clear the auto-approve set, typically called at task reset."""
        self.auto_approve_tools.clear()
