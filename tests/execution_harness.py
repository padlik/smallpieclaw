"""
execution_harness.py
---------------------
Reusable, deterministic test harness for high-level ReAct execution scenarios.

The harness scripts an LLM's turn-by-turn responses and routes tool calls through
a recording executor so a whole multi-step flow can be asserted in one place:
final result, tool-call order, progress messages, and failure handling. It makes
no network calls, starts no Telegram bot, needs no graph DB, and runs no real
shell commands.

Typical use::

    from tests.execution_harness import ScriptedLLM, RecordingExecutor, run_react

    llm = ScriptedLLM([
        '{"action": "tool", "tool": "shell", "args": {"command": "ls"}}',
        '{"action": "finish", "result": "done"}',
    ])
    ex = RecordingExecutor({"shell": make_outcome(output="file.txt")})
    result, calls, progress = run_react(llm, ex, "list files")
    assert result == "done"
    assert [c.tool for c in calls] == ["shell"]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

from llm_client import LLMClient
from react_loop import ReactContext, react_loop


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def make_outcome(success: bool = True, output: str = "", error: str = "",
                 exit_code: int = 0) -> dict:
    """Build a tool-outcome dict in the shape react_loop expects."""
    return {"success": success, "output": output, "error": error, "exit_code": exit_code}


# ---------------------------------------------------------------------------
# Scripted LLM
# ---------------------------------------------------------------------------

@dataclass
class LLMCall:
    """Record of one chat_with_fallback invocation."""
    messages: list
    system: Optional[str]
    images_present: bool


class ScriptedLLM:
    """A fake LLMClient that returns a fixed sequence of responses.

    When the script is exhausted the final response is repeated, so a trailing
    ``finish`` (or trailing prose for protocol-failure scenarios) keeps the loop
    deterministic. Every call is recorded in ``.calls``.
    """

    def __init__(self, responses: list[str], model: str = "test-model"):
        if not responses:
            raise ValueError("ScriptedLLM needs at least one response")
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[LLMCall] = []
        self._model = model
        self._active_idx = 0
        self._trace_id = ""

    @property
    def llm_cfg(self) -> dict:
        return {"model": self._model}

    def set_trace_id(self, trace_id: str | None) -> None:
        self._trace_id = (trace_id or "").strip()

    def _next(self) -> str:
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    def chat_with_fallback(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        images_present = any(m.get("images") for m in messages)
        self.calls.append(LLMCall(messages=list(messages), system=system,
                                  images_present=images_present))
        return self._next()

    # Compaction path (only used if a scenario forces compaction).
    def chat(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        return "• scripted compaction summary"


def build_real_llm(models: list[dict], default: str, fallback: list[str],
                   script: list[str]):
    """Build a *real* LLMClient with a scripted ``chat`` for routing scenarios.

    Returns ``(client, used_models)`` where ``used_models`` records the active
    model name for each ``chat`` invocation. The real ``chat_with_fallback`` runs
    (so vision filtering, fallback ordering, and active-index restoration are
    genuinely exercised) while ``chat`` itself is replaced by the script.
    """
    from llm_client import LLMClient

    cfg = {"models": models,
           "agent": {"default_model": default, "fallback_models": fallback}}
    client = LLMClient(cfg)
    used: list[str] = []
    seq = list(script)
    idx = [0]

    def fake_chat(messages, system=None, progress_cb=None, json_mode=False):
        used.append(client._models[client._active_idx].get("model"))
        resp = seq[min(idx[0], len(seq) - 1)]
        idx[0] += 1
        return resp

    client.chat = fake_chat
    return client, used


class NativeScriptedLLM(LLMClient):
    """A fake LLMClient that scripts native tool-calling responses.

    Subclasses ``LLMClient`` (bypassing ``__init__``) so react_loop's
    ``isinstance(ctx.llm, LLMClient)`` gate admits it into the native
    tool-calling path. Each ``chat_with_tools_fallback`` call returns the next
    scripted ``ChatResponse`` and snapshots the messages it was handed, so a
    multi-turn payload's shape can be asserted after the run.

    When the script is exhausted the final response repeats, mirroring
    ``ScriptedLLM``.
    """

    def __init__(self, responses: list, model: str = "test-model"):
        if not responses:
            raise ValueError("NativeScriptedLLM needs at least one response")
        self._responses = list(responses)
        self._idx = 0
        self._model = model
        self._active_idx = 0
        # Snapshot of the messages list handed to each native call.
        self.tool_calls_seen: list[list[dict]] = []
        # Snapshot of messages handed to the json_mode fallback
        # (chat_with_fallback), captured after react_loop linearizes any native
        # tool-calling turns already in history.
        self.json_mode_calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": self._model}

    def set_trace_id(self, trace_id: str | None = None) -> None:
        # Base LLMClient.set_trace_id writes through a context-local property
        # backed by state built in the real __init__ (which we bypass); store to
        # a plain attribute instead so the base property is never touched.
        self._trace_id_value = (trace_id or "").strip()

    def chat_with_tools_fallback(self, messages, tools, system=None, progress_cb=None):
        # Shallow-copy each message so later in-place mutation of the shared
        # list does not retroactively change what we assert was sent this turn.
        self.tool_calls_seen.append([dict(m) for m in messages])
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        # A scripted exception simulates the native path failing on this turn,
        # exercising react_loop's fallback to json_mode.
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def chat_with_fallback(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        # Reached when the native path is skipped or a scripted native failure
        # falls back to json_mode. Record what the builder received (after
        # react_loop linearizes native turns) so the payload's shape can be
        # asserted, then return a finish so the loop terminates deterministically.
        self.json_mode_calls.append([dict(m) for m in messages])
        return '{"action": "finish", "result": "done"}'

    def chat(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        return "• scripted compaction summary"


# ---------------------------------------------------------------------------
# Recording executor
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    tool: str
    args: dict = field(default_factory=dict)


class RecordingExecutor:
    """Routes registered-tool calls, records them, and returns configured outcomes.

    ``outcomes`` maps a tool name to either a static outcome dict or a callable
    ``(args) -> outcome``. Unmapped tools return a generic success outcome.
    """

    def __init__(self, outcomes: Optional[dict] = None):
        self._outcomes = outcomes or {}
        self.calls: list[ToolCall] = []

    def execute(self, tool_name: str, args: Optional[dict] = None) -> dict:
        args = args or {}
        self.calls.append(ToolCall(tool=tool_name, args=dict(args)))
        spec = self._outcomes.get(tool_name)
        if callable(spec):
            return spec(args)
        if isinstance(spec, dict):
            return spec
        return make_outcome(output=f"{tool_name} ok")

    @property
    def tool_order(self) -> list[str]:
        return [c.tool for c in self.calls]


# ---------------------------------------------------------------------------
# Context builder + runner
# ---------------------------------------------------------------------------

def build_context(llm, executor: RecordingExecutor, *, label: str = "main",
                  trace_id: str = "r-harness1", max_iterations: int = 8,
                  **overrides) -> ReactContext:
    """Build a ReactContext wired to *llm* and *executor* with safe defaults.

    builtin_executor and mcp_manager are None so tool actions route through the
    recording executor; graph memory and skills are disabled.
    """
    kwargs = dict(
        llm=llm,
        tool_index=_NullToolIndex(),
        executor=executor,
        creator=_NullCreator(),
        memory=_NullMemory(),
        builtin_executor=None,
        mcp_manager=None,
        skill_registry=None,
        max_iterations=max_iterations,
        cancel_event=threading.Event(),
        label=label,
        trace_id=trace_id,
    )
    kwargs.update(overrides)
    return ReactContext(**kwargs)


def run_react(llm, executor: RecordingExecutor, goal: str, *,
              images: Optional[list[str]] = None, system: str = "sys-prompt",
              **ctx_overrides):
    """Run react_loop deterministically; return (result, executor.calls, progress).

    The system-prompt builder is patched so no real prompt assembly/token work is
    required and scenarios stay isolated from prompt content.
    """
    ctx = build_context(llm, executor, **ctx_overrides)
    progress: list[str] = []
    with patch("react_loop._build_system_prompt", return_value=(system, None)):
        result = react_loop(ctx, goal, progress_callback=progress.append, images=images)
    return result, executor.calls, progress


# ---------------------------------------------------------------------------
# Minimal null collaborators (the harness routes tools through the executor)
# ---------------------------------------------------------------------------

class _NullToolIndex:
    def search(self, *_a, **_k):
        return []

    def all_tools(self):
        return []


class _NullCreator:
    pass


class _NullMemory:
    def record_event(self, *_a, **_k):
        pass

    def as_prompt_text(self, *_a, **_k):
        return ""
