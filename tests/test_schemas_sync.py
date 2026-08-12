"""Tests that built-in tool metadata stays synchronized across modules."""

from __future__ import annotations

from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS


def test_builtin_tool_schemas_match_descriptors() -> None:
    """Every tool listed in descriptors.py must have a schema and vice versa."""
    schema_keys = set(BUILTIN_TOOL_SCHEMAS.keys())
    descriptor_keys = set(BUILTIN_TOOLS.keys())
    assert schema_keys == descriptor_keys, (
        f"BUILTIN_TOOL_SCHEMAS and BUILTIN_TOOLS keys differ:\n"
        f"  Only in schemas: {schema_keys - descriptor_keys}\n"
        f"  Only in descriptors: {descriptor_keys - schema_keys}"
    )
