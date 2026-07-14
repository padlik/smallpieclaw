"""Memory built-in tools: memory_write and the graph-memory search/store pair.

Handler module: ``MemoryTools`` holds a back-reference to the ``BuiltinExecutor``
façade (``owner``) and reads the late-bound collaborators (``_memory``,
``_graph_memory``, ``_graph_memory_writer``) through it at call time — they are
wired onto the executor after construction, so they must never be snapshotted.
Confirmation is staged only through ``owner._requires_confirmation`` (Decision 8
seam constraint). The ``graph_memory`` import is kept function-local to avoid an
import cycle; the ``builtin_executor`` import is under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)


class MemoryTools:
    """Persistent-memory and graph-memory tool handlers."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_memory_write(self, args: dict, caller_tag: str = "") -> dict:
        """Read or update persistent MemoryStore (data/memory.json)."""
        if self._owner._memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_write: MemoryStore is not available in this context.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Memory storage is disabled in this runtime; do not rely on memory_write.",
            }

        action = args.get("action", "").strip().lower()
        key = args.get("key", "").strip()

        import json as _json

        def _ok(out: str) -> dict:
            return {
                "success": True,
                "output": out,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }

        def _err(msg: str, error_type: str = "", suggestion: str = "") -> dict:
            return {
                "success": False,
                "output": "",
                "error": msg,
                "exit_code": -1,
                "error_type": error_type or "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": suggestion,
            }

        if action == "get":
            if not key:
                return _err("memory_write get: 'key' is required.")
            value = self._owner._memory.get(key)
            return _ok(_json.dumps(value))

        if not key:
            return _err("memory_write: 'key' is required.")

        if action == "set":
            value = args.get("value")
            # Guard against LLM pre-serializing the value as a JSON string.
            # e.g. value="{\"count\":7}" → stored as {"count": 7} not a raw string.
            if isinstance(value, str):
                try:
                    parsed = _json.loads(value)
                    # Only replace if it decoded to a non-string type (object, list, number, bool, None)
                    if not isinstance(parsed, str):
                        logger.warning(
                            "memory_write set key=%s: value was a JSON string — auto-parsed to %s",
                            key, type(parsed).__name__,
                        )
                        value = parsed
                except _json.JSONDecodeError:
                    pass  # Keep original string value
            self._owner._memory.set(key, value)
            logger.info("memory_write set: key=%s type=%s", key, type(value).__name__)
            return _ok(f"Memory key '{key}' updated.")

        elif action == "append":
            value = args.get("value")
            current = self._owner._memory.get(key)
            if not isinstance(current, list):
                current = []
            current.append(value)
            self._owner._memory.set(key, current)
            logger.info("memory_write append: key=%s (now %d items)", key, len(current))
            return _ok(f"Appended to '{key}' ({len(current)} items total).")

        elif action == "delete":
            self._owner._memory.delete(key)
            logger.info("memory_write delete: key=%s", key)
            return _ok(f"Memory key '{key}' deleted.")

        else:
            return _err(
                f"memory_write: unknown action '{action}'. Valid: set, append, delete, get.",
                error_type="fundamentally_wrong_approach",
                suggestion="Use one of: set, append, delete, get.",
            )

    # ---- memory_graph_search ----

    def _exec_memory_graph_search(self, args: dict, caller_tag: str = "") -> dict:
        if self._owner._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_search: graph memory is not enabled or not available. "
                         "Set [graph_memory] enabled = true in config.toml and install ladybug.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        query = str(args.get("query", "")).strip()
        if not query:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_search: 'query' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a non-empty query string.",
            }
        try:
            context = self._owner._graph_memory.format_for_prompt(query)
            if not context:
                return {
                    "success": True,
                    "output": "No relevant entities or facts found in graph memory.",
                    "error": "",
                    "exit_code": 0,
                    "error_type": "",
                    "recoverable": False,
                    "suggestion": "",
                }
            logger.info("memory_graph_search: query=%s", query[:60])
            return {
                "success": True,
                "output": context,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "output": "",
                "error": f"memory_graph_search failed: {exc}",
                "exit_code": -1,
                "error_type": "network_error",
                "recoverable": True,
                "suggestion": "Retry the graph memory search; the database may be temporarily unavailable.",
            }

    # ---- memory_graph_store ----

    def _exec_memory_graph_store(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        if self._owner._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_store: graph memory is not enabled or not available. "
                         "Set [graph_memory] enabled = true in config.toml and install ladybug.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        content = str(args.get("content", "")).strip()
        if not content:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_store: 'content' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the fact or relationship you want to store.",
            }
        # Writing to graph memory changes future recalled prompt context, so it is
        # a confirmation-requiring operation. Operator approval admits the memory
        # as "confirmed"; the model/sub-agent cannot self-approve it.
        preview = content if len(content) <= 200 else content[:200] + "…"
        desc = (
            "Store this in graph memory as a *confirmed* fact "
            "(it will influence future recalled context):\n"
            f"`{preview}`"
        )
        return self._owner._requires_confirmation(
            "memory_graph_store", args, desc, caller_depth=caller_depth, caller_tag=caller_tag
        )

    def _run_memory_graph_store(self, args: dict, caller_tag: str = "") -> dict:
        """Execute a confirmed graph-memory store. Only reached after operator approval."""
        from graph_memory import ADMISSION_CONFIRMED, CONFIDENCE_CONFIRMED

        if self._owner._graph_memory is None:
            return {
                "success": False, "output": "",
                "error": "memory_graph_store: graph memory is not enabled or not available.",
                "exit_code": -1,
                "error_type": "impossible_with_current_tools",
                "recoverable": False,
                "suggestion": "Graph memory is disabled or ladybug is not installed; do not retry.",
            }
        content = str(args.get("content", "")).strip()
        if not content:
            return {
                "success": False,
                "output": "",
                "error": "memory_graph_store: 'content' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the fact or relationship you want to store.",
            }
        user_id = str(args.get("user_id", "agent")).strip() or "agent"
        try:
            # Store the operator-approved note as a confirmed episode.
            ep_id = self._owner._graph_memory.add_episode(
                content,
                user_id=user_id,
                source="manual",
                admission_status=ADMISSION_CONFIRMED,
                confidence=CONFIDENCE_CONFIRMED,
            )
            # Derived relations from background extraction remain "observed"; only
            # the explicitly approved note itself is confirmed.
            if self._owner._graph_memory_writer is not None:
                self._owner._graph_memory_writer.enqueue(content, user_id=user_id, source="manual")
                self._owner._graph_memory_writer.flush()
            logger.info("memory_graph_store: stored confirmed episode %s", ep_id)
            return {
                "success": True,
                "output": f"Stored in graph memory as confirmed (episode {ep_id}). "
                          "Extraction scheduled in background.",
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "output": "",
                "error": f"memory_graph_store failed: {exc}",
                "exit_code": -1,
                "error_type": "network_error",
                "recoverable": True,
                "suggestion": "Retry the graph memory store; the database may be temporarily unavailable.",
            }
