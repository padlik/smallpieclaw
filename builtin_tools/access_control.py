"""Zone-based file access control for file_* built-in tools.

TrustedZoneChecker classifies every path into one of four zones:
  INTERNAL      - agent-owned dirs (data/, tools/, skills/, etc.) — auto-allow
  TRUSTED       - user workspace + downloads + tmp + user-added dirs — auto-allow
  REQUEST_GRANT - per-request directory grant (cleared each user message) — auto-allow
  UNRECOGNISED  - everything else — stage confirmation prompt

data/trusted_dirs.json is excluded from INTERNAL auto-allow to prevent the LLM
from silently modifying the trust store; writes to it go through _requires_confirmation.
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

    INTERNAL = "internal"
    TRUSTED = "trusted"
    REQUEST_GRANT = "request_grant"
    UNRECOGNISED = "unrecognised"


@dataclass
class TrustedDir:
    """A user-added trusted directory entry."""

    path: str
    added: str  # ISO8601 timestamp


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
    ) -> None:
        """Construct checker from resolved config paths.

        Args:
            paths_config: Typed PathsConfig from AppConfig.
            data_dir: Absolute resolved data directory path.
            agent_name: Agent name used to derive log and vault XDG paths.
        """
        self._data_dir = os.path.realpath(data_dir)
        self._trusted_dirs_path = os.path.normcase(
            os.path.join(self._data_dir, "trusted_dirs.json")
        )

        # Internal dirs — auto-allow, resolved at construction time
        internal_candidates = [
            paths_config.tools_dir,
            paths_config.generated_tools_dir,
            data_dir,
            paths_config.skills_dir,
            paths_config.prompts_dir,
            # XDG log dir: ~/.local/state/<agent>/logs/
            os.path.expanduser(f"~/.local/state/{agent_name}/logs"),
            # XDG vault dir: ~/.local/share/<agent>/
            os.path.expanduser(f"~/.local/share/{agent_name}"),
        ]
        self._internal_dirs: list[str] = []
        for d in internal_candidates:
            resolved = os.path.realpath(os.path.expanduser(d))
            if resolved and resolved not in self._internal_dirs:
                self._internal_dirs.append(resolved)

        # Default trusted dirs (protected, non-removable)
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

        # Write-protected internal subdirs — INTERNAL zone but writes require confirmation
        # (ops that write/patch/send these dirs are downgraded to UNRECOGNISED)
        _wp_candidates = [
            paths_config.tools_dir,
            paths_config.generated_tools_dir,
            paths_config.prompts_dir,
            paths_config.skills_dir,
            # XDG dirs are INTERNAL but also write-protected — vault.toml and logs must not be
            # silently overwritten by the LLM
            os.path.expanduser(f"~/.local/state/{agent_name}/logs"),
            os.path.expanduser(f"~/.local/share/{agent_name}"),
        ]
        self._write_protected_internal_dirs: list[str] = [
            os.path.realpath(os.path.expanduser(d)) for d in _wp_candidates if d
        ]

        # User-added trusted dirs — loaded from disk
        self._user_trusted: list[TrustedDir] = self._load_user_trusted()
        self._user_trusted_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, path: str, request_grants: frozenset[str] = frozenset()) -> ZoneClassification:
        """Classify path into a zone. Always uses realpath for comparison.

        Args:
            path: File path to classify (expanded and realpath'd internally).
            request_grants: Snapshot from GrantTracker.snapshot() for the current
                request cycle. Pass frozenset() when no grants are active.
        """
        real = os.path.realpath(os.path.expanduser(path))

        # INTERNAL: check first, but explicitly exclude trusted_dirs.json
        if os.path.normcase(real) == self._trusted_dirs_path:
            return ZoneClassification.UNRECOGNISED
        for zone in self._internal_dirs:
            if _is_contained(real, zone):
                return ZoneClassification.INTERNAL

        # TRUSTED: default protected dirs + user-added
        for zone in self._default_trusted_dirs:
            if _is_contained(real, zone):
                return ZoneClassification.TRUSTED
        with self._user_trusted_lock:
            user_trusted_snapshot = list(self._user_trusted)
        for entry in user_trusted_snapshot:
            zone = os.path.realpath(os.path.expanduser(entry.path))
            if _is_contained(real, zone):
                return ZoneClassification.TRUSTED

        # REQUEST_GRANT: per-request directory grants (caller-supplied snapshot)
        for zone in request_grants:
            if _is_contained(real, zone):
                return ZoneClassification.REQUEST_GRANT

        return ZoneClassification.UNRECOGNISED

    def is_write_protected_internal(self, real_path: str) -> bool:
        """Return True if real_path is inside an agent code directory (tools/prompts/skills)."""
        for zone in self._write_protected_internal_dirs:
            if _is_contained(real_path, zone):
                return True
        return False

    def add_trusted(self, path: str) -> None:
        """Persist a user-added trusted directory to data/trusted_dirs.json."""
        real = os.path.realpath(os.path.expanduser(path))
        with self._user_trusted_lock:
            existing = {os.path.realpath(os.path.expanduser(e.path)) for e in self._user_trusted}
            if real in existing:
                logger.info("Zone: %s already in trusted list", real)
                return
            entry = TrustedDir(
                path=real,
                added=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
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

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_user_trusted(self) -> list[TrustedDir]:
        """Load user-added dirs from disk. Returns empty list if file missing."""
        if not os.path.exists(self._trusted_dirs_path):
            return []
        try:
            with open(self._trusted_dirs_path) as f:
                raw = json.load(f)
            return [TrustedDir(path=e["path"], added=e.get("added", "")) for e in raw if "path" in e]
        except Exception as exc:
            logger.warning("Zone: failed to load trusted_dirs.json: %s", exc)
            return []

    def _save_user_trusted(self) -> None:
        """Atomically persist user-added dirs to trusted_dirs.json."""
        data = [{"path": e.path, "added": e.added} for e in self._user_trusted]
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
