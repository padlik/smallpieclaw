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

import enum
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Marker prefixes sent to the progress callback for the Telegram UI
CONFIRM_PREFIX = "__CONFIRM__"
EXTEND_PREFIX = "__EXTEND__"
RETRY_PREFIX = "__LLM_ERROR__"

# Tools that can never hold standing grants (sink-side veto). Confirmations for
# these tools are always per-operation.
NO_STANDING_GRANT_TOOLS: frozenset[str] = frozenset({"shell", "secret_get"})


class GrantLifetime(enum.Enum):
    """Lifetime of a standing operator grant."""

    PROMPT = "prompt"
    SESSION = "session"


@dataclass(frozen=True)
class Grant:
    """A single standing operator consent grant.

    Args:
        tool: The tool name the grant applies to (exactly one tool).
        dir: Realpath of the directory whose contents are covered recursively.
        lifetime: PROMPT (one user-message cycle) or SESSION (until /reset).
        scope_owner: None for main-agent grants; a sub-agent id for scoped grants.
    """

    tool: str
    dir: str
    lifetime: GrantLifetime
    scope_owner: Optional[str] = None


def _grant_normalize_dir(dir_path: str) -> str:
    """Return the canonical realpath for a directory grant path."""
    return os.path.realpath(os.path.expanduser(dir_path))


def grant_covers(grant_dir: str, path: str) -> bool:
    """Return True when *grant_dir* recursively covers *path*.

    Boundary-safe containment: *path* is covered when it is identical to
    *grant_dir* or sits somewhere underneath it. Uses normcase to tolerate
    case-insensitive filesystems on a best-effort basis; separator handling
    avoids false positives for path-prefix collisions (e.g. ``/data/reports``
    must not cover ``/data/repo``).

    Callers must pass *grant_dir* already realpath-normalized (typically
    ``os.path.dirname(real_path)``); *path* is normalized inside this function.
    """
    norm_zone = os.path.normcase(grant_dir)
    norm_path = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    if norm_path == norm_zone:
        return True
    # os.sep boundary: norm_path must be grant_dir + sep + something
    prefix = norm_zone + os.path.normcase(os.sep)
    return norm_path.startswith(prefix)


