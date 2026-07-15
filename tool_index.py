"""
tool_index.py
-------------
Semantic tool index: embeds tool descriptions and supports cosine-similarity
search to find the most relevant tools for a given natural-language query.

Vectors are persisted in data/tool_index.json to avoid re-embedding on restart.
Heavy ML libraries (numpy, faiss, etc.) are intentionally avoided.

Built-in tools (shell, file_read, file_write, schedule) are indexed alongside
registered tools so the semantic search considers them for relevance scoring.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import httpx

from llm_client import LLMClient, LLMError
from tool_registry import Tool, ToolRegistry
from vector_utils import cosine_similarity

logger = logging.getLogger(__name__)

_EMBEDDING_ERRORS = (
    LLMError,
    httpx.HTTPError,
    json.JSONDecodeError,
    KeyError,
    ValueError,
)
_JSON_INDEX_LOAD_ERRORS = (OSError, json.JSONDecodeError)


def _builtin_as_tool(name: str, description: str) -> Tool:
    """Wrap a built-in tool description as a Tool object for uniform handling."""
    return Tool(name=name, path="", language="builtin", description=description)


class ToolIndex:
    """
    Maintains an embedding-based index over all tools: registered (file-based)
    and built-in (shell, file_read, file_write, schedule).

    Workflow:
        1. On startup, load persisted vectors from disk.
        2. Embed any tools not yet in the index (or whose description changed).
        3. For each query, embed the query and rank ALL tools by cosine similarity.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm: LLMClient,
        index_path: str,
        builtin_executor=None,   # Optional[BuiltinExecutor] — avoids circular import
    ):
        self.registry = registry
        self.llm = llm
        self.index_path = index_path
        self.builtin_executor = builtin_executor
        # { tool_name: {"description": str, "vector": list[float]} }
        self._index: dict[str, dict] = {}
        self._lock = threading.RLock()  # guards _index for concurrent rebuild/search
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> None:
        """
        Ensure all currently registered and built-in tools have embeddings.
        Skips tools already in the index (no description change detected).
        Persists updated index to disk.
        """
        registered_tools = list(self.registry.all())
        builtin_tools = self._builtin_tools()
        total = len(registered_tools) + len(builtin_tools)
        logger.info(
            "Building tool index: %d registered + %d built-in tools",
            len(registered_tools),
            len(builtin_tools),
        )

        changed = False
        embedded = 0
        skipped = 0
        failed = 0

        for tool in registered_tools:
            result = self._embed_if_needed(tool.name, tool.description)
            changed |= result
            if result:
                embedded += 1
            else:
                skipped += 1

        for bt in builtin_tools:
            result = self._embed_if_needed(bt.name, bt.description)
            changed |= result
            if result:
                embedded += 1
            else:
                skipped += 1

        # Remove stale entries for tools no longer present
        valid_names = {t.name for t in registered_tools} | {bt.name for bt in builtin_tools}
        with self._lock:
            stale = [n for n in self._index if n not in valid_names]
            for name in stale:
                del self._index[name]
                changed = True

        logger.info(
            "Tool index build complete: %d/%d new embeddings, %d cached, %d failed, %d stale removed",
            embedded,
            total,
            skipped,
            failed,
            len(stale) if changed else 0,
        )
        if changed:
            self._save()

    def rebuild(self) -> dict:
        """
        Force re-embed ALL tools (registered + built-in), ignoring cached vectors.
        Refreshes the registry first, then re-indexes everything from scratch.
        Returns a summary dict: {total, embedded, failed, removed}.
        """
        new_index: dict[str, dict] = {}
        embedded = 0
        failed = 0

        all_tools = list(self.registry.all()) + self._builtin_tools()
        for tool in all_tools:
            try:
                vector = self.llm.embed(tool.description)
                new_index[tool.name] = {
                    "description": tool.description,
                    "vector": vector,
                }
                embedded += 1
            except _EMBEDDING_ERRORS as exc:
                logger.error("rebuild: failed to embed '%s': %s", tool.name, exc)
                failed += 1

        with self._lock:
            self._index = new_index

        self._save()
        total = embedded + failed
        logger.info("Tool index rebuilt: %d embedded, %d failed", embedded, failed)
        return {"total": total, "embedded": embedded, "failed": failed, "removed": 0}

    def add_tool(self, tool: Tool) -> None:
        """Embed and index a single newly created tool."""
        try:
            vector = self.llm.embed(tool.description)
            with self._lock:
                self._index[tool.name] = {
                    "description": tool.description,
                    "vector": vector,
                }
            self._save()
            logger.info("Tool '%s' added to semantic index", tool.name)
        except _EMBEDDING_ERRORS as exc:
            logger.error("Failed to index tool '%s': %s", tool.name, exc)

    def search(self, query: str, top_k: int = 3) -> list[Tool]:
        """
        Return the top-k most semantically relevant tools for a query.
        Results may include both registered and built-in tools.
        """
        with self._lock:
            if not self._index:
                logger.warning("Tool index is empty — returning all registered tools")
                return self.registry.all()[:top_k]
            index_snapshot = dict(self._index)

        try:
            query_vec = self.llm.embed(query)
        except _EMBEDDING_ERRORS as exc:
            logger.error("Failed to embed query: %s — falling back to all tools", exc)
            return self.registry.all()[:top_k]

        # Build a name→Tool lookup that covers both registered and built-in tools
        tool_lookup: dict[str, Tool] = {t.name: t for t in self.registry.all()}
        for bt in self._builtin_tools():
            tool_lookup[bt.name] = bt

        scores: list[tuple[float, str]] = []
        for name, entry in index_snapshot.items():
            if name not in tool_lookup:
                continue
            sim = cosine_similarity(query_vec, entry["vector"])
            scores.append((sim, name))

        scores.sort(reverse=True)
        results: list[Tool] = []
        for _, name in scores[:top_k]:
            t = tool_lookup.get(name)
            if t:
                results.append(t)

        logger.debug(
            "Semantic search for '%s' → %s",
            query[:60],
            [t.name for t in results],
        )
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _builtin_tools(self) -> list[Tool]:
        """Return built-in tools as Tool objects if a builtin_executor is wired in."""
        if self.builtin_executor is None:
            return []
        return [
            _builtin_as_tool(bt.name, bt.description)
            for bt in self.builtin_executor.all_tools()
        ]

    def _embed_if_needed(self, name: str, description: str) -> bool:
        """Embed a tool if missing or description changed. Returns True if index was updated."""
        existing = self._index.get(name)
        if existing is not None and existing.get("description") == description:
            logger.debug("Tool '%s' already indexed — skipping embed", name)
            return False
        logger.info("Embedding tool: %s", name)
        try:
            vector = self.llm.embed(description)
            with self._lock:
                self._index[name] = {"description": description, "vector": vector}
            return True
        except _EMBEDDING_ERRORS as exc:
            logger.error("Failed to embed tool '%s': %s", name, exc)
            return False

    def _load(self) -> None:
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r") as f:
                    self._index = json.load(f)
                logger.info("Tool index loaded from %s (%d entries)", self.index_path, len(self._index))
            except _JSON_INDEX_LOAD_ERRORS as exc:
                logger.warning("Could not load tool index: %s — starting empty", exc)
                self._index = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.index_path) or ".", exist_ok=True)
        tmp = f"{self.index_path}.tmp.{os.getpid()}.{id(self)}"
        try:
            with self._lock:
                snapshot = dict(self._index)
            with open(tmp, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, self.index_path)
            logger.debug("Tool index saved: %d entries", len(snapshot))
        except OSError as exc:
            logger.error("Could not save tool index: %s", exc)
