"""Disk-based checkpoint store for crash recovery of agent runs."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CheckpointRetireStatus(Enum):
    """Outcome of a guarded pending-checkpoint retirement."""

    RETIRED = "retired"
    ABSENT = "absent"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CheckpointRetireResult:
    """Safe result returned by ``retire_pending`` without exposing file data."""

    status: CheckpointRetireStatus
    message: str = ""


class CheckpointOwnershipError(RuntimeError):
    """Raised when a trace is invalid or already owned by another local run."""


class CheckpointOwnership:
    """Nonblocking process-local ownership of one trace's checkpoint lifecycle."""

    __slots__ = ("_store_key", "trace_id", "_token", "_released", "expected_snapshot")

    def __init__(self, store_key: tuple[str, str], trace_id: str, token: object) -> None:
        self._store_key = store_key
        self.trace_id = trace_id
        self._token = token
        self._released = False
        self.expected_snapshot: dict | None = None

    def release(self) -> None:
        """Release the trace owner exactly once."""
        if not self._released:
            CheckpointStore._release_owner(self)

    @property
    def released(self) -> bool:
        """Whether the lease has already been released."""
        return self._released


class CheckpointStore:
    """Persist run checkpoints to disk for crash recovery.

    Checkpoints are retained up to ``max_checkpoints`` (default 20) within
    their own format version. Saves never prune files from another format, so
    legacy recovery artifacts remain untouched by new-format writes.
    """

    _ownership_lock = threading.Lock()
    _owners: dict[tuple[str, str], CheckpointOwnership] = {}

    def __init__(self, data_dir: str, max_checkpoints: int = 20) -> None:
        """Initialize the store.

        Args:
            data_dir: The agent's data directory. Checkpoints are saved in
                ``data_dir/run_checkpoints/``. The directory is created if it
                does not already exist.
            max_checkpoints: Maximum number of checkpoint files to retain.
                0 disables pruning (unlimited). Defaults to 20.
        """
        self._checkpoint_dir = os.path.join(data_dir, "run_checkpoints")
        self._checkpoint_dir = os.path.realpath(self._checkpoint_dir)
        self._max_checkpoints = max_checkpoints
        os.makedirs(self._checkpoint_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._checkpoint_dir, 0o700)
        except OSError:
            pass

    @staticmethod
    def _validate_trace_id(trace_id: str) -> bool:
        """Accept only a bounded single filename component for a trace ID."""
        return (
            isinstance(trace_id, str)
            and 1 <= len(trace_id) <= 128
            and trace_id not in {".", ".."}
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", trace_id) is not None
        )

    def _path_for(self, trace_id: str) -> str | None:
        if not self._validate_trace_id(trace_id):
            return None
        return os.path.join(self._checkpoint_dir, f"{trace_id}.json")

    def acquire_owner(self, trace_id: str) -> CheckpointOwnership | None:
        """Acquire a nonblocking process-local owner for one checkpoint trace."""
        if not self._validate_trace_id(trace_id):
            raise CheckpointOwnershipError("Checkpoint trace identity is invalid.")
        key = (self._checkpoint_dir, trace_id)
        with self._ownership_lock:
            if key in self._owners:
                return None
            owner = CheckpointOwnership(key, trace_id, object())
            self._owners[key] = owner
            return owner

    @classmethod
    def _release_owner(cls, owner: CheckpointOwnership) -> None:
        with cls._ownership_lock:
            if cls._owners.get(owner._store_key) is owner:
                del cls._owners[owner._store_key]
            owner._released = True

    def _owns(self, trace_id: str, owner: CheckpointOwnership | None) -> bool:
        if owner is None or owner.trace_id != trace_id or owner._released:
            return False
        with self._ownership_lock:
            return self._owners.get(owner._store_key) is owner and owner._store_key[0] == self._checkpoint_dir

    def _active_owner(self, trace_id: str) -> CheckpointOwnership | None:
        with self._ownership_lock:
            return self._owners.get((self._checkpoint_dir, trace_id))

    def save(
        self,
        trace_id: str,
        state_dict: dict,
        *,
        owner: CheckpointOwnership | None = None,
    ) -> bool:
        """Persist a checkpoint under caller ownership or a temporary trace lease."""
        if owner is not None:
            return self._save_with_owner(trace_id, state_dict, owner)
        try:
            temporary_owner = self.acquire_owner(trace_id)
        except CheckpointOwnershipError:
            logger.warning("Checkpoint save refused: invalid trace identity")
            return False
        if temporary_owner is None:
            logger.warning("Checkpoint save refused: trace is owned by another run")
            return False
        try:
            return self._save_with_owner(trace_id, state_dict, temporary_owner)
        finally:
            temporary_owner.release()

    def _save_with_owner(
        self,
        trace_id: str,
        state_dict: dict,
        owner: CheckpointOwnership,
    ) -> bool:
        """Atomically persist a checkpoint for ``trace_id``.

        The state is written to a temporary file in the checkpoint directory,
        then renamed with ``os.replace`` so readers always see a complete
        file. OSError during write is caught, logged as a warning, and not
        re-raised.

        Args:
            trace_id: Unique run identifier used as the file base name.
            state_dict: Checkpoint payload to persist.
        """
        path = self._path_for(trace_id)
        if path is None:
            logger.warning("Checkpoint save refused: invalid trace identity")
            return False
        active_owner = self._active_owner(trace_id)
        if active_owner is not None and not self._owns(trace_id, owner):
            logger.warning("Checkpoint save refused: trace is owned by another run")
            return False
        if not self._owns(trace_id, owner):
            logger.warning("Checkpoint save refused: invalid trace owner")
            return False
        tmp_path = f"{path}.tmp"
        try:
            try:
                os.lstat(path)
                exists = True
            except FileNotFoundError:
                exists = False
            except OSError:
                logger.warning("Checkpoint save refused: existing snapshot cannot be inspected")
                return False
            if exists:
                current = self._read_supported_snapshot(path, trace_id)
                if current is None or owner.expected_snapshot != current:
                    logger.warning("Checkpoint save refused: existing snapshot is protected")
                    return False
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(state_dict, tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, path)
            owner.expected_snapshot = deepcopy(state_dict)
            self._prune_old_checkpoints(state_dict.get("format_version"))
            return True
        except (OSError, TypeError) as exc:
            logger.warning("Checkpoint save failed (%s)", type(exc).__name__)
            return False
        finally:
            # Clean up leftover .tmp file if the write was interrupted
            # or os.replace failed.
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _read_supported_snapshot(path: str, trace_id: str) -> dict | None:
        """Read a supported v2 envelope, leaving malformed files untouched."""
        required = {
            "format_version", "trace_id", "user_goal", "messages", "step",
            "goal_idx", "max_steps", "json_fail_streak", "model_id",
            "attempt_state", "error_info", "created_at",
        }
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if (
                not isinstance(data, dict)
                or set(data) != required
                or type(data.get("format_version")) is not int
                or data["format_version"] != 2
                or data.get("trace_id") != trace_id
                or not isinstance(data.get("created_at"), str)
            ):
                return None
            return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def retire_pending(
        self,
        trace_id: str,
        owner: CheckpointOwnership | None = None,
        *,
        expected_snapshot: dict | None = None,
    ) -> CheckpointRetireResult:
        """Retire an exact snapshot under an existing or temporary trace lease."""
        if owner is not None:
            return self._retire_with_owner(
                trace_id, owner, expected_snapshot=expected_snapshot
            )
        try:
            temporary_owner = self.acquire_owner(trace_id)
        except CheckpointOwnershipError:
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "Checkpoint retirement is blocked by invalid ownership or identity.",
            )
        if temporary_owner is None:
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "Checkpoint retirement is blocked by an active run.",
            )
        if expected_snapshot is not None:
            temporary_owner.expected_snapshot = deepcopy(expected_snapshot)
        try:
            return self._retire_with_owner(
                trace_id, temporary_owner, expected_snapshot=expected_snapshot
            )
        finally:
            temporary_owner.release()

    def _retire_with_owner(
        self,
        trace_id: str,
        owner: CheckpointOwnership,
        *,
        expected_snapshot: dict | None = None,
    ) -> CheckpointRetireResult:
        """Retire only the exact valid v2 snapshot owned by this active run."""
        path = self._path_for(trace_id)
        if path is None or not self._owns(trace_id, owner):
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "Checkpoint retirement is blocked by invalid ownership or identity.",
            )
        expected = owner.expected_snapshot if expected_snapshot is None else expected_snapshot
        try:
            file_stat = os.lstat(path)
        except FileNotFoundError:
            if expected is not None:
                return CheckpointRetireResult(
                    CheckpointRetireStatus.BLOCKED,
                    "The saved checkpoint changed or disappeared; no model call was sent.",
                )
            return CheckpointRetireResult(CheckpointRetireStatus.ABSENT)
        except OSError:
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "The saved checkpoint could not be inspected; no model call was sent.",
            )
        if not stat.S_ISREG(file_stat.st_mode):
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "The saved checkpoint is not a regular file; no model call was sent.",
            )
        current = self._read_supported_snapshot(path, trace_id)
        if current is None or expected is None or current != expected:
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "The saved checkpoint could not be safely verified; no model call was sent.",
            )
        try:
            os.remove(path)
        except OSError:
            return CheckpointRetireResult(
                CheckpointRetireStatus.BLOCKED,
                "The saved checkpoint could not be retired; no model call was sent.",
            )
        owner.expected_snapshot = None
        return CheckpointRetireResult(CheckpointRetireStatus.RETIRED)

    def recovery_available(self, trace_id: str, owner: CheckpointOwnership | None) -> bool:
        """Return whether the owner's exact saved checkpoint is still on disk."""
        if not self._owns(trace_id, owner) or owner is None or owner.expected_snapshot is None:
            return False
        path = self._path_for(trace_id)
        current = self._read_supported_snapshot(path, trace_id) if path else None
        return current is not None and current == owner.expected_snapshot

    def _prune_old_checkpoints(self, format_version: object = None) -> None:
        """Delete oldest checkpoints when the count exceeds the retention cap.

        Supported checkpoints are sorted by ``created_at`` descending (newest
        first). Files beyond ``_max_checkpoints`` are deleted. Unknown, legacy,
        malformed, and unreadable files are preserved as operator recovery data.
        """
        if self._max_checkpoints <= 0:
            return  # 0 = unlimited

        if not os.path.isdir(self._checkpoint_dir):
            return

        # Only prune checkpoints written in the current format. Old-format files
        # are operator recovery artifacts and are never migration/deletion targets.
        entries: list[tuple[str, Optional[dict]]] = []
        for name in os.listdir(self._checkpoint_dir):
            trace_id = name[:-5] if name.endswith(".json") else ""
            if not self._validate_trace_id(trace_id):
                continue
            path = os.path.join(self._checkpoint_dir, name)
            try:
                data = self._read_supported_snapshot(path, trace_id)
                if (
                    data is None
                    or type(format_version) is not int
                    or data["format_version"] != format_version
                ):
                    continue
                entries.append((path, data))
            except OSError:
                # Transient I/O error — skip this file, don't delete it
                continue

        # Sort: valid checkpoints by created_at descending, corrupted files last
        entries.sort(
            key=lambda entry: entry[1].get("created_at", "") if entry[1] is not None else "",
            reverse=True,
        )

        # Reacquire each candidate trace and revalidate its exact inventory
        # snapshot immediately before unlinking; a new run may have started
        # after the initial inventory was built.
        for i, (path, data) in enumerate(entries):
            if i < self._max_checkpoints or data is None:
                continue
            trace_id = os.path.basename(path)[:-5]
            try:
                prune_owner = self.acquire_owner(trace_id)
            except CheckpointOwnershipError:
                continue
            if prune_owner is None:
                continue
            try:
                current = self._read_supported_snapshot(path, trace_id)
                if (
                    current is None
                    or current != data
                    or current.get("format_version") != format_version
                ):
                    continue
                try:
                    os.remove(path)
                    logger.info("Pruned old checkpoint: %s", os.path.basename(path))
                except OSError:
                    pass
            finally:
                prune_owner.release()

    def load(
        self,
        trace_id: str,
        *,
        owner: CheckpointOwnership | None = None,
    ) -> Optional[dict]:
        """Load a checkpoint for ``trace_id``.

        Args:
            trace_id: Unique run identifier used as the file base name.

        Returns:
            The parsed checkpoint dictionary, or ``None`` if the file does not
            exist or is corrupted (invalid JSON or missing required key).
        """
        path = self._path_for(trace_id)
        if path is None:
            return None
        active_owner = self._active_owner(trace_id)
        if active_owner is not None and not self._owns(trace_id, owner):
            return None
        if owner is not None and not self._owns(trace_id, owner):
            return None
        if not os.path.exists(path):
            return None
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                if not isinstance(data, dict):
                    return None
                _ = data["created_at"]
                if owner is not None:
                    owner.expected_snapshot = deepcopy(data)
                return data
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
            return None

    def delete(self, trace_id: str, *, owner: CheckpointOwnership | None = None) -> None:
        """Delete the checkpoint for ``trace_id`` if it exists.

        Args:
            trace_id: Unique run identifier used as the file base name.
        """
        path = self._path_for(trace_id)
        if path is None:
            return
        if owner is not None:
            if not self._owns(trace_id, owner):
                raise CheckpointOwnershipError("Checkpoint deletion is blocked by active ownership.")
            result = self.retire_pending(trace_id, owner)
            if result.status is CheckpointRetireStatus.BLOCKED:
                raise CheckpointOwnershipError(result.message)
            return
        try:
            temporary_owner = self.acquire_owner(trace_id)
        except CheckpointOwnershipError:
            return
        if temporary_owner is None:
            raise CheckpointOwnershipError("Checkpoint deletion is blocked by active ownership.")
        try:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        finally:
            temporary_owner.release()

    def list(self) -> list[dict]:
        """List all valid checkpoints sorted by ``created_at`` descending.

        Returns:
            A list of parsed checkpoint dictionaries, newest first. Corrupted
            files are skipped. Returns an empty list if the checkpoint
            directory does not exist.
        """
        if not os.path.isdir(self._checkpoint_dir):
            return []

        checkpoints: list[dict] = []
        for name in os.listdir(self._checkpoint_dir):
            trace_id = name[:-5] if name.endswith(".json") else ""
            if not self._validate_trace_id(trace_id):
                continue
            path = os.path.join(self._checkpoint_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    if not isinstance(data, dict):
                        continue
                    _ = data["created_at"]
                    checkpoints.append(data)
            except (OSError, json.JSONDecodeError, KeyError):
                continue

        checkpoints.sort(
            key=lambda checkpoint: checkpoint.get("created_at", ""),
            reverse=True,
        )
        return checkpoints
