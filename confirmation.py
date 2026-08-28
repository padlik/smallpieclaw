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
from typing import Callable

logger = logging.getLogger(__name__)

# Marker prefixes sent to the progress callback for the Telegram UI
CONFIRM_PREFIX = "__CONFIRM__"
EXTEND_PREFIX = "__EXTEND__"
RETRY_PREFIX = "__LLM_ERROR__"


class ConfirmationManager:
    """Manages all pending operator confirmations for a single agent session.

    Two confirmation flows are supported:

    1. **Tool confirmation** — request_confirmation / signal_confirmation /
       signal_approve_all.  Used for shell / file_write (and any other builtin
       that returns ``requires_confirmation``).

    2. **Step extension** — request_extension / signal_extension.  Prompted
       when the agent reaches its ``max_iterations`` limit.

    3. **LLM error retry** — request_retry / signal_retry.  Prompted when an
       LLM call fails with a retriable error so the operator can decide whether
       to retry or cancel.
    """

    def __init__(self) -> None:
        # --- Confirmation ---
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}
        self.auto_approve_tools: set[str] = set()

        # --- Extension ---
        self._extend_events: dict[str, threading.Event] = {}
        self._extend_results: dict[str, str] = {}

        # --- LLM error retry ---
        self._retry_events: dict[str, threading.Event] = {}
        self._retry_results: dict[str, str] = {}

        # --- Headless (sub-agent) confirmation ---
        self._headless_confirm_events: dict[str, threading.Event] = {}
        self._headless_confirm_results: dict[str, bool] = {}
        self.default_headless_timeout: int = 120

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

        Only writes the result and unblocks the waiter if the request has not
        already timed out (i.e. the event entry still exists).
        """
        if event := self._confirm_events.get(token):
            logger.info("signal_confirmation: token=%s confirmed=%s", token[:8], confirmed)
            self._confirm_results[token] = confirmed
            event.set()
        else:
            logger.warning(
                "signal_confirmation: token=%s already resolved or timed out", token[:8]
            )

    def signal_approve_all(self, token: str, tool_name: str) -> None:
        """Approve-all: register *tool_name* for automatic approval for this
        task, then unblock the current ``request_confirmation`` as confirmed.

        Only acts if the request has not already timed out.
        """
        self.auto_approve_tools.add(tool_name)
        if event := self._confirm_events.get(token):
            logger.info("signal_approve_all: token=%s tool_name=%s", token[:8], tool_name)
            self._confirm_results[token] = True
            event.set()
        else:
            logger.warning(
                "signal_approve_all: token=%s already resolved or timed out", token[:8]
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
        Only acts if the request has not already timed out.
        """
        if event := self._extend_events.get(token):
            logger.info("signal_extension: token=%s response=%s", token[:8], response)
            self._extend_results[token] = response
            event.set()
        else:
            logger.warning("signal_extension: token=%s already resolved or timed out", token[:8])

    # ------------------------------------------------------------------
    # LLM error retry
    # ------------------------------------------------------------------

    def request_retry(
        self,
        token: str,
        error_info_json: str,
        progress_cb: Callable[[str], None],
        timeout_seconds: int = 120,
    ) -> str:
        """Block until the operator responds to an LLM error retry prompt.

        Sends ``RETRY_PREFIX:token:error_info_json`` to *progress_cb*.
        Returns ``'retry'``, ``'cancel'``, or ``'timeout'``.
        """
        event = threading.Event()
        self._retry_events[token] = event
        self._retry_results[token] = "timeout"
        progress_cb(f"{RETRY_PREFIX}:{token}:{error_info_json}")
        event.wait(timeout=timeout_seconds)
        self._retry_events.pop(token, None)
        return self._retry_results.pop(token, "timeout")

    def signal_retry(self, token: str, response: str) -> None:
        """Called from an external thread with the operator's retry decision.

        *response* must be ``'retry'`` or ``'cancel'``.
        Only acts if the request has not already timed out.
        """
        if event := self._retry_events.get(token):
            logger.info("signal_retry: token=%s response=%s", token[:8], response)
            self._retry_results[token] = response
            event.set()
        else:
            logger.warning("signal_retry: token=%s already resolved or timed out", token[:8])

    # ------------------------------------------------------------------
    # Headless (sub-agent) confirmation
    # ------------------------------------------------------------------

    def request_headless_confirmation(
        self,
        token: str,
        tool_name: str,
        description: str,
        prompt_fn: Callable[[str, str, str, str], None],
        caller_tag: str = "",
    ) -> bool:
        """Block the calling sub-agent thread until the operator responds via Telegram.

        Creates a ``threading.Event``, stores it in ``_headless_confirm_events``,
        invokes *prompt_fn* to send the Telegram inline keyboard, then blocks on
        the event. Returns True if the operator approved, False if denied/timed-out.
        Cleans up its event/result entries on return.
        """
        event = threading.Event()
        self._headless_confirm_events[token] = event
        self._headless_confirm_results[token] = False
        try:
            prompt_fn(token, tool_name, description, caller_tag)
        except Exception:
            self._headless_confirm_events.pop(token, None)
            self._headless_confirm_results.pop(token, None)
            raise
        answered = event.wait(self.default_headless_timeout)
        if not answered:
            self._headless_confirm_events.pop(token, None)
            self._headless_confirm_results.pop(token, None)
            return False
        return self._headless_confirm_results.pop(token, False)

    def signal_headless_confirmation(
        self,
        token: str,
        approved: bool,
        approve_all: bool = False,
        tool_name: str = "",
    ) -> bool:
        """Atomically signal the outcome of a headless (sub-agent) confirmation prompt.

        If *approve_all* and *approved* and *tool_name* are set, adds *tool_name*
        to ``auto_approve_tools`` AND sets the event in one call — no concurrent
        sub-agent thread can observe the set without the event already being set.

        Returns True if the token was found and signalled, False if it was
        already expired/resolved (double-press / stale button).
        """
        # NOTE: The approve-all tool allowlist (_ALLOWED_APPROVE_ALL_TOOLS) is
        # enforced in telegram_callbacks.py, not here. This method will add any
        # tool_name when approve_all=True. The single caller (cb_subagent_confirm)
        # gates on the allowlist before calling. Defense-in-depth at the
        # transport boundary is intentional — the coordinator is transport-agnostic.
        event = self._headless_confirm_events.pop(token, None)
        if event is None:
            return False
        if approve_all and approved and tool_name:
            self.auto_approve_tools.add(tool_name)
        self._headless_confirm_results[token] = approved
        event.set()
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear_auto_approve(self) -> None:
        """Clear the auto-approve set, typically called at task reset."""
        self.auto_approve_tools.clear()
