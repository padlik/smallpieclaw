"""Contract that the superseded model-runtime paths stay removed (remove-obsolete-model-paths).

The removals are deliberate breaking API changes with no wrappers or shims, so
each check asserts absence of the old name next to the live replacement.
"""

from __future__ import annotations

import pytest

import backfill_graph_memory
import graph_memory
from interfaces import LLMProvider
from llm_client import LLMClient
from model_registry import ModelRegistry


class TestGraphHelperChainRemoved:
    """``resolve_graph_startup`` is the sole graph-prerequisite entry point."""

    def test_catalog_pinned_graph_helpers_are_gone(self) -> None:
        assert not hasattr(ModelRegistry, "resolve_graph_model")
        assert not hasattr(graph_memory, "resolve_graph_prerequisites")
        assert not hasattr(graph_memory, "_probe_embeddings_with_client")
        assert not hasattr(backfill_graph_memory, "prepare_backfill_prerequisites")

    def test_explicit_startup_entry_points_remain(self) -> None:
        assert callable(graph_memory.resolve_graph_startup)
        assert callable(graph_memory._require_embeddings)
        assert hasattr(graph_memory, "GraphPrerequisites")
        assert hasattr(graph_memory, "GraphPrerequisiteError")


class TestFallbackNamedAliasesRemoved:
    """``chat`` and ``chat_with_tools`` are the only chat interface."""

    @pytest.mark.parametrize("owner", [LLMProvider, LLMClient], ids=["protocol", "client"])
    def test_aliases_are_absent(self, owner: type) -> None:
        assert not hasattr(owner, "chat_with_fallback")
        assert not hasattr(owner, "chat_with_tools_fallback")

    @pytest.mark.parametrize("owner", [LLMProvider, LLMClient], ids=["protocol", "client"])
    def test_canonical_chat_and_embedding_methods_remain(self, owner: type) -> None:
        for name in ("chat", "chat_with_tools", "embed", "embed_batch"):
            assert callable(getattr(owner, name)), name


class TestEmptyResponseDiagnosticsRemoved:
    """The curl diagnostics helper, its config field and provider plumbing are gone."""

    def test_helper_config_field_and_context_field_are_absent(self) -> None:
        import dataclasses

        import providers._utils
        from config_schema import AgentConfig
        from interfaces import ProviderContext

        assert not hasattr(providers._utils, "diagnose_empty_response")
        assert "diagnose_empty_responses" not in {f.name for f in dataclasses.fields(AgentConfig)}
        assert "diagnose_empty" not in {f.name for f in dataclasses.fields(ProviderContext)}


class TestReactDefinitionHashRemoved:
    """The unread ReAct definition digest is gone; token-estimator revision keys stay."""

    def test_react_hash_helper_is_absent_and_estimator_revision_key_remains(self) -> None:
        import inspect

        import react_loop
        from token_estimator import estimate_request_payload_tokens

        assert not hasattr(react_loop, "_definition_revision")
        assert "definition_revision" in inspect.signature(estimate_request_payload_tokens).parameters
