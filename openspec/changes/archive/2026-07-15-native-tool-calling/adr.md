# ADR Review Manifest

- Status: completed
- Review date: 2026-07-14

## Review Summary

ADR review completed for this change. The design introduces one major durable architectural decision: adopting native tool calling as the primary dispatch path with text-based JSON as a universal fallback.

## In-Force ADRs Reviewed

- ADR-0007: Use AgentRuntime for agent execution construction — constrains `ReactContext` as the per-run state carrier; `_tool_defs` caching respects this boundary.
- ADR-0008: Use a façade + handler-module package for built-in tools — constrains per-built-in-tool metadata to the `builtin_tools/` package; `builtin_tools/schemas.py` is co-located with `descriptors.py` per this ADR.

## New Durable ADRs Created

- ADR-0009: Use native tool calling as primary path with text-based JSON fallback — establishes the native-first dispatch strategy, the parse-in-place text handling, and the `LLMProvider` protocol extension pattern.
