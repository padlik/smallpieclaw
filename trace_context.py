"""
trace_context.py
----------------
Request-scoped trace IDs for correlating one agent run across log surfaces.

A trace ID is a short, grep-friendly token (``r-<8 hex>``) generated per
interactive/scheduled run and threaded through the ReAct loop, LLM client, and
tool dispatch via log prefixes. It deliberately contains no user text, file
paths, secrets, or other payload — it exists only to stitch interleaved log
lines (deferred messages, sub-agents, scheduler jobs, fallback model calls,
graph extraction) back into a single timeline.

Trace IDs are passed explicitly through call arguments / dataclass fields; no
process-global mutable trace state is used for correctness-critical behavior.
"""

from __future__ import annotations

import secrets

_TRACE_PREFIX = "r-"
_TRACE_HEX_LEN = 8


def new_trace_id() -> str:
    """Return a fresh short trace ID, e.g. ``r-a1b2c3d4``."""
    return f"{_TRACE_PREFIX}{secrets.token_hex(_TRACE_HEX_LEN // 2)}"


def child_trace_id(parent: str | None) -> str:
    """Return a trace ID for a child run.

    If *parent* is a non-empty trace ID, the child reuses it so the parent and
    child share one timeline; otherwise a fresh ID is generated. Identity of the
    specific actor is conveyed separately by the agent label (e.g. ``sa-1a2b``).
    """
    if parent and parent.strip():
        return parent.strip()
    return new_trace_id()
