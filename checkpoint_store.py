"""Disk-based checkpoint store for crash recovery of agent runs."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class CheckpointStore:
    """Persist run checkpoints to disk for crash recovery.

    Checkpoints are retained up to ``max_checkpoints`` (default 20). When
    the cap is exceeded, the oldest checkpoints are pruned on each ``save()``
    call. Corrupted checkpoint files are also removed during pruning.
    """

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
        self._max_checkpoints = max_checkpoints
        os.makedirs(self._checkpoint_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._checkpoint_dir, 0o700)
        except OSError:
            pass

    def save(self, trace_id: str, state_dict: dict) -> None:
        """Atomically persist a checkpoint for ``trace_id``.

        The state is written to a temporary file in the checkpoint directory,
        then renamed with ``os.replace`` so readers always see a complete
        file. OSError during write is caught, logged as a warning, and not
        re-raised.

        Args:
            trace_id: Unique run identifier used as the file base name.
            state_dict: Checkpoint payload to persist.
        """
        path = os.path.join(self._checkpoint_dir, f"{trace_id}.json")
        tmp_path = f"{path}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(state_dict, tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, path)
            self._prune_old_checkpoints()
        except (OSError, TypeError) as exc:
            logger.warning("Failed to save checkpoint for %s: %s", trace_id, exc)
        finally:
            # Clean up leftover .tmp file if the write was interrupted
            # or os.replace failed.
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _prune_old_checkpoints(self) -> None:
        """Delete oldest checkpoints when the count exceeds the retention cap.

        Checkpoints are sorted by ``created_at`` descending (newest first).
        Files beyond ``_max_checkpoints`` are deleted. Corrupted files that
        cannot be parsed are also removed (they serve no recovery purpose).
        """
        if self._max_checkpoints <= 0:
            return  # 0 = unlimited

        if not os.path.isdir(self._checkpoint_dir):
            return

        # Collect all .json files with their parsed data (or None if corrupted)
        entries: list[tuple[str, Optional[dict]]] = []
        for name in os.listdir(self._checkpoint_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._checkpoint_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    _ = data["created_at"]
                    entries.append((path, data))
            except (json.JSONDecodeError, KeyError):
                # Corrupted file — mark for deletion
                entries.append((path, None))
            except OSError:
                # Transient I/O error — skip this file, don't delete it
                continue

        # Sort: valid checkpoints by created_at descending, corrupted files last
        entries.sort(
            key=lambda entry: entry[1].get("created_at", "") if entry[1] is not None else "",
            reverse=True,
        )

        # Delete entries beyond the cap + all corrupted files
        for i, (path, data) in enumerate(entries):
            if data is None:
                # Corrupted — always delete
                try:
                    os.remove(path)
                except OSError:
                    pass
            elif i >= self._max_checkpoints:
                # Beyond retention cap — delete
                try:
                    os.remove(path)
                    logger.info("Pruned old checkpoint: %s", os.path.basename(path))
                except OSError:
                    pass

    def load(self, trace_id: str) -> Optional[dict]:
        """Load a checkpoint for ``trace_id``.

        Args:
            trace_id: Unique run identifier used as the file base name.

        Returns:
            The parsed checkpoint dictionary, or ``None`` if the file does not
            exist or is corrupted (invalid JSON or missing required key).
        """
        path = os.path.join(self._checkpoint_dir, f"{trace_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                _ = data["created_at"]
                return data
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def delete(self, trace_id: str) -> None:
        """Delete the checkpoint for ``trace_id`` if it exists.

        Args:
            trace_id: Unique run identifier used as the file base name.
        """
        path = os.path.join(self._checkpoint_dir, f"{trace_id}.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

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
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._checkpoint_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    _ = data["created_at"]
                    checkpoints.append(data)
            except (OSError, json.JSONDecodeError, KeyError):
                continue

        checkpoints.sort(
            key=lambda checkpoint: checkpoint.get("created_at", ""),
            reverse=True,
        )
        return checkpoints
