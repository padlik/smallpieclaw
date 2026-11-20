"""Shared test fixture: a real ``ModelCache`` and ``ModelPreference`` pair.

Production wires the disposable discovery cache (``ModelCache``) and the
durable deliberate selection (``ModelPreference``) as two separate files.
Tests that need a real ``ModelRegistry`` build that same pair here instead of
mocking the stores, so persistence behaves exactly as in production.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_registry import CatalogDocument, ModelRegistry
from model_settings import ModelCache, ModelPreference


@dataclass(frozen=True)
class ModelStores:
    """A real cache file and preference file under one directory."""

    cache: ModelCache
    preference: ModelPreference

    @classmethod
    def at(cls, directory: Path, stem: str = "models") -> ModelStores:
        """Bind ``<stem>-cache.json`` and ``<stem>-selection.toml`` below ``directory``."""
        return cls(
            ModelCache(Path(directory) / f"{stem}-cache.json"),
            ModelPreference(Path(directory) / f"{stem}-selection.toml"),
        )

    def seed(self, document: CatalogDocument, selected: str | None = None) -> ModelStores:
        """Write the cache document and, when given, the saved selection."""
        self.cache.write(document)
        if selected is not None:
            self.preference.write(selected)
        return self

    def registry(
        self,
        connections: Mapping[str, Any],
        discovery: Any,
        resolver: Callable[..., Any],
        **kwargs: Any,
    ) -> ModelRegistry:
        """Build a real registry over this cache and preference."""
        return ModelRegistry(connections, self.cache, discovery, resolver, preference=self.preference, **kwargs)

    def snapshot(self) -> tuple[bytes | None, bytes | None]:
        """Return the raw cache and preference bytes (``None`` when absent) for no-write checks."""
        def raw(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        return raw(self.cache.path), raw(self.preference.path)

    def selected(self) -> str | None:
        """Return the saved selector as re-read from disk (``None`` if missing/corrupt)."""
        return self.preference.load().model