class GrantLedger:
    """Thread-safe in-memory store for standing operator grants.

    Grants are ``(tool, dir, lifetime, scope_owner)`` and are checked with
    boundary-safe recursive directory coverage. The ledger enforces a sink-side
    veto: ``shell`` and ``secret_get`` may never hold grants, so even crafted
    callbacks cannot create them.

    Scope semantics:
    * ``scope_owner=None`` grants are main-agent scoped and do NOT cover sub-agent
      calls (``check`` with a non-None *scope_owner* returns False for them).
    * Sub-agent grants (``scope_owner="sa-..."``) cover only calls whose
      *scope_owner* matches exactly; they never cover the main agent.
    * SESSION grants created scope-free (``scope_owner=None``) cover every scope —
      per the approval-grants spec, "Till /reset" consent stops prompting for the
      whole session, main agent and sub-agents alike. Sub-agent confirmations
      create session grants scope-free for exactly this reason.
    This treats the ledger as single, depth-0-owned with explicit scoping, while
    honouring the operator's session-wide consent intent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: set[Grant] = set()

    @staticmethod
    def may_hold_grant(tool: str) -> bool:
        """Return True when *tool* is permitted to hold a standing grant."""
        return tool not in NO_STANDING_GRANT_TOOLS

    def add(
        self,
        tool: str,
        dir: str,
        lifetime: GrantLifetime,
        scope_owner: Optional[str] = None,
    ) -> bool:
        """Add a grant. Returns True on success, False if sink-vetoed.

        Adding an identical grant is a no-op and returns True. The directory is
        normalized with realpath/expanduser before storage.
        """
        if not self.may_hold_grant(tool):
            logger.warning("GrantLedger refused standing grant for tool=%s", tool)
            return False
        norm_dir = _grant_normalize_dir(dir)
        grant = Grant(tool=tool, dir=norm_dir, lifetime=lifetime, scope_owner=scope_owner)
        with self._lock:
            self._grants.add(grant)
        return True

    def check(self, tool: str, dir: str, scope_owner: Optional[str] = None) -> bool:
        """Return True when an active grant covers ``(tool, dir)`` for *scope_owner*.

        A grant matches when its tool equals *tool* and its directory covers
        *dir* (recursively, with boundary-safe containment). Scope matching:
        * PROMPT-lifetime grants match only when their scope equals
          *scope_owner* exactly (sub-agent prompt grants never cover the main
          agent — no upward privilege leak).
        * SESSION-lifetime grants created scope-free (``scope_owner=None``)
          cover every scope: "Till /reset" consent is session-wide by spec.
        The sink-side veto is also applied here: shell/secret_get always
        return False.
        """
        if not self.may_hold_grant(tool):
            return False
        norm_dir = _grant_normalize_dir(dir)
        with self._lock:
            grants = list(self._grants)
        return any(
            g.tool == tool
            and grant_covers(g.dir, norm_dir)
            and (
                (g.lifetime == GrantLifetime.SESSION and g.scope_owner is None)
                or g.scope_owner == scope_owner
            )
            for g in grants
        )

    def clear_prompt_scope(self, scope_owner: Optional[str] = None) -> None:
        """Clear PROMPT-lifetime grants.

        With *scope_owner=None* (the default), clears main-scoped prompt grants.
        Pass a sub-agent id to clear that scope's prompt grants.
        """
        with self._lock:
            self._grants = {
                g
                for g in self._grants
                if not (g.lifetime == GrantLifetime.PROMPT and g.scope_owner == scope_owner)
            }

    def clear_all(self) -> None:
        """Clear every grant (all lifetimes, all scopes)."""
        with self._lock:
            self._grants.clear()


class ConfirmationManager:
    """Manages all pending operator confirmations for a single agent session.

    1. **Tool confirmation** — request_confirmation / signal_confirmation.
       Used for shell / file_write (and any other builtin that returns
       ``requires_confirmation``).

    2. **Step extension** — request_extension / signal_extension.  Retained
       as part of the ADR-0024 four-flow taxonomy but now dormant: the react
       loop no longer enforces a step limit, so this flow has no active callers.

    3. **LLM error retry** — request_retry / signal_retry.  Prompted when an
       LLM call fails with a retriable error so the operator can decide whether
       to retry or cancel.
    """

    def __init__(self) -> None:
        # --- Confirmation ---
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}

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

        # --- Grant ledger (depth-0 owned single ledger) ---
        self.grant_ledger = GrantLedger()

    # ------------------------------------------------------------------
    # Grant ledger convenience helpers
    # ------------------------------------------------------------------

    def add_grant(
        self,
        tool: str,
        dir: str,
        lifetime: GrantLifetime,
        scope_owner: Optional[str] = None,
    ) -> bool:
        """Delegate to ``self.grant_ledger.add``."""
        return self.grant_ledger.add(tool, dir, lifetime, scope_owner=scope_owner)

    def check_grant(
        self,
        tool: str,
        dir: str,
        scope_owner: Optional[str] = None,
    ) -> bool:
        """Delegate to ``self.grant_ledger.check``."""
        return self.grant_ledger.check(tool, dir, scope_owner=scope_owner)

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
        try:
            progress_cb(f"{CONFIRM_PREFIX}:{token}:{tool_name}:{description}")
        except Exception:
            self._confirm_events.pop(token, None)
            self._confirm_results.pop(token, None)
            raise
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
        try:
            progress_cb(f"{EXTEND_PREFIX}:{token}:{max_steps}")
        except Exception:
            self._extend_events.pop(token, None)
            self._extend_results.pop(token, None)
            raise
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
        try:
            progress_cb(f"{RETRY_PREFIX}:{token}:{error_info_json}")
        except Exception:
            self._retry_events.pop(token, None)
            self._retry_results.pop(token, None)
            raise
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
    ) -> bool:
        """Atomically signal the outcome of a headless (sub-agent) confirmation prompt.

        Returns True if the token was found and signalled, False if it was
        already expired/resolved (double-press / stale button).
        """
        event = self._headless_confirm_events.pop(token, None)
        if event is None:
            return False
        self._headless_confirm_results[token] = approved
        event.set()
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
