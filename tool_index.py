"""
tool_index.py — Semantic search index over registered tools.

Primary:  vector embeddings via LLM API + cosine similarity
Fallback: simple TF-IDF when EMBEDDING_PROVIDER=none
"""

from __future__ import annotations
import json
import logging
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import config
import llm_client
from tool_registry import Tool, ToolRegistry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── cosine similarity (no numpy required) ─────────────────────────────────────

def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def cosine(a: list[float], b: list[float]) -> float:
    denom = _norm(a) * _norm(b)
    return _dot(a, b) / denom if denom else 0.0


# ── TF-IDF fallback ───────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class _TfIdfIndex:
    """Minimal in-memory TF-IDF index as a fallback when embeddings are off."""

    def __init__(self):
        self._docs: list[tuple[str, list[str]]] = []  # (tool_name, tokens)
        self._idf: dict[str, float] = {}

    def build(self, tools: list[Tool]) -> None:
        self._docs = [(t.name, _tokenize(t.description)) for t in tools]
        df: dict[str, int] = defaultdict(int)
        for _, tokens in self._docs:
            for tok in set(tokens):
                df[tok] += 1
        n = len(self._docs) or 1
        self._idf = {tok: math.log((n + 1) / (cnt + 1)) + 1 for tok, cnt in df.items()}

    def query(self, text: str, top_k: int = 3) -> list[str]:
        q_tokens = _tokenize(text)
        scores: dict[str, float] = {}
        for name, tokens in self._docs:
            score = sum(self._idf.get(tok, 0) for tok in q_tokens if tok in tokens)
            scores[name] = score
        ranked = sorted(scores, key=lambda n: scores[n], reverse=True)
        return ranked[:top_k]


# ── main index ────────────────────────────────────────────────────────────────

class ToolIndex:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        self._index: list[dict] = []   # [{tool, description, embedding}]
        self._tfidf = _TfIdfIndex()
        self._use_embeddings = (config.EMBEDDING_PROVIDER != "none")

    # ── build ──────────────────────────────────────────────────────────────────

    def build(self) -> None:
        tools = self._registry.all_tools()
        if not tools:
            logger.warning("No tools to index.")
            self._index = []
            self._save()
            return

        self._tfidf.build(tools)

        if not self._use_embeddings:
            logger.info("Embedding disabled — using TF-IDF index only.")
            self._index = []
            return

        descriptions = [t.description for t in tools]
        logger.info("Generating embeddings for %d tools …", len(tools))
        try:
            vectors = llm_client.embed(descriptions)
        except Exception as e:
            logger.error("Embedding failed: %s — falling back to TF-IDF.", e)
            self._use_embeddings = False
            self._index = []
            return

        self._index = [
            {
                "tool": t.name,
                "description": t.description,
                "embedding": vec,
            }
            for t, vec in zip(tools, vectors or [])
        ]
        self._save()
        logger.info("Tool index built with %d entries.", len(self._index))

    def _save(self) -> None:
        try:
            config.TOOL_INDEX_FILE.write_text(
                json.dumps(self._index, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Could not save tool index: %s", e)

    def load(self) -> bool:
        """Load a previously saved index. Returns True if successful."""
        if not config.TOOL_INDEX_FILE.exists():
            return False
        try:
            data = json.loads(config.TOOL_INDEX_FILE.read_text(encoding="utf-8"))
            self._index = data
            # Rebuild TF-IDF in case embeddings are stale
            self._tfidf.build(self._registry.all_tools())
            logger.info("Tool index loaded (%d entries).", len(self._index))
            return True
        except Exception as e:
            logger.warning("Could not load tool index: %s", e)
            return False

    # ── query ──────────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 3) -> list[Tool]:
        """Return the top-k most relevant tools for a query."""
        if not self._use_embeddings or not self._index:
            names = self._tfidf.query(query, top_k)
            return [t for n in names if (t := self._registry.get(n))]

        try:
            vecs = llm_client.embed([query])
        except Exception as e:
            logger.warning("Query embedding failed: %s — using TF-IDF fallback.", e)
            names = self._tfidf.query(query, top_k)
            return [t for n in names if (t := self._registry.get(n))]

        if not vecs:
            return []
        q_vec = vecs[0]

        scored = [
            (entry["tool"], cosine(q_vec, entry["embedding"]))
            for entry in self._index
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        results = []
        for name, score in scored[:top_k]:
            tool = self._registry.get(name)
            if tool:
                results.append(tool)
        return results

    # ── incremental update ─────────────────────────────────────────────────────

    def add_tool(self, tool: Tool) -> None:
        """Add or update a single tool in the index."""
        # Remove old entry if exists
        self._index = [e for e in self._index if e["tool"] != tool.name]

        if self._use_embeddings:
            try:
                vecs = llm_client.embed([tool.description])
                if vecs:
                    self._index.append({
                        "tool": tool.name,
                        "description": tool.description,
                        "embedding": vecs[0],
                    })
                    self._save()
            except Exception as e:
                logger.warning("Could not embed new tool: %s", e)

        # Always rebuild TF-IDF
        self._tfidf.build(self._registry.all_tools())
