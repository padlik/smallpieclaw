"""Zone-based file access control for file_* built-in tools.

TrustedZoneChecker classifies every path into one of three zones:
  TRUSTED       - user workspace + downloads + tmp + user-added dirs — auto-allow
  REQUEST_GRANT - per-request directory grant (cleared each user message) — auto-allow
  UNRECOGNISED  - everything else — stage confirmation prompt

data/trusted_dirs.json and the vault file are always UNRECOGNISED even if a parent
directory appears in the trusted set (defense-in-depth: prevents silent rewrite of
the trust store or silent read of secrets).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_schema import PathsConfig

logger = logging.getLogger(__name__)


class ZoneClassification(Enum):
    """Priority-ordered zone classification for file path access control."""

    TRUSTED = "trusted"
    REQUEST_GRANT = "request_grant"
    UNRECOGNISED = "unrecognised"


@dataclass
class TrustedDir:
    """A user-added trusted directory entry."""

    path: str
    added: str  # ISO8601 timestamp
    mode: str = "rw"  # "rw" or "r"


def _is_contained(path: str, zone_dir: str) -> bool:
    """Return True if path is inside zone_dir using separator-boundary matching.

    Prevents sibling-prefix bypass: /srv/shared-evil is NOT inside /srv/shared.
    Uses normcase so case-insensitive filesystems (macOS, Windows) match correctly.
    """
    norm_path = os.path.normcase(path)
    norm_zone = os.path.normcase(zone_dir)
    return norm_path == norm_zone or norm_path.startswith(norm_zone + os.path.normcase(os.sep))


class GrantTracker:
    """Per-agent ephemeral request grant set. One instance per BuiltinExecutor."""

    def __init__(self) -> None:
        self._grants: set[str] = set()
        self._lock: threading.Lock = threading.Lock()

    def add(self, path: str) -> None:
        """Grant access to the parent directory of path for the current request cycle."""
        real = os.path.realpath(os.path.expanduser(path))
        grant_dir = os.path.dirname(real)
        with self._lock:
            self._grants.add(grant_dir)
        logger.info("Zone: request grant for dir %s", grant_dir)

    def reset(self) -> None:
        """Clear all grants. Called at react_loop() entry."""
        with self._lock:
            self._grants.clear()

    def snapshot(self) -> frozenset[str]:
        """Thread-safe snapshot for use in classify()."""
        with self._lock:
            return frozenset(self._grants)


class TrustedZoneChecker:
    """Classifies file paths into zones and manages trusted directory state."""

    def __init__(
        self,
        paths_config: PathsConfig,
        data_dir: str,
        agent_name: str,
        vault_path: str = "",
        trusted_dirs_path: str = "",
    ) -> None:
        """Construct checker from resolved config paths.

        Args:
            paths_config: Typed PathsConfig from AppConfig.
            data_dir: Absolute resolved data directory path.
            agent_name: Agent name used to derive tmp dir fallback path.
            vault_path: Absolute path to the vault file; always UNRECOGNISED
                to prevent silent reads bypassing secret_get confirmation.
            trusted_dirs_path: Absolute path to trusted_dirs.json. If empty,
                falls back to ``data_dir/trusted_dirs.json``.
        """
        self._data_dir = os.path.realpath(data_dir)
        self._trusted_dirs_path = os.path.normcase(
            os.path.realpath(trusted_dirs_path) if trusted_dirs_path
            else os.path.realpath(os.path.join(self._data_dir, "trusted_dirs.json"))
        )
        self._vault_path: str = (
            os.path.normcase(os.path.realpath(os.path.expanduser(vault_path)))
            if vault_path else ""
        )
        self._override_inodes: set[tuple[int, int]] = self._collect_override_inodes()

        # Default trusted dirs (non-removable)
        trusted_candidates = [
            paths_config.workspace_dir,
            paths_config.downloads_dir,
            paths_config.tmp_dir if paths_config.tmp_dir else f"/tmp/{agent_name}",
        ]
        self._default_trusted_dirs: list[str] = []
        for d in trusted_candidates:
            if not d:
                continue
            resolved = os.path.realpath(os.path.expanduser(d))
            if resolved and resolved not in self._default_trusted_dirs:
                self._default_trusted_dirs.append(resolved)

        # User-added trusted dirs — loaded from disk
        self._user_trusted: list[TrustedDir] = self._load_user_trusted()
        self._user_trusted_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, path: str, operation: str = "write", request_grants: frozenset[str] = frozenset()) -> ZoneClassification:
        """Classify path into a zone. Always uses realpath for comparison.

        Args:
            path: File path to classify (expanded and realpath'd internally).
            operation: "read" or "write" — used to enforce r-only user-added dirs.
            request_grants: Snapshot from GrantTracker.snapshot() for the current
                request cycle. Pass frozenset() when no grants are active.
        """
        real = os.path.realpath(os.path.expanduser(path))

        # Defense-in-depth overrides: trust-store and vault are always UNRECOGNISED
        # even if a parent directory is in the trusted set.
        if os.path.normcase(real) == self._trusted_dirs_path:
            return ZoneClassification.UNRECOGNISED
        if self._vault_path and os.path.normcase(real) == self._vault_path:
            return ZoneClassification.UNRECOGNISED

        # Hardlink alias: same inode as vault or trust-store — always UNRECOGNISED
        if self._override_inodes:
            try:
                st = os.stat(real)
                if (st.st_dev, st.st_ino) in self._override_inodes:
                    return ZoneClassification.UNRECOGNISED
            except OSError:
                pass

        # TRUSTED: default protected dirs (always rw)
        for zone in self._default_trusted_dirs:
            if _is_contained(real, zone):
                return ZoneClassification.TRUSTED

        # TRUSTED: user-added dirs — most restrictive match wins
        # (if any matching entry is r-mode and operation is write, block)
        with self._user_trusted_lock:
            user_trusted_snapshot = list(self._user_trusted)
        has_r_match = False
        has_rw_match = False
        for entry in user_trusted_snapshot:
            zone = os.path.realpath(os.path.expanduser(entry.path))
            if _is_contained(real, zone):
                if entry.mode == "r":
                    has_r_match = True
                else:
                    has_rw_match = True
        if has_r_match or has_rw_match:
            if has_r_match and operation == "write":
                return ZoneClassification.UNRECOGNISED
            return ZoneClassification.TRUSTED

        # REQUEST_GRANT: per-request directory grants
        for zone in request_grants:
            if _is_contained(real, zone):
                return ZoneClassification.REQUEST_GRANT

        return ZoneClassification.UNRECOGNISED

    def add_trusted(self, path: str, mode: str = "rw") -> None:
        """Persist a user-added trusted directory to data/trusted_dirs.json."""
        if mode not in {"r", "rw"}:
            raise ValueError(f"Invalid mode {mode!r}; must be 'r' or 'rw'")
        real = os.path.realpath(os.path.expanduser(path))
        with self._user_trusted_lock:
            existing = {os.path.realpath(os.path.expanduser(e.path)) for e in self._user_trusted}
            if real in existing:
                logger.info("Zone: %s already in trusted list", real)
                return
            entry = TrustedDir(
                path=real,
                added=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
                mode=mode,
            )
            self._user_trusted.append(entry)
            self._save_user_trusted()
        logger.info("Zone: added trusted dir %s", real)

    def remove_trusted(self, index: int) -> str:
        """Remove user-added trusted dir by 1-based index (matches list_user_trusted order).

        Raises:
            IndexError: If index is out of range.
        """
        with self._user_trusted_lock:
            sorted_dirs = sorted(self._user_trusted, key=lambda e: e.path)
            if index < 1 or index > len(sorted_dirs):
                raise IndexError(f"No trusted directory #{index}.")
            target_path = sorted_dirs[index - 1].path
            self._user_trusted = [e for e in self._user_trusted if e.path != target_path]
            self._save_user_trusted()
        logger.info("Zone: removed trusted dir #%d (%s)", index, target_path)
        return target_path

    def list_user_trusted(self) -> list[TrustedDir]:
        """Return user-added trusted dirs sorted by path."""
        with self._user_trusted_lock:
            return sorted(self._user_trusted, key=lambda e: e.path)

    def reload_user_trusted(self) -> int:
        """Reload user-added trusted dirs from disk, replacing the in-memory list.

        Raises on malformed content so the caller can report the error and the
        existing in-memory list is not silently wiped.

        Returns:
            Number of entries loaded from disk.
        """
        with self._user_trusted_lock:
            fresh = self._read_trusted_from_disk()
            self._user_trusted = fresh
            n = len(fresh)
        logger.info("Zone: reloaded trusted_dirs.json — %d entries", n)
        return n

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _collect_override_inodes(self) -> set[tuple[int, int]]:
        """Stat override files at construction so hardlink aliases are caught in classify()."""
        inodes: set[tuple[int, int]] = set()
        for p in (self._trusted_dirs_path, self._vault_path):
            if not p:
                continue
            try:
                st = os.stat(p)
                inodes.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        return inodes

    def _read_trusted_from_disk(self) -> list[TrustedDir]:
        """Parse trusted_dirs.json; raises on malformed content."""
        if not os.path.exists(self._trusted_dirs_path):
            return []
        with open(self._trusted_dirs_path) as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raise ValueError(
                f"trusted_dirs.json must be a JSON array, got {type(raw).__name__}"
            )
        entries: list[TrustedDir] = []
        for e in raw:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                raise ValueError(f"invalid entry in trusted_dirs.json: {e!r}")
            added = e.get("added")
            mode = e.get("mode", "rw")
            if mode not in ("r", "rw"):
                raise ValueError(f"invalid mode {mode!r} for {e['path']!r}")
            entries.append(TrustedDir(
                path=e["path"],
                added=added if isinstance(added, str) else "",
                mode=mode,
            ))
        return entries

    def _load_user_trusted(self) -> list[TrustedDir]:
        """Load user-added dirs from disk. Returns empty list if file missing or unreadable."""
        try:
            return self._read_trusted_from_disk()
        except Exception as exc:
            logger.warning("Zone: failed to load trusted_dirs.json: %s", exc)
            return []

    def _save_user_trusted(self) -> None:
        """Atomically persist user-added dirs to trusted_dirs.json."""
        data = [{"path": e.path, "added": e.added, "mode": e.mode} for e in self._user_trusted]
        _atomic_save_json(self._trusted_dirs_path, data)


def _atomic_save_json(path: str, data: object) -> None:
    """Write data as JSON to path atomically (write temp, then rename)."""
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
