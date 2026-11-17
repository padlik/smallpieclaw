"""Tests that built-in tool metadata stays synchronized across modules."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import (
    BUILTIN_TOOL_SCHEMAS,
    PSEUDO_TOOL_SCHEMAS,
    build_tool_definitions,
)

SPAWN_PUBLIC_PROPERTIES = {"task", "model", "response_format", "context_payload", "context_key"}
OBSOLETE_SPAWN_KEYS = ("max_tokens", "temperature", "top_p")


def test_builtin_tool_schemas_match_descriptors() -> None:
    """Every tool listed in descriptors.py must have a schema and vice versa."""
    schema_keys = set(BUILTIN_TOOL_SCHEMAS.keys())
    descriptor_keys = set(BUILTIN_TOOLS.keys())
    assert schema_keys == descriptor_keys, (
        f"BUILTIN_TOOL_SCHEMAS and BUILTIN_TOOLS keys differ:\n"
        f"  Only in schemas: {schema_keys - descriptor_keys}\n"
        f"  Only in descriptors: {descriptor_keys - schema_keys}"
    )


# ---------------------------------------------------------------------------
# C1: public spawn_agent argument migration (descriptor + fallback schema)
# ---------------------------------------------------------------------------

def _spawn_schema() -> dict:
    return BUILTIN_TOOL_SCHEMAS["spawn_agent"]


def _descriptor_arg_names(description: str) -> set[str]:
    """Names listed in the ``Args:`` block (two-space-indented ``name (type`` lines)."""
    return set(re.findall(r"^  (\w+)\s+\(", description, flags=re.MULTILINE))


def test_spawn_schema_exposes_exactly_the_public_properties() -> None:
    props = _spawn_schema()["parameters"]["properties"]
    assert set(props) == SPAWN_PUBLIC_PROPERTIES
    assert _spawn_schema()["parameters"]["required"] == ["task"]
    for key in OBSOLETE_SPAWN_KEYS:
        assert key not in props


def test_spawn_descriptor_args_match_schema_properties() -> None:
    description = BUILTIN_TOOLS["spawn_agent"].description
    assert _descriptor_arg_names(description) == SPAWN_PUBLIC_PROPERTIES
    assert _descriptor_arg_names(description) == set(_spawn_schema()["parameters"]["properties"])


@pytest.mark.parametrize("key", OBSOLETE_SPAWN_KEYS)
def test_obsolete_generation_keys_absent_from_all_spawn_metadata(key: str) -> None:
    descriptor_text = BUILTIN_TOOLS["spawn_agent"].description
    schema_text = json.dumps(_spawn_schema())
    assert key not in descriptor_text
    assert key not in schema_text


def test_spawn_model_wording_uses_models_command_and_active_main_inheritance() -> None:
    schema_model = _spawn_schema()["parameters"]["properties"]["model"]
    assert schema_model["type"] == "string"
    for text in (schema_model["description"], BUILTIN_TOOLS["spawn_agent"].description):
        lowered = text.lower()
        assert "background_model" not in lowered
        assert "available models" not in lowered
        assert "/models" in text
        assert "omit" in lowered
        assert "main" in lowered


def test_spawn_descriptor_example_uses_only_public_keys_and_no_legacy_alias() -> None:
    description = BUILTIN_TOOLS["spawn_agent"].description
    assert "kimi-k2.5:cloud" not in description
    match = re.search(r'^\{"task".*\}$', description, flags=re.MULTILINE)
    assert match is not None, "spawn_agent must retain a public-argument JSON example"
    example = json.loads(match.group(0))
    assert set(example) <= SPAWN_PUBLIC_PROPERTIES
    assert "task" in example


def test_builtin_and_plan_inventory_preserved() -> None:
    assert len(BUILTIN_TOOLS) == 23
    assert set(BUILTIN_TOOL_SCHEMAS) == set(BUILTIN_TOOLS)
    assert "plan" in PSEUDO_TOOL_SCHEMAS
    names = [t["function"]["name"] for t in build_tool_definitions()]
    assert set(names) == set(BUILTIN_TOOLS) | {"plan"}
    assert len(names) == len(set(names)) == 24


def test_built_definitions_expose_public_spawn_schema_and_keep_mcp_tools() -> None:
    mcp_schema = {
        "type": "object",
        "properties": {"max_tokens": {"type": "integer"}, "q": {"type": "string"}},
    }
    mcp_manager = SimpleNamespace(
        get_tools=lambda: [
            SimpleNamespace(name="mcp_search", description="MCP tool", input_schema=mcp_schema)
        ]
    )
    definitions = {t["function"]["name"]: t["function"] for t in build_tool_definitions(mcp_manager)}
    assert set(definitions["spawn_agent"]["parameters"]["properties"]) == SPAWN_PUBLIC_PROPERTIES
    # MCP definitions are opaque and untouched by the spawn migration.
    assert definitions["mcp_search"]["parameters"] == mcp_schema
    assert len(definitions) == 25


def test_spawn_public_metadata_has_no_trusted_source_marker() -> None:
    built = {t["function"]["name"]: t["function"] for t in build_tool_definitions()}
    marker = re.compile(r"\b(scheduled|source|trusted|job_tag|supervision)\b", re.IGNORECASE)
    for text in (
        BUILTIN_TOOLS["spawn_agent"].description,
        json.dumps(_spawn_schema()),
        json.dumps(built["spawn_agent"]),
    ):
        assert marker.findall(text) == []
