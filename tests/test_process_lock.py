"""Shared agent PID/config lock against live daemon and stale-inode races."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _lock_type():
    """Import lazily: the not-yet-implemented module must not break collection."""
    from process_lock import PidFileLock

    return PidFileLock


def test_main_compatibility_alias_is_shared_lock() -> None:
    """Existing daemon callers retain their original lock name and semantics."""
    import main

    assert main._PidFileLock is _lock_type()


def test_held_lock_refuses_second_holder(tmp_path: Path) -> None:
    """A running daemon and a migrator cannot own the same PID lock."""
    pid = tmp_path / "agent.pid"
    with _lock_type()(str(pid)):
        with pytest.raises(SystemExit) as caught:
            with _lock_type()(str(pid)):
                pytest.fail("a second holder entered the protected section")
        assert caught.value.code != 0
        assert pid.exists()
    assert not pid.exists()


def test_stale_inode_is_retried_before_entering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Locking an unlinked fd is not enough: the current pathname must match."""
    pid = tmp_path / "agent.pid"
    original_open = os.open
    attempts: list[int] = []

    def swap_first_inode(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        """Simulate a competing unlock/recreate between open and flock."""
        fd = original_open(path, flags, mode)
        if str(path) == str(pid):
            attempts.append(fd)
            if len(attempts) == 1:
                pid.unlink()
                pid.touch()
        return fd

    monkeypatch.setattr(os, "open", swap_first_inode)
    with _lock_type()(str(pid)):
        assert len(attempts) >= 2
        assert (os.fstat(attempts[-1]).st_dev, os.fstat(attempts[-1]).st_ino) == (
            pid.stat().st_dev, pid.stat().st_ino,
        )
    with pytest.raises(OSError):
        os.fstat(attempts[0])


def test_repeated_inode_swap_fails_bounded_and_closes_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent pathname races fail closed with no leaked descriptor."""
    pid = tmp_path / "agent.pid"
    original_open = os.open
    attempts: list[int] = []

    def swap_every_inode(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        """Replace the path after every open, forcing bounded lock retries."""
        fd = original_open(path, flags, mode)
        if str(path) == str(pid):
            attempts.append(fd)
            pid.unlink()
            # Recreate via the captured original so the stub does not recurse.
            os.close(original_open(str(pid), os.O_CREAT | os.O_WRONLY, 0o644))
        return fd

    monkeypatch.setattr(os, "open", swap_every_inode)
    with pytest.raises(SystemExit) as caught:
        with _lock_type()(str(pid)):
            pytest.fail("a stale inode was admitted")
    assert caught.value.code != 0
    assert 1 < len(attempts) <= 8
    for fd in attempts:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_unlink_precedes_release_by_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PID path is removed before the lock's descriptor (the real release) closes.

    Production releases the flock via ``os.close`` (no explicit unlock), so the
    close of the lock's own descriptor is observed: at that moment the pathname
    must already be gone, proving there is no unlocked-but-present window.
    """
    pid = tmp_path / "agent.pid"
    original_open, original_close = os.open, os.close
    held: set[int] = set()
    released_while_present: list[int] = []

    def record_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        """Track descriptors opened on the PID pathname."""
        fd = original_open(path, flags, mode)
        if str(path) == str(pid):
            held.add(fd)
        return fd

    def observe_close(fd: int) -> None:
        """At the release boundary, the pathname must already be unlinked."""
        if fd in held and pid.exists():
            released_while_present.append(fd)
        original_close(fd)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", observe_close)
    with _lock_type()(str(pid)):
        assert pid.exists()
    assert not released_while_present
    assert not pid.exists()
