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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch


from llm_client import LLMClient
from call_attempt_state import CallAttemptState
from model_registry import ModelMetadata
from react_loop import ReactContext, react_loop

DEFAULT_FAKE_METADATA = ModelMetadata(context_tokens=1_000_000, output_tokens=4_096)


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
    """Record of one scripted ``chat`` invocation."""
    messages: list
    system: Optional[str]
    images_present: bool


class ScriptedLLM:
    """A fake LLMClient that returns a fixed sequence of responses.

    When the script is exhausted the final response is repeated, so a trailing
    ``finish`` (or trailing prose for protocol-failure scenarios) keeps the loop
    deterministic. Every call is recorded in ``.calls``.
    """

    def __init__(self, responses: list[str], model: str = "test-model",
                 metadata: ModelMetadata | None = None):
        if not responses:
            raise ValueError("ScriptedLLM needs at least one response")
        self.metadata = metadata or DEFAULT_FAKE_METADATA
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[LLMCall] = []
        self._model = model
        self._active_idx = 0
        self._trace_id = ""
        self._attempt_binding: tuple[CallAttemptState, bool] | None = None

    @contextmanager
    def bind_attempt(self, state: CallAttemptState, *, manual: bool = False, definition_revision=0):
        """Bind a scripted request to its logical attempt state."""
        del definition_revision
        previous = self._attempt_binding
        self._attempt_binding = (state, manual)
        try:
            yield
        finally:
            self._attempt_binding = previous

    def _count_attempt(self, mode: str) -> None:
        if self._attempt_binding is not None:
            state, manual = self._attempt_binding
            state.begin_outbound_attempt(mode, manual=manual)

    @property
    def llm_cfg(self) -> dict:
        return {"model": self._model}

    def set_trace_id(self, trace_id: str | None) -> None:
        self._trace_id = (trace_id or "").strip()

    def _next(self) -> str:
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    def chat(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        if not json_mode:
            # Compaction path (only used if a scenario forces compaction).
            return "• scripted compaction summary"
        images_present = any(m.get("images") for m in messages)
        self.calls.append(LLMCall(messages=list(messages), system=system,
                                  images_present=images_present))
        response = self._next()
        self._count_attempt("json")
        return response


class NativeScriptedLLM(LLMClient):
    """A fake LLMClient that scripts native tool-calling responses.

    Subclasses ``LLMClient`` (bypassing ``__init__``) so react_loop's
    ``isinstance(ctx.llm, LLMClient)`` gate admits it into the native
    tool-calling path. Each ``chat_with_tools`` call returns the next
    scripted ``ChatResponse`` and snapshots the messages it was handed, so a
    multi-turn payload's shape can be asserted after the run.

    When the script is exhausted the final response repeats, mirroring
    ``ScriptedLLM``.
    """

    def __init__(self, responses: list, model: str = "test-model",
                 metadata: ModelMetadata | None = None):
        if not responses:
            raise ValueError("NativeScriptedLLM needs at least one response")
        self._fake_metadata = metadata or DEFAULT_FAKE_METADATA
        self._responses = list(responses)
        self._idx = 0
        self._model = model
        self._active_idx = 0
        self._attempt_binding: tuple[CallAttemptState, bool] | None = None
        # Snapshot of the messages list handed to each native call.
        self.tool_calls_seen: list[list[dict]] = []
        # Snapshot of messages handed to the json_mode fallback
        # (``chat`` with ``json_mode``), captured after react_loop linearizes any native
        # tool-calling turns already in history.
        self.json_mode_calls: list[list[dict]] = []

    @property
    def llm_cfg(self) -> dict:
        return {"model": self._model}

    @property
    def metadata(self) -> ModelMetadata:
        return self._fake_metadata

    def set_trace_id(self, trace_id: str | None = None) -> None:
        # Base LLMClient.set_trace_id writes through a context-local property
        # backed by state built in the real __init__ (which we bypass); store to
        # a plain attribute instead so the base property is never touched.
        self._trace_id_value = (trace_id or "").strip()

    @contextmanager
    def bind_attempt(self, state: CallAttemptState, *, manual: bool = False, definition_revision=0):
        """Bind a scripted native/JSON call to the real retry counters."""
        del definition_revision
        previous = self._attempt_binding
        self._attempt_binding = (state, manual)
        try:
            yield
        finally:
            self._attempt_binding = previous

    def _count_attempt(self, mode: str) -> None:
        if self._attempt_binding is not None:
            state, manual = self._attempt_binding
            state.begin_outbound_attempt(mode, manual=manual)

    def chat_with_tools(self, messages, tools, system=None, progress_cb=None):
        # Shallow-copy each message so later in-place mutation of the shared
        # list does not retroactively change what we assert was sent this turn.
        self.tool_calls_seen.append([dict(m) for m in messages])
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        # A scripted exception simulates the native path failing on this turn,
        # exercising react_loop's fallback to json_mode.
        if isinstance(resp, BaseException):
            if not isinstance(resp, NotImplementedError):
                self._count_attempt("native")
            raise resp
        self._count_attempt("native")
        return resp

    def chat(self, messages, system=None, progress_cb=None, json_mode=False) -> str:
        if not json_mode:
            return "• scripted compaction summary"
        # Reached when the native path is skipped or a local native-unsupported
        # conversion falls to json_mode. Record what the builder received (after
        # react_loop linearizes native turns) so the payload's shape can be
        # asserted, then return a finish so the loop terminates deterministically.
        self.json_mode_calls.append([dict(m) for m in messages])
        self._count_attempt("json")
        return '{"action": "finish", "result": "done"}'


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


class _StubBuiltinExecutor:
    """Minimal builtin-executor stand-in for the test harness.

    Wraps a RecordingExecutor so react_loop routes tool actions through it
    after hand-written-tool removal, while still recording call order.
    """

    def __init__(self, executor: RecordingExecutor):
        self._executor = executor

    @property
    def path_policy(self):
        # Delegate to the wrapped executor so tests can wire a PathPolicy via
        # ``ex.path_policy = ...`` and have the gated built-ins (e.g. vision_query)
        # see it through ctx.builtin_executor.
        return getattr(self._executor, "path_policy", None)

    def is_builtin(self, tool_name: str) -> bool:
        # Treat every tool as a builtin so react_loop routes here instead of
        # returning "unknown tool" errors after hand-written-tool removal.
        # create_tool is excluded so it falls through to the unknown-tool error
        # path (the action was fully removed).
        return tool_name != "create_tool"

    def execute(self, tool_name: str, args: dict, **kwargs) -> dict:
        return self._executor.execute(tool_name, args)


# ---------------------------------------------------------------------------
# Context builder + runner
# ---------------------------------------------------------------------------

def build_context(llm, executor: RecordingExecutor, *, label: str = "main",
                  trace_id: str = "r-harness1", max_iterations: int = 8,
                  **overrides) -> ReactContext:
    """Build a ReactContext wired to *llm* and *executor* with safe defaults.

    The executor is wrapped as a stub BuiltinExecutor so the ReAct loop routes
    tool actions through it after hand-written tools were removed.
    """
    kwargs = dict(
        llm=llm,
        tool_index=_NullToolIndex(),
        memory=_NullMemory(),
        builtin_executor=_StubBuiltinExecutor(executor),
        mcp_manager=None,
        skill_registry=None,
        max_iterations=max_iterations,
        cancel_event=threading.Event(),
        retry_wait=lambda event, seconds: event.is_set(),
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
