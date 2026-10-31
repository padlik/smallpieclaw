"""Exclusive agent PID-file lock shared by the daemon and offline tools.

The daemon (``main.py``) and mutating maintenance tools such as
``migrate_models.py --apply`` hold the same lock, so they can never run at the
same time for one agent.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Bounded retries when the PID pathname is replaced between open and flock.
_MAX_ATTEMPTS = 5


class PidFileLock:
    """
    Advisory exclusive lock on a PID file using fcntl.flock().

    The OS releases the lock automatically when the process exits (even on
    crash), so stale PID files left by a previous crash are handled without
    any PID-alive check — if flock() succeeds, the previous holder is gone.

    A lock on a descriptor only protects the pathname while that descriptor
    still refers to the file at the path. After flock() succeeds the inode of
    the descriptor is compared with the inode at the path; on mismatch (a
    competing holder unlinked and recreated it) the attempt is retried a
    bounded number of times. On release the PID file is unlinked while the
    lock is still held, and only if it is still this holder's inode.

    Usage::

        with PidFileLock("/run/agent.pid"):
            ...  # only one process can hold this at a time
    """

    def __init__(self, path: str) -> None:
        """Bind the lock to *path* without touching the filesystem."""
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> "PidFileLock":
        """Acquire the lock or exit 1 with an actionable message."""
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        for _ in range(_MAX_ATTEMPTS):
            fd = self._open()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                existing_pid = self._holder()
                os.close(fd)
                logger.error(
                    "Another instance is already running (PID %s). Aborting.", existing_pid
                )
                sys.exit(1)
            except OSError as exc:
                os.close(fd)
                logger.error("Cannot lock PID file %s: %s", self._path, exc)
                sys.exit(1)
            if self._is_current(fd):
                break
            os.close(fd)
        else:
            logger.error(
                "PID file %s kept being replaced while locking; another instance "
                "may be starting. Aborting.", self._path,
            )
            sys.exit(1)

        # Lock acquired on the inode at the path — write our PID
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError as exc:
            self._fd = fd
            self.__exit__()
            logger.error("Cannot write PID file %s: %s", self._path, exc)
            sys.exit(1)
        self._fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        """Unlink our own PID file while still locked, then release it."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if self._is_current(fd):
                os.unlink(self._path)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open(self) -> int:
        """Open (creating if needed) the PID file or exit 1."""
        try:
            return os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            logger.error("Cannot open PID file %s: %s", self._path, exc)
            sys.exit(1)

    def _is_current(self, fd: int) -> bool:
        """Whether *fd* still refers to the file currently at the path."""
        try:
            at_path = os.stat(self._path)
        except OSError:
            return False
        held = os.fstat(fd)
        return (held.st_dev, held.st_ino) == (at_path.st_dev, at_path.st_ino)

    def _holder(self) -> str:
        """Best-effort PID recorded by the current holder."""
        try:
            with open(self._path) as stream:
                return stream.read().strip() or "unknown"
        except OSError:
            return "unknown"
