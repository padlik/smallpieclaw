"""
tool_index.py
-------------
Semantic tool index: embeds tool descriptions and supports cosine-similarity
search to find the most relevant tools for a given natural-language query.

Vectors are persisted in data/tool_index.json to avoid re-embedding on restart.
Heavy ML libraries (numpy, faiss, etc.) are intentionally avoided.
"""

import json
import logging
import os
from typing import Optional

from llm_client import LLMClient
from tool_registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)


class ToolIndex:
    """
    Maintains an embedding-based index over registered tools.

    Workflow:
        1. On startup, load persisted vectors from disk.
        2. Embed any tools not yet in the index.
        3. For each query, embed the query and rank tools by cosine similarity.
    """

    def __init__(self, registry: ToolRegistry, llm: LLMClient, index_path: str):
        self.registry = registry
        self.llm = llm
        self.index_path = index_path
        # { tool_name: {"description": str, "vector": list[float]} }
        self._index: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> None:
        """
        Ensure all currently registered tools have embeddings.
        Skips tools already in the index (no description change detected).
        Persists updated index to disk.
        """
        changed = False
        for tool in self.registry.all():
            existing = self._index.get(tool.name)
            # Re-embed if missing or description changed
            if existing is None or existing.get("description") != tool.description:
                logger.info("Embedding tool: %s", tool.name)
                try:
                    vector = self.llm.embed(tool.description)
                    self._index[tool.name] = {
                        "description": tool.description,
                        "vector": vector,
                    }
                    changed = True
                except Exception as exc:
                    logger.error("Failed to embed tool '%s': %s", tool.name, exc)

        # Remove stale entries for tools no longer registered
        registered_names = {t.name for t in self.registry.all()}
        stale = [n for n in self._index if n not in registered_names]
        for name in stale:
            del self._index[name]
            changed = True

        if changed:
            self._save()

    def add_tool(self, tool: Tool) -> None:
        """Embed and index a single newly created tool."""
        try:
            vector = self.llm.embed(tool.description)
            self._index[tool.name] = {
                "description": tool.description,
                "vector": vector,
            }
            self._save()
            logger.info("Tool '%s' added to semantic index", tool.name)
        except Exception as exc:
            logger.error("Failed to index tool '%s': %s", tool.name, exc)

    def search(self, query: str, top_k: int = 3) -> list[Tool]:
        """
        Return the top-k most semantically relevant tools for a query.
        Returns fewer results if fewer tools are indexed.
        """
        if not self._index:
            logger.warning("Tool index is empty — returning all registered tools")
            return self.registry.all()[:top_k]

        try:
            query_vec = self.llm.embed(query)
        except Exception as exc:
            logger.error("Failed to embed query: %s — falling back to all tools", exc)
            return self.registry.all()[:top_k]

        scores: list[tuple[float, str]] = []
        for name, entry in self._index.items():
            tool = self.registry.get(name)
            if tool is None:
                continue
            sim = self.llm.cosine_similarity(query_vec, entry["vector"])
            scores.append((sim, name))

        scores.sort(reverse=True)
        results: list[Tool] = []
        for _, name in scores[:top_k]:
            t = self.registry.get(name)
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

    def _load(self) -> None:
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r") as f:
                    self._index = json.load(f)
                logger.debug("Tool index loaded: %d entries", len(self._index))
            except Exception as exc:
                logger.warning("Could not load tool index: %s — starting empty", exc)
                self._index = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.index_path) or ".", exist_ok=True)
        tmp = self.index_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._index, f, indent=2)
            os.replace(tmp, self.index_path)
            logger.debug("Tool index saved: %d entries", len(self._index))
        except Exception as exc:
            logger.error("Could not save tool index: %s", exc)
